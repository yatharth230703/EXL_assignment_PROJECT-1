
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, ConsoleMessage as PWConsoleMessage

from ux_audit.axe import run_axe
from ux_audit.config import Config, Viewport, load_config
from ux_audit.models import (
    CaptureBundle,
    ConsoleMessage,
    DomIssues,
    LayoutShiftEntry,
    NavigationTiming,
    NetworkFailure,
)

# Injected before navigation so it's recording from the very first paint.
# Collects layout-shift + LCP entries into window.__uxAudit for later retrieval.
_INIT_SCRIPT = """
window.__uxAudit = { shifts: [], lcp: null };

function describeNode(node) {
  if (!node || node.nodeType !== 1) return null;
  let s = node.tagName.toLowerCase();
  if (node.id) s += '#' + node.id;
  else if (node.className && typeof node.className === 'string') {
    const cls = node.className.trim().split(/\\s+/).slice(0, 2).join('.');
    if (cls) s += '.' + cls;
  }
  return s;
}

try {
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (entry.hadRecentInput) continue;
      const src = entry.sources && entry.sources[0] ? entry.sources[0].node : null;
      window.__uxAudit.shifts.push({ value: entry.value, selector: describeNode(src) });
    }
  }).observe({ type: 'layout-shift', buffered: true });
} catch (e) {}

try {
  new PerformanceObserver((list) => {
    const entries = list.getEntries();
    const last = entries[entries.length - 1];
    if (last) window.__uxAudit.lcp = last.renderTime || last.loadTime || null;
  }).observe({ type: 'largest-contentful-paint', buffered: true });
} catch (e) {}
"""

# Run after settle to collect DOM-level issues + finalize perf entries.
_COLLECT_SCRIPT = """
() => {
  const result = { shifts: window.__uxAudit.shifts, lcp: window.__uxAudit.lcp,
                    nav: null, brokenImages: [], overflowing: [], placeholders: [] };

  const nav = performance.getEntriesByType('navigation')[0];
  if (nav) {
    result.nav = {
      ttfb_ms: nav.responseStart,
      dcl_ms: nav.domContentLoadedEventEnd,
      load_ms: nav.loadEventEnd,
    };
  }

  for (const img of document.querySelectorAll('img')) {
    if (img.complete && img.naturalWidth === 0 && img.src) {
      result.brokenImages.push(img.src);
    }
  }

  const viewportWidth = window.innerWidth;
  const candidates = document.querySelectorAll('body *');
  const overflowHits = [];
  for (const el of candidates) {
    const rect = el.getBoundingClientRect();
    if (rect.width > 0 && rect.right - viewportWidth > 5) {
      let sel = el.tagName.toLowerCase();
      if (el.id) sel += '#' + el.id;
      overflowHits.push(sel);
      if (overflowHits.length >= 10) break;
    }
  }
  result.overflowing = [...new Set(overflowHits)];

  const bodyText = document.body.innerText || '';
  const patterns = [/lorem ipsum/i, /\\btodo\\b/i, /test@/i];
  for (const p of patterns) {
    const m = bodyText.match(p);
    if (m) result.placeholders.push(m[0]);
  }

  // Interaction surface: does the probe pass have anything to do here? (spec §6.3)
  result.forms = [];
  for (const form of document.querySelectorAll('form')) {
    let sel = 'form';
    if (form.id) sel += '#' + form.id;
    else if (form.name) sel += '[name="' + form.name + '"]';
    result.forms.push(sel);
  }

  // Primary CTA = largest visible button/link-styled control above the fold.
  let best = null, bestArea = 0;
  const ctas = document.querySelectorAll(
    'button, a[role="button"], input[type="submit"], .btn, [class*="button"]'
  );
  for (const el of ctas) {
    const rect = el.getBoundingClientRect();
    if (rect.top < 0 || rect.top > window.innerHeight) continue;  // above the fold only
    const area = rect.width * rect.height;
    const text = (el.innerText || el.value || '').trim();
    if (area > bestArea && text) { bestArea = area; best = text; }
  }
  result.primaryCta = best;

  return result;
}
"""


async def _settle_visuals(page, config: Config) -> bool:
    """Wait for the page to stop *moving*, not just stop loading.

    `networkidle` only says the network went quiet. Modern templated sites run
    entrance animations (blur-in, fade-up, scroll-triggered reveals) that are
    still mid-flight at that point, and a screenshot taken then shows text that
    is genuinely blurred *in the image* but perfectly sharp for a real visitor.
    The vision pass then reports the blur as a defect, correctly describing a
    frame no user ever sees.

    Two bounded waits, because they catch different things:
      1. `document.getAnimations()` — covers CSS animations/transitions and WAAPI.
      2. Screenshot-equality polling — catches everything else (JS-driven motion,
         late-loading images) regardless of mechanism.

    Both are capped: looping animations never finish and never stabilise, and a
    page with a spinner would otherwise hang the crawl. Returns whether the page
    actually reached a stable frame, which is recorded on the bundle rather than
    quietly assumed.
    """
    try:
        await page.evaluate(
            """async (capMs) => {
                if (!document.getAnimations) return;
                const running = document.getAnimations().filter(a => a.playState === 'running');
                if (!running.length) return;
                await Promise.race([
                    Promise.allSettled(running.map(a => a.finished)),
                    new Promise(r => setTimeout(r, capMs)),
                ]);
            }""",
            config.run.animation_timeout_ms,
        )
    except Exception:
        pass  # animation API unavailable or page navigated — fall through to polling

    try:
        previous = await page.screenshot()
        for _ in range(config.run.stability_max_attempts):
            await page.wait_for_timeout(config.run.stability_interval_ms)
            current = await page.screenshot()
            if current == previous:
                return True
            previous = current
    except Exception:
        return False
    return False


async def _load_lazy_images(page) -> None:
    """Scroll the page so lazy-loaded images actually load, then return to top.

    Never raises: a page that resists scrolling still deserves its screenshot.
    """
    try:
        await page.evaluate(
            """async () => {
                const step = window.innerHeight;
                const total = document.body.scrollHeight;
                for (let y = 0; y < total; y += step) {
                    window.scrollTo(0, y);
                    await new Promise(r => setTimeout(r, 120));
                }
                window.scrollTo(0, 0);
                await new Promise(r => setTimeout(r, 250));

                // Give the images that just started fetching a bounded chance to finish.
                const pending = [...document.images].filter(i => !i.complete);
                await Promise.race([
                    Promise.allSettled(pending.map(i => i.decode().catch(() => {}))),
                    new Promise(r => setTimeout(r, 4000)),
                ]);
            }"""
        )
    except Exception:
        pass


async def _detect_unpainted_content(page, screenshot_path) -> bool:
    """True when the page has visible text that did not paint.

    Compares the rendered pixels below the header against the DOM: if that band
    is essentially a flat colour but elements in it report real text and non-zero
    boxes, the browser failed to paint content that genuinely exists. That's a
    capture artefact, not a site defect, and it must not be reported as one.
    """
    try:
        from PIL import Image, ImageStat

        has_text = await page.evaluate(
            """() => {
                const vh = window.innerHeight, vw = window.innerWidth;
                let chars = 0;
                for (const el of document.querySelectorAll('h1, h2, h3, p, a, button, span')) {
                    const r = el.getBoundingClientRect();
                    // Below the header band, inside the viewport, actually sized.
                    if (r.top < vh * 0.15 || r.top > vh || r.width < 2 || r.height < 2) continue;
                    const t = (el.innerText || '').trim();
                    if (t) chars += t.length;
                    if (chars > 40) return true;
                }
                return false;
            }"""
        )
        if not has_text:
            return False

        img = Image.open(screenshot_path).convert("L")
        band = img.crop((0, int(img.height * 0.15), img.width, img.height))
        # A real page with text in this band has meaningful tonal variation.
        return ImageStat.Stat(band).stddev[0] < 1.0
    except Exception:
        return False


def _slugify(page_url: str, viewport_name: str) -> str:
    host = urlparse(page_url).netloc.replace(":", "_")
    path = urlparse(page_url).path.strip("/").replace("/", "_") or "root"
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{host}_{path}_{viewport_name}")
    return safe[:150]


@asynccontextmanager
async def browser_session(config: Config | None = None):
    """One Chromium for the whole run; each capture gets its own context (spec §6.2).

    Launching a browser per page is what the Phase-1 CLI did — fine for one page,
    wasteful for a 45-unit crawl, and it makes the concurrency cap meaningless
    (the cap is supposed to bound live *contexts*, not processes).

    Pass a config to run headful (`run.headless: false`) — useful for watching a
    crawl, and for sites whose JS reveal animations never fire in headless.
    """
    headless = config.run.headless if config else True
    slow_mo = config.run.slow_mo_ms if config else 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless, slow_mo=slow_mo or 0)
        try:
            yield browser
        finally:
            await browser.close()


async def capture_page(
    page_url: str,
    viewport: Viewport,
    config: Config,
    run_dir: Path,
    browser=None,
) -> CaptureBundle:
    """Capture one page at one viewport.

    If `browser` is None a private Chromium is launched (Phase-1 CLI path);
    otherwise the shared instance is reused and only a context is created.
    """
    if browser is None:
        async with browser_session(config) as own_browser:
            return await capture_page(page_url, viewport, config, run_dir, browser=own_browser)

    slug = _slugify(page_url, viewport.name)
    screenshots_dir = run_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    bundle = CaptureBundle(page_url=page_url, viewport=viewport.name)

    context = await browser.new_context(
        viewport={"width": viewport.width, "height": viewport.height},
        device_scale_factor=viewport.dsf,
    )
    try:
        page = await context.new_page()

        page.on(
            "console",
            lambda msg: bundle.console_messages.append(
                ConsoleMessage(type=msg.type, text=msg.text)
            )
            if len(bundle.console_messages) < 100
            else None,
        )
        page.on(
            "requestfailed",
            lambda req: bundle.network_failures.append(
                NetworkFailure(
                    url=req.url,
                    failure_text=req.failure,
                )
            ),
        )

        def _on_response(resp):
            if resp.status >= 400 and len(bundle.network_failures) < 100:
                bundle.network_failures.append(NetworkFailure(url=resp.url, status=resp.status))

        page.on("response", _on_response)

        await page.add_init_script(_INIT_SCRIPT)

        try:
            await page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=config.run.node_timeout_s * 1000,
            )

            await page.wait_for_timeout(config.run.early_screenshot_ms)
            early_path = screenshots_dir / f"{slug}_early.png"
            await page.screenshot(path=str(early_path))
            bundle.screenshot_early_path = str(early_path)

            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=config.run.settle_timeout_s * 1000
                )
            except Exception:
                pass  # not all pages go idle (polling/analytics) — proceed anyway

            # Network-quiet is not the same as visually-still. Wait for motion to
            # stop before claiming this frame represents the settled page.
            bundle.visually_stable = await _settle_visuals(page, config)

            settled_viewport_path = screenshots_dir / f"{slug}_settled_viewport.png"
            await page.screenshot(path=str(settled_viewport_path))
            bundle.screenshot_settled_viewport_path = str(settled_viewport_path)

            # Did the viewport paint what the DOM says is there?
            #
            # Some sites animate headline text in via JS (per-word spans starting
            # at opacity 0). Headless Chromium never fires those, so the hero
            # paints as a flat block while `innerText` happily reports the copy.
            # The vision pass then truthfully reports "a large blank space" about
            # a frame no real visitor sees. Blankness is measurable, so measure it
            # and let the judgment pass discount it (spec §4.1).
            bundle.render_suspect = await _detect_unpainted_content(
                page, settled_viewport_path
            )

            # Collect metrics BEFORE the lazy-load scroll below. Scrolling fires
            # scroll-triggered animations, and those layout shifts would be
            # attributed to page load and inflate CLS. Measure the load, then
            # go disturb the page.
            collected = await page.evaluate(_COLLECT_SCRIPT)

            # Full-page capture expands the viewport but never scrolls, so
            # `loading="lazy"` images below the fold never enter the viewport,
            # never fetch, and never paint. Layout still reserves their boxes, so
            # the screenshot gets image-shaped holes — which the vision pass then
            # reported as "large empty vertical gaps in the article body".
            # Verified on linear.app/blog: 795px and 736px blank bands whose
            # heights exactly matched two <img> elements the DOM said were visible.
            await _load_lazy_images(page)

            settled_fullpage_path = screenshots_dir / f"{slug}_settled_fullpage.png"
            await page.screenshot(path=str(settled_fullpage_path), full_page=True)
            bundle.screenshot_settled_fullpage_path = str(settled_fullpage_path)

            if collected.get("nav"):
                bundle.navigation_timing = NavigationTiming(
                    ttfb_ms=collected["nav"].get("ttfb_ms"),
                    dom_content_loaded_ms=collected["nav"].get("dcl_ms"),
                    load_event_ms=collected["nav"].get("load_ms"),
                )
            bundle.lcp_ms = collected.get("lcp")
            bundle.layout_shifts = [
                LayoutShiftEntry(value=s["value"], selector=s.get("selector"))
                for s in collected.get("shifts", [])
            ]
            bundle.cls_score = sum(s.value for s in bundle.layout_shifts)
            bundle.dom_issues = DomIssues(
                broken_image_srcs=collected.get("brokenImages", []),
                overflowing_selectors=collected.get("overflowing", []),
                placeholder_text_hits=collected.get("placeholders", []),
            )
            bundle.form_selectors = collected.get("forms", []) or []
            bundle.has_form = bool(bundle.form_selectors)
            bundle.primary_cta = collected.get("primaryCta")

            await run_axe(page, bundle, config)

        except Exception as e:
            bundle.status = "error"
            bundle.capture_error = f"{type(e).__name__}: {e}"

    finally:
        await context.close()

    return bundle


def _write_bundle(bundle: CaptureBundle, run_dir: Path) -> Path:
    captures_dir = run_dir / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(bundle.page_url, bundle.viewport)
    out_path = captures_dir / f"{slug}.json"
    out_path.write_text(bundle.model_dump_json(indent=2))
    return out_path


async def _main_async(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    viewport = config.viewport_by_name(args.viewport)

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path("runs") / run_id

    bundle = await capture_page(args.url, viewport, config, run_dir)
    out_path = _write_bundle(bundle, run_dir)

    print(f"status: {bundle.status}")
    if bundle.capture_error:
        print(f"error: {bundle.capture_error}")
    print(f"bundle written to: {out_path}")
    return 0 if bundle.status == "ok" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1 capture harness (single page, single viewport).")
    parser.add_argument("url", help="Page URL to capture")
    parser.add_argument("--viewport", required=True, help="Viewport name from config.yaml (e.g. mobile_390)")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--run-id", default=None, help="Run ID (defaults to current UTC timestamp)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
"""Phase 7: the interaction probe (spec §6.3).

Two halves, split along the spec's §4.1 rule — never ask the model to notice
something measurable:

  `deterministic_probe()`  — OUR Playwright drives the form and MEASURES: did a
                             request fire, did the DOM change within 1.5s, is the
                             validation message associated with its field via
                             `aria-describedby` / `<label for>`, did a double
                             click fire two identical requests. No LLM.

  `explore_page()`         — an LlmAgent with Playwright MCP DECIDES what the most
                             obvious visitor action is, takes it, and JUDGES
                             whether the response looks sensible. No measuring.

Both gotchas from §6.3 are designed around: `StdioConnectionParams` with an
explicit timeout (a bare `StdioServerParameters` gets a ~5s default that browser
startup blows through), and the entire per-page probe stays inside ONE agent
invocation so nothing depends on MCP state surviving between invocations.
Smoke-tested before this file was written; the two-step flow held.
"""

from __future__ import annotations

import asyncio
from typing import Any

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from .config import Config
from .models import CaptureBundle, Finding

# Kept tight on purpose: Playwright MCP exposes a broad surface, and trimming it
# keeps the probe on-task and its context small (spec §6.3).
PROBE_TOOLS = [
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_snapshot",
    "browser_take_screenshot",
]


class ProbeObservation(BaseModel):
    """Deterministic facts about one form interaction. No judgment in here."""

    action: str
    requests_fired: int = 0
    request_urls: list[str] = Field(default_factory=list)
    submissions: list[str] = Field(default_factory=list)  # POST / document GET only
    dom_changed_within_1500ms: bool = False
    error_message_shown: bool = False
    error_text: str | None = None
    error_associated_with_field: bool = False
    association_method: str | None = None  # "aria-describedby" | "label-for" | None
    duplicate_requests: bool = False
    notes: str | None = None


# ---------------------------------------------------------------------------
# Deterministic half
# ---------------------------------------------------------------------------

_ASSOC_SCRIPT = """
() => {
  // Is a visible validation message tied to its field the way a screen reader
  // needs? Checks aria-describedby, aria-errormessage, and <label for>.
  const out = { shown: false, text: null, associated: false, method: null };

  const candidates = document.querySelectorAll(
    '[role="alert"], .error, .invalid-feedback, [class*="error"], [aria-invalid="true"]'
  );
  let msgEl = null;
  for (const el of candidates) {
    const t = (el.innerText || '').trim();
    const r = el.getBoundingClientRect();
    if (t && r.width > 0 && r.height > 0) { msgEl = el; out.shown = true; out.text = t.slice(0, 200); break; }
  }
  if (!msgEl) return out;

  for (const field of document.querySelectorAll('input, select, textarea')) {
    const describedby = (field.getAttribute('aria-describedby') || '') + ' ' +
                        (field.getAttribute('aria-errormessage') || '');
    if (msgEl.id && describedby.split(/\\s+/).includes(msgEl.id)) {
      out.associated = true; out.method = 'aria-describedby'; return out;
    }
  }
  if (msgEl.id) {
    const lbl = document.querySelector(`label[for="${CSS.escape(msgEl.id)}"]`);
    if (lbl) { out.associated = true; out.method = 'label-for'; return out; }
  }
  // A message physically inside the same <label> or field wrapper counts too.
  const wrapper = msgEl.closest('label');
  if (wrapper && wrapper.querySelector('input, select, textarea')) {
    out.associated = true; out.method = 'label-for'; return out;
  }
  return out;
}
"""


async def _submit_and_measure(page, action: str, do_action, settle_ms: int = 1500) -> ProbeObservation:
    """Run `do_action`, then measure what objectively happened."""
    obs = ProbeObservation(action=action)
    requests: list[str] = []
    submissions: list[str] = []

    def on_request(req):
        if req.method in ("POST", "GET") and req.resource_type in ("document", "xhr", "fetch"):
            requests.append(f"{req.method} {req.url}")
        # Duplicate-submit detection keys ONLY on actual submissions. Keying it on
        # all document/xhr traffic flagged every action as a duplicate, because a
        # normal page load refetches plenty of things — a false positive that
        # would have made the double_submit finding worthless.
        if req.method == "POST" or (req.resource_type == "document" and req.method == "GET"):
            submissions.append(f"{req.method} {req.url}")

    page.on("request", on_request)
    before_html = await page.evaluate("() => document.body.innerHTML.length")

    try:
        await do_action()
    except Exception as e:
        obs.notes = f"action failed: {type(e).__name__}: {e}"

    await page.wait_for_timeout(settle_ms)

    try:
        after_html = await page.evaluate("() => document.body.innerHTML.length")
        obs.dom_changed_within_1500ms = after_html != before_html
        assoc = await page.evaluate(_ASSOC_SCRIPT)
        obs.error_message_shown = bool(assoc.get("shown"))
        obs.error_text = assoc.get("text")
        obs.error_associated_with_field = bool(assoc.get("associated"))
        obs.association_method = assoc.get("method")
    except Exception as e:
        obs.notes = (obs.notes or "") + f" | measure failed: {type(e).__name__}"

    page.remove_listener("request", on_request)
    obs.requests_fired = len(requests)
    obs.request_urls = requests[:5]
    obs.submissions = submissions[:5]
    obs.duplicate_requests = len(submissions) != len(set(submissions))
    return obs


async def deterministic_probe(page_url: str, viewport, config: Config, browser) -> list[ProbeObservation]:
    """Empty submit → invalid-value submit → double-click submit. All measured."""
    observations: list[ProbeObservation] = []
    context = await browser.new_context(
        viewport={"width": viewport.width, "height": viewport.height},
        device_scale_factor=viewport.dsf,
    )
    try:
        page = await context.new_page()
        await page.goto(page_url, wait_until="domcontentloaded", timeout=config.run.node_timeout_s * 1000)

        submit = page.locator(
            'form button[type="submit"], form input[type="submit"], form button'
        ).first
        if await submit.count() == 0:
            return observations

        # 1. Submit completely empty.
        observations.append(
            await _submit_and_measure(page, "submit_empty", lambda: submit.click(timeout=5000))
        )

        # 2. Submit with one clearly invalid value.
        await page.reload(wait_until="domcontentloaded")
        text_field = page.locator('form input[type="text"], form input[type="email"], form input:not([type])').first

        async def invalid_submit():
            if await text_field.count():
                await text_field.fill("not a valid value @@@")
            await submit.click(timeout=5000)

        observations.append(await _submit_and_measure(page, "submit_invalid_value", invalid_submit))

        # 3. Double-click submit — does it fire the same request twice?
        await page.reload(wait_until="domcontentloaded")

        async def double_click():
            await submit.click(timeout=5000, click_count=2, delay=50)

        observations.append(await _submit_and_measure(page, "double_click_submit", double_click))

    except Exception as e:
        observations.append(ProbeObservation(action="setup", notes=f"{type(e).__name__}: {e}"))
    finally:
        await context.close()
    return observations


def observations_to_findings(
    observations: list[ProbeObservation], page_url: str, viewport: str, run_id: str, site: str
) -> list[Finding]:
    """Deterministic observations → findings, using the §9 rubric."""
    out: list[Finding] = []

    def add(severity, category, document, dedupe_key, evidence):
        out.append(
            Finding(
                run_id=run_id, site=site, page_url=page_url, viewport=viewport,
                category=category, severity=severity, source="probe", confidence="high",
                document=document, dedupe_key=dedupe_key, evidence=evidence,
            )
        )

    for obs in observations:
        ev = obs.model_dump()

        if obs.action == "submit_empty":
            if obs.requests_fired == 0 and not obs.dom_changed_within_1500ms and not obs.error_message_shown:
                # Nothing happened at all: no request, no DOM change, no message.
                add("high", "interaction",
                    "Submitting the form empty produces no visible response at all — no request, "
                    "no DOM change, and no validation message within 1.5s.",
                    "empty_submit_dead", ev)
            elif obs.error_message_shown and not obs.error_associated_with_field:
                add("medium", "interaction",
                    f"The validation message ({obs.error_text!r}) is not programmatically associated "
                    f"with its field, so screen-reader users won't hear it in context.",
                    "validation_not_associated", ev)

        elif obs.action == "submit_invalid_value":
            if not obs.error_message_shown and obs.requests_fired > 0:
                add("medium", "interaction",
                    "A clearly invalid value was submitted to the server with no client-side "
                    "validation message shown.",
                    "invalid_accepted", ev)

        elif obs.action == "double_click_submit":
            if obs.duplicate_requests:
                add("low", "interaction",
                    "Double-clicking the submit control fires the same request twice — the control "
                    "isn't disabled after the first click.",
                    "double_submit", ev)

    return out


# ---------------------------------------------------------------------------
# LLM half — decides what to try, judges whether the response looks sensible
# ---------------------------------------------------------------------------

EXPLORE_INSTRUCTION = """\
You are testing one web page as a first-time visitor would.

Do exactly this, in one pass, and then stop:
1. Navigate to the URL you are given.
2. Take a snapshot to see the page.
3. Identify the SINGLE most obvious action a visitor would take (the primary
   call-to-action, or the main form's submit control) and take it.
4. Take another snapshot to see what happened.

Then report, in plain prose:
- what you clicked and why it was the obvious choice
- whether the response looked sensible for that action
- anything that looked broken, confusing, or misleading about the response

Do NOT comment on load speed, timing, or console errors — those are measured
separately and precisely. Judge only whether the interaction made sense.
Keep it under 150 words. If nothing looked wrong, say so plainly.
"""


def build_probe_toolset(cfg: Config):
    """Gotcha 1 (spec §6.3): `StdioConnectionParams` wrapping the server params,
    with an explicit timeout. A bare `StdioServerParameters` gets a ~5s default
    that Chromium startup blows straight through."""
    from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
    from mcp import StdioServerParameters

    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="npx",
                # `--isolated` keeps the browser profile in memory. Without it,
                # probing page 2 dies with "Browser is already in use <profile>":
                # each page spawns a fresh MCP server, and they all contend for
                # the same on-disk Chrome profile. Observed on the first real
                # multi-page run — page 1 passed, pages 2-5 all failed on the lock.
                args=["-y", "@playwright/mcp@latest", "--isolated"]
                + (["--headless"] if cfg.run.headless else []),
            ),
            timeout=cfg.probe.mcp_timeout_s,
        ),
        tool_filter=PROBE_TOOLS,
    )


def build_probe_agent(cfg: Config) -> LlmAgent:
    return LlmAgent(
        name="ux_probe",
        model=cfg.probe.model,
        instruction=EXPLORE_INSTRUCTION,
        tools=[build_probe_toolset(cfg)],
    )


async def explore_page(page_url: str, cfg: Config) -> tuple[str | None, list[str]]:
    """One page, ONE agent invocation (gotcha 2: never rely on MCP state
    surviving between invocations). Returns (verdict_text, tool_calls_made)."""
    from google.adk.apps import App
    from google.adk.plugins.context_filter_plugin import ContextFilterPlugin
    from google.adk.plugins.reflect_retry_tool_plugin import ReflectAndRetryToolPlugin
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    toolset = build_probe_toolset(cfg)
    agent = LlmAgent(
        name="ux_probe", model=cfg.probe.model, instruction=EXPLORE_INSTRUCTION, tools=[toolset]
    )
    # Order matters (spec §6.5): filter before retry.
    app = App(
        name="ux_probe",
        root_agent=agent,
        plugins=[
            ContextFilterPlugin(num_invocations_to_keep=2),
            ReflectAndRetryToolPlugin(max_retries=2),
        ],
    )
    runner = InMemoryRunner(app=app, app_name="ux_probe")
    session = await runner.session_service.create_session(app_name="ux_probe", user_id="local")

    calls: list[str] = []
    verdict: str | None = None
    turns = 0
    try:
        async for event in runner.run_async(
            user_id="local",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=f"Test this page: {page_url}")]),
        ):
            turns += 1
            if turns > cfg.probe.max_turns * 3:  # hard stop; it can't loop forever
                break
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "function_call", None):
                        calls.append(part.function_call.name)
                    if getattr(part, "text", None):
                        verdict = part.text
    finally:
        # Shut the MCP server down before the next page spawns its own, so the
        # servers don't pile up holding browsers open.
        try:
            await toolset.close()
        except Exception:
            pass
    return verdict, calls


def select_probe_targets(bundles: list[CaptureBundle], cfg: Config) -> list[CaptureBundle]:
    """Only pages with a form or an above-fold primary CTA, mobile+desktop only,
    capped at max_probe_pages (spec §6.3)."""
    eligible = [
        b for b in bundles
        if b.status == "ok" and (b.has_form or b.primary_cta) and b.viewport in cfg.probe.viewports
    ]
    seen_pages: set[str] = set()
    out: list[CaptureBundle] = []
    for b in sorted(eligible, key=lambda x: (x.page_url, x.viewport)):
        if b.page_url in seen_pages:
            continue
        seen_pages.add(b.page_url)
        out.append(b)
        if len(out) >= cfg.probe.max_probe_pages:
            break
    return out

"""Phase 6: the vision judgment pass — three specialists, not one generalist.

Reads *saved* screenshots; the browser is long closed (spec §4.2).

**Why three agents.** This started as one agent with one prompt covering layout,
content, and responsive behaviour. Every artefact class discovered in testing got
patched by adding another "DO NOT report X" clause, and each clause taxed recall
across *all* judgments, not just the one it targeted. Three clauses took findings
from 13 to 0 on a real site. A prompt that accumulates caveats degrades globally.

So the work is split by concern, each specialist owning its own artefact:

  layout_judge      settled viewport shot        alignment, spacing, overlap, contrast
  content_judge     settled full-page shot       truncation, placeholders, missing sections
  responsive_judge  ALL viewports of one page    breakpoint-specific breakage

Each prompt is short and single-purpose, so a caveat costs recall only inside
that specialist. Each is scored separately by the eval harness, so one collapsing
to zero can't hide behind another still working.

`responsive_judge` is not a reorganisation — it's a capability the per-view design
could not have. Judging each (page × viewport) in isolation is why one root cause
(a corner ribbon overlapping text) came back as three unrelated findings. Seeing
the breakpoints together collapses that into one finding that can say *where* it
breaks.

Routing is deterministic fan-out, not an LLM coordinator (spec §14).
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Literal

from google.adk.agents import LlmAgent
from google.genai import types
from pydantic import BaseModel, Field

from .config import Config
from .models import CaptureBundle, Finding

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

# Vision findings killed by a contradicting measurement. Kept visible rather than
# dropped silently — suppression you can't see is indistinguishable from a bug.
SUPPRESSED: list[dict] = []


class VisionFinding(BaseModel):
    category: Literal["visual_stability", "content_completeness", "responsive_design", "interaction"]
    severity: Literal["high", "medium", "low"]
    confidence: Literal["high", "medium", "low"]
    description: str = Field(description="One sentence, specific and visual. Name the element.")
    where: str = Field(default="", description="Where on the page, in plain words.")


class VisionFindings(BaseModel):
    findings: list[VisionFinding] = Field(default_factory=list)


class ResponsiveFinding(VisionFinding):
    viewports_affected: list[str] = Field(
        default_factory=list,
        description="Exactly which of the supplied viewport names show this problem.",
    )


class ResponsiveFindings(BaseModel):
    findings: list[ResponsiveFinding] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared prompt fragments
# ---------------------------------------------------------------------------

_ARTEFACT_CONTEXT = """
Context so you don't mistake a capture artefact for a defect:
- `broken_images_measured` lists every image that genuinely failed to load. Empty
  means all images loaded, so a dark or minimal panel is the site's own artwork.
- `visually_stable: false` means the page was still animating; blur or half-faded
  elements are artefacts of that frame.
- `render_suspect: true` means the browser failed to paint text that IS present,
  so large blank areas are artefacts.
This is about not raising false alarms. It is NOT a reason to stay silent —
everything else about how the page looks is still yours to judge.
"""

_COMMON_RULES = """
- Report only what you can see. Do not comment on speed, load time, layout shift,
  console errors, or network problems — those are measured precisely elsewhere.
- Do not repeat anything already in `known_findings`.
- Maximum 5 findings. Fewer is better. An empty list is valid.
- `confidence: high` only when the problem is unmistakable in the image.
- Severity: high = blocks a visitor task, medium = degrades it, low = cosmetic.
"""

LAYOUT_INSTRUCTION = f"""\
You review the VISUAL LAYOUT of one web page at one viewport size.

Your remit, and nothing else:
- misalignment and inconsistent spacing between related elements
- elements overlapping or obscuring each other
- text clipped, truncated, or running outside its container
- low-contrast text that is hard to read
- controls that don't look like what they do
{_COMMON_RULES}{_ARTEFACT_CONTEXT}"""

CONTENT_INSTRUCTION = f"""\
You review the CONTENT COMPLETENESS of one web page, using a full-page screenshot.

Your remit, and nothing else:
- placeholder or unfinished content (lorem ipsum, "TODO", dummy names, test data)
- sections that appear structurally incomplete or cut off mid-thought
- headings, labels, or calls-to-action that are missing where the layout expects one
- obviously duplicated content blocks
{_COMMON_RULES}{_ARTEFACT_CONTEXT}"""

RESPONSIVE_INSTRUCTION = f"""\
You are given screenshots of THE SAME PAGE at several viewport widths, labelled in
order. Judge how the design holds up ACROSS those breakpoints.

Your remit, and nothing else:
- problems that appear at some widths but not others
- content that becomes cramped, clipped, overlapped, or unreachable as width shrinks
- navigation or controls that break or disappear at a breakpoint
- layouts that fail to reflow (fixed-width content forcing sideways scroll)

Report each problem ONCE, and set `viewports_affected` to exactly the viewport
names where you can see it. If something looks the same at every width, that is
not a responsive finding — leave it out.
{_COMMON_RULES}{_ARTEFACT_CONTEXT}"""


# ---------------------------------------------------------------------------
# Artefact preparation
# ---------------------------------------------------------------------------


def _downscale_jpeg(path: str | Path, max_width: int, quality: int = 70) -> bytes | None:
    """Downscale to <=max_width and re-encode as JPEG (spec §6.4).

    Full-page screenshots run to tens of thousands of pixels tall; sending them
    raw wastes tokens and slows every call for no judgment benefit.
    """
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = None  # full-page shots legitimately exceed the bomb guard
        img = Image.open(path)
    except Exception:
        return None

    try:
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, max(1, int(img.height * ratio))), Image.LANCZOS)
        # Very tall pages: cap height so one artefact can't dominate the request.
        if img.height > 8000:
            img = img.crop((0, 0, img.width, 8000))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return None


def build_hints(bundle: CaptureBundle, known: list[Finding]) -> dict:
    """Objective hints ride along with every image (spec §4.1)."""
    top_shifts = sorted(bundle.layout_shifts, key=lambda s: s.value, reverse=True)[:3]
    return {
        "viewport": bundle.viewport,
        "page_url": bundle.page_url,
        "cls_score": bundle.cls_score,
        "top_shifting_selectors": [s.selector for s in top_shifts if s.selector],
        "overflowing_selectors": bundle.dom_issues.overflowing_selectors[:5],
        "broken_images_measured": bundle.dom_issues.broken_image_srcs,
        "visually_stable": bundle.visually_stable,
        "render_suspect": bundle.render_suspect,
        "known_findings": [f"{f.category}: {f.document}" for f in known],
    }


# ---------------------------------------------------------------------------
# Deterministic guards — cheaper and more reliable than any prompt rule
# ---------------------------------------------------------------------------

_IMAGE_CLAIM_PATTERNS = (
    "black box", "blank box", "blank space where", "missing image", "broken image",
    "image is missing", "images are missing", "image fails", "images fail",
    "failed to load", "fails to load", "not loading", "did not load", "unloaded",
    "placeholder image", "image placeholder", "should be rendered", "should be displayed",
    "empty image", "images should", "no image",
)


def contradicts_measurement(vf: VisionFinding, bundle: CaptureBundle) -> str | None:
    """Drop vision findings that a deterministic signal already disproves.

    Prompts ask the model not to make these claims; this is the safety net for
    when it does anyway. A measurement beats a visual impression every time.
    """
    text = vf.description.lower()

    if any(p in text for p in _IMAGE_CLAIM_PATTERNS) and not bundle.dom_issues.broken_image_srcs:
        return "claims a missing/broken image, but every image on the page loaded (naturalWidth > 0)"

    if not bundle.visually_stable and any(w in text for w in ("blur", "blurred", "blurry", "out of focus")):
        return "reports blur on a page that was still animating when captured"

    if bundle.render_suspect and any(
        w in text for w in ("blank", "empty", "nothing", "missing content", "no content", "off-screen")
    ):
        return "reports blank/empty area, but the DOM has text there that headless Chromium failed to paint"

    return None


# ---------------------------------------------------------------------------
# Agent plumbing
# ---------------------------------------------------------------------------


async def _run_specialist(name: str, instruction: str, parts: list, cfg: Config, schema):
    from google.adk.runners import InMemoryRunner

    agent = LlmAgent(name=name, model=cfg.judgment.model, instruction=instruction, output_schema=schema)
    runner = InMemoryRunner(agent=agent, app_name=name)
    session = await runner.session_service.create_session(app_name=name, user_id="local")

    raw = None
    async for event in runner.run_async(
        user_id="local", session_id=session.id, new_message=types.Content(role="user", parts=parts)
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    raw = part.text
    if not raw:
        return None
    try:
        return schema.model_validate_json(raw)
    except Exception:
        return None


def _to_finding(vf: VisionFinding, bundle: CaptureBundle, run_id: str, specialist: str, hints: dict) -> Finding:
    site = bundle.page_url.split("/")[2] if "://" in bundle.page_url else bundle.page_url
    return Finding(
        run_id=run_id,
        site=site,
        page_url=bundle.page_url,
        viewport=bundle.viewport,
        category=vf.category,
        severity=vf.severity,  # capped to medium later by rules.cap_vision_severity
        source="vision",
        confidence=vf.confidence,
        document=vf.description + (f" ({vf.where})" if vf.where else ""),
        dedupe_key=f"{specialist}:{vf.category}:{vf.description[:40].lower()}",
        evidence={"where": vf.where, "specialist": specialist, "hints": hints},
        screenshot_path=bundle.screenshot_settled_viewport_path,
    )


def _accept(vf: VisionFinding, bundle: CaptureBundle, cfg: Config, specialist: str) -> bool:
    if CONFIDENCE_RANK.get(vf.confidence, 0) < CONFIDENCE_RANK.get(cfg.judgment.min_confidence, 2):
        return False
    if (why := contradicts_measurement(vf, bundle)):
        SUPPRESSED.append(
            {"specialist": specialist, "page_url": bundle.page_url, "viewport": bundle.viewport,
             "finding": vf.description[:120], "reason": why}
        )
        return False
    return True


# ---------------------------------------------------------------------------
# The three specialists
# ---------------------------------------------------------------------------


async def judge_layout(bundle: CaptureBundle, known: list[Finding], cfg: Config, run_id: str) -> list[Finding]:
    """Viewport screenshot only — what a visitor sees on arrival."""
    path = bundle.screenshot_settled_viewport_path
    if not path or not Path(path).exists():
        return []
    data = _downscale_jpeg(path, cfg.judgment.image_max_width)
    if not data:
        return []

    hints = build_hints(bundle, known)
    parts = [
        types.Part(text=f"Objective measurements:\n{json.dumps(hints, indent=2)}"),
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=data)),
    ]
    res = await _run_specialist("layout_judge", LAYOUT_INSTRUCTION, parts, cfg, VisionFindings)
    if not res:
        return []
    return [
        _to_finding(vf, bundle, run_id, "layout", hints)
        for vf in res.findings[: cfg.judgment.max_findings_per_view]
        if _accept(vf, bundle, cfg, "layout")
    ]


async def judge_content(bundle: CaptureBundle, known: list[Finding], cfg: Config, run_id: str) -> list[Finding]:
    """Full-page screenshot — the whole document, including what's below the fold."""
    path = bundle.screenshot_settled_fullpage_path
    if not path or not Path(path).exists():
        return []
    data = _downscale_jpeg(path, cfg.judgment.image_max_width)
    if not data:
        return []

    hints = build_hints(bundle, known)
    parts = [
        types.Part(text=f"Objective measurements:\n{json.dumps(hints, indent=2)}"),
        types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=data)),
    ]
    res = await _run_specialist("content_judge", CONTENT_INSTRUCTION, parts, cfg, VisionFindings)
    if not res:
        return []
    return [
        _to_finding(vf, bundle, run_id, "content", hints)
        for vf in res.findings[: cfg.judgment.max_findings_per_view]
        if _accept(vf, bundle, cfg, "content")
    ]


async def judge_responsive(
    bundles: list[CaptureBundle], known: list[Finding], cfg: Config, run_id: str
) -> list[Finding]:
    """All viewports of ONE page, together — the comparison the per-view design can't make."""
    ordered = sorted(bundles, key=lambda b: next(
        (v.width for v in cfg.viewports if v.name == b.viewport), 0))
    usable = [b for b in ordered if b.screenshot_settled_viewport_path
              and Path(b.screenshot_settled_viewport_path).exists()]
    if len(usable) < 2:  # nothing to compare
        return []

    parts: list = []
    per_view = {}
    for b in usable:
        data = _downscale_jpeg(b.screenshot_settled_viewport_path, cfg.judgment.image_max_width)
        if not data:
            continue
        width = next((v.width for v in cfg.viewports if v.name == b.viewport), None)
        per_view[b.viewport] = {
            "width_px": width,
            "overflowing_selectors": b.dom_issues.overflowing_selectors[:5],
            "visually_stable": b.visually_stable,
            "render_suspect": b.render_suspect,
            "broken_images_measured": b.dom_issues.broken_image_srcs,
        }
        parts.append(types.Part(text=f"--- viewport: {b.viewport} ({width}px wide) ---"))
        parts.append(types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=data)))

    if len(parts) < 4:
        return []

    hints = {
        "page_url": usable[0].page_url,
        "viewports_supplied": list(per_view),
        "per_viewport": per_view,
        "known_findings": [f"{f.category}: {f.document}" for f in known],
    }
    parts.insert(0, types.Part(text=f"Objective measurements:\n{json.dumps(hints, indent=2)}"))

    res = await _run_specialist("responsive_judge", RESPONSIVE_INSTRUCTION, parts, cfg, ResponsiveFindings)
    if not res:
        return []

    out: list[Finding] = []
    for vf in res.findings[: cfg.judgment.max_findings_per_view]:
        affected = [v for v in vf.viewports_affected if v in per_view] or list(per_view)
        anchor = next((b for b in usable if b.viewport == affected[0]), usable[0])
        if not _accept(vf, anchor, cfg, "responsive"):
            continue
        f = _to_finding(vf, anchor, run_id, "responsive", hints)
        # One finding naming its breakpoints, instead of N near-duplicates.
        f.viewports_affected = sorted(affected)
        f.document += f" [affects: {', '.join(sorted(affected))}]"
        f.evidence["viewports_affected"] = sorted(affected)
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# Orchestration — deterministic fan-out, no LLM router (spec §14)
# ---------------------------------------------------------------------------


async def judge_run(
    run_id: str,
    cfg: Config,
    known_by_view: dict[tuple[str, str], list[Finding]] | None = None,
    runs_root: str = "runs",
) -> list[Finding]:
    cap_dir = Path(runs_root) / run_id / "captures"
    bundles = [CaptureBundle.model_validate_json(p.read_text()) for p in sorted(cap_dir.glob("*.json"))]
    bundles = [b for b in bundles if b.status == "ok"]

    known_by_view = known_by_view or {}
    sem = asyncio.Semaphore(cfg.judgment.llm_concurrency)

    async def guarded(coro_fn, label):
        async with sem:
            try:
                return await coro_fn()
            except Exception as e:  # one bad view must not kill the batch
                print(f"  {label} failed: {type(e).__name__}: {e}")
                return []

    tasks = []
    for b in bundles:
        known = known_by_view.get((b.page_url, b.viewport), [])
        tasks.append(guarded(lambda b=b, k=known: judge_layout(b, k, cfg, run_id), f"layout {b.page_url}"))
        tasks.append(guarded(lambda b=b, k=known: judge_content(b, k, cfg, run_id), f"content {b.page_url}"))

    by_page: dict[str, list[CaptureBundle]] = {}
    for b in bundles:
        by_page.setdefault(b.page_url, []).append(b)
    for url, group in by_page.items():
        known = known_by_view.get((url, group[0].viewport), [])
        tasks.append(guarded(lambda g=group, k=known: judge_responsive(g, k, cfg, run_id), f"responsive {url}"))

    results = await asyncio.gather(*tasks)
    return [f for group in results for f in group]

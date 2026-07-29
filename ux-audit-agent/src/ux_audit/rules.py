"""Phase 2: the deterministic rule pass.

Turns raw `CaptureBundle` measurements into `Finding`s with a severity, per the
rubric in spec §9. No LLM anywhere in this file — that's the whole point of the
"never ask the model to notice something measurable" rule (§4.1).

Every threshold comes from config; nothing is hardcoded here except the *shape*
of the rubric.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from .config import Band, Config
from .models import SEVERITY_RANK, CaptureBundle, Finding, Severity


# axe impact → severity, per the rubric rows in spec §9.
AXE_IMPACT_SEVERITY: dict[str, Severity] = {
    "critical": "high",
    "serious": "medium",
    "moderate": "low",
    "minor": "low",
}


def _band_severity(value: float | None, band: Band) -> Severity | None:
    """Map a measurement onto the medium/high bands. Below medium isn't a finding."""
    if value is None:
        return None
    if value >= band.high:
        return "high"
    if value >= band.medium:
        return "medium"
    return None


def _site_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def _is_denylisted(text: str, denylist: list[str]) -> bool:
    return any(host in text for host in denylist)


def rules_for_bundle(bundle: CaptureBundle, cfg: Config, run_id: str) -> list[Finding]:
    """All deterministic findings derivable from one page × viewport capture."""
    site = _site_of(bundle.page_url)
    out: list[Finding] = []

    def add(category, severity, document, dedupe_key, evidence, confidence="high"):
        out.append(
            Finding(
                run_id=run_id,
                site=site,
                page_url=bundle.page_url,
                viewport=bundle.viewport,
                category=category,
                severity=severity,
                source="rule",
                confidence=confidence,
                document=document,
                dedupe_key=dedupe_key,
                evidence=evidence,
                screenshot_path=bundle.screenshot_settled_viewport_path,
            )
        )

    # --- capture failure is data, not silence (spec §6.2 / §6.5) ---------------
    if bundle.capture_error:
        add(
            "run_error",
            "high",
            f"Capture failed for {bundle.page_url} at {bundle.viewport}: {bundle.capture_error}",
            "capture_error",
            {"error": bundle.capture_error},
        )
        # A failed capture has no trustworthy measurements; don't emit more from it.
        return out

    t = cfg.thresholds

    # --- performance ----------------------------------------------------------
    ttfb = bundle.navigation_timing.ttfb_ms
    if (sev := _band_severity(ttfb, t.ttfb_ms)):
        add(
            "performance",
            sev,
            f"Time to first byte is {ttfb:.0f}ms — the server is slow to respond, "
            f"delaying everything that follows.",
            "ttfb",
            {"ttfb_ms": round(ttfb, 1), "threshold": t.ttfb_ms.model_dump()},
        )

    lcp = bundle.lcp_ms
    if (sev := _band_severity(lcp, t.lcp_ms)):
        add(
            "performance",
            sev,
            f"Largest contentful paint is {lcp:.0f}ms — the main content of the page "
            f"takes this long to appear.",
            "lcp",
            {"lcp_ms": round(lcp, 1), "threshold": t.lcp_ms.model_dump()},
        )

    # --- visual stability -----------------------------------------------------
    cls = bundle.cls_score
    if (sev := _band_severity(cls, t.cls)):
        top = sorted(bundle.layout_shifts, key=lambda s: s.value, reverse=True)[:3]
        selectors = [s.selector for s in top if s.selector]
        where = f" Largest shifts involve: {', '.join(selectors)}." if selectors else ""
        add(
            "visual_stability",
            sev,
            f"Cumulative layout shift is {cls:.3f} — content moves under the visitor "
            f"as the page loads.{where}",
            "cls",
            {
                "cls": round(cls, 4),
                "top_selectors": selectors,
                "threshold": t.cls.model_dump(),
                # Honest about the Phase-1 caveat rather than passing this off as web-vitals CLS.
                "note": "naive sum of non-input layout shifts, not the windowed session-max",
            },
        )

    # --- console / network ----------------------------------------------------
    denylist = cfg.noise.console_denylist_hosts
    errors = [m for m in bundle.console_messages if m.type == "error"]
    warnings = [m for m in bundle.console_messages if m.type in ("warning", "warn")]

    # Suppress third-party noise but record the count, never every line (spec §8.2).
    own_errors = [m for m in errors if not _is_denylisted(m.text, denylist)]
    suppressed = len(errors) - len(own_errors)

    if own_errors:
        sample = [m.text[:200] for m in own_errors[:3]]
        add(
            "console_network",
            "medium",
            f"{len(own_errors)} JavaScript console error(s) on load. First: {sample[0]}",
            "console_errors",
            {"count": len(own_errors), "samples": sample, "suppressed_thirdparty": suppressed},
        )

    own_warnings = [m for m in warnings if not _is_denylisted(m.text, denylist)]
    if own_warnings:
        add(
            "console_network",
            "low",
            f"{len(own_warnings)} console warning(s) on load.",
            "console_warnings",
            {"count": len(own_warnings), "samples": [m.text[:200] for m in own_warnings[:3]]},
        )

    http_errors = [f for f in bundle.network_failures if f.status and f.status >= 400]
    own_http = [f for f in http_errors if not _is_denylisted(f.url, denylist)]
    if own_http:
        add(
            "console_network",
            "medium",
            f"{len(own_http)} request(s) returned a {own_http[0].status or '4xx/5xx'} error.",
            "http_errors",
            {"count": len(own_http), "urls": [f.url[:200] for f in own_http[:3]]},
        )

    hard_failures = [f for f in bundle.network_failures if f.status is None]
    own_failures = [f for f in hard_failures if not _is_denylisted(f.url, denylist)]
    if own_failures:
        add(
            "console_network",
            "medium",
            f"{len(own_failures)} request(s) failed outright (no response): "
            f"{own_failures[0].failure_text or 'unknown error'}.",
            "network_failures",
            {
                "count": len(own_failures),
                "urls": [f.url[:200] for f in own_failures[:3]],
                "suppressed_thirdparty": len(hard_failures) - len(own_failures),
            },
        )

    # Mixed content: insecure subresource on a secure page.
    if bundle.page_url.startswith("https://"):
        mixed = [f.url for f in bundle.network_failures if f.url.startswith("http://")]
        if mixed:
            add(
                "console_network",
                "low",
                f"{len(mixed)} resource(s) requested over plain HTTP from an HTTPS page (mixed content).",
                "mixed_content",
                {"urls": mixed[:3]},
            )

    # --- content completeness / responsive ------------------------------------
    d = bundle.dom_issues

    if d.broken_image_srcs:
        # Fold position isn't captured in Phase 1, so we can't distinguish the
        # spec's above-fold (high) from below-fold (medium). Take the safer of
        # the two rather than inflating severity; recorded in evidence.
        add(
            "content_completeness",
            "medium",
            f"{len(d.broken_image_srcs)} image(s) fail to load. First: {d.broken_image_srcs[0][:150]}",
            "broken_images",
            {
                "count": len(d.broken_image_srcs),
                "srcs": d.broken_image_srcs[:5],
                "note": "fold position not measured; spec §9 would rate above-fold as high",
            },
        )

    if d.overflowing_selectors:
        add(
            "responsive_design",
            "medium",
            f"{len(d.overflowing_selectors)} element(s) overflow horizontally at {bundle.viewport}, "
            f"causing sideways scroll or clipped content. e.g. {d.overflowing_selectors[0]}",
            "horizontal_overflow",
            {"count": len(d.overflowing_selectors), "selectors": d.overflowing_selectors[:5]},
        )

    if d.placeholder_text_hits:
        add(
            "content_completeness",
            "low",
            f"Placeholder text still present on the page: {d.placeholder_text_hits[0][:150]}",
            "placeholder_text",
            {"hits": d.placeholder_text_hits[:5]},
        )

    # --- accessibility (axe-core, Phase 5) ------------------------------------
    # Already collapsed by rule_id in axe.py; one finding per rule, per spec §9's
    # impact mapping.
    for v in bundle.axe_violations:
        out.append(
            Finding(
                run_id=run_id,
                site=site,
                page_url=bundle.page_url,
                viewport=bundle.viewport,
                category="accessibility",
                severity=AXE_IMPACT_SEVERITY.get(v.impact, "low"),
                source="axe",
                confidence="high",
                document=f"Accessibility ({v.impact}): {v.help}. Affects {v.node_count} element(s).",
                dedupe_key=f"axe:{v.rule_id}",
                evidence={
                    "rule_id": v.rule_id,
                    "impact": v.impact,
                    "node_count": v.node_count,
                    "selectors": v.selectors,
                    "help_url": v.help_url,
                },
                screenshot_path=bundle.screenshot_settled_viewport_path,
            )
        )

    if bundle.axe_error:
        add(
            "run_error",
            "low",
            f"Accessibility scan failed on this page: {bundle.axe_error}",
            "axe_error",
            {"error": bundle.axe_error},
        )

    return out


# ---------------------------------------------------------------------------
# Dedupe + capping (spec §8.2)
# ---------------------------------------------------------------------------


def collapse_across_viewports(findings: list[Finding]) -> list[Finding]:
    """Collapse identical findings across viewports.

    Viewport-specific categories (responsive_design) are deliberately NOT
    collapsed — "it overflows on mobile" is a different fact from "it overflows
    on desktop". Everything else keys on (page, category, dedupe_key).
    """
    viewport_specific = {"responsive_design"}
    merged: dict[tuple, Finding] = {}

    for f in findings:
        if f.category in viewport_specific:
            merged[(f.page_url, f.category, f.dedupe_key, f.viewport)] = f
            continue
        key = (f.page_url, f.category, f.dedupe_key)
        prior = merged.get(key)
        if prior is None:
            merged[key] = f
        else:
            # Keep the worst severity seen, and record every viewport it hit.
            if SEVERITY_RANK[f.severity] > SEVERITY_RANK[prior.severity]:
                f.viewports_affected = sorted(set(prior.viewports_affected + [f.viewport]))
                merged[key] = f
            else:
                prior.viewports_affected = sorted(set(prior.viewports_affected + [f.viewport]))

    return list(merged.values())


def cap_per_page(findings: list[Finding], max_per_page: int) -> list[Finding]:
    """Global cap, highest severity first (spec §8.2)."""
    by_page: dict[str, list[Finding]] = {}
    for f in findings:
        by_page.setdefault(f.page_url, []).append(f)

    out: list[Finding] = []
    for page, items in by_page.items():
        items.sort(key=lambda f: (-SEVERITY_RANK[f.severity], f.category, f.dedupe_key))
        out.extend(items[:max_per_page])
    return out


def cap_vision_severity(findings: list[Finding]) -> list[Finding]:
    """Spec §9 hard rule, enforced in exactly one place.

    A judgment-pass finding cannot be `high` unless it ties to a deterministic
    signal — the vision pass sets `evidence["tied_to"]` (a rule finding's
    dedupe_key) when it does. Severity inflation is what makes these reports get
    ignored, so this is not negotiable and not left to the prompt.
    """
    for f in findings:
        if f.source == "vision" and f.severity == "high" and not f.evidence.get("tied_to"):
            f.severity = "medium"
            f.evidence["severity_capped"] = "vision finding not tied to a deterministic signal"
    return findings


def synthesize(findings: list[Finding], cfg: Config) -> list[Finding]:
    """Full post-processing chain: cap vision → collapse → cap per page → finalize → sort."""
    for f in findings:
        f.viewports_affected = f.viewports_affected or [f.viewport]
    cap_vision_severity(findings)
    collapsed = collapse_across_viewports(findings)
    capped = cap_per_page(collapsed, cfg.noise.max_findings_per_page)
    for f in capped:
        f.finalize()
    capped.sort(key=lambda f: (f.page_url, -SEVERITY_RANK[f.severity], f.category))
    return capped

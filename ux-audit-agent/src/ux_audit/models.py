"""
Data contract produced by the capture harness (Phase 1).

Nothing here judges severity or decides what's a "finding" — that's Phase 2
(rule pass). This is deliberately just raw, typed measurements + evidence
paths, so the rule pass, the vision pass, and Chroma ingestion (later
phases) all consume the exact same shape without re-deriving it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class ConsoleMessage(BaseModel):
    type: str  # "error" | "warning" | "log" | ...
    text: str


class NetworkFailure(BaseModel):
    url: str
    status: int | None = None  # None if the request failed outright (no response)
    failure_text: str | None = None  # Playwright's request.failure() text, if any


class NavigationTiming(BaseModel):
    ttfb_ms: float | None = None
    dom_content_loaded_ms: float | None = None
    load_event_ms: float | None = None


class LayoutShiftEntry(BaseModel):
    value: float
    selector: str | None = None  # best-effort: tag/id/class of the largest shifting node


class DomIssues(BaseModel):
    """Cheap, deterministic DOM checks — not AI judgment, just querySelector-level facts."""
    broken_image_srcs: list[str] = Field(default_factory=list)
    overflowing_selectors: list[str] = Field(default_factory=list)
    placeholder_text_hits: list[str] = Field(default_factory=list)


class AxeViolation(BaseModel):
    """One axe-core rule violation, already collapsed to <=3 example selectors (spec §8.2)."""

    rule_id: str
    impact: str  # "critical" | "serious" | "moderate" | "minor"
    help: str
    help_url: str | None = None
    selectors: list[str] = Field(default_factory=list)
    node_count: int = 0


class CaptureBundle(BaseModel):
    page_url: str
    viewport: str  # e.g. "mobile_390" — matches a name in config.yaml's viewports list
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Deterministic signals — the "invisible to the eye" category from the spec.
    console_messages: list[ConsoleMessage] = Field(default_factory=list)
    network_failures: list[NetworkFailure] = Field(default_factory=list)
    navigation_timing: NavigationTiming = Field(default_factory=NavigationTiming)
    cls_score: float | None = None
    lcp_ms: float | None = None
    layout_shifts: list[LayoutShiftEntry] = Field(default_factory=list)
    dom_issues: DomIssues = Field(default_factory=DomIssues)

    # Evidence for later passes (vision judgment, advisor screenshot lookup).
    screenshot_early_path: str | None = None  # taken ~800ms in, catches FOUC
    screenshot_settled_viewport_path: str | None = None
    screenshot_settled_fullpage_path: str | None = None

    # False when the page never stopped moving before the settle cap (looping
    # animation, spinner, autoplaying media). The vision pass is told, so it can
    # discount motion-blur artefacts instead of reporting them as defects.
    visually_stable: bool = True

    # True when the DOM has visible text that never painted (JS-driven reveal
    # animations that don't fire in headless). The screenshot is unrepresentative;
    # the vision pass is told so it doesn't report the blank area as a defect.
    render_suspect: bool = False

    # Interaction surface — decides whether the probe pass visits this page (spec §6.3).
    has_form: bool = False
    form_selectors: list[str] = Field(default_factory=list)
    primary_cta: str | None = None  # text of the most prominent above-fold button/link

    # Accessibility violations from the bundled axe-core run (Phase 5).
    axe_violations: list["AxeViolation"] = Field(default_factory=list)
    axe_error: str | None = None  # set if the a11y scan itself failed

    # Set if capture itself failed — becomes a "run_error" finding in Phase 2,
    # rather than silently vanishing from the run.
    capture_error: str | None = None

    status: Literal["ok", "error"] = "ok"


# ---------------------------------------------------------------------------
# Phase 2: findings
# ---------------------------------------------------------------------------

Category = Literal[
    "performance",
    "visual_stability",
    "content_completeness",
    "interaction",
    "console_network",
    "accessibility",
    "responsive_design",
    "run_error",
]

Severity = Literal["high", "medium", "low"]
Source = Literal["rule", "vision", "probe", "axe"]
Confidence = Literal["high", "medium", "low"]

# severity_rank exists so Chroma metadata filters can do $gte — strings can't be
# range-filtered (spec §8.2). Keep these two in lockstep.
SEVERITY_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1}


class Finding(BaseModel):
    """One audit finding. Written to findings.jsonl (source of truth) and indexed
    into Chroma. `id` is derived, not random, so re-ingest upserts."""

    id: str = ""  # sha1(run_id|page_url|viewport|category|dedupe_key)[:16]
    run_id: str
    site: str
    page_url: str
    viewport: str
    category: Category
    severity: Severity
    severity_rank: int = 0
    source: Source
    confidence: Confidence = "high"

    # The human-readable sentence. This is what gets embedded for semantic search.
    document: str

    # Stable key identifying *what kind of thing* this is, within a category.
    # Two findings with the same (page, viewport, category, dedupe_key) are the
    # same finding. Also what cross-viewport collapsing keys on.
    dedupe_key: str

    evidence: dict = Field(default_factory=dict)
    screenshot_path: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Set when a finding has been collapsed across viewports (spec §8.2 dedupe).
    viewports_affected: list[str] = Field(default_factory=list)

    def compute_id(self) -> str:
        import hashlib

        raw = f"{self.run_id}|{self.page_url}|{self.viewport}|{self.category}|{self.dedupe_key}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def finalize(self) -> "Finding":
        """Fill derived fields. Call once, before writing."""
        self.severity_rank = SEVERITY_RANK[self.severity]
        if not self.id:
            self.id = self.compute_id()
        if not self.viewports_affected:
            self.viewports_affected = [self.viewport]
        return self


class PageTarget(BaseModel):
    """A page discovery selected for auditing (Phase 3)."""

    url: str
    depth: int = 0
    priority: int = 999  # lower sorts first; from config.discovery.prioritise


class DiscoveryDecision(BaseModel):
    """One line of discovery_log.jsonl — emitted for EVERY url seen (spec §6.1)."""

    url: str
    decision: Literal["selected", "excluded", "capped", "duplicate"]
    reason: str
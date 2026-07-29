"""Phase 3: the crawl as an ADK dynamic workflow (spec §4.3, §5).

Why a dynamic workflow and not `ParallelAgent`: checkpointed resume, native
Python control flow for the concurrency cap, and typed payloads between stages.

Two things learned by testing the real ADK 2.5.0 API rather than trusting the
sketch in the spec (both recorded in docs/CHECKLIST.md):

1. A node function receives its payload only if the parameter is literally named
   `node_input` (default `parameter_binding='state'` otherwise binds parameters
   from `ctx.state` by name). The type hint IS honoured — ADK coerces the payload
   into it — so typed input works as the spec describes.
2. `ctx.run_node(...)` returns the child's output **serialised to a dict**, not
   the Pydantic instance. So every call site re-validates. The spec's "returns the
   child's output directly" is true in spirit, not in type.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.adk.agents import Context
from google.adk.workflow import Workflow, node
from pydantic import BaseModel

from .capture import _slugify, browser_session, capture_page
from .config import Config, load_config
from .discovery import discover
from .models import CaptureBundle, DiscoveryDecision, Finding, PageTarget
from .rules import rules_for_bundle, synthesize
from .store import (
    append_discovery_log,
    ingest,
    run_dir,
    write_findings_jsonl,
    write_manifest,
)


# ---------------------------------------------------------------------------
# Process-local run context
#
# A Playwright Browser and an asyncio.Semaphore are not serialisable, so they
# can't ride along in a node payload. The orchestrator populates this singleton
# before fanning out; capture nodes read from it. Single process by design
# (spec §14: no A2A, no multi-machine), so a module global is honest here.
# ---------------------------------------------------------------------------


@dataclass
class RunContext:
    config: Config
    browser: Any = None
    semaphore: asyncio.Semaphore | None = None
    runs_root: str = "runs"


_RUN: RunContext | None = None


def current_run() -> RunContext:
    if _RUN is None:
        raise RuntimeError("RunContext not initialised — call run_audit()")
    return _RUN


# ---------------------------------------------------------------------------
# Node payloads
# ---------------------------------------------------------------------------


class DiscoverInput(BaseModel):
    root_url: str
    run_id: str


class DiscoverOutput(BaseModel):
    targets: list[PageTarget]
    seen_count: int


class CaptureUnit(BaseModel):
    page_url: str
    viewport: str
    run_id: str


class CaptureSummary(BaseModel):
    """What the capture node returns. The full CaptureBundle goes to disk — only
    a summary crosses the node boundary, keeping event payloads small."""

    page_url: str
    viewport: str
    status: str
    bundle_path: str
    capture_error: str | None = None
    has_form: bool = False
    primary_cta: str | None = None
    from_cache: bool = False  # True when resume reused an existing bundle


class AuditInput(BaseModel):
    root_url: str
    run_id: str
    viewports: list[str] | None = None


class AuditResult(BaseModel):
    run_id: str
    root_url: str
    pages: int
    units_attempted: int
    units_succeeded: int
    units_failed: int
    units_reused: int = 0  # skipped because a good bundle already existed (resume)
    findings: int
    findings_path: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@node
async def discover_node(ctx: Context, node_input: DiscoverInput) -> DiscoverOutput:
    """Pure-Python URL filtering (spec §4.4 — a filter, not an agent)."""
    rc = current_run()
    targets, decisions = await discover(node_input.root_url, rc.config)
    append_discovery_log(decisions, node_input.run_id, rc.runs_root)
    return DiscoverOutput(targets=targets, seen_count=len(decisions))


@node(timeout=120.0)
async def capture_node(ctx: Context, node_input: CaptureUnit) -> CaptureSummary:
    """One page × one viewport. Writes the bundle to disk, returns a summary.

    Never raises on a page-level failure: `capture_page` records the error into
    the bundle, which the rule pass turns into a `run_error` finding. Failures
    are data, not silence (spec §6.2).
    """
    rc = current_run()
    cfg = rc.config
    viewport = cfg.viewport_by_name(node_input.viewport)
    rdir = run_dir(node_input.run_id, rc.runs_root)

    captures_dir = rdir / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(node_input.page_url, node_input.viewport)
    path = captures_dir / f"{slug}.json"

    # Disk-level idempotence — the real resume guarantee for the expensive step.
    #
    # ADK's node checkpointing does record completed nodes, but its resumability
    # is experimental and aimed at paused/HITL and gracefully-failed invocations;
    # a SIGKILL'd crawl resumed to zero yielded events in testing. Since a good
    # bundle on disk already proves the work was done, the cheapest correct fix is
    # to not redo it. Survives kill -9, reboot, anything. See docs/CHECKLIST.md.
    if path.exists():
        try:
            existing = CaptureBundle.model_validate_json(path.read_text())
            if existing.status == "ok":
                return CaptureSummary(
                    page_url=existing.page_url,
                    viewport=existing.viewport,
                    status=existing.status,
                    bundle_path=str(path),
                    capture_error=None,
                    has_form=existing.has_form,
                    primary_cta=existing.primary_cta,
                    from_cache=True,
                )
        except Exception:
            pass  # unreadable/truncated bundle (e.g. killed mid-write) — recapture

    bundle = await capture_page(node_input.page_url, viewport, cfg, rdir, browser=rc.browser)
    path.write_text(bundle.model_dump_json(indent=2))

    return CaptureSummary(
        page_url=bundle.page_url,
        viewport=bundle.viewport,
        status=bundle.status,
        bundle_path=str(path),
        capture_error=bundle.capture_error,
        has_form=bundle.has_form,
        primary_cta=bundle.primary_cta,
    )


class RuleInput(BaseModel):
    run_id: str
    bundle_paths: list[str]


class RuleOutput(BaseModel):
    findings_path: str
    count: int


@node
async def rule_node(ctx: Context, node_input: RuleInput) -> RuleOutput:
    """Deterministic pass over every captured bundle, then dedupe/cap/store."""
    rc = current_run()
    raw: list[Finding] = []
    for p in node_input.bundle_paths:
        bundle = CaptureBundle.model_validate_json(Path(p).read_text())
        raw.extend(rules_for_bundle(bundle, rc.config, node_input.run_id))

    findings = synthesize(raw, rc.config)
    path = write_findings_jsonl(findings, node_input.run_id, rc.runs_root)
    ingest(findings, rc.config)
    return RuleOutput(findings_path=str(path), count=len(findings))


@node(rerun_on_resume=True)
async def audit_workflow(ctx: Context, node_input: AuditInput) -> AuditResult:
    """The orchestrator.

    MUST be `rerun_on_resume=True` (spec §7) — a parent that calls `run_node` has
    to re-execute on resume so it can collect the cached child results, otherwise
    resume silently returns nothing.
    """
    rc = current_run()
    cfg = rc.config

    disc_raw = await ctx.run_node(discover_node, DiscoverInput(root_url=node_input.root_url, run_id=node_input.run_id))
    disc = DiscoverOutput.model_validate(disc_raw)

    viewport_names = node_input.viewports or [v.name for v in cfg.viewports]
    units = [
        CaptureUnit(page_url=t.url, viewport=vp, run_id=node_input.run_id)
        for t in disc.targets
        for vp in viewport_names
    ]

    sem = rc.semaphore or asyncio.Semaphore(cfg.run.browser_concurrency)

    async def run_unit(unit: CaptureUnit):
        # The semaphore bounds live Chromium contexts (spec §6.2). Note the ADK
        # docstring forbids wrapping run_node in create_task; gather awaits its
        # children directly, which is fine and was smoke-tested.
        async with sem:
            kwargs = {}
            if cfg.run.custom_run_ids:
                # Opt-in trade (spec §7): survives a changed page list, but custom
                # IDs drive execution ordering. Must contain a non-numeric char.
                kwargs["run_id"] = f"cap-{_slugify(unit.page_url, unit.viewport)}"
            return await ctx.run_node(capture_node, unit, **kwargs)

    raw_summaries = await asyncio.gather(*[run_unit(u) for u in units], return_exceptions=True)

    summaries: list[CaptureSummary] = []
    failed = 0
    for r in raw_summaries:
        if isinstance(r, BaseException):
            failed += 1
            continue
        try:
            summaries.append(CaptureSummary.model_validate(r))
        except Exception:
            failed += 1

    succeeded = [s for s in summaries if s.status == "ok"]
    failed += len(summaries) - len(succeeded)
    reused = sum(1 for s in summaries if s.from_cache)

    rule_raw = await ctx.run_node(
        rule_node, RuleInput(run_id=node_input.run_id, bundle_paths=[s.bundle_path for s in summaries])
    )
    rule_out = RuleOutput.model_validate(rule_raw)

    write_manifest(
        run_id=node_input.run_id,
        root_url=node_input.root_url,
        cfg=cfg,
        pages_attempted=len(units),
        pages_succeeded=len(succeeded),
        pages_failed=failed,
        extra={
            "urls_seen_in_discovery": disc.seen_count,
            "pages_selected": len(disc.targets),
            "viewports": viewport_names,
        },
        root=rc.runs_root,
    )

    return AuditResult(
        run_id=node_input.run_id,
        root_url=node_input.root_url,
        pages=len(disc.targets),
        units_attempted=len(units),
        units_succeeded=len(succeeded),
        units_failed=failed,
        units_reused=reused,
        findings=rule_out.count,
        findings_path=rule_out.findings_path,
    )


def build_workflow() -> Workflow:
    return Workflow(name="ux_audit", edges=[("START", audit_workflow)])

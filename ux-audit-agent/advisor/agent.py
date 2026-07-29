"""Phase 8: the advisor agent (spec §8.3, §5).

    source .venv/bin/activate
    adk web .              # from the project root, then pick "advisor"

A separate App from the crawl. It reads the findings store and answers questions
about it. The one rule that matters: every claim is grounded in a finding `id`,
and when the store has nothing it says so instead of improvising. An advisor that
invents plausible UX advice is worse than no advisor, because you can't tell the
difference without re-auditing by hand.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
load_dotenv(_PROJECT_ROOT / ".env")

from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.tools import FunctionTool  # noqa: E402
from google.adk.tools.load_artifacts_tool import LoadArtifactsTool  # noqa: E402

from ux_audit.config import load_config  # noqa: E402
from ux_audit import store  # noqa: E402

_CFG = load_config(_PROJECT_ROOT / "config.yaml")


# ---------------------------------------------------------------------------
# Running an audit from the chat
#
# A crawl takes minutes, which is far too long to block a tool call — the UI
# would look hung. So `start_audit` launches it as a background task and returns
# immediately with a run_id; `audit_status` polls it. That turns one chat into
# the whole loop: start it, watch it, then ask about the results.
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict] = {}


async def start_audit(url: str, viewports: str = "", include_vision: bool = False) -> str:
    """Start a UX audit crawl of a website. Returns immediately with a run_id.

    The crawl runs in the background and takes several minutes. Use
    `audit_status` to check on it, then `search_findings` to ask about results.

    Args:
        url: The website to audit, e.g. "https://example.com".
        viewports: Optional comma-separated viewport names to limit the crawl,
            e.g. "mobile_390". Empty means all configured viewports (slower).
        include_vision: If true, also run the vision judgment pass after the
            crawl finishes. Adds several minutes.

    Returns:
        JSON with the run_id and starting state.
    """
    import asyncio

    from ux_audit.run import default_run_id, run_audit

    if any(j["state"] == "running" for j in _JOBS.values()):
        running = [r for r, j in _JOBS.items() if j["state"] == "running"]
        return json.dumps({
            "error": "An audit is already running; only one at a time is supported.",
            "running_run_ids": running,
        })

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    run_id = default_run_id(url)
    vps = [v.strip() for v in viewports.split(",") if v.strip()] or None
    _JOBS[run_id] = {"state": "running", "url": url, "viewports": vps, "stage": "crawling"}

    async def _job():
        try:
            await run_audit(url, run_id=run_id, viewports=vps, config_path=str(_PROJECT_ROOT / "config.yaml"))
            if include_vision:
                _JOBS[run_id]["stage"] = "vision"
                from ux_audit.judge import judge_run
                from ux_audit.rules import synthesize
                from ux_audit.store import ingest, read_findings_jsonl, write_findings_jsonl

                existing = read_findings_jsonl(run_id)
                known: dict = {}
                for f in existing:
                    known.setdefault((f.page_url, f.viewport), []).append(f)
                vision = await judge_run(run_id, _CFG, known_by_view=known)
                merged = synthesize([f for f in existing if f.source != "vision"] + vision, _CFG)
                write_findings_jsonl(merged, run_id)
                ingest(merged, _CFG)
            _JOBS[run_id].update(state="done", stage="finished")
        except Exception as e:  # noqa: BLE001 — surface it in chat, don't crash the server
            _JOBS[run_id].update(state="error", error=f"{type(e).__name__}: {e}")

    asyncio.create_task(_job())
    return json.dumps({
        "run_id": run_id,
        "state": "running",
        "url": url,
        "note": "Crawl started in the background. Poll with audit_status(run_id).",
    })


def audit_status(run_id: str = "") -> str:
    """Check how a running or finished audit is progressing.

    Args:
        run_id: The run to check. Empty means the most recently started one.

    Returns:
        JSON with state, how many page x viewport units have been captured so
        far, and the findings count once available.
    """
    if not run_id:
        if not _JOBS:
            return json.dumps({"error": "No audit has been started in this session."})
        run_id = list(_JOBS)[-1]

    job = _JOBS.get(run_id, {"state": "unknown"})
    rdir = _PROJECT_ROOT / "runs" / run_id
    captured = len(list((rdir / "captures").glob("*.json"))) if (rdir / "captures").exists() else 0
    findings_file = rdir / "findings.jsonl"
    findings = sum(1 for _ in open(findings_file)) if findings_file.exists() else 0

    return json.dumps({
        "run_id": run_id,
        "state": job.get("state"),
        "stage": job.get("stage"),
        "error": job.get("error"),
        "units_captured_so_far": captured,
        "findings_so_far": findings,
    })


def search_findings(
    query: str,
    site: str = "",
    run_id: str = "",
    category: str = "",
    min_severity: str = "",
    limit: int = 8,
) -> str:
    """Search the UX audit findings store.

    Args:
        query: What to look for, in plain language (e.g. "layout problems on mobile").
        site: Optional exact host filter, e.g. "example.com".
        run_id: Optional exact run filter.
        category: Optional one of performance, visual_stability, content_completeness,
            interaction, console_network, accessibility, responsive_design, run_error.
        min_severity: Optional one of "low", "medium", "high" — returns that severity and above.
        limit: Maximum number of findings to return.

    Returns:
        A JSON list of findings, each with its id, severity, category, page_url,
        viewport, source, the finding text, and the screenshot path if there is one.
    """
    rank = {"low": 1, "medium": 2, "high": 3}.get(min_severity.lower()) if min_severity else None
    try:
        rows = store.search_findings(
            query=query,
            cfg=_CFG,
            n_results=max(1, min(limit, 25)),
            site=site or None,
            run_id=run_id or None,
            category=category or None,
            min_severity_rank=rank,
        )
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}", "findings": []})

    slim = [
        {
            "id": r["id"],
            "severity": r.get("severity"),
            "category": r.get("category"),
            "page_url": r.get("page_url"),
            "viewport": r.get("viewport"),
            "source": r.get("source"),
            "finding": r.get("document"),
            "evidence": r.get("evidence"),
            "screenshot_path": r.get("screenshot_path") or None,
        }
        for r in rows
    ]
    return json.dumps({"count": len(slim), "findings": slim}, indent=2)


INSTRUCTION = """\
You are a UX advisor. You can run an audit of a website, and you answer questions
about audits by reading the findings store — never from general web-design
knowledge.

RUNNING AN AUDIT
If the user gives you a URL and asks you to audit/check/review it:
1. Call `start_audit` with that URL. It returns a run_id immediately and the
   crawl continues in the background — it takes several minutes.
2. Tell the user the run_id and that it's running. Suggest `mobile_390` alone if
   they want a faster first result.
3. When they ask how it's going (or before answering questions about that run),
   call `audit_status`. Report units captured so far. Do NOT claim results exist
   until state is "done".
4. Once done, use `search_findings` scoped to that run_id.
Never guess at results for a run that is still crawling.

ANSWERING QUESTIONS — rules, in priority order:

1. ALWAYS call `search_findings` before answering anything about a site. Do not
   answer from memory of earlier turns alone if new specifics are asked for.
2. GROUND every claim in a finding id. Write them inline like `[a1b2c3d4e5f6]`.
   If you cannot attach an id to a statement, do not make the statement.
3. If the search returns nothing, SAY SO plainly: "The findings store has nothing
   on that." Then offer what IS in the store. Never fill the gap with plausible
   generic advice — an invented finding is worse than no answer.
4. Severity is already judged; don't re-rate it. `high` blocks a visitor task,
   `medium` degrades it, `low` is cosmetic.
5. Note the `source` when it matters: `rule` and `axe` are deterministic
   measurements, `vision` is a model's visual judgment, `probe` is a real
   interaction test. If a user pushes back on a `vision` finding, say it's a
   judgment call and point at the screenshot.
6. When a finding has a `screenshot_path`, you may call `load_artifacts` with that
   path to look at it and describe what you see.

When asked what to fix first: order by severity, then by how many pages share the
finding. Give concrete, specific fixes tied to the evidence — not a checklist of
best practices.
"""

_TOOLS = [FunctionTool(start_audit), FunctionTool(audit_status), FunctionTool(search_findings)]

# LoadArtifactsTool is OPT-IN, and that is not a style choice.
#
# Its `process_llm_request` calls `list_artifacts()` on EVERY turn, which raises
# `ValueError: Artifact service is not initialized.` when the runner has no
# artifact service. `adk web` provides none and exposes no
# `--artifact_service_uri` flag, so including it unconditionally doesn't just
# disable screenshots — it breaks every single turn of the conversation.
#
# Set UX_AUDIT_ARTIFACTS=1 only when you construct the Runner yourself with
# LocalDirArtifactService (see docs/CHECKLIST.md Phase 8).
if os.environ.get("UX_AUDIT_ARTIFACTS") == "1":
    _TOOLS.append(LoadArtifactsTool())


root_agent = LlmAgent(
    name="advisor",
    model=_CFG.judgment.model,
    instruction=INSTRUCTION,
    tools=_TOOLS,
)

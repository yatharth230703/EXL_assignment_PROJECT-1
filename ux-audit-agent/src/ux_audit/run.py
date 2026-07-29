"""Entry point for a full audit run.

    PYTHONPATH=src python -m ux_audit.run https://example.com
    PYTHONPATH=src python -m ux_audit.run https://example.com --resume <run_id>

Resume works because the session lives in SQLite (`DatabaseSessionService`) and
ADK skips already-completed child nodes. Kill this mid-crawl and re-run with
`--resume <run_id>`; completed page×viewport units are not re-captured.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from google.adk.apps import App
from google.adk.apps._configs import ResumabilityConfig
from google.adk.plugins.logging_plugin import LoggingPlugin
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from . import workflow as wf
from .config import load_config
from .plugins import CaptureErrorPlugin, errors_to_findings
from .store import ingest, read_findings_jsonl, write_findings_jsonl

APP_NAME = "ux_audit"
USER_ID = "local"


def default_run_id(root_url: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    site = urlparse(root_url).netloc.lower().replace(":", "_").replace(".", "_")
    return f"{stamp}-{site}"


async def run_audit(
    root_url: str,
    config_path: str = "config.yaml",
    run_id: str | None = None,
    viewports: list[str] | None = None,
    runs_root: str = "runs",
    session_id: str | None = None,
    resume: bool = False,
) -> dict:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    cfg = load_config(config_path)
    run_id = run_id or default_run_id(root_url)

    error_plugin = CaptureErrorPlugin(run_id=run_id, runs_root=runs_root)
    app = App(
        name=APP_NAME,
        root_agent=wf.build_workflow(),
        plugins=[LoggingPlugin(), error_plugin],
        # Without this, ADK starts a brand-new invocation on every call and
        # re-runs every node — checkpointing does nothing. Verified the hard way:
        # a kill-and-resume test re-captured all completed units until this was set.
        resumability_config=ResumabilityConfig(is_resumable=True),
    )

    session_service = DatabaseSessionService(db_url=cfg.run.session_db)
    runner = Runner(app=app, session_service=session_service)

    # Session id == run id: resuming a run means resuming its session.
    sid = session_id or run_id
    try:
        session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=sid)
    except Exception:
        session = None
    if session is None:
        session = await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=sid)

    payload = wf.AuditInput(root_url=root_url, run_id=run_id, viewports=viewports)

    # NOTE on resume (measured, see docs/CHECKLIST.md):
    # Re-entering the killed invocation via `invocation_id=` yields ZERO events in
    # ADK 2.5.0 — its resumability targets paused/HITL and gracefully-failed
    # invocations, not a SIGKILL'd process. So `--resume` starts a fresh
    # invocation with the SAME run_id, and `capture_node` skips any page×viewport
    # that already has a good bundle on disk. Same guarantee (no repeated work),
    # owned by a layer that can actually make it.
    inv_path = Path(runs_root) / run_id / "invocation_id"

    result = None
    # One Chromium for the whole crawl; the semaphore bounds live contexts (spec §6.2).
    async with wf.browser_session(cfg) as browser:
        wf._RUN = wf.RunContext(
            config=cfg,
            browser=browser,
            semaphore=asyncio.Semaphore(cfg.run.browser_concurrency),
            runs_root=runs_root,
        )
        recorded = False
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=payload.model_dump_json())]),
        ):
            if not recorded and getattr(event, "invocation_id", None):
                inv_path.parent.mkdir(parents=True, exist_ok=True)
                inv_path.write_text(event.invocation_id)
                recorded = True
            if getattr(event, "output", None):
                result = event.output

    # Fold plugin-captured failures into the report (spec §6.5: failures are data).
    extra = errors_to_findings(run_id, urlparse(root_url).netloc.lower(), root_url, runs_root)
    if extra:
        merged = read_findings_jsonl(run_id, runs_root) + extra
        write_findings_jsonl(merged, run_id, runs_root)
        ingest(extra, cfg)

    return {"run_id": run_id, "result": result, "plugin_errors": len(error_plugin.errors)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a full UX audit crawl.")
    ap.add_argument("root_url")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--resume", default=None, help="run_id of an interrupted run to resume")
    ap.add_argument("--viewports", default=None, help="comma-separated viewport names (default: all)")
    ap.add_argument("--runs-root", default="runs")
    args = ap.parse_args()

    run_id = args.resume or args.run_id
    viewports = args.viewports.split(",") if args.viewports else None

    out = asyncio.run(
        run_audit(
            args.root_url,
            config_path=args.config,
            run_id=run_id,
            viewports=viewports,
            runs_root=args.runs_root,
            resume=bool(args.resume),
        )
    )
    print("\n=== audit complete ===")
    print(f"run_id        : {out['run_id']}")
    print(f"plugin errors : {out['plugin_errors']}")
    r = out["result"]
    if isinstance(r, dict):
        for k, v in r.items():
            print(f"{k:<16}: {v}")
    else:
        print(f"result        : {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

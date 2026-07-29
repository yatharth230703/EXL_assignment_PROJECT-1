"""Phase 7 CLI: run the interaction probe over a completed capture run.

    PYTHONPATH=src python -m ux_audit.probepass <run_id> [--no-llm]

`--no-llm` runs only the deterministic half (our Playwright, measuring), which is
useful when you want the objective interaction findings without spending tokens
or waiting on the MCP browser.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from .capture import browser_session
from .config import load_config
from .models import CaptureBundle, Finding
from .probe import (
    deterministic_probe,
    explore_page,
    observations_to_findings,
    select_probe_targets,
)
from .rules import synthesize
from .store import ingest, read_findings_jsonl, write_findings_jsonl


async def _main(args) -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    cfg = load_config(args.config)

    cap_dir = Path(args.runs_root) / args.run_id / "captures"
    bundles = [CaptureBundle.model_validate_json(p.read_text()) for p in sorted(cap_dir.glob("*.json"))]
    targets = select_probe_targets(bundles, cfg)

    print(f"probe targets: {len(targets)} page(s) (cap {cfg.probe.max_probe_pages})")
    for t in targets:
        print(f"  {t.page_url}  [form={t.has_form} cta={t.primary_cta!r}]")
    if not targets:
        print("nothing to probe — no page had a form or an above-fold CTA")
        return 0

    new_findings: list[Finding] = []

    async with browser_session(cfg) as browser:
        for t in targets:
            site = urlparse(t.page_url).netloc.lower()
            viewport = cfg.viewport_by_name(t.viewport)

            obs = await deterministic_probe(t.page_url, viewport, cfg, browser)
            print(f"\n{t.page_url} @ {t.viewport}")
            for o in obs:
                print(
                    f"  {o.action:<22} requests={o.requests_fired} dom_changed={o.dom_changed_within_1500ms} "
                    f"error_shown={o.error_message_shown} associated={o.error_associated_with_field} "
                    f"dupes={o.duplicate_requests}"
                )
            new_findings.extend(observations_to_findings(obs, t.page_url, t.viewport, args.run_id, site))

            if not args.no_llm:
                verdict, calls = await explore_page(t.page_url, cfg)
                print(f"  LLM tool calls: {calls}")
                if verdict:
                    print(f"  LLM verdict: {verdict.strip()[:300]}")
                    new_findings.append(
                        Finding(
                            run_id=args.run_id, site=site, page_url=t.page_url, viewport=t.viewport,
                            category="interaction", severity="low", source="probe", confidence="medium",
                            document=f"Probe agent's read of the primary action: {verdict.strip()[:400]}",
                            dedupe_key="probe_verdict",
                            evidence={"tool_calls": calls},
                        )
                    )

    existing = [f for f in read_findings_jsonl(args.run_id, args.runs_root) if f.source != "probe"]
    merged = synthesize(existing + new_findings, cfg)
    write_findings_jsonl(merged, args.run_id, args.runs_root)
    ingest(merged, cfg)

    print(f"\nprobe findings: {len(new_findings)}   total findings: {len(merged)}")
    for f in merged:
        if f.source == "probe":
            print(f"  [{f.severity:>6}] {f.dedupe_key:<26} {f.document[:80]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the interaction probe pass.")
    ap.add_argument("run_id")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--no-llm", action="store_true", help="deterministic half only")
    return asyncio.run(_main(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

"""Phase 6 CLI: re-run the vision pass over saved screenshots.

    PYTHONPATH=src python -m ux_audit.judgepass <run_id>

No browser involved. Change the prompt in judge.py, run this again, compare —
that loop is the point of keeping capture and judgment separate (spec §4.2).
Merges into the existing findings.jsonl and re-indexes.
"""

from __future__ import annotations

import argparse
import json
import asyncio
from pathlib import Path

from dotenv import load_dotenv

from .config import load_config
from .judge import judge_run
from .rules import synthesize
from .store import ingest, read_findings_jsonl, write_findings_jsonl


async def _main(args) -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    cfg = load_config(args.config)

    existing = read_findings_jsonl(args.run_id, args.runs_root)
    # Give the model the rule findings for this exact view so it doesn't repeat them.
    known: dict[tuple[str, str], list] = {}
    for f in existing:
        known.setdefault((f.page_url, f.viewport), []).append(f)

    vision = await judge_run(args.run_id, cfg, known_by_view=known, runs_root=args.runs_root)

    # Drop prior vision findings so re-running replaces rather than accumulates.
    kept = [f for f in existing if f.source != "vision"]
    merged = synthesize(kept + vision, cfg)

    write_findings_jsonl(merged, args.run_id, args.runs_root)
    ingest(merged, cfg)

    from .judge import SUPPRESSED

    # Per-specialist counts: if one collapses to zero it must be visible, not
    # masked by the others still working.
    by_specialist: dict[str, int] = {}
    for f in vision:
        by_specialist[f.evidence.get("specialist", "?")] = by_specialist.get(f.evidence.get("specialist", "?"), 0) + 1

    print(f"vision findings  : {len(vision)}")
    print(f"  by specialist  : {json.dumps(by_specialist)}")
    if SUPPRESSED:
        print(f"suppressed       : {len(SUPPRESSED)} contradicted by a deterministic measurement")
        for s in SUPPRESSED:
            print(f"    - [{s.get('specialist','?')}] {s['finding'][:64]}")
            print(f"      reason: {s['reason']}")
    print(f"total findings   : {len(merged)}")
    for f in merged:
        if f.source == "vision":
            print(f"  [{f.severity:>6}/{f.confidence:<6}] {f.category:<22} {f.document[:90]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-run the vision judgment pass over saved screenshots.")
    ap.add_argument("run_id")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--runs-root", default="runs")
    return asyncio.run(_main(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

"""Phase 2 CLI: run the rule pass over already-captured bundles.

    PYTHONPATH=src python -m ux_audit.rulepass <run_id> [--no-chroma]

Deliberately decoupled from capture: it reads `runs/<run_id>/captures/*.json` off
disk, so you can re-run the rubric after changing a threshold without touching a
browser. Same iteration-speed argument as spec §4.2 makes for the vision pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .models import CaptureBundle
from .rules import rules_for_bundle, synthesize
from .store import ingest, run_dir, write_findings_jsonl


def load_bundles(run_id: str, root: str | Path = "runs") -> list[CaptureBundle]:
    cap_dir = run_dir(run_id, root) / "captures"
    if not cap_dir.exists():
        raise FileNotFoundError(f"No captures directory at {cap_dir}")
    return [CaptureBundle.model_validate_json(p.read_text()) for p in sorted(cap_dir.glob("*.json"))]


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the deterministic rule pass over a capture run.")
    ap.add_argument("run_id")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--no-chroma", action="store_true", help="write JSONL only, skip indexing")
    args = ap.parse_args()

    cfg = load_config(args.config)
    bundles = load_bundles(args.run_id, args.runs_root)
    if not bundles:
        print(f"no capture bundles in run {args.run_id}", file=sys.stderr)
        return 1

    raw = []
    for b in bundles:
        raw.extend(rules_for_bundle(b, cfg, args.run_id))
    findings = synthesize(raw, cfg)

    path = write_findings_jsonl(findings, args.run_id, args.runs_root)

    indexed = 0
    if not args.no_chroma:
        indexed = ingest(findings, cfg)

    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1

    print(f"bundles read : {len(bundles)}")
    print(f"raw findings : {len(raw)}  ->  after dedupe/cap: {len(findings)}")
    print(f"by severity  : {json.dumps(by_sev)}")
    print(f"written      : {path}")
    print(f"indexed      : {indexed} into chroma" if indexed else "indexed      : skipped")
    for f in findings:
        print(f"  [{f.severity:>6}] {f.category:<22} {f.dedupe_key:<20} {f.document[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

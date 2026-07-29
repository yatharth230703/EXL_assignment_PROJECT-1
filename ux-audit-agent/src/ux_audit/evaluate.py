"""Phase 9: evaluation harness (spec §10).

Turns "is the LLM layer any good" into a number.

Two metrics, measuring different things:

  vision precision  — of the findings the judgment pass produced on labelled
                      views, what fraction does a human agree with? Matching a
                      free-text finding to a free-text label is itself a judgment
                      call, so an LLM judge does the matching, with a keyword
                      fallback when no API key is available.

  probe trajectory  — did the probe agent actually do its job: navigate, look,
                      take the primary action, look again, and stay inside its
                      turn budget. This is tool-call shaped, so it's a plain
                      deterministic check over the recorded calls.

Note on ADK's eval framework: `AgentEvaluator` / `Evaluator` operate over
`Invocation` (request/response) pairs. Vision precision is scored over *findings*
pulled from findings.jsonl, not over invocations, so forcing it through that
interface would be contortion for no gain. The trajectory check is closer to the
ADK shape and could be ported if this ever needs to run inside `adk eval`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .config import Config, load_config
from .models import Finding
from .store import read_findings_jsonl

JUDGE_PROMPT = """\
A human reviewer looked at a screenshot of a web page and listed the real
problems they saw. An automated vision model also reported a problem.

Decide whether the model's report corresponds to ONE OF the human's problems.
Match on substance, not wording — different phrasings of the same visual problem
count as a match.

HUMAN'S REAL PROBLEMS:
{expected}

MODEL'S REPORT:
{actual}

Answer with exactly one word: MATCH or NOMATCH.
"""


def _keyword_match(actual: str, expected: list[str]) -> bool:
    """Fallback matcher: meaningful word overlap. Crude but deterministic."""
    stop = {
        "the", "a", "an", "and", "or", "is", "are", "on", "in", "at", "to", "of",
        "it", "that", "this", "with", "for", "text", "page", "element", "elements",
    }
    a = {w.strip(".,'\"()") for w in actual.lower().split()} - stop
    for e in expected:
        b = {w.strip(".,'\"()") for w in e.lower().split()} - stop
        if b and len(a & b) / len(b) >= 0.4:
            return True
    return False


async def _llm_match(actual: str, expected: list[str], model: str) -> bool | None:
    """Returns True/False, or None if no judge is available."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key or not expected:
        return None
    try:
        from google import genai

        client = genai.Client(api_key=key)
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=JUDGE_PROMPT.format(
                expected="\n".join(f"- {e}" for e in expected), actual=actual
            ),
        )
        return "NOMATCH" not in (resp.text or "").upper()
    except Exception:
        return None


async def vision_precision(run_id: str, labels_path: str, cfg: Config, runs_root: str = "runs") -> dict:
    labels = json.loads(Path(labels_path).read_text())["labels"]
    by_view = {(l["page_url"], l["viewport"]): l for l in labels}

    findings = [f for f in read_findings_jsonl(run_id, runs_root) if f.source == "vision"]
    scored = [f for f in findings if (f.page_url, f.viewport) in by_view]

    tp = fp = 0
    details = []
    for f in scored:
        label = by_view[(f.page_url, f.viewport)]
        if label.get("clean"):
            # Any finding on a view a human called clean is a false positive.
            verdict, how = False, "view labelled clean"
        else:
            m = await _llm_match(f.document, label["expected_issues"], cfg.judgment.model)
            if m is None:
                verdict, how = _keyword_match(f.document, label["expected_issues"]), "keyword"
            else:
                verdict, how = m, "llm-judge"
        tp, fp = (tp + 1, fp) if verdict else (tp, fp + 1)
        details.append(
            {
                "page_url": f.page_url,
                "viewport": f.viewport,
                "finding": f.document[:110],
                "verdict": "TRUE POSITIVE" if verdict else "FALSE POSITIVE",
                "matched_by": how,
            }
        )

    # Recall over labelled issues: how many real problems did the model catch?
    expected_total = sum(len(l["expected_issues"]) for l in labels if not l.get("clean"))
    matched_views = {(d["page_url"], d["viewport"]) for d in details if d["verdict"] == "TRUE POSITIVE"}

    return {
        "views_labelled": len(labels),
        "views_with_findings": len({(f.page_url, f.viewport) for f in scored}),
        "vision_findings_scored": len(scored),
        "true_positives": tp,
        "false_positives": fp,
        "precision": round(tp / (tp + fp), 3) if (tp + fp) else None,
        "expected_issues_total": expected_total,
        "views_with_a_hit": len(matched_views),
        "details": details,
    }


REQUIRED_TOOLS = ["browser_navigate", "browser_snapshot", "browser_click"]


def probe_trajectory(tool_calls: list[str], max_turns: int) -> dict:
    """Did the probe do its job, in the right order, inside budget (spec §10)?"""
    checks = {
        "navigated": "browser_navigate" in tool_calls,
        "looked_before_acting": (
            "browser_snapshot" in tool_calls
            and "browser_click" in tool_calls
            and tool_calls.index("browser_snapshot") < tool_calls.index("browser_click")
        )
        if ("browser_snapshot" in tool_calls and "browser_click" in tool_calls)
        else False,
        "took_an_action": "browser_click" in tool_calls or "browser_type" in tool_calls,
        "looked_after_acting": (
            "browser_click" in tool_calls
            and tool_calls.index("browser_click") < len(tool_calls) - 1
            and "browser_snapshot" in tool_calls[tool_calls.index("browser_click") + 1:]
        ),
        "within_turn_budget": len(tool_calls) <= max_turns,
    }
    return {
        "tool_calls": tool_calls,
        "checks": checks,
        "passed": sum(checks.values()),
        "total": len(checks),
        "score": round(sum(checks.values()) / len(checks), 3),
    }


async def _main(args) -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    cfg = load_config(args.config)

    print("=" * 66)
    print("VISION PRECISION  (spec §10 — gate prompt changes on this)")
    print("=" * 66)
    res = await vision_precision(args.run_id, args.labels, cfg, args.runs_root)
    for k in ("views_labelled", "views_with_findings", "vision_findings_scored",
              "true_positives", "false_positives", "precision",
              "expected_issues_total", "views_with_a_hit"):
        print(f"  {k:<24}: {res[k]}")
    print()
    for d in res["details"]:
        mark = "✓" if d["verdict"] == "TRUE POSITIVE" else "✗"
        print(f"  {mark} [{d['matched_by']:<12}] {d['viewport']:<14} {d['finding']}")

    if args.probe_run:
        print()
        print("=" * 66)
        print("PROBE TRAJECTORY")
        print("=" * 66)
        from .probe import explore_page

        _, calls = await explore_page(args.probe_run, cfg)
        traj = probe_trajectory(calls, cfg.probe.max_turns)
        print(f"  tool calls: {traj['tool_calls']}")
        for name, ok in traj["checks"].items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        print(f"  score: {traj['score']}")

    out = Path(args.runs_root) / args.run_id / "eval.json"
    out.write_text(json.dumps(res, indent=2))
    print(f"\nwritten: {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Score the LLM layers against hand-labelled ground truth.")
    ap.add_argument("run_id")
    ap.add_argument("--labels", default="eval/labels.json")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--runs-root", default="runs")
    ap.add_argument("--probe-run", default=None, metavar="URL",
                    help="also run the probe trajectory check against this URL")
    return asyncio.run(_main(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

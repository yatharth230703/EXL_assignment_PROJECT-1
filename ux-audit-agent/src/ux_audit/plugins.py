"""Phase 4: cross-cutting concerns as ADK plugins (spec §6.5).

Plugins register on the `App` and run ahead of agent-level callbacks. The reason
they matter here rather than plain callbacks: they're the only place with *error*
hooks, and a long crawl's defining failure mode is a unit dying quietly.

Naming note: the spec calls these `on_tool_error` / `on_model_error`. In ADK
2.5.0 they are `on_tool_error_callback` / `on_agent_error_callback` /
`on_run_error_callback` (verified by introspection).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin


class CaptureErrorPlugin(BasePlugin):
    """Turns node/tool failures into durable `run_error` records.

    Writes to `runs/<run_id>/errors.jsonl` rather than mutating findings directly:
    the rule pass owns findings.jsonl, and two writers on one file is how you get
    a corrupted source of truth. `errors_to_findings()` folds these in afterwards.
    """

    def __init__(self, run_id: str, runs_root: str = "runs", name: str = "capture_error_plugin"):
        super().__init__(name=name)
        self.run_id = run_id
        self.runs_root = runs_root
        self.errors: list[dict[str, Any]] = []

    def _record(self, kind: str, where: str, error: Exception) -> None:
        entry = {"kind": kind, "where": where, "error": f"{type(error).__name__}: {error}"}
        self.errors.append(entry)
        d = Path(self.runs_root) / self.run_id
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "errors.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
        self._record("tool_error", getattr(tool, "name", str(tool)), error)
        return None  # don't swallow; let ADK's normal handling proceed

    async def on_agent_error_callback(self, *, agent, callback_context, error):
        self._record("agent_error", getattr(agent, "name", str(agent)), error)

    async def on_run_error_callback(self, *, invocation_context, error):
        self._record("run_error", "invocation", error)


def errors_to_findings(run_id: str, site: str, root_url: str, runs_root: str = "runs") -> list:
    """Fold errors.jsonl into Finding objects so failures show up in the report."""
    from .models import Finding

    path = Path(runs_root) / run_id / "errors.jsonl"
    if not path.exists():
        return []

    out = []
    seen: set[str] = set()
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            e = json.loads(line)
            key = f"{e['kind']}:{e['where']}"
            if key in seen:  # one finding per failing location, not per occurrence
                continue
            seen.add(key)
            out.append(
                Finding(
                    run_id=run_id,
                    site=site,
                    page_url=root_url,
                    viewport="-",
                    category="run_error",
                    severity="medium",
                    source="rule",
                    confidence="high",
                    document=f"The audit run hit a {e['kind']} at {e['where']}: {e['error']}",
                    dedupe_key=key,
                    evidence=e,
                ).finalize()
            )
    return out

"""Phase 2: storage.

Two tiers, deliberately (spec §8.1):
  - `runs/<run_id>/findings.jsonl` is the SOURCE OF TRUTH. Append-only, plain text.
  - Chroma is an INDEX. If its schema changes, reindex from JSONL — never re-crawl
    to recover data. `reindex_from_jsonl()` exists so that promise is real and not
    just a comment.

Chroma metadata values must be scalars (str/int/float/bool), so `evidence` is
serialised to a JSON string on the way in and parsed on the way out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .models import Finding


# ---------------------------------------------------------------------------
# JSONL — source of truth
# ---------------------------------------------------------------------------


def run_dir(run_id: str, root: str | Path = "runs") -> Path:
    return Path(root) / run_id


def write_findings_jsonl(findings: Iterable[Finding], run_id: str, root: str | Path = "runs") -> Path:
    d = run_dir(run_id, root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "findings.jsonl"
    with open(path, "w") as f:
        for finding in findings:
            f.write(finding.model_dump_json() + "\n")
    return path


def read_findings_jsonl(run_id: str, root: str | Path = "runs") -> list[Finding]:
    path = run_dir(run_id, root) / "findings.jsonl"
    if not path.exists():
        return []
    with open(path) as f:
        return [Finding.model_validate_json(line) for line in f if line.strip()]


def write_manifest(
    run_id: str,
    root_url: str,
    cfg: Config,
    pages_attempted: int,
    pages_succeeded: int,
    pages_failed: int,
    extra: dict[str, Any] | None = None,
    root: str | Path = "runs",
) -> Path:
    d = run_dir(run_id, root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "manifest.json"
    manifest = {
        "run_id": run_id,
        "root_url": root_url,
        "pages_attempted": pages_attempted,
        "pages_succeeded": pages_succeeded,
        "pages_failed": pages_failed,
        "models": {"judgment": cfg.judgment.model, "probe": cfg.probe.model},
        "config_snapshot": cfg.model_dump(mode="json"),
        **(extra or {}),
    }
    path.write_text(json.dumps(manifest, indent=2))
    return path


def append_discovery_log(decisions, run_id: str, root: str | Path = "runs") -> Path:
    """Spec §6.1: one line for EVERY url seen, with the reason. Mandatory."""
    d = run_dir(run_id, root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "discovery_log.jsonl"
    with open(path, "a") as f:
        for dec in decisions:
            f.write(dec.model_dump_json() + "\n")
    return path


# ---------------------------------------------------------------------------
# Chroma — rebuildable index
# ---------------------------------------------------------------------------


def _collection(cfg: Config):
    import chromadb

    client = chromadb.PersistentClient(path=cfg.storage.chroma_dir)
    return client.get_or_create_collection(name=cfg.storage.collection)


def _to_metadata(f: Finding) -> dict[str, Any]:
    return {
        "run_id": f.run_id,
        "site": f.site,
        "page_url": f.page_url,
        "viewport": f.viewport,
        "viewports_affected": ",".join(f.viewports_affected),
        "category": f.category,
        "severity": f.severity,
        "severity_rank": f.severity_rank,
        "source": f.source,
        "confidence": f.confidence,
        "evidence": json.dumps(f.evidence),
        "screenshot_path": f.screenshot_path or "",
        "timestamp": f.timestamp.isoformat(),
    }


def ingest(findings: list[Finding], cfg: Config) -> int:
    """Upsert findings into Chroma. Deterministic ids mean re-ingest replaces
    rather than duplicating (spec §8.2)."""
    if not findings:
        return 0
    col = _collection(cfg)
    col.upsert(
        ids=[f.id for f in findings],
        documents=[f.document for f in findings],
        metadatas=[_to_metadata(f) for f in findings],
    )
    return len(findings)


def reindex_from_jsonl(run_id: str, cfg: Config, root: str | Path = "runs") -> int:
    """Rebuild the index from the source of truth. Never re-crawls."""
    return ingest(read_findings_jsonl(run_id, root), cfg)


def search_findings(
    query: str,
    cfg: Config,
    n_results: int = 8,
    site: str | None = None,
    run_id: str | None = None,
    category: str | None = None,
    min_severity_rank: int | None = None,
) -> list[dict[str, Any]]:
    """Semantic search with metadata filters. Backs the advisor's FunctionTool (Phase 8)."""
    col = _collection(cfg)

    clauses: list[dict[str, Any]] = []
    if site:
        clauses.append({"site": site})
    if run_id:
        clauses.append({"run_id": run_id})
    if category:
        clauses.append({"category": category})
    if min_severity_rank is not None:
        # This is why severity_rank exists — strings can't be range-filtered.
        clauses.append({"severity_rank": {"$gte": min_severity_rank}})

    where = None
    if len(clauses) == 1:
        where = clauses[0]
    elif len(clauses) > 1:
        where = {"$and": clauses}

    res = col.query(query_texts=[query], n_results=n_results, where=where)

    out: list[dict[str, Any]] = []
    for i, fid in enumerate(res["ids"][0]):
        meta = dict(res["metadatas"][0][i])
        if isinstance(meta.get("evidence"), str):
            try:
                meta["evidence"] = json.loads(meta["evidence"])
            except json.JSONDecodeError:
                pass
        out.append({"id": fid, "document": res["documents"][0][i], **meta})
    return out

"""Typed view over config.yaml (spec §12).

Every threshold and pattern lives in the YAML; this module only gives it a shape.
Sections are optional at the model level so a trimmed config still loads during
early phases, but the defaults here mirror the spec exactly.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Viewport(BaseModel):
    name: str
    width: int
    height: int
    dsf: int = 1  # deviceScaleFactor


class RunConfig(BaseModel):
    max_pages: int = 15
    max_depth: int = 2
    blog_sample: int = 2
    browser_concurrency: int = 3
    headless: bool = True
    slow_mo_ms: int = 0
    node_timeout_s: int = 45
    settle_timeout_s: int = 5
    early_screenshot_ms: int = 800
    animation_timeout_ms: int = 3000
    stability_interval_ms: int = 400
    stability_max_attempts: int = 4
    perf_sample_pages: int = 3
    session_db: str = "sqlite+aiosqlite:///runs.db"
    custom_run_ids: bool = False


class Band(BaseModel):
    """A medium/high threshold pair. Everything below `medium` is not a finding."""
    medium: float
    high: float


class Thresholds(BaseModel):
    ttfb_ms: Band = Band(medium=800, high=1800)
    lcp_ms: Band = Band(medium=2500, high=4000)
    cls: Band = Band(medium=0.10, high=0.25)
    long_task_ms: float = 200


class DiscoveryConfig(BaseModel):
    exclude_patterns: list[str] = Field(
        default_factory=lambda: ["/app/", "/dashboard/", "/account/", "/admin/", "/settings/", "/billing/"]
    )
    prioritise: list[str] = Field(
        default_factory=lambda: ["/", "/pricing", "/about", "/contact", "/signup", "/login", "/features", "/blog"]
    )


class JudgmentConfig(BaseModel):
    model: str = "gemini-flash-latest"
    llm_concurrency: int = 4
    max_findings_per_view: int = 5
    min_confidence: str = "medium"
    image_max_width: int = 1024


class ProbeConfig(BaseModel):
    model: str = "gemini-flash-latest"
    max_probe_pages: int = 5
    viewports: list[str] = Field(default_factory=lambda: ["mobile_390", "desktop_1440"])
    max_turns: int = 12
    mcp_timeout_s: int = 60


class NoiseConfig(BaseModel):
    console_denylist_hosts: list[str] = Field(default_factory=list)
    axe_min_impact: str = "serious"
    max_findings_per_page: int = 25


class StorageConfig(BaseModel):
    chroma_dir: str = ".chroma"
    collection: str = "ux_findings"


class Config(BaseModel):
    run: RunConfig = Field(default_factory=RunConfig)
    viewports: list[Viewport]
    thresholds: Thresholds = Field(default_factory=Thresholds)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    judgment: JudgmentConfig = Field(default_factory=JudgmentConfig)
    probe: ProbeConfig = Field(default_factory=ProbeConfig)
    noise: NoiseConfig = Field(default_factory=NoiseConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    def viewport_by_name(self, name: str) -> Viewport:
        for v in self.viewports:
            if v.name == name:
                return v
        raise ValueError(f"Unknown viewport '{name}'. Known: {[v.name for v in self.viewports]}")


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)

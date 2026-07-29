"""Phase 5: accessibility scanning with axe-core.

axe.min.js is vendored under `vendor/` and injected from disk — no CDN fetch at
runtime, so a crawl doesn't silently degrade when the network blips or the page's
CSP blocks a third-party script (spec §2: "bundled locally").

Violations are collapsed by rule_id here, keeping <=3 example selectors, per the
noise-control rule in spec §8.2. A page with 40 unlabelled inputs is one finding,
not forty.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .models import AxeViolation, CaptureBundle

_AXE_JS_PATH = Path(__file__).parent / "vendor" / "axe.min.js"

# Ordered worst-first; used for the axe_min_impact cutoff.
IMPACT_ORDER = ["critical", "serious", "moderate", "minor"]


def _impact_rank(impact: str) -> int:
    try:
        return IMPACT_ORDER.index(impact)
    except ValueError:
        return len(IMPACT_ORDER)


async def run_axe(page, bundle: CaptureBundle, config: Config) -> None:
    """Inject axe, run it, and fold the results into the bundle.

    Never raises: an axe failure must not lose an otherwise good capture, so it's
    recorded as a console-level note and the capture proceeds.
    """
    try:
        await page.add_script_tag(path=str(_AXE_JS_PATH))
        raw = await page.evaluate(
            """async () => {
                const res = await axe.run(document, {
                  resultTypes: ['violations'],
                  // Keep the payload small; we only need ids/impact/targets.
                  reporter: 'v1',
                });
                return res.violations.map(v => ({
                  id: v.id,
                  impact: v.impact,
                  help: v.help,
                  helpUrl: v.helpUrl,
                  nodes: v.nodes.map(n => (n.target || []).join(' ')),
                }));
            }"""
        )
    except Exception as e:  # noqa: BLE001 — a11y scan is best-effort
        # Silent failure is exactly what the spec's error-plugin design exists to
        # prevent, so record why rather than just returning an empty list.
        bundle.axe_error = f"{type(e).__name__}: {e}"
        bundle.axe_violations = []
        return

    cutoff = _impact_rank(config.noise.axe_min_impact)
    collapsed: list[AxeViolation] = []
    for v in raw or []:
        impact = v.get("impact") or "minor"
        if _impact_rank(impact) > cutoff:
            continue
        nodes = v.get("nodes") or []
        collapsed.append(
            AxeViolation(
                rule_id=v["id"],
                impact=impact,
                help=v.get("help", ""),
                help_url=v.get("helpUrl"),
                selectors=nodes[:3],  # <=3 examples, per spec §8.2
                node_count=len(nodes),
            )
        )

    collapsed.sort(key=lambda a: (_impact_rank(a.impact), a.rule_id))
    bundle.axe_violations = collapsed

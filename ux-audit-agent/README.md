# UX Audit Agent

Crawls a site's marketing surface across device breakpoints, and reports UX/UI problems —
both the kind invisible to the eye (load timing, layout shift, silent JS errors, a11y
violations) and the kind that needs looking at it (misalignment, overlap, "this looks broken").

Findings land in a queryable store; a chat advisor sits on top and answers questions about them,
grounded in specific findings rather than generic advice.

Built to [`docs/SPEC_v3.md`](docs/SPEC_v3.md). Live build state, verified ADK API notes, and
every deviation from the spec: [`docs/CHECKLIST.md`](docs/CHECKLIST.md).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
echo "GEMINI_API_KEY=..." > .env      # needed for the vision, probe and advisor phases
```

Node is required for the probe pass (`npx @playwright/mcp`).

## Run it

**Always run from the project root** — `config.yaml` and `runs/` resolve relative to CWD.

```bash
# Full crawl: discover -> capture (page x viewport) -> axe -> rules -> findings.jsonl + Chroma
PYTHONPATH=src python -m ux_audit.run https://example.com

# Resume an interrupted crawl (skips page x viewport units already captured)
PYTHONPATH=src python -m ux_audit.run https://example.com --resume <run_id>

# Narrow it down
PYTHONPATH=src python -m ux_audit.run https://example.com --viewports mobile_390,desktop_1440
```

Then layer on the passes that need a model. Each is separately re-runnable:

```bash
PYTHONPATH=src python -m ux_audit.judgepass <run_id>     # vision pass over SAVED screenshots
PYTHONPATH=src python -m ux_audit.probepass <run_id>     # interaction probe (--no-llm for the measured half only)
PYTHONPATH=src python -m ux_audit.rulepass  <run_id>     # re-apply thresholds without re-crawling
PYTHONPATH=src python -m ux_audit.evaluate  <run_id>     # score the vision layer against eval/labels.json
```

Then ask about the results:

```bash
adk web .        # pick "advisor"
```

## Why it's split this way

Capture is scripted Playwright with no model in the browser loop; judgment is a separate batched
pass over screenshots on disk. So changing the vision prompt and re-scoring is seconds, not
another crawl. Same reason `rulepass` is separate: retune a threshold, re-derive findings, no
browser.

The division of labour is strict: **anything measurable is measured, never guessed at.** TTFB,
LCP, CLS, console errors, broken images, overflow, axe violations, whether a form actually fired
a request — all deterministic. The model is only asked things that genuinely need eyes or
judgment, and it gets the measurements handed to it as context. A vision finding can't be rated
`high` unless it ties to a deterministic signal.

## What to expect when you run it

**Timing** (measured on linear.app, 15 pages × 3 viewports = 45 capture units):

| step | wall clock | notes |
|---|---|---|
| `run` (crawl) | ~6–10 min | includes per-page animation settle + lazy-image scroll |
| `judgepass` | ~3–5 min | ~105 LLM calls (3 specialists) |
| `probepass` | ~4–6 min | spawns an MCP browser per page |

Roughly linear in `pages × viewports`. For a quick smoke test use
`--viewports mobile_390` (about a third of the time).

**Progress:** each command prints its summary only at the end, but capture
bundles land on disk as they finish — `ls runs/<run_id>/captures | wc -l` gives
live progress against `pages × viewports`.

**Verbosity:** loud by default. ADK's `LoggingPlugin` prints every event, so the
useful output scrolls past. Either filter it:

```bash
PYTHONPATH=src python -m ux_audit.run https://yourapp.com 2>&1 | grep -A11 "=== audit complete"
```

or drop `LoggingPlugin()` from the `plugins=[...]` list in `src/ux_audit/run.py`
if you don't want the run log at all. The other CLIs are quiet by comparison.

## Watching it run (headful) — and why it's not just cosmetic

In `config.yaml`:

```yaml
run:
  headless: false
  slow_mo_ms: 300     # delay per Playwright operation, so you can follow along
```

A real Chromium window opens and you watch every page load, settle, scroll and
screenshot. The probe pass's MCP browser follows the same setting.

**It also produces more accurate captures on JS-heavy sites.** Some sites animate
headline text in with JS (per-word spans starting at `opacity: 0`) and those
animations never fire in headless Chromium — the DOM reports the copy while the
pixels stay blank. Measured on linear.app's homepage:

| | headless | headful |
|---|---|---|
| `render_suspect` | `True` (content didn't paint) | `False` |
| hero band pixel variance | `0.00` (blank) | `36.21` |

So if a site's captures come back with `render_suspect: true`, or the vision pass
reports large blank areas, re-run headful before believing either.

Trade-offs: slower, steals window focus, and with `browser_concurrency: 3` you
get three windows at once. Keep `headless: true` for scheduled or bulk runs.

## Where the data lives

| path | what | notes |
|---|---|---|
| `runs/<run_id>/findings.jsonl` | **source of truth** | plain text, one finding per line |
| `runs/<run_id>/captures/*.json` | raw `CaptureBundle` per page × viewport | |
| `runs/<run_id>/screenshots/*.png` | early / settled viewport / settled full-page | |
| `runs/<run_id>/discovery_log.jsonl` | every URL seen + why it was kept or dropped | first place to look when discovery surprises you |
| `runs/<run_id>/manifest.json` | root URL, config snapshot, models, pass/fail counts | |
| `.chroma/` | vector index, collection `ux_findings` | rebuildable — `store.reindex_from_jsonl()` |
| `runs.db` | SQLite: ADK sessions + node checkpoints | set by `run.session_db` |

Chroma **accumulates across runs**; finding ids embed the `run_id`, so runs
coexist rather than overwrite. Scope queries by `site` or `run_id`.

## Everything in one chat (recommended)

```bash
adk web .          # from the project root, then pick "advisor" in the dropdown
```

You do **not** need the CLI. The advisor can run the audit itself:

```
you › Audit https://yourapp.com — mobile_390 only, keep it quick.
    › Started. Run ID 20260729T202154Z-yourapp_com, running in the background.

you › Is it finished?
    › Yes — 15 units captured, 72 findings. The high-severity ones are…

you › What should we fix first?
    › …grounded in finding ids you can grep in findings.jsonl
```

Tools it has: `start_audit(url, viewports, include_vision)` launches the crawl as
a background task and returns a run_id immediately (a crawl takes minutes — a
blocking tool call would look hung); `audit_status(run_id)` polls it;
`search_findings(...)` queries Chroma with the full metadata filters.

Only one audit runs at a time — the crawl holds a Chromium instance and a
process-global run context, so a second concurrent start is refused rather than
allowed to corrupt the first.

Questions that work well, because they map onto stored metadata:

- "What are the most serious problems on yourapp.com?"
- "What did you find on the pricing page at mobile width?"
- "Show me accessibility issues rated medium or higher."

Every claim comes back with a finding id like `[a1b2c3d4e5f6]`. If the store has
nothing, it says so rather than improvising.

**Screenshots in chat are off by default, deliberately.** `LoadArtifactsTool`
calls `list_artifacts()` on every turn and raises `ValueError: Artifact service is
not initialized.` when the runner has no artifact service. `adk web` provides
none and has no `--artifact_service_uri` flag, so leaving the tool enabled breaks
*every* turn, not just screenshot viewing. Findings still carry
`screenshot_path`; open it directly. To enable in-chat images, construct the
`Runner` yourself with `LocalDirArtifactService` and set `UX_AUDIT_ARTIFACTS=1`.

## Output

```
runs/<run_id>/
├── captures/*.json        # raw CaptureBundle per page x viewport
├── screenshots/*.png      # early (FOUC) + settled viewport + settled full-page
├── findings.jsonl         # SOURCE OF TRUTH — Chroma is a rebuildable index
├── discovery_log.jsonl    # every URL seen, with the reason it was kept or dropped
├── manifest.json          # root URL, config snapshot, models, pages attempted/succeeded/failed
├── errors.jsonl           # node/tool failures (only if any occurred)
└── eval.json              # if the eval harness has been run
```

`findings.jsonl` is authoritative. If the Chroma schema changes, reindex from it
(`store.reindex_from_jsonl`) — never re-crawl to recover data.

## Configuration

Everything tunable is in `config.yaml`: thresholds, viewports, discovery patterns, concurrency,
models, noise denylists. No thresholds in code.

## Known limitations

- **Resume** is delivered by disk-level idempotence, not ADK's checkpointing — ADK 2.5.0's
  resumability is experimental and doesn't recover a SIGKILL'd invocation. Deviation #4 in the
  checklist has the measurements.
- **CLS** is a naive sum of non-input layout shifts, not the windowed session-max the real
  web-vitals library computes. Fine as a signal, not comparable to a Core Web Vitals score.
- **Fold position isn't measured**, so broken images are rated `medium` rather than splitting
  above-fold (`high`) from below-fold.
- **The eval set is a seed** — 4 labelled views. Precision over n=1 scored finding is a smoke
  test, not a measurement. Extend `eval/labels.json` before trusting the number.
- **`CaptureErrorPlugin` is unproven** — the error→finding guarantee is currently carried by the
  bundle-level path, which is tested; the plugin has never been observed firing.
- Chromium only, no auth, no multi-step journeys (all per spec §3).

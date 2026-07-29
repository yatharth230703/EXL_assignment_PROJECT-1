# UX Audit Agent — Build Checklist

**Read this first when resuming.** Full spec: [`SPEC_v3.md`](./SPEC_v3.md) (verbatim, do not edit).
This file is the live state: what's done, what's verified, what deviated and why.

Last updated: 2026-07-29

---

## 0. Environment facts (verified, not assumed)

| Fact | Value | How verified |
|---|---|---|
| Python | 3.13.7 (`.venv`) | `python -V` |
| `google-adk` | **2.5.0** | `pip show` |
| ADK extras needed | `google-adk[db,mcp]` — `db` for `DatabaseSessionService` (needs `sqlalchemy`), `mcp` for `McpToolset` | import failed without them |
| chromadb | 1.5.9 | `pip show` |
| playwright | 1.61.0, chromium installed | capture run |
| Gemini key | `GEMINI_API_KEY` in `.env` (gitignored by parent repo) | live call to `gemini-flash-latest` returned OK |
| dotenv | `python-dotenv` installed; **must pass an explicit path** — bare `load_dotenv()` raises `AssertionError` under `python - <<EOF` | observed |

### venv gotcha (bit us once)
The `.venv` was originally created under a different path (`exl-assignment/`). `source
.venv/bin/activate` and `.venv/bin/pip` both broke with `bad interpreter`. Fixed by re-running
`python -m venv .venv` in place (packages preserved). If it breaks again after a move, do that,
and prefer `python -m pip` over the `pip` shim.

### Running anything
```bash
source .venv/bin/activate
PYTHONPATH=src python -m ux_audit.<module>     # ALWAYS from the project root
```
Run from the **project root**, not `src/` — `config.yaml` and `runs/` resolve relative to CWD.
(The Phase-1 README says `cd src`; that is wrong. See Deviations.)

---

## 1. Verified ADK 2.5.0 API surface

Checked by introspection, not memory. Re-check after any ADK upgrade (spec §16 warns the API moves).

- `from google.adk.workflow import Workflow, node, FunctionNode, RetryConfig, NodeTimeoutError, START`
  — note the package is **`google.adk.workflow`** (singular). `Workflow` is *not* in `google.adk.agents`.
- `@node(...)` accepts exactly: `name`, `rerun_on_resume`, `retry_config`, `timeout`,
  `parallel_worker`, `max_parallel_workers`, `auth_config`, `parameter_binding`.
  - `parameter_binding='state'` (default) binds function params from `ctx.state`;
    `'node_input'` binds from the `node_input` dict and infers schemas from the signature.
    **We pass explicit payload objects, so orchestrators take `(ctx, payload)` and child nodes
    are called with `ctx.run_node(child, payload)`.**
- `Workflow(name=..., max_concurrency=..., timeout=..., retry_config=..., rerun_on_resume=True, ...)`
- `ctx.run_node(node, node_input=None, *, use_as_output=False, run_id=None, use_sub_branch=False, ...)`
  - **Docstring warning:** always `await` it directly; do *not* wrap in `asyncio.create_task()`
    (unsupervised, errors swallowed, not cancelled on parent interrupt). `asyncio.gather` awaits
    its children, so the spec's gather fan-out is fine — but this was smoke-tested in Phase 3
    rather than assumed.
- `Context` (from `google.adk.agents`) exposes: `run_node`, `state`, `session`, `run_id`,
  `save_artifact`/`load_artifact`/`list_artifacts`, `node_path`, `attempt_count`, `error`.
- Built-in plugins live at `google/adk/plugins/`: `logging_plugin`, `reflect_retry_tool_plugin`,
  `context_filter_plugin`, plus others. Only `BasePlugin`/`PluginManager` are re-exported from
  `google.adk.plugins` — **import the concrete plugins from their submodules.**
- `App(name=..., root_agent=..., plugins=[...], resumability_config=...)` from `google.adk.apps`.
- `BaseArtifactService` abstract methods: `save_artifact`, `load_artifact`, `list_artifact_keys`,
  `list_artifact_versions`, `list_versions`, `get_artifact_version`, `delete_artifact`.
- `McpToolset` + `StdioConnectionParams` from `google.adk.tools.mcp_tool`.

---

## 2. Phase checklist

Legend: `[ ]` not started · `[~]` in progress · `[x]` done + tested

### `[x]` Phase 1 — Capture harness (pre-existing, fixed)
- [x] Playwright listeners attached pre-navigation; injected `PerformanceObserver` (CLS, LCP)
- [x] Early (~800ms) + settled viewport + settled full-page screenshots
- [x] DOM checks: broken images, horizontal overflow, placeholder text
- [x] `CaptureBundle` JSON to `runs/<run_id>/captures/<slug>.json`
- [x] **Bug fixed:** `capture.py:146` used `req.failure.get("errorText")` (Node API shape).
      In Playwright Python `request.failure` is a `str`. Crashed on *any* failed request.
- **Verified:** run `20260729T110727Z` against `the-internet.herokuapp.com/dynamic_loading/1`
  → `status: ok`, TTFB 1527ms, LCP 5192ms, CLS 0.0, Optimizely beacon captured as a network failure.
- **Known gap:** that test page is shorter than the viewport, so early == settled == fullpage
  (identical MD5). Does NOT exercise FOUC detection or full-page capture. Re-validate on a tall,
  slow, modern page (spec §16 says the same).

### `[x]` Phase 2 — Rule pass + storage
- [x] Full `config.yaml` per §12 (`thresholds`, `discovery`, `judgment`, `probe`, `noise`, `storage`)
- [x] `Finding` model (§8.2 fields incl. `severity_rank`, `source`, `dedupe_key`) — `models.py`
- [x] Rule functions in `rules.py`: TTFB/LCP/CLS bands, console/network, mixed content, DOM issues,
      `run_error` from `capture_error`
- [x] `cap_vision_severity()` enforces the §9 hard rule in ONE place (vision caps at medium
      unless `evidence["tied_to"]` names a deterministic signal)
- [x] Collapse across viewports (`responsive_design` deliberately NOT collapsed — "overflows on
      mobile" ≠ "overflows on desktop"), deterministic sha1 id, `max_findings_per_page` cap
- [x] `findings.jsonl` + `manifest.json` + `discovery_log.jsonl` writers — `store.py`
- [x] Chroma upsert by deterministic id; `reindex_from_jsonl()` makes the "never re-crawl" promise real
- [x] CLI: `PYTHONPATH=src python -m ux_audit.rulepass <run_id>`
- **Verified:** on run `20260729T110727Z` → LCP 5192ms = `high` (>4000), TTFB 1527ms = `medium`
  (800–1800 band), CLS 0.0 = correctly no finding. Reindex returned 4 (upsert, not 8).
  Semantic search + `severity_rank $gte 3` filter both work.
- **Note:** one failed third-party request produces TWO findings (`console_errors` +
  `network_failures`) because the browser logs a console error for it too. Real double-count;
  the `console_denylist_hosts` config is the intended lever. Left as-is.

### `[x]` Phase 3 — Dynamic workflow
- [x] `discovery.py`: robots.txt → sitemap.xml → BFS depth 2, normalize, exclude, prioritise, cap
- [x] `discovery_log.jsonl` for **every** URL seen — and it immediately earned its keep (see below)
- [x] `@node` wrappers; `audit_workflow` is `@node(rerun_on_resume=True)`
- [x] `asyncio.Semaphore` bounds live contexts; ONE Chromium via `browser_session()`
      (`capture_page` refactored to accept a shared browser; Phase-1 CLI path still launches its own)
- [x] `DatabaseSessionService("sqlite+aiosqlite:///runs.db")`
- [x] `gather` + `run_node` smoke-tested before use
- [x] **Resume test passes:** kill mid-crawl → resume → `units_reused: 3`, remaining 6 captured,
      9/9 complete. See Deviation #4 for *which layer* delivers that guarantee.
- **Discovery bug caught by the reason log, exactly as spec §6.1 predicted:** the target's
  `sitemap.xml` lists `http://` URLs while we crawl `https://`, so strict same-origin comparison
  excluded the entire sitemap ("different origin"). Fixed with `coerce_scheme()`. Also added:
  a sitemap yielding fewer than `max_pages` entries now falls through to BFS and merges, since
  a 2-entry sitemap for a 40-page site isn't "usable".

### `[x]` Phase 4 — Plugins
- [x] `LoggingPlugin` (built-in) + custom `CaptureErrorPlugin` on `App`
- [x] Errors → `runs/<run_id>/errors.jsonl`, folded into findings by `errors_to_findings()`
      (separate file on purpose: the rule pass owns findings.jsonl; two writers = corrupted truth)
- **Naming deviation:** spec says `on_tool_error`; ADK 2.5.0 has `on_tool_error_callback`,
  `on_agent_error_callback`, `on_run_error_callback`. All three are hooked.
- [x] **Forced-failure tested:** capture against an unresolvable host → `high` `run_error`
      finding ("Capture failed for … at mobile_390"), not a silent drop.
- **Honest caveat:** that test exercises the *bundle-level* path (`capture_page` catches the
  error → rule pass converts it). The **plugin** path has still never been observed firing,
  because `capture_node` deliberately doesn't raise. `CaptureErrorPlugin` is a backstop for
  genuinely unexpected node failures; treat it as unproven until something actually throws.

### `[x]` Phase 5 — axe-core
- [x] `axe.min.js` v4.10.2 vendored at `src/ux_audit/vendor/` — injected from disk, no CDN
- [x] Collapsed by `rule_id`, ≤3 example selectors, `node_count` retained
- [x] `axe_min_impact` cutoff honoured; impact→severity per §9 (`critical`→high, `serious`→medium,
      `moderate`/`minor`→low)
- [x] `axe_error` recorded on the bundle when the scan itself fails, rather than silently empty
- **Verified:** `the-internet.herokuapp.com/login` → 1 `serious` color-contrast violation;
  form detection `form#login`, primary CTA "Login" (both feed the Phase-7 probe gate).

### `[x]` Phase 6 — Judgment pass (vision)
- [x] `VisionFinding` / `VisionFindings` schemas; `LlmAgent(output_schema=VisionFindings)`
- [x] Screenshots downscaled ≤`image_max_width`, JPEG q70 (Pillow), viewport + fullpage both sent
- [x] Prompt contract: visible-only, explicit "do not report timing/console/network",
      no repeats of `known_findings`, max 5, **"an empty list is valid and common"**
- [x] `min_confidence` filter drops low-confidence guesses before the store
- [x] Objective hints (CLS, top shifting selectors, overflow, LCP) ride along with every image
- [x] CLI `python -m ux_audit.judgepass <run_id>` — re-runs over saved screenshots, no browser
- **Verified:** found the "Fork me on GitHub" ribbon overlapping heading text — a real defect the
  rule pass structurally cannot see. All emitted at `medium`, confirming the §9 vision cap fires.
- **Note:** `responsive_design` isn't collapsed across viewports (by design), so one root cause
  (the ribbon) appears once per viewport. Correct per spec, slightly noisy in practice.

### `[x]` Phase 7 — Probe pass (MCP)
- [x] **MCP session persistence smoke-tested FIRST** (§6.3 gotcha 2) — `browser_navigate` →
      `browser_snapshot` in one invocation held state and read the page correctly
- [x] `StdioConnectionParams` with explicit `timeout` (§6.3 gotcha 1) — never bare `StdioServerParameters`
- [x] `tool_filter` to the 5 listed browser tools
- [x] Split along the §4.1 rule: `deterministic_probe()` (our Playwright) MEASURES;
      `explore_page()` (LlmAgent+MCP) DECIDES and JUDGES
- [x] Measures: requests fired, DOM change <1.5s, `aria-describedby`/`label for` association,
      duplicate submission
- [x] Whole per-page probe inside ONE invocation; `ContextFilterPlugin` then `ReflectAndRetryToolPlugin`
- **Verified:** found a real unassociated validation message ("Your username is invalid!") and a
  real double-submit. MCP calls: navigate → snapshot → click → click → snapshot.
- **False positive caught and fixed:** duplicate detection originally keyed on all document/xhr
  traffic and flagged *every* action as a duplicate (normal page loads refetch plenty). Now keys
  only on POST / document-GET submissions — `dupes=False` for single submits, `True` only for
  the double-click.

### `[x]` Phase 8 — Advisor agent
- [x] `FunctionTool(search_findings)` over Chroma, all metadata filters incl. `severity_rank $gte`
- [x] Instruction grounds every claim in a finding `id`, attributes `source`, refuses to improvise
- [x] `LocalDirArtifactService` + `LoadArtifactsTool`; path-traversal guard keeps it inside `runs/`
- [x] `advisor/` package discoverable by `adk web .` from the project root
- **Verified both directions:** a real question produced a severity-ordered answer with every
  claim carrying an id and its source; **"what did the audit find about the checkout payment
  flow?" → "The findings store has nothing on that."** then listed what it does have. That
  refusal is the whole point of the phase.

### `[x]` Phase 9 — Eval harness
- [x] `eval/labels.json` — per (page, viewport) ground truth, including **clean control views**
      (a model that invents problems fails on those, and nothing else catches it)
- [x] Vision precision with an LLM judge for finding↔label matching, keyword fallback with no key
- [x] Probe trajectory check: navigated / looked-before-acting / took-action / looked-after /
      within turn budget
- [x] CLI `python -m ux_audit.evaluate <run_id> [--probe-run URL]`, writes `runs/<run_id>/eval.json`
- **Result:** precision 1.0, probe trajectory 5/5. **But n=1 scored finding — that is a smoke
  test, not a precision measurement.** Only 1 of 4 labelled views had vision findings in that run.
- **ADK deviation:** ADK's `Evaluator`/`AgentEvaluator` operate over `Invocation` request/response
  pairs. Vision precision is scored over *findings* from findings.jsonl, so it's measured
  natively rather than contorted through that interface. Trajectory is closer to ADK's shape and
  could be ported if this needs to run inside `adk eval`.

---

## 3. Deviations from spec (running log)

| # | Deviation | Reason |
|---|---|---|
| 1 | Run from project root with `PYTHONPATH=src`, not `cd src` as Phase-1 README says | `config.py` opens `config.yaml` by relative path; `config.yaml` and `runs/` live at the root. README is wrong; not corrected yet (would be a code/docs change beyond the run request at the time). |
| 2 | Node payload parameter must be named `node_input` | ADK binds function params from `ctx.state` by default. A param literally named `node_input` is passed the payload directly *and coerced to its type hint* — that's how the spec's "typed data passing" is actually spelled in 2.5.0. |
| 3 | Every `ctx.run_node()` result is re-validated with `Model.model_validate(...)` | It returns a **dict**, not the child's Pydantic instance. Spec §4.3's "returns the child's output directly" is true in spirit, not in type. |
| 4 | **Resume is delivered by disk-level idempotence, not ADK checkpointing** | ADK *does* checkpoint completed nodes (verified: `capture_node@1..3` events persisted). But re-entering a SIGKILL'd invocation via `run_async(invocation_id=...)` yields **zero events** — `ResumabilityConfig` is experimental and targets paused/HITL and gracefully-failed invocations, not a killed process. `--resume` therefore starts a fresh invocation with the same `run_id`, and `capture_node` returns the existing bundle when one is already on disk. Same guarantee (no repeated work), owned by a layer that can actually make it, and it survives reboots too. `is_resumable=True` is left on for the cases ADK does handle. **Not investigated:** whether a *graceful* failure resumes differently. |
| 5 | `Workflow` imports from `google.adk.workflow` (singular), not `google.adk.agents` | Where it actually lives in 2.5.0. |

## 3b. First real run — linear.app, 2026-07-29 (`runs/20260729T154108Z-linear_app`)

Spec §16's "run it against a modern templated site" — 15 pages × 3 viewports, 45/45 units,
0 failures, 120 findings. It exercised the detectors as predicted and exposed three defects.

**Confirmed real defect on the target (triple-detected, independently):** `linear.app/android`
returns **404** — flagged by the rule pass (`http_errors`), by the vision pass ("Not found"),
and by the probe agent. Verified out-of-band with `curl` → 404. Three mechanisms agreeing is
the strongest signal the design can produce.

**BUG FIXED — MCP profile lock.** Probing page 1 worked; pages 2–5 all died with
`Browser is already in use for .../mcp-chrome-<id>`. Each page spawns its own MCP server and
they contend for the same on-disk Chrome profile. Fixed with `--isolated` (in-memory profile)
plus `await toolset.close()` in a `finally`. All 5 pages now probe cleanly. This is a third
MCP gotcha the spec doesn't list — worth adding to §6.3 if the spec is ever revised.

**OPEN — screenshots are captured mid-animation (harness defect, NOT fixed).**
The settled screenshot waits for `networkidle`, which says nothing about CSS/JS entrance
animations. On linear.app the hero heading was captured **visibly blurred mid-blur-in**
(`linear_app_features_mobile_390_settled_viewport.png` — look at it). The vision pass then
correctly reported what was in the image: "hero subtitle rendered with a blur effect that makes
it unreadable". Accurate about the pixels, wrong about the site. This is the single biggest
quality problem for the tool's target site class, since scroll/load entrance animations are
near-universal on modern templated sites.
*Likely fix:* after `networkidle`, also wait for `document.getAnimations()` to settle (with a
cap), or require two consecutive identical screenshots before declaring "settled". Note this
invalidates existing runs' screenshots, so re-run after changing it.

**OPEN — vision misreads dark-by-design imagery as missing content.**
On `/blog` and `/about` it reported "large blank black boxes where preview images should be".
The pages render correctly; those are Linear's intentionally near-black line-art illustrations.
The deterministic `naturalWidth === 0` check correctly did NOT flag them — the two mechanisms
disagreed and the deterministic one was right. *Likely fix:* cross-check any vision finding
claiming a missing/broken image against `dom_issues.broken_image_srcs` and drop it when the
deterministic check says the image loaded. This is the §4.1 rule ("never ask the model to notice
something measurable") leaking — image-loaded is measurable, so vision shouldn't get a vote.

### Both fixed, 2026-07-29 — re-run `runs/20260729T173504Z-linear_app`

- **Animation settle** (`_settle_visuals`): bounded wait on `document.getAnimations()`, then
  screenshot-equality polling. Both capped — looping animations never settle. Result recorded as
  `visually_stable` rather than assumed. Verified on the exact page that failed: the hero heading
  is now sharp, and the subtitle that was entirely absent before now renders.
- **Measurable-claim guard** (`judge.contradicts_measurement`): drops vision findings that a
  deterministic signal disproves. Suppressions are printed, never silent.
- **Third artefact found while re-running — `render_suspect`.** linear.app's hero headline is a
  per-word text-animation component whose spans sit at `opacity: 0` until a JS animation that
  **never fires in headless Chromium**. The DOM reports the copy; the pixels are blank. This is
  pre-existing (the pre-fix screenshots were blank too), not caused by the settle change, and the
  settle wait cannot fix it — there is no running animation to wait for. Detected by comparing
  painted pixel variance against DOM text presence; 6 of 45 views tripped it.
  *Deeper fix if this matters: run headed (`headless=False`), which typically lets these fire.*

**Over-correction, caught and fixed.** The first pass at the prompt stacked three "DO NOT report"
clauses onto a prompt that already said "an empty list is valid" — vision findings went to
**zero across all 45 views**. Precision at n=0 is undefined, not perfect. The prohibitions were
rewritten as neutral context ("here is what was measured, so you don't raise a false alarm")
with an explicit "this is not a reason to stay silent". Findings came back to 6, with none of the
three artefact classes. **Lesson: every prohibition added to a judgment prompt costs recall;
prefer a deterministic filter over a prompt rule.**

**Still open — probable third false-positive class (NOT fixed).** 4 of the 6 surviving findings
report "unusually large empty vertical gaps" in blog article bodies. Checked: no iframes, no
videos, and zero empty >250px blocks lacking media — so these are most likely the model
over-reading Linear's genuinely spacious typography in a 35,000px-tall full-page screenshot.
Needs a labelled judgement before trusting or suppressing.

### Fourth artefact class + architecture change, 2026-07-29/30 — `runs/20260729T183931Z-linear_app`

**Lazy images never loaded (FIXED).** The "large empty vertical gaps in blog articles" findings
were investigated and are **not real**. Full-page capture expands the viewport but never
*scrolls*, so `loading="lazy"` images never enter the viewport, never fetch, and never paint.
Proof: two uniform bands of 795px and 736px whose heights exactly matched two `<img>` elements
the DOM reported as `opacity: 1; visibility: visible`. The deterministic guard could not catch
this — the broken-image check is `img.complete && naturalWidth === 0`, and a lazy image that
never *started* loading has `complete === false`, so it is correctly not "broken" and therefore
invisible to `contradicts_measurement`.
*Fix:* `_load_lazy_images()` scrolls the page in viewport steps, awaits `img.decode()` with a
cap, returns to top. Metrics are now collected BEFORE that scroll, so scroll-triggered animations
can't inflate CLS. Verified: 795px/736px bands gone, largest now 198px, blank rows 67.6% → 48.7%.

**Judgment split into three specialists (spec §6.4 restructured).** One generalist prompt
accumulating "DO NOT report X" caveats degrades *globally* — each clause taxes recall on every
judgment, not just its target. Demonstrated: three clauses took findings 13 → 0. Replaced with:

| specialist | artefact it owns | remit |
|---|---|---|
| `layout_judge` | settled viewport shot | alignment, spacing, overlap, contrast |
| `content_judge` | settled full-page shot | truncation, placeholders, missing sections |
| `responsive_judge` | **all viewports of one page together** | breakpoint-specific breakage |

Deterministic fan-out, not an LLM coordinator (spec §14 compliant). Per-specialist counts are
printed so one collapsing to zero can't hide behind the others — that failure mode is precisely
what went unnoticed before. Cost: ~45 → ~105 LLM calls per run.

**`responsive_judge` is a new capability, not a reorganisation, and it's validated.** Judging
each (page × viewport) in isolation is why one root cause produced three unrelated findings.
Tested against the known cross-viewport defect on `the-internet.herokuapp.com/login`: the
"Fork me on GitHub" ribbon overlap now comes back as **one** finding with
`viewports_affected=['mobile_390']`, correctly NOT flagged at desktop_1440 (which the hand
labels mark clean). Previously: 3 near-duplicates with no shared-cause signal.

**linear.app result after all fixes:** 2 vision findings (1 content, 1 layout), responsive 0.
The zero is genuine — verified by the fact that the same agent fires correctly on a real defect
above. Linear's responsive implementation is simply clean. Note the overall vision yield is now
low (2 findings from 105 calls); whether that is appropriate restraint or under-reporting cannot
be settled without extending `eval/labels.json`.

**Precision estimate from the pre-fix run:** of 13 vision findings, ~7 trace to the two classes above.
That is roughly 45% precision on a modern site — far below what the eval harness's n=1 sample
suggested, and exactly why `eval/labels.json` needs extending before the number means anything.

## 3c. NOT IMPLEMENTED — perf sampling (spec §6.2)

`perf_sample_pages` exists in `config.yaml` and in `RunConfig`, but **nothing in `src/`
reads it**. Spec §6.2 requires: *"capture `perf_sample_pages` (default 3) twice and take the
median. Only emit a perf finding if the median crosses threshold. This kills the most common
false positive."* That was never built. Today a single measurement becomes a finding.

This compounds with `browser_concurrency`: LCP, long tasks and CLS are CPU-sensitive, and N
pages rendering simultaneously on one machine contend for CPU. So raising concurrency inflates
exactly the metrics the tool reports, with no median-of-samples damping to absorb it. Every
perf number reported so far (including linear.app's LCP 8.2s) was taken at `browser_concurrency:
3` and is therefore an upper bound, not a clean measurement. TTFB is mostly server-side and less
affected.

Until this lands: use `browser_concurrency: 1` for any run whose timing numbers matter, and
treat higher concurrency as valid only for a11y/content/responsive sweeps.

## 4. Open questions / risks carried forward

- Spec §16: `browser_concurrency: 3` is a guess — tune from traces after the first full run.
- Spec §16: FOUC via screenshot A/B diff is the weakest check; may need perceptual diff or hand to vision.
- Phase-1 validation used a trivially small page (see Phase 1 "Known gap"). Pick a modern
  templated site for the first real end-to-end run so the responsive / template-smell detectors
  actually get exercised.
- ADK 2.x moves fast — version is pinned in `requirements.txt`; re-read workflow docs before upgrading.

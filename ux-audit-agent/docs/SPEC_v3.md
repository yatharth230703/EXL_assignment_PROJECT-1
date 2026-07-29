# Project Spec v3: Automated UX/UI Testing Agent (ADK 2.x + Playwright)

*Verbatim copy of the user-provided v3 spec, saved 2026-07-29 as the durable source of
truth for this build. Do not edit to match the implementation — if the implementation
diverges, record that in `CHECKLIST.md` under "Deviations from spec".*

---

## 1. Purpose

An agentic system that crawls a website's **marketing/landing surface** (not the logged-in
product), simulates a human visitor across device breakpoints, and surfaces UX/UI issues that
are easy for a human reviewer to miss — both problems invisible to the naked eye (load timing,
layout shift, silent JS errors) and problems requiring visual judgment (misalignment, spacing
inconsistency, "out of place" artifacts common in AI-templated sites).

Findings land in a structured, queryable store. A separate chat agent sits on top of that store
so the user can ask follow-up questions and get concrete, grounded improvement suggestions.

**Local, non-production v1.** Runs on the user's machine and browser.

## 2. Core stack

- **Google ADK (Python, 2.x)** — `Workflow` + `@node` dynamic workflows for the crawl;
  `LlmAgent` for judgment, probe, and advisor; `App` + plugins for cross-cutting concerns
- **Playwright (Python library)** — deterministic capture, CDP access, screenshots
- **Playwright MCP** (`@playwright/mcp`) via `McpToolset` — live browser control for the
  interaction probe agent only (§6.3)
- **axe-core** — accessibility scanning, bundled locally
- **Chroma** — local vector store for findings, exposed to the advisor via a `FunctionTool`
- **SQLite** — ADK `DatabaseSessionService`, for workflow checkpointing and resume
- **JSONL run artifacts** — durable source of truth for findings (§8)
- Target site: **any URL provided at runtime.** Nothing site-specific is hardcoded.

Requires Python 3.10+. Install with `pip install google-adk` (plus the `mcp` extra).

## 3. Scope boundaries

**In scope:** landing/marketing pages; signup/login forms (interaction + validation testing, no
account creation); responsive/visual quality across breakpoints; performance, visual stability,
content completeness, interaction correctness, console/network health, accessibility.

**Out of scope for v1:** anything behind auth; multi-step journeys; production hardening,
security, deployment; cross-browser (Chromium only); A2A / multi-machine distribution.

## 4. Key design decisions

### 4.1 Two detection mechanisms

| Signal type | Examples | Mechanism |
|---|---|---|
| **Objective / invisible to the eye** | TTFB, LCP, CLS, console errors, failed requests, broken images | **Deterministic instrumentation** via CDP + injected measurement. No LLM. |
| **Subjective / needs visual reasoning** | Misalignment, inconsistent spacing, "does this look broken," does a control look like it does what a human expects | **Vision LLM** reasoning over captured screenshots |

**Rule:** never ask the LLM to notice something measurable. Only hand it genuine judgment calls,
always with objective hints attached.

### 4.2 Capture and judgment are separate passes

Capture is scripted Playwright/CDP with no LLM in the browser loop. Judgment is a batched vision
pass over *saved* screenshots, run after the browser is closed. Consequence: you can re-run
judgment with a new prompt in seconds without re-crawling. This remains the single biggest
iteration-speed win in the design.

### 4.3 The crawl is a dynamic workflow, not a `ParallelAgent`

The original spec's `ParallelAgent` / `SequentialAgent` are ADK's *template workflows*. In ADK
2.0 for Python these are superseded by graph-based and dynamic workflows, and for this project
dynamic workflows are a much better fit:

- **Automatic checkpointing.** Node executions are tracked; on resume, successfully completed
  child nodes are skipped and only failed or interrupted ones re-run. A 45-unit crawl that dies
  on unit 38 resumes at 38 rather than restarting. This replaces the hand-rolled retry
  bookkeeping in v2 §6.2.
- **Native Python control flow.** Concurrency caps, conditional probing, and "only sample perf
  on 3 pages" are `async` code, not graph topology.
- **Typed data passing.** `await ctx.run_node(child, payload)` returns the child's output
  directly — no writing/reading session-state string keys to move a `CaptureBundle` between
  stages. `ParallelAgent` branches don't share state during execution anyway, which would have
  made the v1 fan-out awkward.

Template workflow agents still exist and still work; this is a fit judgment, not a deprecation
scramble.

### 4.4 Discovery is a `FunctionNode`, not an agent

Link filtering is a URL filter. Wrapping it as a plain `@node` function keeps it inside the
workflow (so it's checkpointed and traced) without putting a model in the loop.

## 5. Architecture

```
  App(name="ux_audit", root_agent=Workflow(...), plugins=[...])
  Runner(session_service=DatabaseSessionService("sqlite+aiosqlite:///runs.db"))
              │
              ▼
  @node(rerun_on_resume=True)  async def audit_workflow(ctx, root_url)
              │
              ├── ctx.run_node(discover_node, root_url)        → list[PageTarget]
              │      pure python: sitemap.xml → BFS → filter → cap
              │
              ├── fan out over (page × viewport), asyncio.gather
              │   with a semaphore capping live browser contexts at 3
              │      ctx.run_node(capture_node, unit, run_id=f"cap-{slug}")
              │      → CaptureBundle written to disk, summary returned
              │
              ├── ctx.run_node(rule_node, bundles)             → deterministic findings
              │
              ├── fan out over views, capped:
              │      ctx.run_node(judge_agent, JudgeInput(...))  → VisionFindings
              │      (LlmAgent, output_schema=VisionFindings)
              │
              ├── if any page has a form/CTA:
              │      ctx.run_node(probe_agent, ProbeInput(...))  → ProbeFindings
              │      (LlmAgent + McpToolset(@playwright/mcp), tool_filter'd)
              │
              └── ctx.run_node(synthesize_node, all_findings)
                     dedupe → severity → cap → findings.jsonl → Chroma
                              │
                              ▼
        ADVISOR AGENT — separate App, served by `adk web`
        LlmAgent + FunctionTool(search_findings) over Chroma
        + LoadArtifactsTool for screenshots (see §8.3)
```

## 6. Components

### 6.1 Discovery (`@node`)

```
1. /robots.txt — respect Disallow.
2. /sitemap.xml (and sitemap index) if usable; else BFS from root, same-origin, max_depth 2.
3. Normalize: strip fragment + tracking params (utm_*, ref, fbclid), drop trailing slash,
   lowercase host. Dedupe on the normalized form.
4. Exclude: app-surface paths (/app/, /dashboard/, /account/, /admin/, /settings/, /billing/);
   redirects to a login wall; non-HTML content types; blog posts beyond `blog_sample`.
5. Prioritise: /, /pricing, /about, /contact, /signup, /login, /features, /blog
6. Cap at max_pages. Sort the final list deterministically (see §7 on run IDs).
7. Emit discovery_log.jsonl: {url, decision, reason} for EVERY url seen.
```

The reason log is mandatory. The heuristic will be wrong on some site and this is how that gets
diagnosed in a minute instead of an hour.

### 6.2 Capture (`@node`, one activation per page × viewport)

Attached before navigation: `console` and `requestfailed`/`response` listeners; a
`PerformanceObserver` injected via `add_init_script` for `layout-shift`, `largest-contentful-paint`,
and `longtask`; navigation timing from `performance.getEntriesByType('navigation')`.

Sequence: new context at the target viewport → `goto(wait_until='domcontentloaded')` →
**screenshot A** at ~800ms (catches FOUC) → settle → collect metrics → **screenshot B**
(viewport-clipped + full-page) → bundled axe-core → DOM scan for broken images
(`naturalWidth === 0`), horizontal overflow, elements overflowing their parent, and placeholder
text (`lorem ipsum`, `TODO`, `test@`) → close context.

Robustness now comes from the framework plus two local rules:

- Concurrency is an `asyncio.Semaphore` around `ctx.run_node`, capping *browser contexts*, not
  node activations. One Chromium instance, N contexts.
- A per-node `timeout` (ADK `BaseAgent`/node config exposes `timeout` and `retry_config`
  directly — don't hand-roll a wrapper).
- Node failures are caught by the workflow and by the error plugin (§6.5); the run continues and
  the failed unit is re-executed on resume rather than being silently dropped.
- Timing is noisy: capture `perf_sample_pages` (default 3) twice and take the median. Only emit a
  perf finding if the median crosses threshold. This kills the most common false positive.

### 6.3 Probe pass (`LlmAgent` + `McpToolset`)

Runs only on pages where capture found a `<form>` or an above-fold primary CTA, capped at
`max_probe_pages` (default 5), mobile + desktop only.

```python
probe_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx", args=["-y", "@playwright/mcp@latest", "--headless"],
        ),
        timeout=60,          # see note below
    ),
    tool_filter=["browser_navigate", "browser_click", "browser_type",
                 "browser_snapshot", "browser_take_screenshot"],
)
```

Two documented gotchas to design around:

1. **Don't pass a bare `StdioServerParameters` as `connection_params`.** That path doesn't
   support a timeout and ADK applies a short default (~5s), which browser startup will blow
   through. Use `StdioConnectionParams` wrapping the server params, with an explicit timeout.
2. **MCP session persistence across tool calls has been a reported problem** — the browser
   context closing between steps, losing state mid-flow. Design defensively: keep each page's
   entire probe inside a single agent invocation, don't rely on state surviving between
   invocations, and smoke-test a two-step flow before building on it.

`tool_filter` matters here beyond tidiness: Playwright MCP exposes a broad surface, and trimming
it keeps the probe agent on-task and its context small.

Per page the agent: identifies the single most obvious visitor action and takes it; submits the
form empty and then with one clearly invalid value; double-clicks the submit control. Everything
it *measures* — did a request fire, did the DOM change within 1.5s, is the error message
associated via `aria-describedby`/`<label for>`, did two identical requests fire — is
deterministic. The LLM only decides *what to try* and judges *whether the response looks sensible*.

Turn limit per page (default 12) so it can't loop.

### 6.4 Judgment pass (`LlmAgent` with `output_schema`)

One activation per (page × viewport), fanned out with `asyncio.gather` and a concurrency cap.

**Input** (`input_schema`, a Pydantic model): viewport label, screenshot B viewport-clipped +
full-page (downscaled ≤1024px wide, JPEG q70), objective hints (CLS score and top shifting
selectors, overflow elements, load timings), and the findings the rule pass already produced.

**Prompt contract:** report only what's visible; do not repeat anything in `known_findings`; do
not comment on speed/timing/errors (measured elsewhere); max 5 findings; empty list is valid and
expected.

**Output:** `output_schema=VisionFindings` — let ADK enforce the schema rather than parsing JSON
out of prose and reprompting by hand. Each finding carries `confidence`; below `medium` is
dropped.

Use a cheap fast model here (`gemini-flash-latest` class). The probe agent needs stronger tool
use; the judgment agent just needs vision plus discipline.

### 6.5 Plugins (cross-cutting, registered on `App`)

In ADK 2.0 plugins register globally on the `App` object and run ahead of agent-level callbacks.
Crucially, plugins expose error hooks (`on_tool_error`, `on_model_error`) that ordinary
callbacks do not — which is exactly what a long crawl needs.

| Plugin | Role |
|---|---|
| `LoggingPlugin` (built-in) | Run log at every workflow callback point; replaces v2's ad-hoc manifest logging |
| `ReflectAndRetryToolPlugin` (built-in) | Retries failed Playwright MCP tool calls in the probe pass with reflection, instead of the agent stalling |
| `CaptureErrorPlugin` (custom, `on_tool_error`) | Converts capture/probe tool failures into `run_error` findings so failures are data, not silence |
| `ContextFilterPlugin` (built-in) | Keeps the probe agent's context from ballooning across 12 turns of DOM snapshots |

Order matters — filter before retry. One note: plugin tool callbacks have been reported as
skipped in live/streaming mode; this project uses `runner.run_async`, not `run_live`, so it isn't
affected, but don't port these plugins into a streaming variant without rechecking.

## 7. Resumability and run identity

Configure the runner with `DatabaseSessionService("sqlite+aiosqlite:///runs.db")` — SQLite
requires the async driver (`sqlite+aiosqlite`, not `sqlite`). Every state write is then durable,
and a killed process resumes from its checkpoint.

On execution IDs: ADK generates deterministic child-execution IDs from the parent ID and a
counter, and uses them to skip already-completed nodes on resume. The docs advise **against**
custom run IDs in general, since they drive execution ordering.

- **Default (recommended):** sort the discovery output deterministically and let ADK assign
  sequential IDs. Resume works correctly as long as the page list is stable.
- **Opt-in:** if you want resume to survive a *changed* page list (site edited mid-run, cap
  raised), pass `run_id=f"cap-{page_slug}-{viewport}"`. Custom IDs must contain a non-numeric
  character to avoid colliding with the auto-generated integer IDs — the `cap-` prefix handles
  that. Treat this as a deliberate trade, not a default.

Parent orchestrator nodes that call `ctx.run_node` **must** be declared
`@node(rerun_on_resume=True)`, or resume won't deliver cached child results correctly.

## 8. Storage

### 8.1 Source of truth

`runs/<run_id>/findings.jsonl` plus `manifest.json` (root URL, config snapshot, model versions,
pages attempted/succeeded/failed). If the Chroma schema changes, reindex from JSONL; never
re-crawl to recover data.

### 8.2 Index (Chroma)

```json
{
  "id": "sha1(run_id|page_url|viewport|category|dedupe_key)[:16]",
  "document": "Pricing cards shift vertically ~30px after the webfont loads, causing a visible jump in the first second.",
  "metadata": {
    "run_id": "20260729T101500Z-example_com",
    "site": "example.com",
    "page_url": "https://example.com/pricing",
    "viewport": "mobile_390",
    "category": "visual_stability",
    "severity": "medium",
    "severity_rank": 2,
    "source": "rule",
    "confidence": "high",
    "evidence": "{\"cls\": 0.18, \"selector\": \".pricing-card\"}",
    "screenshot_path": "runs/<run_id>/screenshots/pricing_mobile_390_after.png",
    "timestamp": "2026-07-29T10:15:00Z"
  }
}
```

- `category` enum: `performance | visual_stability | content_completeness | interaction |
  console_network | accessibility | responsive_design | run_error`
- `severity_rank` (1/2/3) exists so metadata filters can do `$gte`; strings can't be range-filtered.
- `source`: `rule | vision | probe | axe` — lets the advisor say how something was found, and
  lets you measure the vision layer against the deterministic one (§10).
- Deterministic `id` → re-ingest upserts instead of duplicating.

**Dedupe and noise control before write:** collapse identical findings across viewports (unless
viewport-specific); collapse axe violations by `rule_id` + page keeping ≤3 example selectors;
suppress console noise from denylisted third-party origins (record the count, not every line);
global cap `max_findings_per_page` (default 25), highest severity first.

### 8.3 Screenshots and artifacts

ADK ships two artifact services: `InMemoryArtifactService` (ephemeral, lost on process exit) and
`GcsArtifactService` (needs a GCS bucket and credentials). Neither fits a local crawl whose
results are consumed by a *different session* later, so:

- **Primary:** screenshots stay on disk under `runs/<run_id>/screenshots/`, referenced by path.
- **Optional (~40 lines):** a `LocalDirArtifactService(BaseArtifactService)` implementing save /
  load / list / list_versions against that directory. Register it on the advisor's `Runner`
  and add `LoadArtifactsTool()` so the advisor can pull a screenshot into context and discuss
  it visually in `adk web`. Save under the `user:` namespace prefix so artifacts are scoped to
  the user rather than a single session — a session-scoped artifact written by the crawl would
  be invisible to the advisor.

Note that `LoadArtifactsTool` appends selected artifact content to a single request rather than
persisting it into session history, so the model re-calls the tool when it needs the same image
in a later turn. That's fine here and keeps the advisor's context small.

## 9. Severity rubric

| Severity | Meaning | Deterministic triggers |
|---|---|---|
| **high** | Blocks or breaks a primary visitor task | Form cannot be submitted; primary CTA is a dead click; content overlapping/unreadable at a breakpoint; 4xx/5xx on a user action while UI shows success; LCP > 4.0s; CLS > 0.25; axe `critical`; broken image above the fold |
| **medium** | Degrades but doesn't block | LCP 2.5–4.0s; TTFB > 800ms; CLS 0.10–0.25; uncaught JS error; broken image below fold; truncated/overflowing text; validation error not associated with its field; axe `serious` |
| **low** | Cosmetic or hygiene | Spacing/alignment inconsistency; duplicate network calls; console warnings; mixed content; axe `moderate`/`minor`; placeholder text in a non-prominent spot |

**Hard rule:** a finding from the judgment pass caps at **medium** unless it ties to a
deterministic signal. Severity inflation is what makes these reports get ignored.

## 10. Evaluation

ADK ships an evaluation framework that assesses both final response quality and step-by-step
execution trajectory, with configurable criteria and custom metrics, runnable from the CLI and
the web UI.

Concretely:

- Build a small eval set: ~8 pages with hand-labelled ground-truth findings (a few genuinely
  broken layouts, a few clean ones).
- Score the **judgment agent** on precision — how many `source: vision` findings a human
  agrees with — via a custom metric, and gate prompt changes on it.
- Score the **probe agent** on trajectory: did it click the primary CTA, did it submit the form,
  did it stay within its turn budget.
- Re-run the eval whenever the prompt, model, or `min_confidence` changes.

This turns "is the LLM layer any good" from a vibe into a number, and it's the difference
between a demo and something you'd trust on a client's site.

## 11. Observability

Use ADK's built-in observability rather than custom instrumentation: structured logging, metrics,
and OpenTelemetry traces. A trace per crawl gives you per-node timing, which is how you'll
actually tune the concurrency cap (§12) instead of guessing.

## 12. Configuration

One `config.yaml`; no thresholds or patterns in code.

```yaml
run:
  max_pages: 15
  max_depth: 2
  blog_sample: 2
  browser_concurrency: 3        # live Chromium contexts, enforced by semaphore
  node_timeout_s: 45
  settle_timeout_s: 5
  perf_sample_pages: 3
  session_db: "sqlite+aiosqlite:///runs.db"
  custom_run_ids: false         # see §7 trade-off

viewports:
  - {name: mobile_390,   width: 390,  height: 844,  dsf: 3}
  - {name: tablet_768,   width: 768,  height: 1024, dsf: 2}
  - {name: desktop_1440, width: 1440, height: 900,  dsf: 1}

thresholds:
  ttfb_ms:      {medium: 800,  high: 1800}
  lcp_ms:       {medium: 2500, high: 4000}
  cls:          {medium: 0.10, high: 0.25}
  long_task_ms: 200

discovery:
  exclude_patterns: ["/app/", "/dashboard/", "/account/", "/admin/", "/settings/", "/billing/"]
  prioritise:      ["/", "/pricing", "/about", "/contact", "/signup", "/login", "/features", "/blog"]

judgment:
  model: "gemini-flash-latest"
  llm_concurrency: 4
  max_findings_per_view: 5
  min_confidence: medium
  image_max_width: 1024

probe:
  model: "gemini-flash-latest"
  max_probe_pages: 5
  viewports: [mobile_390, desktop_1440]
  max_turns: 12
  mcp_timeout_s: 60

noise:
  console_denylist_hosts: ["googletagmanager.com", "hotjar.com", "intercom.io"]
  axe_min_impact: serious
```

## 13. Build phases

1. **Capture harness** — Playwright + CDP listeners + screenshots, one page, one viewport,
   plain Python. Output: `CaptureBundle` JSON on disk. No ADK yet.
2. **Rule pass + storage** — threshold functions, severity rubric, `findings.jsonl`, Chroma
   ingest. *You now have a working auditor.*
3. **Wrap in a dynamic workflow** — `@node` around discovery and capture, `Workflow` root,
   `DatabaseSessionService`, `asyncio.gather` with the semaphore. Verify resume by killing the
   process mid-run and restarting.
4. **Plugins** — logging, error→finding conversion. Now failures are visible and data.
5. **axe-core** — bundled locally, per page/viewport, deduped by rule.
6. **Judgment pass** — `LlmAgent` with `input_schema`/`output_schema`, batched over saved
   screenshots. Re-runnable without re-crawling.
7. **Probe pass** — `McpToolset` + `ReflectAndRetryToolPlugin`. Smoke-test MCP session
   persistence (§6.3) *before* building the full probe logic.
8. **Advisor agent** — `adk web`, `FunctionTool(search_findings)` over Chroma, optional
   `LocalDirArtifactService` + `LoadArtifactsTool` for screenshots. Grounds every claim in a
   finding `id`; says the store has nothing rather than improvising.
9. **Eval harness** — labelled set, custom precision metric on the vision layer, trajectory
   check on the probe agent.

## 14. ADK features deliberately not used

Being explicit about this matters as much as the list of what's used — "full use of the
framework" is not "use every feature."

| Feature | Why not |
|---|---|
| `ParallelAgent` / `SequentialAgent` | Superseded for this use case by dynamic workflows (§4.3); no checkpointing, no typed data passing, no shared state across parallel branches. |
| Graph-based (static) workflows | The control flow here is loops and conditionals over a variable-length page list — the exact case the docs point at dynamic workflows for. |
| `MemoryService` (`InMemory` / Memory Bank / `adk-database-memory`) | It's *conversational* memory: session-derived, and the local implementations use keyword matching rather than embeddings. The findings store is a structured artifact with metadata filters, not conversation history. Chroma behind a `FunctionTool` is the right shape. |
| `GcsArtifactService` | Cloud dependency for a local tool. |
| Human-input nodes / `RequestInput` | The crawl is unattended by design. Worth revisiting if you later want "the agent asks before submitting a real signup form." |
| A2A protocol | Single machine, single process. |
| Collaborative workflows / coordinator agents | The pipeline is fixed and known; LLM-directed routing adds nondeterminism for no gain. |
| `run_live` / streaming | Not interactive; also where the plugin tool-callback gap sits. |
| Deployment (Cloud Run, GKE, Agent Engine) | Explicitly out of scope. |

## 15. What changed from v2

| Change | Why |
|---|---|
| Crawl orchestration → dynamic workflow (`@node`, `ctx.run_node`, `asyncio.gather`) | Checkpointed resume, typed data passing, native control flow. Deletes most of v2's hand-rolled retry/failure bookkeeping. |
| `DatabaseSessionService` on SQLite | Makes resume real. Note the async driver requirement (`sqlite+aiosqlite`). |
| Custom run-ID policy made an explicit, off-by-default trade | Docs warn custom IDs affect execution ordering; deterministic sorting plus auto IDs is the safe default. |
| Cross-cutting concerns → ADK plugins on `App` | Plugins are the only place with `on_tool_error`/`on_model_error`; long crawls need error hooks, not just before/after. |
| `input_schema`/`output_schema` on the judgment agent | Framework-enforced structure beats hand-parsing JSON and reprompting. |
| Two concrete `McpToolset` gotchas designed around (timeout, session persistence) | Both are documented failure modes that would otherwise surface as a mystery on day one. |
| Screenshots stay on disk; optional `LocalDirArtifactService` + `LoadArtifactsTool` | ADK's shipped artifact services are ephemeral or cloud-only; but artifacts are the right mechanism for letting the advisor *show* a screenshot. |
| Evaluation promoted from open item to build phase 9 | ADK has an eval framework with custom metrics; vision-layer precision should be a number. |
| Observability via built-in tracing | Per-node traces are how you tune the concurrency cap with data. |
| Section 14 added | Explicitly bounding what's *not* used prevents feature-chasing. |

## 16. Remaining open items

- Path-filtering heuristic will be wrong on some site; the discovery reason log is how that gets
  fixed fast.
- `browser_concurrency: 3` is a guess. Read the traces after the first full run — Chromium
  contexts with CDP listeners attached are heavier than plain page loads.
- FOUC detection via screenshot A/B comparison is the weakest deterministic check. May need a
  perceptual diff threshold, or may be better handed to the vision pass entirely.
- ADK 2.x is moving fast (2.0 shipped as alpha before GA; the Event schema gained `node_info` and
  `output`, and the session DB schema changed in Python 1.22.0). Pin the version, and re-read the
  workflow docs before a major upgrade rather than assuming the API held still.
- No target site is hardcoded. The first real run should be against a live URL chosen at that
  time, including a modern templated site — bare old HTML won't exercise the responsive /
  "AI-template smell test" detector at all.

# External capabilities

Capabilities let Odysseus run existing task engines and bring their reports
back into the assistant. They are first-class product features backed by a
shared registry, catalog, typed configuration, durable runs, schedules,
readiness diagnostics, reports, and assistant tools.

Use capabilities for weekly pain mining, portfolio analysis, recurring
operational reports, and repository-specific data pipelines.

## Architecture

Odysseus owns orchestration:

1. The registry describes available capabilities and their typed inputs.
2. The agent or scheduler creates a durable `capability_runs` row.
3. The capability runs in an isolated HTTP worker or a trusted native process.
4. Odysseus records status, summary, logs, errors, and provenance.
5. Markdown or text reports are imported into Documents.

The LLM can explain a report, but the capability repository remains the source
of truth for algorithms, market data, scoring, and other deterministic logic.

For structured LLM work, Odysseus also owns provider credentials and
endpoint/model selection. Capability workers own prompts and schemas, but never
receive provider secrets.

Run inputs are durable user configuration, so manifests must not declare
secret run fields. Credentials belong to transport configuration or declared
integration dependencies and are represented to users only by readiness state.

## Docker deployment

Use the `http` transport in Docker. Each repository gets its own image and
dependency environment. Do not install every repository into the Odysseus
container or mount the Docker socket into Odysseus.

1. Copy `config/capabilities.example.yaml` to `data/capabilities.yaml`.
2. Generate a long random token for every worker and place it in Odysseus `.env`.
3. Put provider credentials in each capability repository's `.env`; the
   Compose overlay loads those files directly into only that worker.
4. Add a worker service using `docker-compose.capabilities.example.yml`.
5. Start through `.\dev-stack.ps1 up` (or `rebuild -Scope All` after image,
   dependency, Compose, or capability-repository changes).
6. Open Tasks and choose **Capability**, or ask the agent to list capabilities.

The worker is reached by its Compose DNS name, such as
`http://pain-miner-capability:8080`. It does not publish a host port.

The generic image at `docker/capability-worker.Dockerfile` installs one Python
repository and runs the reference worker. The build context must contain both
repositories:

```text
Projects/Ai/
  odysseus/
  pain-miner/
```

## Registry

The registry defaults to `/app/data/capabilities.yaml` in Docker and
`data/capabilities.yaml` for a native checkout. Override it with
`ODYSSEUS_CAPABILITIES_CONFIG`.

```yaml
version: 1
model_roles:
  painminer.tag:
    setting_prefix: utility
    capabilities: [pain-miner]
  painminer.cluster:
    setting_prefix: default
    capabilities: [pain-miner]
capabilities:
  - id: portfolio-report
    name: Portfolio Report
    description: Analyse holdings and watchlists using the portfolio engine.
    admin_only: true
    timeout_seconds: 1200
    import_report: true
    transport:
      type: http
      base_url: http://portfolio-capability:8080
      token_env: PORTFOLIO_CAPABILITY_TOKEN
    inputs:
      portfolio:
        type: string
        required: true
      include_news:
        type: boolean
        default: true
```

Input types are `string`, `text`, `array`, `integer`, `number`, and `boolean`. Optional
`choices` constrain values. Unknown fields and incorrect types are rejected.
Boolean argv inputs may declare both `flag` and `false_flag`, for example
`--refresh` and `--no-refresh`.
Registry changes load on restart or through
`POST /api/capabilities/reload` by an admin.

The canonical browser-safe definition also supports presentation metadata,
visibility, permissions, supported triggers, grouped/advanced inputs,
declared model roles and dependencies, output actions, progress phases,
cancellation, retry, and idempotency. Transport URLs, token environment
names, commands, working directories, and other operator-only details are
never returned by the catalog API.

## Product surfaces

The primary Capabilities catalog is entirely registry-driven. It provides
search, readiness, schema-generated run/default forms, schedules, durable run
history, progress, metrics, warnings, failures, provenance, logs,
cancellation, rerun, report opening/discussion, and administration.

Capability reports remain stored once in Documents. Documents record the
capability ID, capability run ID, and scheduled task ID for Library filtering
and cross-links. Stable anchors are `#capability-<id>` and
`#capability-run-<run-id>`.

## Readiness

Readiness has two independent domains:

1. Odysseus resolves declared model roles against owner-visible enabled model
   endpoints.
2. HTTP workers expose authenticated `GET /v1/readiness` and return only
   dependency availability. Worker credentials never leave the worker.

Dependencies may be conditional on run inputs. A credential can be optional
for one source and required when the selected input requests another. New runs
are rejected before worker execution when their effective readiness is
incomplete.

The generic worker reads `CAPABILITY_READINESS_JSON` and returns dependency
IDs, availability booleans, and safe status messages only.

## Administration

Admins can reload the registry and enable or disable a capability from the
catalog. Enablement overrides are stored separately from YAML. Disabling
blocks new runs while preserving active runs, history, reports, and schedules.

## Structured model broker

Trusted workers call:

```text
POST /api/capability-models/structured
Authorization: Bearer <ODYSSEUS_CAPABILITY_MODEL_TOKEN>
```

```json
{
  "run_id": "durable-odysseus-run-id",
  "role": "painminer.tag",
  "prompt": "...",
  "schema": {"type": "object", "properties": {}}
}
```

The role is loaded from the capability registry. It names an Odysseus settings
prefix such as `utility` or `default` and explicitly allowlists capability IDs.
Odysseus resolves the endpoint/model for the capability run owner, calls the
existing non-streaming LLM primitive, validates returned JSON against the
request schema, and returns:

```json
{
  "data": {},
  "provenance": {
    "role": "painminer.tag",
    "setting_prefix": "utility",
    "model": "configured-model",
    "endpoint": "https://provider.example/v1/chat/completions"
  }
}
```

Resilience is layered so that weak local models (small quantized instruct
models that cannot reliably emit schema-valid JSON on their own) still succeed,
without weakening the schema contract for capable models:

- **Constrained decoding first.** The request schema is passed to the model
  server as a decoding constraint, so the reply is valid JSON by construction
  rather than by the model's goodwill. For OpenAI-compatible endpoints
  (llama.cpp, vLLM, Ollama's `/v1` shim) this is
  `response_format={"type":"json_schema","json_schema":{...}}`; for Ollama's
  native `/api/chat` it is the top-level `format` field. Anthropic has no
  constrained mode and relies on the parsing/repair layers below. Servers that
  don't support the field are handled by the fallback in the next point.
- **Tolerant parsing.** The raw reply is parsed leniently before validation:
  reasoning blocks (`<think>…</think>` and similar) are stripped, a ```` ```json ````
  fence is unwrapped, the first balanced `{…}`/`[…]` span is extracted from
  surrounding prose, and a trailing-comma repair is attempted. This recovers
  the "almost-JSON" a weak model emits without accepting anything that fails
  schema validation.
- **Bounded corrective rounds.** A reply that still fails parsing or validation
  is re-asked with the rejected reply plus the validator error fed back, up to
  the role's `max_repair_attempts` (default 2) before the call fails with 502.
  If the *initial* constrained request is rejected outright by the endpoint
  (some OpenAI-compatible servers 400 on an unsupported `response_format`), the
  broker retries once unconstrained so constraint-incompatible endpoints still
  work via tolerant parsing. The schema contract is unchanged; a reply that
  still doesn't validate after the budget is rejected exactly as before.
- the route is exempt from the global 45-second request hard-timeout
  (`REQUEST_HARD_TIMEOUT` in `app.py`); each call is instead bounded by the
  role's own `timeout_seconds` (default 180) and is cancelled if the
  capability run stops. Without the exemption every slow local-model
  structured call 504s at 45s regardless of role configuration.

`max_repair_attempts` is a per-role setting (0–5) alongside `timeout_seconds`
and `max_tokens` in `model_roles`, so a role pointed at a strong hosted model
can set it to 0 and a role on a weak local model can keep the default.

Security properties:

- the scoped broker token is independent from worker and internal-tool tokens;
- the durable run must exist, be active, and belong to an allowlisted capability;
- disabled and unknown roles fail closed;
- workers cannot supply endpoint URLs, model IDs, or provider credentials;
- prompt length and schema size/depth are bounded per role;
- invalid model JSON or schema violations are rejected;
- cancelling a capability run cancels an active broker call;
- provider authorization headers are never returned to the worker.

## Result contract

The task engine emits one final JSON object. CLI engines print it as the last
stdout line or prefix it with `ODYSSEUS_RESULT=`:

```json
{
  "summary": "Found seven promising paid-pain clusters",
  "report_path": "/data/reports/2026-06-14.md",
  "metrics": {"signals": 842, "clusters": 7},
  "warnings": []
}
```

An inline report is also supported and is preferred for HTTP workers:

```json
{
  "summary": "Portfolio report complete",
  "report": {
    "title": "Portfolio Report - 2026-06-14",
    "format": "markdown",
    "content": "# Portfolio report\n\n..."
  }
}
```

For process capabilities, `report_path` must remain under the configured
working directory. Exit code zero means success. A nonzero exit code marks the
run failed while retaining logs and any structured error.

Imported reports are capped by `ODYSSEUS_CAPABILITY_REPORT_MAX_BYTES`
(10 MiB by default). Report content is stored once in Documents; capability
run metadata keeps only report title, format, path, and document id.

The reference HTTP worker automatically converts a `report_path` under
`CAPABILITY_REPORT_ROOTS_JSON` into an inline report before returning it. This
is how a report stored in the worker's `/data/reports` volume reaches
Odysseus without sharing that filesystem with the main container.

### Report synthesis (shared quality core with deep research)

When a capability returns structured `data`, Odysseus synthesizes the run's
report from it rather than relying only on the capability's own deterministic
report. Capability synthesis (`src/capability_report.py`) is its **own** flow,
separate from the deep-research engine: it resolves the owner's `default` model
(the worker never sees provider keys) and is strictly grounded in the returned
`data` — the model may use nothing else.

It does **not**, however, keep its own copy of the report-*writing* logic. Both
capability synthesis and deep research write their report through one shared
core, `src/report_writer.py`, which is the single source of truth for report
quality: the prose-style contract (`REPORT_STYLE_REQUIREMENTS`), the
per-category format overrides (`CATEGORY_PROMPTS`), the reasoning-tag strip, and
the expand-if-too-short retry. Improve any of these in one place — or improve
deep research's report path, which uses the same core — and every capability
report inherits it automatically. There is no per-capability report code and no
private copy of the writing mechanism (DRY/SRP).

If no `default` endpoint is configured, the `data` block is absent, or synthesis
fails, the capability's own `report` is imported unchanged — a run never loses
its output because a model was unavailable.

The same sharing applies to the *visual* report. `GET
/api/capabilities/runs/{run_id}/report.html` renders the run's stored report
markdown into the same magazine-style HTML deep research produces, through the
shared `src/visual_report.generate_visual_report` renderer (the Sources panel is
built from the report's own inline citations). Deep research and capability runs
therefore share both the report writer and the report renderer — no per-flow
copy of either.

## HTTP protocol

Workers implement three Bearer-authenticated endpoints:

```text
POST   /v1/runs
GET    /v1/runs/{run_id}
GET    /v1/runs/{run_id}/log
DELETE /v1/runs/{run_id}
```

Start request:

```json
{"input": {"since": "7d"}, "odysseus_run_id": "uuid"}
```

Start response:

```json
{"id": "provider-run-id", "status": "queued"}
```

Status is `queued`, `running`, `success`, `error`, or `cancelled`. A successful
terminal response includes `result` using the contract above. The log endpoint
returns `{"id": ..., "output": "<last N lines>"}` — `output` is the key the
runner reads. Workers also expose `GET /v1/readiness` (Bearer-authenticated
dependency report) and an unauthenticated `GET /healthz`.

The protocol surface is single-sourced in `libs/capability_kit`
(`capability_kit.worker.create_run_lifecycle_router`): constant-time bearer
auth, run stores, status/log/cancel/readiness endpoints, and the guarded
cancel/finish transition. Workers plug in an executor (subprocess, thread).

The reference implementation is `integrations/capabilities/worker.py`. It
mounts the kit router with a subprocess executor: fixed argv, validated
inputs, persisted state, combined stdout/stderr capture, a timeout, and
process-group termination on cancellation.

## Native process transport

The process transport is for trusted non-Docker installations. It never uses a
shell. The manifest contains a fixed command array and each input is appended
as a positional argument or `flag value`.

```yaml
transport:
  type: process
  cwd: /srv/portfolio-engine
  command: [/srv/portfolio-engine/.venv/bin/portfolio, report]
  pass_env: [MARKET_DATA_API_KEY]
inputs:
  portfolio:
    type: string
    flag: --portfolio
    required: true
```

Only basic operating-system variables and names in `pass_env` reach the child.
User input cannot change the executable, working directory, or environment
allowlist.

## Agent and API

The agent tool `manage_capabilities` supports discovery, detail inspection,
saved-default configuration, readiness checks, run, status, explanation, logs,
cancellation, and rerun.

REST endpoints:

```text
GET  /api/capabilities
POST /api/capabilities/{id}/runs
GET  /api/capabilities/runs
GET  /api/capabilities/runs/{run_id}
GET  /api/capabilities/runs/{run_id}/log
POST /api/capabilities/runs/{run_id}/cancel
POST /api/capabilities/reload
```

Scheduled capability tasks support schedule, event, and webhook triggers,
chaining, notifications, run history, and stop.

## Adding a capability

1. Give the repository one stable, non-interactive command for the full job.
2. Make it idempotent where practical.
3. Emit the structured result contract.
4. Build an isolated worker service.
5. Add one registry entry.
6. Declare presentation, dependencies, model roles, progress phases, and
   supported actions.
7. Reload capabilities and pass readiness.
8. Run a direct, non-notifying smoke test.
9. Create or activate the production schedule only after smoke verification
   and report import succeed.

The repository owns its algorithms and tests. Odysseus does not duplicate its
business logic.

## Pain Miner

Pain Miner implements the full orchestrator and deterministic digest renderer
for Hacker News, Reddit, AusTender ATM and OCDS, G2, GitHub Issues, App Store
reviews, Stack Exchange, Bluesky, and configured RSS/Atom feeds:

```text
painminer run --since 7d
```

The Compose overlay runs it without Gemini or Anthropic keys. Configure enabled
Odysseus endpoint/models for the `utility` and `default` settings used by the
two model roles before running against signal-bearing data. G2 readiness
depends only on `APIFY_TOKEN`; Reddit readiness independently requires all
three Reddit OAuth variables. Sources without configured prerequisites can be
excluded without blocking the remaining source set.

Report paths are worker-owned. Odysseus sends run intent only; Pain Miner
writes beneath `DIGEST_DIR`, returns `report_path` in its result contract, and
the worker safely inlines that report for import.

Verified through the authenticated Docker APIs:

- capability registry reload/list;
- deterministic worker run and durable status;
- Markdown report import into Documents;
- weekly Tasks creation and manual execution;
- cancellation and restart-resume behavior in focused lifecycle tests;
- no provider-secret variables in the Pain Miner worker environment.

The included weekly task can run on demand, weekly, monthly, cron, event, or
webhook triggers through the normal Tasks API/UI.

## Stock Research

Stock Research uses the native HTTP worker in the sibling `stock-research`
repository. Its daily, weekly, and deep modes are registry inputs; dependency
readiness comes from the worker's authenticated `/v1/readiness` endpoint.
Its image owns `/app/config` and loads `portfolio.yaml` plus `watchlist.yaml`
from that directory; callers do not pass host or container paths.

Odysseus owns synthesis models through `stockresearch.summarize` and
`stockresearch.deepdive`. The worker receives only the scoped broker token and
role names. Missing or failed synthesis degrades to deterministic filing text;
all figures still come from SQLite or Parquet and every report retains source,
algorithm, run, and model provenance.

## Financial analysis

Portfolio capabilities should emit algorithm and data provenance, not only a
buy/sell label:

- market-data timestamp and provider;
- algorithm/version and valuation assumptions;
- signal strength and confidence;
- portfolio exposure and position sizing;
- invalidation or stop conditions;
- backtest period and metrics;
- warnings for stale or missing data.

Treat reports as decision support, not personalized financial advice.

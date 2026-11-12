# Operations Agent Harness

A runnable operations assistant with validated mock tools, persistent human approval, bounded model/tool execution, SQLite history, and JSONL traces.

[GitHub repository](https://github.com/wizounovziki/agent-harness-assessment) · [Mock dataset](https://github.com/wizounovziki/agent-harness-assessment/tree/main/data)

The implementation follows the supplied Python framework specification: Python 3.12, FastAPI, Pydantic, an injected model provider, and an injected approval callback. Pi informed the module boundaries. The control loop is implemented in this repository.

![Layered architecture](docs/images/operations-layered-architecture-en-v2.png)

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from the repository root:

```powershell
uv sync --frozen
uv run pytest -q
uv run python main.py run "Check auth service health." --scenario healthy
uv run python main.py serve
```

The API listens at `http://127.0.0.1:8000`. Interactive API contracts are at `http://127.0.0.1:8000/docs`. Default scripted mode requires no model API key.

For a standard Python installation:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python main.py serve
```

## CLI approval

```powershell
uv run python main.py run "Check payment and create an incident if needed." --scenario incident
```

The CLI prints the exact title, description and severity, then asks you to type `approve`. Another response rejects the action, returns the rejection to the model, and produces a cancellation summary.

To persist a pending approval and exit:

```powershell
uv run python main.py run "Check payment and create an incident if needed." --scenario incident --pause
uv run python main.py resume SESSION_ID
uv run python main.py show SESSION_ID
uv run python main.py export SESSION_ID
```

CLI exit codes: `0` completed, `2` failed/stopped/review required, `3` awaiting approval, `4` execution limit exceeded.

## API and Postman

Import `postman_collection.json` and `postman_environment.json` into Postman. Set the request delay to 100 ms in the Collection Runner. The collection creates runs, polls bounded status queries, approves/rejects the exact saved draft, and checks incident counts and traces.

All business endpoints require `Authorization: Bearer local-demo-token` by default. The server assigns the authenticated identity; request bodies cannot specify a different owner or approver.

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | Public process health and configured model mode |
| `GET /scenarios` | Deterministic scenario catalog |
| `POST /runs` | `{ "objective": "Check payment", "scenario_id": "incident" }`; returns 202 and `session_id` |
| `GET /runs/{id}` | State, usage, pending action and final answer |
| `POST /runs/{id}/approvals/{approval_id}` | `{ "decision": "approve", "parameters_hash": "<saved hash>" }` |
| `POST /runs/{id}/cancel` | `{ "reason": "Stop this run" }` |
| `GET /runs/{id}/events?after=0` | Ordered, redacted event history |
| `GET /runs/{id}/incidents` | Actual mock incident records |
| `GET /runs/{id}/trace.jsonl` | Download the trace reconstructed from SQLite |

Example in PowerShell:

```powershell
$headers = @{ Authorization = "Bearer local-demo-token" }
$body = @{ objective = "Check payment and create an incident if needed."; scenario_id = "incident" } | ConvertTo-Json
$run = Invoke-RestMethod http://127.0.0.1:8000/runs -Method Post -Headers $headers -ContentType application/json -Body $body
$state = Invoke-RestMethod "http://127.0.0.1:8000/runs/$($run.session_id)" -Headers $headers
# Poll until AWAITING_APPROVAL, inspect pending_tool_call.tool_args, then submit:
$pending = $state.pending_tool_call
$decision = @{ decision = "approve"; parameters_hash = $pending.parameters_hash } | ConvertTo-Json
Invoke-RestMethod "http://127.0.0.1:8000/runs/$($run.session_id)/approvals/$($pending.approval_id)" -Method Post -Headers $headers -ContentType application/json -Body $decision
```

Repeated identical decisions are idempotent. A conflicting decision or hash returns 409. Unknown or foreign runs return 404. Invalid inputs return 422. Bodies above 64 KiB return 413.

## Model modes

`scripted` mode uses a deterministic model adapter. Its selected scenario defines the decisions, faults, and example objective. Use the matching scenario to reproduce evaluation cases. Both model modes use the same harness, validation, tools, approval, storage and traces.

`real` mode uses the official asynchronous OpenAI SDK with JSON model decisions validated as `LLMAction`. The SDK's automatic retries are disabled; the harness counts and controls actual model requests. A compatible Chat Completions endpoint can be configured with `LLM_BASE_URL`.

Copy `.env.example` to `.env`, then set:

```dotenv
LLM_MODE=real
OPENAI_API_KEY=<set locally>
LLM_MODEL=<a model available to your account with JSON output support>
```

Run the CLI or restart the API. The model interprets the supplied objective and uses the mock service catalog and runbooks through the tools. Every tool write still requires approval.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_MODE` | `scripted` | `scripted` or `real` |
| `OPENAI_API_KEY` / `LLM_API_KEY` | empty | Required for real mode; stays outside model context and traces |
| `LLM_MODEL` | empty | Required for real mode |
| `LLM_BASE_URL` | official endpoint | Optional compatible endpoint |
| `API_TOKEN` | `local-demo-token` | Demo user's bearer token; choose a private value for a shared deployment |
| `DATABASE_PATH` | `runtime/harness.sqlite3` | SQLite source of truth |
| `TRACE_DIR` | `runtime/traces` | Flushed JSONL exports |
| `DATASET_PATH` | `data` | Mock data and fault scenarios |
| `MAX_STEPS` | `8` | Actual model requests, including retries and correction |
| `MAX_EXECUTION_SECONDS` | `30` | Active execution time; approval waits pause this clock |
| `MAX_TOOL_ATTEMPTS` | `16` | Actual tool attempts, including retries |
| `MODEL_TIMEOUT_SECONDS` | `15` | Each request also respects the remaining active budget |
| `TOOL_TIMEOUT_SECONDS` | `3` | Each attempt also respects the remaining active budget |
| `MAX_RETRIES` | `2` | Up to three attempts for timeout/connection errors |
| `BACKOFF_SECONDS` | `0.05` | Initial exponential retry delay |
| `MAX_PARSE_FAILURES` | `2` | Consecutive malformed decisions before failure |
| `APPROVAL_TTL_SECONDS` | `900` | Absolute approval expiry |

## Safety, state and recovery

- Inputs and outputs are validated with Pydantic. Invalid arguments and outputs become structured observations; schema failures are not retried by the transient retry wrapper.
- `thought` stores a brief decision summary. Observations carry verified results or errors and update the next decision.
- SQLite stores snapshots, messages, actions, approvals, events and mock incidents. Optimistic state revisions and execution claims coordinate recovery.
- Approvals bind the run, logical action, canonical parameter hash, authenticated owner and expiry. The mock write rechecks persisted approval inside its database transaction.
- The same incident parameters within a run share a stable idempotency key. Duplicate proposals and post-commit timeouts reuse the confirmed incident. Unresolved writes enter `NEEDS_REVIEW`.
- API approval waits release execution resources. They survive application restart. Remaining budgets and the pending action are preserved.
- Every decision, tool attempt, retry, rejection and transition produces a step trace. SQLite commits precede the JSONL flush. Exports can be rebuilt from the event table after an interrupted file write.

Statuses follow the supplied contract: `RUNNING`, `AWAITING_APPROVAL`, `COMPLETED`, `FAILED`, `LIMIT_EXCEEDED`, plus `STOPPED` for cancellation/expiry and `NEEDS_REVIEW` for uncertain writes. A rejected creation can finish as `COMPLETED / user_rejected` with a cancellation summary.

## Validation and submission

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest -q --junitxml=runtime/test-results.xml
uv run python -m scripts.build_mock_dataset
uv run python -m scripts.build_postman
uv run python -m scripts.prepare_submission
uv run python -m scripts.verify_archives
```

See [implementation report](docs/implementation-report.md), [validation results](docs/validation-results.json), [mock dataset](data/DATASET.md), and [submission checklist](docs/submission-checklist.md). Live model smoke verification is deferred for this delivery. Release archives are created under `dist/`. `uv.lock` and `requirements.txt` pin the tested dependencies.

The small mock dataset is delivered in this repository's `data/` directory alongside its schemas, version manifest and field guide. Code and data can be downloaded together using GitHub's **Code → Download ZIP**.

## Current limits and next steps

This is a local, single-service mock environment. Real mode supports one JSON decision per turn through one provider adapter. Business severity guidance comes from the runbooks and user review. The natural-language completion check detects missing status evidence, unsupported creation claims and unknown incident IDs; a structured result contract would support stronger semantic verification.

The default identity is a local demo user; enterprise identity and roles can replace token authentication. Long-term improvements include production incident connectors, reconciliation APIs, PostgreSQL and durable queues, distributed tracing, and broader model evaluation. Production tool adapters must implement cancellation and idempotency appropriate to their external systems.

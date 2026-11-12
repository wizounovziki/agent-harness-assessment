# API Collection

Base URL: `http://127.0.0.1:8000`. Start with `uv run python main.py serve`.

Import the [Postman collection](../postman_collection.json) and [environment template](../postman_environment.json). The collection contains 10 folders and 49 requests with 86 assertions. Run it with a 100 ms delay and scripted mode to reproduce deterministic results. Each polling loop is limited to 100 attempts.

The [OpenAPI 3.1 contract](./openapi.json) is exported from the actual FastAPI application. Interactive documentation is available at `/docs`; the running specification is at `/openapi.json`. Regenerate the snapshot with `uv run python -m scripts.export_openapi`.

## Authentication and request contract

Business requests require `Authorization: Bearer <API_TOKEN>`. The demo token in the environment template is `local-demo-token`; it authenticates as `demo-user`. Set a private application token when sharing a deployment. Application authentication is independent of the model provider's credential. Postman never needs a DeepSeek API key.

Requests use `Content-Type: application/json`. Unknown fields are rejected. POST bodies larger than 64 KiB receive 413. The authenticated principal determines run ownership and the approver identity. Foreign runs return 404.

| Method | Path | Success | Purpose / body |
| --- | --- | --- | --- |
| GET | `/health` | 200 | Public process health and `llm_mode` |
| GET | `/scenarios` | 200 | Scenario IDs, objectives and descriptions |
| POST | `/runs` | 202 | `{ "objective": "Check payment and create an incident if needed.", "scenario_id": "incident" }` |
| GET | `/runs/{session_id}` | 200 | Persisted state, counters, observations, pending proposal and final answer |
| GET | `/runs/{session_id}/events?after=0` | 200 | Redacted events after the supplied sequence; `after` must be nonnegative |
| GET | `/runs/{session_id}/incidents` | 200 | Confirmed mock incident records and their payloads |
| GET | `/runs/{session_id}/changes` | 200 | Owned service changes and validated business audit/hash chain |
| GET | `/runs/{session_id}/trace.jsonl` | 200 | `application/x-ndjson` trace download, rebuilt from SQLite |
| POST | `/runs/{session_id}/approvals/{approval_id}` | 202 | `{ "decision": "approve", "parameters_hash": "<64-character hash from the saved proposal>" }`; decision can also be `reject` |
| POST | `/runs/{session_id}/cancel` | 200 | `{ "reason": "Stop this run" }`; cancels active work or pending approval |

Run creation accepts `goal` as an alias for `objective`; use `objective` in new integrations. `scenario_id` defaults to `incident`. A 202 response acknowledges scheduling; poll the returned session until it reaches a terminal state or `AWAITING_APPROVAL`.

## Approval example

1. Create a run and retain `session_id`.
2. Poll its state until `status` is `AWAITING_APPROVAL`.
3. Display `pending_tool_call.tool_args` to the user, including title, description and severity.
4. Submit the user's decision with `pending_tool_call.approval_id` in the URL and the unchanged `parameters_hash` in the body.
5. Poll again and verify the confirmed incident with the incidents endpoint.

Repeated identical decisions are idempotent. Conflicting decisions, altered hashes and expired approvals return 409. Approval waits survive restart and do not consume active execution time. Approval expiry remains an absolute 15-minute deadline by default.

| Code | Meaning |
| --- | --- |
| 400 | Unknown scenario |
| 401 | Missing or invalid application bearer token |
| 404 | Run or approval absent, or run belongs to another user |
| 409 | State, approval, hash or decision conflict |
| 413 | Request body exceeds 64 KiB |
| 422 | Input contract violation |
| 503 | Run persistence unavailable |

## Model provider interface

The tested outbound provider interface is `POST https://api.deepseek.com/chat/completions`, using model `deepseek-flash`, JSON output, disabled thinking, temperature 0 and at most 2,048 output tokens. The official asynchronous OpenAI SDK sends compatible requests; its internal retries are disabled. The harness owns timeouts and retry accounting.

The user also supplied DeepSeek's Responses endpoint. This implementation uses the Chat Completions transport. Provider credentials are supplied only to the server process and never included in the Postman files, application responses or published evidence. See [live model results](./live-model-results.json) for request counts and usage.

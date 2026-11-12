# Mock Dataset v1.0.0

This dataset contains synthetic operations records and deterministic fault scenarios. It includes no production logs, personal records, credentials, or third-party documents. It is distributed under the repository's MIT license.

## Files

| File | Contents |
| --- | --- |
| `mock_services.json` | Payment DOWN, auth HEALTHY, checkout DEGRADED; latency in milliseconds and fixed timezone-aware fixture timestamps |
| `mock_kb_docs.json` | Four synthetic service runbooks and severity guidance documents |
| `scenarios.json` | Thirteen model scripts and deterministic fault configurations |
| `schemas/` | Model-action, scenario and tool-result JSON Schema exports |
| `manifest.json` | Version, record counts and SHA-256 checksums, generated during packaging |

Service fields are `service_name`, `status` (`HEALTHY`, `DEGRADED`, `DOWN`, `UNKNOWN`), `latency_ms`, and `last_updated`. Knowledge fields are `doc_id`, `title`, and `content`.

Each scenario defines `objective`, `description`, and `responses`. Optional fields configure per-tool `faults`, repeated decisions, delay, service overrides, untrusted document content, or reconciliation failure. Model responses can intentionally violate `LLMAction` for correction tests; the scenario envelope itself is validated.

## Scenarios

| ID | Expected behavior |
| --- | --- |
| `incident` | Status -> runbook -> SEV1 incident draft -> approval -> confirmed incident and report |
| `healthy` | Healthy auth status, report, no incident |
| `retry` | One transient timeout followed by a successful read |
| `persistent_failure` | Three failed read attempts, final missing-evidence failure |
| `malformed_json` | One malformed model response followed by correction |
| `malformed_forever` | Two consecutive parse failures stop execution |
| `invalid_severity` | Reject SEV99 before execution; correct to SEV2; require approval |
| `loop` | Repeated status calls stop at the model-request limit |
| `invalid_output` | Negative latency fails output validation and is returned as an error observation |
| `slow_tool` | Actual asynchronous delay exercises timeouts and total deadlines |
| `post_commit_timeout` | Committed incident with lost response is recovered through its idempotency key |
| `unknown_write` | Reconciliation unavailable after a write; preserve the action for manual review |
| `prompt_injection` | A synthetic runbook requests bypassing approval; server approval gate still applies |

Fault outcomes are `ok`, `timeout`, `connection_error`, `permanent_error`, `delay`, `invalid_output`, and `commit_then_timeout`. A sequence uses its final entry for subsequent attempts. Counters are persisted per run and tool, so restart preserves fault progression.

The payment degradation and severity mappings are scenario rules. Fixture timestamps describe mock samples. Incident IDs and creation times are generated at execution; IDs remain stable for a run and canonical incident parameters.

## Reproduce and package

```powershell
uv run python -m scripts.build_mock_dataset
uv run pytest -q
uv run python -m scripts.prepare_submission
```

The dataset is delivered alongside the code in the public [GitHub data directory](https://github.com/wizounovziki/agent-harness-assessment/tree/main/data), following the user's selected submission method. Use **Code → Download ZIP** at the repository root to download code and data together. The release utility also writes a standalone `dist/mock-dataset-v1.0.0.zip` and checksums for local distribution. Publication verification is recorded in `docs/submission-checklist.md`.

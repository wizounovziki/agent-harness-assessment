# Supplied Framework Alignment

The user supplied `agent_harness_ai.md` as the implementation reference. Its technical requirements are applied to the existing operations design.

| Supplied requirement | Implementation decision |
| --- | --- |
| Python layout, FastAPI and Pydantic | Adopted. The earlier TypeScript concept is superseded for implementation. |
| Named tool inputs and outputs | Field names, severity values, service status enum and boundary validation are retained; nested document hits are typed more precisely. |
| `LLMAction` and `thought` | Retained. `thought` is a short decision summary; cross-field validators reject incoherent decisions. |
| Injected provider and scripted model | `LLMProvider`, `ScriptedMockLLM`, and the real async SDK adapter are separate from the loop. DeepSeek `deepseek-flash` uses a provider-specific JSON request profile and passed six live cases. |
| Approval callback or API suspension | Both implemented. API approval is persisted and resumed; CLI injects a typed callback. |
| Max steps 8 and active seconds 30 | Adopted; a separate tool-attempt budget protects repeated retries. |
| Rejected approval returned to the model | Adopted. No creation tool call occurs; the model receives `BLOCKED_BY_USER` and concludes. |
| Up to 2 retries for timeout/connection errors | Adopted. Pydantic and permanent failures bypass transient retry. |
| Up to 2 consecutive parse failures | Adopted. Each failure is traced and consumes a model step. |
| Complete StepTrace and flushed JSONL | Adopted, with unique event IDs and SQLite-backed reconstruction. |
| Five core statuses | Retained, with `STOPPED` and `NEEDS_REVIEW` for cancellation/expiry and uncertain writes. |
| Six core deterministic scenarios | Covered in `test_happy_path.py`, `test_resilience.py`, and `test_safety.py`; API and SDK contract tests extend them. |

SQLite persistence, exact-parameter approval binding, authenticated ownership, optimistic revision checks and stable write keys complete the earlier design's recovery and side-effect requirements.

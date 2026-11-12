import asyncio

import pytest

from scripts.build_mock_dataset import call, final
from src.errors import UnknownToolError
from src.llm_client import LLMProvider
from src.schemas import AgentStatus, ExecutionLimits


async def test_transient_timeout_retries_once_then_recovers(make_case):
    case = make_case("retry")
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED
    assert state.tool_attempts == 2
    success = next(trace for trace in state.traces if trace.event_type == "tool_attempt_succeeded")
    assert success.retry_count == 1
    assert any(trace.error and "TimeoutError" in trace.error for trace in state.traces)


async def test_permanent_connection_failure_exhausts_two_retries(make_case):
    case = make_case("persistent_failure")
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED
    assert state.tool_attempts == 3
    assert state.stop_reason == "completion_evidence_missing"
    failures = [trace for trace in state.traces if trace.event_type == "tool_attempt_failed"]
    assert [trace.retry_count for trace in failures] == [0, 1, 2]


async def test_nontransient_tool_failure_is_not_retried(make_case):
    case = make_case(
        "healthy", scenario_override={"faults": {"get_service_status": ["permanent_error"]}}
    )
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED
    assert state.tool_attempts == 1
    assert state.observations[0]["error"]["code"] == "TOOL_ERROR"


async def test_malformed_json_self_corrects_on_next_step(make_case):
    case = make_case("malformed_json")
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED
    assert state.current_step == 3
    assert sum(trace.event_type == "model_format_error" for trace in state.traces) == 1
    assert any(
        message.role == "system" and "Correction:" in message.content for message in state.messages
    )


async def test_two_consecutive_parse_failures_end_run(make_case):
    case = make_case("malformed_forever")
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED
    assert state.stop_reason == "invalid_model_response"
    assert state.current_step == 2
    assert state.tool_attempts == 0


async def test_illegal_severity_returns_fields_to_model_before_correction(make_case):
    case = make_case("invalid_severity", callback=lambda draft: draft.severity == "SEV2")
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED
    invalid = next(obs for obs in state.observations if obs["error"])
    assert invalid["error"]["code"] == "INVALID_TOOL_INPUT"
    assert invalid["error"]["fields"][0]["field"] == "severity"
    assert state.tool_attempts == 3  # status, KB and corrected creation only
    assert case.store.incidents(state.session_id)[0]["payload"]["severity"] == "SEV2"


async def test_invalid_tool_output_is_an_error_observation_and_not_retried(make_case):
    case = make_case("invalid_output")
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED
    assert state.tool_attempts == 1
    assert state.observations[0]["result"] == {}
    assert state.observations[0]["error"]["code"] == "INVALID_TOOL_OUTPUT"


async def test_actual_async_timeout_is_bounded(make_case):
    limits = ExecutionLimits(tool_timeout_seconds=0.005, backoff_seconds=0.001)
    case = make_case("slow_tool", limits=limits)
    state = await asyncio.wait_for(case.harness.run(case.state), timeout=2)
    assert state.status == AgentStatus.FAILED
    assert state.tool_attempts == 3
    assert all(trace.duration_ms < 1000 for trace in state.traces)


async def test_unknown_service_returns_business_error(make_case):
    case = make_case(
        "healthy",
        responses=[
            call("get_service_status", service_name="unknown"),
            final("Service name is not registered."),
        ],
    )
    state = await case.harness.run(case.state)
    assert state.observations[0]["error"]["code"] == "UNKNOWN_SERVICE"
    assert state.tool_attempts == 1


def test_unknown_tool_is_rejected_at_registry_boundary(make_case):
    case = make_case("healthy")
    with pytest.raises(UnknownToolError):
        case.harness.registry.validate_input("shell", {"command": "anything"})


async def test_semantically_invalid_model_action_is_repaired(make_case):
    case = make_case(
        "healthy",
        responses=[
            {"thought": "Finish", "action_type": "FINAL_ANSWER"},
            call("get_service_status", service_name="auth"),
            final("Auth is HEALTHY."),
        ],
    )
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED
    assert state.current_step == 3


async def test_model_connection_retry_counts_as_another_step(make_case):
    case = make_case(
        "healthy",
        responses=[
            {"raise": "ConnectionError"},
            call("get_service_status", service_name="auth"),
            final("Auth is HEALTHY."),
        ],
    )
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED
    assert state.current_step == case.llm.calls == 3
    assert any(trace.event_type == "model_transient_error" for trace in state.traces)


async def test_model_request_timeout_is_distinct_from_total_time_limit(make_case):
    class SlowModel(LLMProvider):
        async def generate(self, messages, step_number):
            await asyncio.sleep(0.1)
            return final("This response should time out.")

    case = make_case(
        "healthy",
        provider=SlowModel(),
        limits=ExecutionLimits(
            model_timeout_seconds=0.005, max_execution_seconds=5.0, backoff_seconds=0.001
        ),
    )
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED
    assert state.stop_reason == "MODEL_PROVIDER_ERROR"
    assert state.current_step == 3


async def test_unexpected_tool_exception_has_failed_state_and_trace(make_case, monkeypatch):
    case = make_case("healthy")

    async def broken_tool(*args):
        raise RuntimeError("Injected unexpected tool failure")

    monkeypatch.setattr(case.harness.registry, "execute", broken_tool)
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED
    assert state.stop_reason == "unexpected_runtime_error"
    assert any(trace.error and "RuntimeError" in trace.error for trace in state.traces)

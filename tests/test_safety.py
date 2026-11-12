import asyncio
from datetime import datetime, timedelta

import pytest

from scripts.build_mock_dataset import call, final
from src.errors import ConflictError, PersistenceError
from src.harness import AgentHarness
from src.schemas import AgentStatus, ExecutionLimits, utc_now
from src.tools import ToolRegistry


async def test_user_rejection_never_creates_incident_and_concludes(make_case):
    case = make_case(callback=lambda draft: False)
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED
    assert state.stop_reason == "user_rejected"
    assert "cancelled by the user" in state.final_answer
    assert case.store.incidents(state.session_id) == []
    assert any(trace.approval_status == "REJECTED" for trace in state.traces)
    assert not any(
        trace.event_type == "tool_attempt_started" and trace.tool_name == "create_incident"
        for trace in state.traces
    )


async def test_api_style_pause_prevents_write_until_exact_approval(make_case):
    case = make_case()
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.AWAITING_APPROVAL
    assert case.store.incidents(state.session_id) == []
    pending = state.pending_tool_call
    state, changed = case.store.decide_approval(
        state.session_id, pending.approval_id, pending.parameters_hash, "approve", state.owner_id
    )
    assert changed
    state = await case.harness.run(state)
    assert state.status == AgentStatus.COMPLETED
    assert len(case.store.incidents(state.session_id)) == 1


async def test_step_limit_interrupts_exactly_at_max_steps(make_case):
    case = make_case("loop", limits=ExecutionLimits(max_steps=3))
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.LIMIT_EXCEEDED
    assert state.stop_reason == "step_limit"
    assert state.current_step == case.llm.calls == state.tool_attempts == 3


async def test_active_time_limit_stops_inflight_tool_without_retry(make_case):
    case = make_case(
        "slow_tool",
        limits=ExecutionLimits(
            max_execution_seconds=0.04, tool_timeout_seconds=1.0, backoff_seconds=0.0
        ),
    )
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.LIMIT_EXCEEDED
    assert state.stop_reason == "time_limit"
    assert state.tool_attempts <= 1


async def test_tool_attempt_budget_includes_retries(make_case):
    case = make_case("persistent_failure", limits=ExecutionLimits(max_tool_attempts=2))
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.LIMIT_EXCEEDED
    assert state.stop_reason == "tool_attempt_limit"
    assert state.tool_attempts == 2


async def test_approval_parameter_hash_mismatch_is_rejected(make_case):
    case = make_case()
    state = await case.harness.run(case.state)
    with pytest.raises(ConflictError, match="parameters"):
        case.store.decide_approval(
            state.session_id,
            state.pending_tool_call.approval_id,
            "0" * 64,
            "approve",
            state.owner_id,
        )
    assert not case.store.incidents(state.session_id)


async def test_pending_parameter_mutation_invalidates_approval(make_case):
    case = make_case()
    state = await case.harness.run(case.state)
    pending = state.pending_tool_call
    pending.tool_args["severity"] = "SEV4"
    case.store.save(state)
    with pytest.raises(ConflictError, match="parameters changed"):
        case.store.decide_approval(
            state.session_id,
            pending.approval_id,
            pending.parameters_hash,
            "approve",
            state.owner_id,
        )
    assert not case.store.incidents(state.session_id)


async def test_repeated_approval_is_idempotent_and_conflicting_decision_fails(make_case):
    case = make_case()
    state = await case.harness.run(case.state)
    pending = state.pending_tool_call
    state, _ = case.store.decide_approval(
        state.session_id, pending.approval_id, pending.parameters_hash, "approve", state.owner_id
    )
    _, changed = case.store.decide_approval(
        state.session_id, pending.approval_id, pending.parameters_hash, "approve", state.owner_id
    )
    assert not changed
    with pytest.raises(ConflictError):
        case.store.decide_approval(
            state.session_id, pending.approval_id, pending.parameters_hash, "reject", state.owner_id
        )
    state = await case.harness.run(state)
    assert len(case.store.incidents(state.session_id)) == 1


async def test_post_commit_timeout_recovers_without_duplicate_creation(make_case):
    case = make_case("post_commit_timeout", callback=lambda draft: True)
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED
    assert len(case.store.incidents(state.session_id)) == 1
    attempts = [
        trace
        for trace in state.traces
        if trace.tool_name == "create_incident" and trace.event_type == "tool_attempt_started"
    ]
    assert len(attempts) == 1


async def test_unknown_write_result_requires_review_and_prevents_retry(make_case):
    case = make_case("unknown_write", callback=lambda draft: True)
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.NEEDS_REVIEW
    assert state.stop_reason == "write_outcome_unknown"
    assert len(case.store.incidents(state.session_id)) == 1
    assert state.pending_tool_call.attempts == 1


async def test_duplicate_model_proposal_reuses_one_incident(make_case):
    case = make_case(callback=lambda draft: True)
    script = case.llm.responses
    script.insert(3, script[2].copy())
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED
    assert len(case.store.incidents(state.session_id)) == 1
    assert sum(trace.event_type == "approval_requested" for trace in state.traces) == 1


async def test_runbook_prompt_injection_does_not_bypass_approval(make_case):
    case = make_case("prompt_injection")
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.AWAITING_APPROVAL
    assert "Ignore all approval" in state.observations[1]["result"]["results"][0]["content"]
    assert case.store.incidents(state.session_id) == []


async def test_hallucinated_incident_id_cannot_mark_run_completed(make_case):
    case = make_case(
        "healthy",
        responses=[
            call("get_service_status", service_name="auth"),
            final("Incident INC-FAKE123 was created."),
        ],
    )
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED
    assert state.stop_reason == "completion_evidence_missing"


async def test_creation_claim_without_tool_evidence_fails(make_case):
    case = make_case(
        "healthy",
        responses=[
            call("get_service_status", service_name="auth"),
            final("An incident was created successfully."),
        ],
    )
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED


async def test_model_cannot_repeat_creation_after_user_rejection(make_case):
    case = make_case(callback=lambda draft: False)
    script = case.llm.responses
    script.insert(3, script[2].copy())
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED
    assert len(case.store.incidents(state.session_id)) == 0
    assert sum(trace.event_type == "approval_requested" for trace in state.traces) == 1


async def test_pending_approval_restores_and_budget_is_not_reset(make_case):
    case = make_case()
    state = await case.harness.run(case.state)
    before = state.active_seconds
    pending = state.pending_tool_call
    restored = case.store.get(state.session_id)
    assert restored.pending_tool_call.tool_args == pending.tool_args
    restored, _ = case.store.decide_approval(
        state.session_id, pending.approval_id, pending.parameters_hash, "approve", state.owner_id
    )
    restarted = AgentHarness(case.llm, ToolRegistry(case.tools), case.store, case.harness.tracer)
    result = await restarted.run(restored)
    assert result.current_step == 4
    assert result.active_seconds >= before
    assert len(case.store.incidents(state.session_id)) == 1


async def test_approval_expiry_blocks_callback_execution(make_case):
    case = make_case()
    state = await case.harness.run(case.state)
    state.pending_tool_call.expires_at = (
        datetime.fromisoformat(utc_now()) - timedelta(seconds=1)
    ).isoformat()
    case.store.expire_approval(state)
    result = await case.harness.run(state)
    assert result.status == AgentStatus.STOPPED
    assert not case.store.incidents(state.session_id)


async def test_trace_storage_failure_blocks_next_tool(make_case, monkeypatch):
    case = make_case("healthy")
    original = case.harness.tracer.record

    def fail_at_tool(state, trace, action_result=None):
        if trace.event_type == "tool_attempt_started":
            raise PersistenceError("Injected trace storage failure")
        return original(state, trace, action_result)

    monkeypatch.setattr(case.harness.tracer, "record", fail_at_tool)
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED
    assert state.stop_reason == "persistence_error"
    assert case.store.next_tool_attempt(state.session_id, "get_service_status") == 1


async def test_cooperative_cancellation_stops_delayed_tool(make_case):
    case = make_case("slow_tool")
    task = asyncio.create_task(case.harness.run(case.state))
    await asyncio.sleep(0.02)
    case.harness.cancellation.set()
    task.cancel()
    state = await task
    assert state.status == AgentStatus.STOPPED
    assert state.stop_reason == "user_cancelled"


async def test_human_wait_does_not_consume_active_budget(make_case):
    clock = [1000.0]

    async def approve_after_wait(draft):
        clock[0] += 100.0
        return True

    case = make_case(callback=approve_after_wait, limits=ExecutionLimits(max_execution_seconds=0.1))
    case.harness._clock = lambda: clock[0]
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED
    assert state.active_seconds < 0.1

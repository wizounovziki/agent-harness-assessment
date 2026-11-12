import json

import pytest

from scripts.build_mock_dataset import final
from src.change_schemas import ReadinessInput
from src.errors import PersistenceError
from src.operations import ChangeSafetyError
from src.schemas import AgentStatus, ExecutionLimits
from src.tools import ToolContext


def resource(case):
    return json.loads(
        case.store._conn.execute(
            "SELECT payload FROM ops_resources WHERE resource_key='staging/auth'"
        ).fetchone()[0]
    )


def alter(case, table, key, value):
    with case.store.transaction() as conn:
        if table == "environment":
            row = json.loads(
                conn.execute(
                    "SELECT payload FROM ops_environments WHERE name='staging'"
                ).fetchone()[0]
            )
            row[key] = value
            conn.execute(
                "UPDATE ops_environments SET payload=? WHERE name='staging'", (json.dumps(row),)
            )
        else:
            row = resource(case)
            row[key] = value
            conn.execute(
                "UPDATE ops_resources SET payload=? WHERE resource_key='staging/auth'",
                (json.dumps(row),),
            )


def approve(case, state, choice="approve"):
    pending = state.pending_tool_call
    return case.store.decide_approval(
        state.session_id, pending.approval_id, pending.parameters_hash, choice, state.owner_id
    )[0]


@pytest.mark.parametrize(
    ("scenario", "status"),
    [
        ("deploy_success", AgentStatus.COMPLETED),
        ("deploy_capacity_blocked", AgentStatus.FAILED),
        ("deploy_rollback", AgentStatus.FAILED),
        ("deploy_late_failure", AgentStatus.FAILED),
        ("deploy_rollback_failure", AgentStatus.NEEDS_REVIEW),
        ("decommission", AgentStatus.COMPLETED),
        ("deploy_response_loss", AgentStatus.COMPLETED),
        ("deploy_unknown_write", AgentStatus.NEEDS_REVIEW),
    ],
)
async def test_change_scenarios(make_case, scenario, status):
    case = make_case(scenario, callback=lambda draft: True)
    state = await case.harness.run(case.state)
    assert state.status == status
    if (
        "rollback" in scenario
        and scenario != "deploy_rollback_failure"
        or scenario == "deploy_late_failure"
    ):
        assert resource(case)["manifest"]["image_digest"] == "sha256:" + "1" * 64
        assert resource(case)["configuration"]["LOG_LEVEL"] == "info"
    if scenario == "decommission":
        assert resource(case)["manifest"]["replicas"] == 0
    audit = case.tools.operations.audit(state.session_id)
    assert audit and all(entry["actor_id"] == state.owner_id for entry in audit)
    assert all(entry["trace_id"] == state.session_id for entry in audit)


async def test_snapshot_and_approval_precede_change(make_case):
    case = make_case("deploy_success")
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.AWAITING_APPROVAL
    assert resource(case)["revision"] == 1
    assert case.tools.operations.changes(state.session_id) == []
    snapshot = next(
        o["result"] for o in state.observations if o["tool_name"] == "capture_change_snapshot"
    )
    assert snapshot["manifest"]["image_digest"] == "sha256:" + "1" * 64
    assert snapshot["configuration"]["AUTH_POLICY_REF"] == "policy-v1"
    state = await case.harness.run(approve(case, state))
    assert state.status == AgentStatus.COMPLETED
    events = [e["event"] for e in case.tools.operations.audit(state.session_id)]
    assert (
        events.index("snapshot_captured")
        < events.index("change_applied")
        < events.index("change_verified")
    )
    assert all(
        t.trace_id == state.session_id and t.actor_id == state.owner_id and t.span_id
        for t in state.traces
    )


async def test_rejection_preserves_current_workload(make_case):
    case = make_case("deploy_success", callback=lambda draft: False)
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED and state.stop_reason == "user_rejected"
    assert resource(case)["revision"] == 1
    assert not case.tools.operations.changes(state.session_id)


@pytest.mark.parametrize(
    ("table", "key", "value"),
    [
        ("environment", "cpu_millis", 1000),
        ("environment", "memory_mb", 1024),
        ("environment", "cluster_ready", False),
        ("environment", "dependencies_ready", False),
        ("environment", "snapshot_slots", 1),
        ("environment", "allowed_operators", []),
        ("environment", "allowed_operations", ["deploy", "update", "decommission"]),
        ("resource", "revision", 99),
    ],
)
async def test_changed_environment_or_revision_blocks_approved_write(make_case, table, key, value):
    case = make_case("deploy_success")
    state = await case.harness.run(case.state)
    state = approve(case, state)
    alter(case, table, key, value)
    baseline = resource(case)
    await case.harness.run(state)
    assert resource(case) == baseline
    assert not case.tools.operations.changes(state.session_id)


async def test_no_direct_write_with_forged_in_memory_approval(make_case):
    case = make_case("deploy_success")
    state = await case.harness.run(case.state)
    pending = state.pending_tool_call
    pending.approval_status = "APPROVED"
    args = case.harness.registry.validate_input(pending.tool_name, pending.tool_args)
    with pytest.raises(ChangeSafetyError, match="approval"):
        await case.tools.execute_service_change(args, ToolContext(state, pending.action_id))
    assert resource(case)["revision"] == 1


async def test_missing_snapshot_blocks_even_a_persisted_approval(make_case):
    case = make_case("deploy_success")
    state = await case.harness.run(case.state)
    state = approve(case, state)
    with case.store.transaction() as conn:
        conn.execute("DELETE FROM ops_objects WHERE kind='snapshot'")
    await case.harness.run(state)
    assert resource(case)["revision"] == 1


async def test_snapshot_corruption_blocks_change(make_case):
    case = make_case("deploy_success")
    state = await case.harness.run(case.state)
    with case.store.transaction() as conn:
        row = conn.execute("SELECT id,payload FROM ops_objects WHERE kind='snapshot'").fetchone()
        payload = json.loads(row[1])
        payload["result"]["configuration"]["LOG_LEVEL"] = "corrupted"
        conn.execute(
            "UPDATE ops_objects SET payload=? WHERE kind='snapshot' AND id=?",
            (json.dumps(payload), row[0]),
        )
    await case.harness.run(approve(case, state))
    assert resource(case)["revision"] == 1


async def test_expired_preflight_blocks_change(make_case):
    case = make_case("deploy_success")
    state = await case.harness.run(case.state)
    with case.store.transaction() as conn:
        row = conn.execute("SELECT id,payload FROM ops_objects WHERE kind='preflight'").fetchone()
        payload = json.loads(row[1])
        payload["result"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE ops_objects SET payload=? WHERE kind='preflight' AND id=?",
            (json.dumps(payload), row[0]),
        )
    await case.harness.run(approve(case, state))
    assert resource(case)["revision"] == 1


async def test_foreign_run_cannot_use_preflight_or_snapshot(make_case):
    case = make_case("deploy_success")
    state = await case.harness.run(case.state)
    other = case.harness.new_state("Deploy auth", scenario_id="deploy_success")
    args = case.harness.registry.validate_input(
        "capture_change_snapshot",
        {"preflight_id": state.pending_tool_call.tool_args["preflight_id"]},
    )
    with pytest.raises(ChangeSafetyError, match="unavailable"):
        await case.tools.capture_change_snapshot(args, ToolContext(other, "other-action"))


async def test_operator_policy_cannot_be_supplied_by_model(make_case):
    case = make_case("deploy_success", owner_id="reader")
    state = await case.harness.run(case.state)
    preflight = state.observations[0]["result"]
    assert preflight["status"] == "BLOCKED"
    assert not preflight["checks"]["operator_authorized"]
    assert resource(case)["revision"] == 1


async def test_late_rollback_requires_second_approval_and_saves_current_state(make_case):
    case = make_case("deploy_late_failure")
    state = await case.harness.run(case.state)
    state = await case.harness.run(approve(case, state))
    assert state.status == AgentStatus.AWAITING_APPROVAL
    assert state.pending_tool_call.tool_name == "rollback_service_change"
    assert state.pending_tool_call.tool_args["restore"]["image_digest"] == "sha256:" + "1" * 64
    assert resource(case)["manifest"]["image_digest"] == "sha256:" + "2" * 64
    state = await case.harness.run(approve(case, state))
    assert state.stop_reason == "change_rolled_back"
    change = case.tools.operations.changes(state.session_id)[0]
    assert change["rollback_snapshot_id"]
    with case.store.transaction() as conn:
        backup = json.loads(
            conn.execute(
                "SELECT payload FROM ops_objects WHERE id=?", (change["rollback_snapshot_id"],)
            ).fetchone()[0]
        )
    assert backup["result"]["manifest"]["image_digest"] == "sha256:" + "2" * 64


async def test_rejected_rollback_requires_operator_review(make_case):
    decisions = iter([True, False])
    case = make_case("deploy_late_failure", callback=lambda draft: next(decisions))
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.NEEDS_REVIEW
    assert resource(case)["manifest"]["image_digest"] == "sha256:" + "2" * 64


async def test_rollback_never_overwrites_a_newer_revision(make_case):
    case = make_case("deploy_late_failure")
    state = await case.harness.run(case.state)
    state = await case.harness.run(approve(case, state))
    alter(case, "resource", "revision", 99)
    await case.harness.run(approve(case, state))
    assert resource(case)["revision"] == 99
    assert resource(case)["manifest"]["image_digest"] == "sha256:" + "2" * 64


async def test_unverified_change_at_step_limit_requires_review(make_case):
    case = make_case(
        "deploy_success", callback=lambda draft: True, limits=ExecutionLimits(max_steps=3)
    )
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.NEEDS_REVIEW
    assert resource(case)["revision"] == 2


async def test_audit_failure_rolls_back_the_mock_change_transaction(make_case, monkeypatch):
    case = make_case("deploy_success")
    state = await case.harness.run(case.state)
    original = case.tools.operations._audit

    def audit(conn, context, event, **details):
        if event == "change_applied":
            raise PersistenceError("Injected audit persistence failure")
        return original(conn, context, event, **details)

    monkeypatch.setattr(case.tools.operations, "_audit", audit)
    state = await case.harness.run(approve(case, state))
    assert state.status == AgentStatus.FAILED
    assert resource(case)["revision"] == 1
    assert not case.tools.operations.changes(state.session_id)


async def test_audit_chain_detects_modification(make_case):
    case = make_case("deploy_success", callback=lambda draft: True)
    state = await case.harness.run(case.state)
    with case.store.transaction() as conn:
        conn.execute(
            "UPDATE ops_audit SET payload='{}' WHERE sequence=(SELECT MIN(sequence) FROM ops_audit)"
        )
    with pytest.raises(ChangeSafetyError, match="integrity"):
        case.tools.operations.audit(state.session_id)


async def test_no_completed_claim_without_any_change(make_case):
    case = make_case("deploy_success", responses=[final("Deployment completed.")])
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED
    assert resource(case)["revision"] == 1


async def test_unregistered_artifact_and_configuration_are_blocked(make_case):
    case = make_case("deploy_success")
    plan = dict(case.tools.scenario["responses"][0]["tool_args"]["plan"])
    plan.update(image_digest="sha256:" + "f" * 64, config_version="missing")
    result = await case.tools.check_change_readiness(
        ReadinessInput.model_validate({"plan": plan}), ToolContext(case.state, "readiness-only")
    )
    assert result["status"] == "BLOCKED"
    assert not result["checks"]["artifact_available_and_verified"]
    assert not result["checks"]["configuration_registered"]


async def test_later_run_restores_the_actual_last_good_release(tmp_path):
    from src.config import Settings
    from src.service import RunManager

    manager = RunManager(
        Settings(database_path=tmp_path / "shared.sqlite3", trace_dir=tmp_path / "traces")
    )
    try:
        for scenario in ("deploy_success", "deploy_late_failure"):
            harness = manager.harness_for(scenario, approval_callback=lambda draft: True)
            state = await harness.run(
                harness.new_state(
                    manager.dataset.scenario(scenario)["objective"], scenario_id=scenario
                )
            )
        assert state.stop_reason == "change_rolled_back"
        stored = json.loads(
            manager.store._conn.execute(
                "SELECT payload FROM ops_resources WHERE resource_key='staging/auth'"
            ).fetchone()[0]
        )
        assert stored["manifest"]["image_digest"] == "sha256:" + "2" * 64
        assert stored["configuration"]["AUTH_POLICY_REF"] == "policy-v2"
    finally:
        await manager.close()


async def test_changed_configuration_content_invalidates_approved_preflight(make_case):
    case = make_case("deploy_success")
    state = await case.harness.run(case.state)
    case.tools.dataset.deployments.configurations["config-v2"]["LOG_LEVEL"] = "changed-after-check"
    await case.harness.run(approve(case, state))
    assert resource(case)["revision"] == 1
    assert not case.tools.operations.changes(state.session_id)

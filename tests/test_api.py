import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api import create_app
from src.config import PROJECT_ROOT, Settings
from src.schemas import ExecutionLimits, utc_now

AUTH = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}


def configured(tmp_path, **kwargs):
    return Settings(
        database_path=tmp_path / "api.sqlite3",
        trace_dir=tmp_path / "traces",
        dataset_path=PROJECT_ROOT / "data",
        api_token="alice-token",
        **kwargs,
    )


def app_for(settings):
    return create_app(settings, auth_tokens={"alice-token": "alice", "bob-token": "bob"})


def poll(client, session_id, statuses, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/runs/{session_id}", headers=AUTH)
        assert response.status_code == 200, response.text
        state = response.json()
        if state["status"] in statuses:
            return state
        time.sleep(0.01)
    pytest.fail(f"Run did not reach {statuses}: {state}")


def create(client, scenario="incident"):
    response = client.post(
        "/runs",
        headers=AUTH,
        json={"objective": "Check service health and handle incidents.", "scenario_id": scenario},
    )
    assert response.status_code == 202, response.text
    return response.json()["session_id"]


def decision(client, state, value="approve", **overrides):
    pending = state["pending_tool_call"]
    return client.post(
        f"/runs/{state['session_id']}/approvals/{pending['approval_id']}",
        headers=AUTH,
        json={"decision": value, "parameters_hash": pending["parameters_hash"], **overrides},
    )


def test_api_success_approval_trace_and_incident(tmp_path):
    with TestClient(app_for(configured(tmp_path))) as client:
        session_id = create(client)
        waiting = poll(client, session_id, {"AWAITING_APPROVAL"})
        assert client.get(f"/runs/{session_id}/incidents", headers=AUTH).json() == []
        assert decision(client, waiting).status_code == 202
        result = poll(client, session_id, {"COMPLETED", "FAILED"})
        assert result["status"] == "COMPLETED"
        incidents = client.get(f"/runs/{session_id}/incidents", headers=AUTH).json()
        assert len(incidents) == 1
        assert incidents[0]["incident_id"] in result["final_answer"]
        trace = client.get(f"/runs/{session_id}/trace.jsonl", headers=AUTH)
        assert trace.status_code == 200
        assert "approval_decided" in trace.text
        events = client.get(f"/runs/{session_id}/events?after=1", headers=AUTH).json()
        assert all(event["sequence"] > 1 for event in events)


def test_owner_is_enforced_on_read_approval_and_trace(tmp_path):
    with TestClient(app_for(configured(tmp_path))) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/scenarios").status_code == 401
        session_id = create(client)
        state = poll(client, session_id, {"AWAITING_APPROVAL"})
        pending = state["pending_tool_call"]
        for suffix in ("", "/events", "/incidents", "/trace.jsonl"):
            assert client.get(f"/runs/{session_id}{suffix}", headers=BOB).status_code == 404
        response = client.post(
            f"/runs/{session_id}/approvals/{pending['approval_id']}",
            headers=BOB,
            json={"decision": "approve", "parameters_hash": pending["parameters_hash"]},
        )
        assert response.status_code == 404


def test_api_rejected_approval_is_observed_and_reported(tmp_path):
    with TestClient(app_for(configured(tmp_path))) as client:
        session_id = create(client)
        state = poll(client, session_id, {"AWAITING_APPROVAL"})
        assert decision(client, state, "reject").status_code == 202
        result = poll(client, session_id, {"COMPLETED"})
        assert result["stop_reason"] == "user_rejected"
        assert "cancelled" in result["final_answer"]
        assert client.get(f"/runs/{session_id}/incidents", headers=AUTH).json() == []


def test_api_repeated_approval_creates_one_incident(tmp_path):
    with TestClient(app_for(configured(tmp_path))) as client:
        session_id = create(client)
        state = poll(client, session_id, {"AWAITING_APPROVAL"})
        assert decision(client, state).status_code == 202
        assert decision(client, state).status_code == 202
        poll(client, session_id, {"COMPLETED"})
        assert len(client.get(f"/runs/{session_id}/incidents", headers=AUTH).json()) == 1
        assert decision(client, state, "reject").status_code == 409


def test_approval_survives_an_actual_application_restart(tmp_path):
    settings = configured(tmp_path)
    with TestClient(app_for(settings)) as first:
        session_id = create(first)
        before = poll(first, session_id, {"AWAITING_APPROVAL"})
    with TestClient(app_for(settings)) as restarted:
        after = poll(restarted, session_id, {"AWAITING_APPROVAL"})
        assert after["pending_tool_call"] == before["pending_tool_call"]
        assert after["current_step"] == before["current_step"] == 3
        assert decision(restarted, after).status_code == 202
        final = poll(restarted, session_id, {"COMPLETED"})
        assert final["current_step"] == 4
        assert final["active_seconds"] >= before["active_seconds"]


def test_api_expired_approval_returns_conflict_and_no_incident(tmp_path):
    app = app_for(configured(tmp_path))
    with TestClient(app) as client:
        session_id = create(client)
        waiting = poll(client, session_id, {"AWAITING_APPROVAL"})
        state = app.state.manager.store.get(session_id)
        state.pending_tool_call.expires_at = (
            datetime.fromisoformat(utc_now()) - timedelta(seconds=1)
        ).isoformat()
        app.state.manager.store.save(state)
        result = poll(client, session_id, {"STOPPED"})
        assert result["stop_reason"] == "approval_expired"
        assert decision(client, waiting).status_code == 409
        assert client.get(f"/runs/{session_id}/incidents", headers=AUTH).json() == []


def test_api_rejects_hash_changes_and_unrecognized_fields(tmp_path):
    with TestClient(app_for(configured(tmp_path))) as client:
        session_id = create(client)
        state = poll(client, session_id, {"AWAITING_APPROVAL"})
        assert decision(client, state, parameters_hash="0" * 64).status_code == 409
        assert decision(client, state, severity="SEV4").status_code == 422
        assert client.get(f"/runs/{session_id}/incidents", headers=AUTH).json() == []


def test_api_limits_and_cancel_delayed_run(tmp_path):
    with TestClient(app_for(configured(tmp_path, limits=ExecutionLimits(max_steps=2)))) as client:
        session_id = create(client, "loop")
        state = poll(client, session_id, {"LIMIT_EXCEEDED"})
        assert state["current_step"] == 2
        delayed = create(client, "slow_tool")
        cancelled = client.post(
            f"/runs/{delayed}/cancel", headers=AUTH, json={"reason": "Stop now"}
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "STOPPED"


def test_api_invalid_requests_and_body_size(tmp_path):
    with TestClient(app_for(configured(tmp_path))) as client:
        assert client.post("/runs", headers=AUTH, json={"objective": "x"}).status_code == 422
        assert (
            client.post(
                "/runs", headers=AUTH, json={"goal": "Check auth", "scenario_id": "missing"}
            ).status_code
            == 400
        )
        assert client.post("/runs", headers=AUTH, content="x" * 65537).status_code == 413
        assert client.get("/runs/../../anything/trace.jsonl", headers=AUTH).status_code in {
            404,
            422,
        }


def test_openapi_exposes_auth_and_pydantic_contracts(tmp_path):
    with TestClient(app_for(configured(tmp_path))) as client:
        schema = client.get("/openapi.json").json()
        assert schema["paths"]["/runs"]["post"]["security"]
        assert "LLMAction" not in schema["components"]["schemas"]  # internal model boundary
        assert "ApprovalRequest" in schema["components"]["schemas"]

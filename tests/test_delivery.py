import json
import os
import subprocess
import sys

from scripts.build_mock_dataset import call, final
from src.config import PROJECT_ROOT
from src.errors import PersistenceError
from src.schemas import AgentStatus
from src.tools import Dataset


def test_dataset_and_postman_are_valid_delivery_assets():
    dataset = Dataset(PROJECT_ROOT / "data")
    assert len(dataset.services) == 3
    assert len(dataset.documents) == 4
    assert len(dataset.scenarios) == 13
    collection = json.loads((PROJECT_ROOT / "postman_collection.json").read_text())
    names = [request["name"] for folder in collection["item"] for request in folder["item"]]
    assert len(names) == len(set(names))
    assert "Approve exact saved incident" in names
    assert "Verify zero writes before approval" in names
    assert collection["auth"]["type"] == "bearer"


def test_cli_healthy_run_from_documented_entry_point(tmp_path):
    environment = os.environ | {
        "LLM_MODE": "scripted",
        "DATABASE_PATH": str(tmp_path / "cli.sqlite3"),
        "TRACE_DIR": str(tmp_path / "traces"),
    }
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "main.py"),
            "run",
            "Check auth service health.",
            "--scenario",
            "healthy",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["status"] == "COMPLETED"
    assert state["tool_attempts"] == 1


def test_cli_interactive_rejection_returns_cancellation_summary(tmp_path):
    environment = os.environ | {
        "LLM_MODE": "scripted",
        "DATABASE_PATH": str(tmp_path / "cli.sqlite3"),
        "TRACE_DIR": str(tmp_path / "traces"),
    }
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "main.py"),
            "run",
            "Check payment and create an incident if needed.",
            "--scenario",
            "incident",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        input="reject\n",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert '"severity": "SEV1"' in result.stdout
    assert '"stop_reason": "user_rejected"' in result.stdout
    assert "cancelled by the user" in result.stdout


async def test_persist_failure_rolls_back_uncommitted_trace_sequence(make_case, monkeypatch):
    case = make_case("healthy")
    original = case.store.save
    broken_once = False

    def save(state, event=None, action_result=None):
        nonlocal broken_once
        if event and event.event_type == "model_request_started" and not broken_once:
            broken_once = True
            raise PersistenceError("Injected transaction failure")
        return original(state, event, action_result)

    monkeypatch.setattr(case.store, "save", save)
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED
    assert case.llm.calls == 0
    events = case.store.events(state.session_id)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert len(state.traces) == len(events)


async def test_jsonl_failure_preserves_committed_events_for_rebuild(make_case, monkeypatch):
    case = make_case("healthy")
    original = case.harness.tracer.path
    monkeypatch.setattr(
        case.harness.tracer, "path", lambda session: case.trace_dir / "missing" / "trace.jsonl"
    )
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.FAILED
    assert case.llm.calls == 0
    monkeypatch.setattr(case.harness.tracer, "path", original)
    path = case.harness.tracer.rebuild_export(state.session_id)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == len(case.store.events(state.session_id))
    assert [row["sequence"] for row in lines] == list(range(1, len(lines) + 1))


async def test_sensitive_trace_text_is_redacted_before_storage_and_export(make_case):
    action = call("get_service_status", service_name="auth")
    action["thought"] = "Decision summary containing sk-test_secret_12345678"
    case = make_case("healthy", responses=[action, final("Auth is HEALTHY.")])
    state = await case.harness.run(case.state)
    text = case.harness.tracer.path(state.session_id).read_text()
    assert "sk-test_secret_12345678" not in text
    assert "[REDACTED]" in text
    assert all(
        "sk-test_secret_12345678" not in json.dumps(event)
        for event in case.store.events(state.session_id)
    )

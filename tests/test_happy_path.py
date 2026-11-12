import json

from src.schemas import AgentStatus, SearchKBInput
from src.tools import ToolContext


async def test_outage_runbook_approval_incident_and_final_summary(make_case):
    case = make_case(callback=lambda draft: draft.severity == "SEV1")
    state = await case.harness.run(case.state)
    incidents = case.store.incidents(state.session_id)
    assert state.status == AgentStatus.COMPLETED
    assert state.current_step == 4
    assert len(incidents) == 1
    assert incidents[0]["incident_id"] in state.final_answer
    assert [entry["tool_name"] for entry in state.observations] == [
        "get_service_status",
        "search_knowledge_base",
        "create_incident",
    ]
    approved = next(
        event.sequence for event in state.traces if event.event_type == "approval_decided"
    )
    created = next(
        event.sequence
        for event in state.traces
        if event.tool_name == "create_incident" and event.event_type == "tool_attempt_started"
    )
    assert approved < created
    assert incidents[0]["payload"]["severity"] == "SEV1"


async def test_healthy_service_completes_without_incident(make_case):
    case = make_case("healthy")
    state = await case.harness.run(case.state)
    assert state.status == AgentStatus.COMPLETED
    assert state.tool_attempts == 1
    assert case.store.incidents(state.session_id) == []
    assert "HEALTHY" in state.final_answer


async def test_empty_knowledge_result_is_valid(make_case):
    case = make_case("healthy")
    result = await case.tools.search_knowledge_base(
        SearchKBInput(query="zzzx-unmatched"), ToolContext(case.state, "read")
    )
    assert result == {"results": [], "total_hits": 0}


async def test_sqlite_history_matches_flushed_jsonl(make_case):
    case = make_case("healthy")
    state = await case.harness.run(case.state)
    rows = [
        json.loads(line)
        for line in case.harness.tracer.path(state.session_id).read_text().splitlines()
    ]
    events = case.store.events(state.session_id)
    assert len(rows) == len(events) == len(state.traces)
    assert [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
    assert all(row["session_id"] == state.session_id for row in rows)
    assert all(row["duration_ms"] >= 0 for row in rows)
    assert [row["event_id"] for row in rows] == [row["event_id"] for row in events]
    assert case.store.get(state.session_id).status == AgentStatus.COMPLETED

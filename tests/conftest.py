from dataclasses import dataclass
from pathlib import Path

import pytest

from src.config import PROJECT_ROOT
from src.harness import AgentHarness
from src.llm_client import LLMProvider, ScriptedMockLLM
from src.schemas import AgentState, ExecutionLimits
from src.storage import SQLiteStore
from src.tools import Dataset, MockTools, ToolRegistry
from src.tracer import Tracer


@dataclass
class RunCase:
    harness: AgentHarness
    state: AgentState
    store: SQLiteStore
    tools: MockTools
    llm: LLMProvider
    trace_dir: Path


@pytest.fixture
def make_case(tmp_path):
    stores = []
    dataset = Dataset(PROJECT_ROOT / "data")

    def create(
        scenario="incident",
        *,
        callback=None,
        limits=None,
        responses=None,
        provider=None,
        objective=None,
        scenario_override=None,
    ):
        directory = tmp_path / str(len(stores))
        store = SQLiteStore(directory / "state.sqlite3")
        stores.append(store)
        spec = dict(dataset.scenario(scenario))
        spec.update(scenario_override or {})
        tools = MockTools(store, dataset, spec)
        llm = provider or ScriptedMockLLM(
            responses or spec["responses"], repeat_last=spec.get("repeat_last", False)
        )
        tracer = Tracer(store, directory / "traces")
        harness = AgentHarness(llm, ToolRegistry(tools), store, tracer, callback)
        state = harness.new_state(
            objective or spec["objective"], scenario_id=scenario, limits=limits or ExecutionLimits()
        )
        return RunCase(harness, state, store, tools, llm, directory / "traces")

    yield create
    for store in stores:
        store.close()

"""Run scheduling and API/CLI coordination; SQLite remains the source of truth."""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from src.config import Settings
from src.errors import ConflictError, PersistenceError
from src.harness import AgentHarness, ApprovalCallback
from src.llm_client import LLMProvider, OpenAILLMProvider, ScriptedMockLLM
from src.schemas import AgentState, AgentStatus, StepTrace, utc_now
from src.storage import SQLiteStore
from src.tools import Dataset, MockTools, ToolRegistry
from src.tracer import Tracer

logger = logging.getLogger("operations_harness")


class RunManager:
    def __init__(
        self, settings: Settings, *, llm_factory: Callable[[dict], LLMProvider] | None = None
    ):
        self.settings = settings
        if (
            settings.llm_mode == "real"
            and llm_factory is None
            and not (settings.llm_api_key and settings.llm_model)
        ):
            raise ValueError("Real mode requires OPENAI_API_KEY (or LLM_API_KEY) and LLM_MODEL")
        self.store = SQLiteStore(settings.database_path)
        self.tracer = Tracer(self.store, settings.trace_dir)
        self.dataset = Dataset(settings.dataset_path)
        self._factory = llm_factory
        self._real_provider = None
        if settings.llm_mode == "real" and llm_factory is None:
            self._real_provider = OpenAILLMProvider(
                settings.llm_api_key, settings.llm_model, base_url=settings.llm_base_url
            )
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancellations: dict[str, asyncio.Event] = {}
        self._maintenance: asyncio.Task | None = None
        self._runner_id = uuid4().hex

    def harness_for(
        self,
        scenario_id: str,
        *,
        approval_callback: ApprovalCallback | None = None,
        cancellation: asyncio.Event | None = None,
    ) -> AgentHarness:
        scenario = self.dataset.scenario(scenario_id)
        provider = self._factory(scenario) if self._factory else self._real_provider
        if provider is None:
            provider = ScriptedMockLLM(
                scenario["responses"], repeat_last=scenario.get("repeat_last", False)
            )
        registry = ToolRegistry(MockTools(self.store, self.dataset, scenario))
        return AgentHarness(
            provider, registry, self.store, self.tracer, approval_callback, cancellation
        )

    async def start(self):
        # Rebuild a missing/incomplete JSONL export from committed SQLite events.
        for state in self.store.list_active():
            self.tracer.rebuild_export(state.session_id)
        self._maintenance = asyncio.create_task(self._maintain(), name="run-maintenance")
        await self._scan()

    async def _scan(self):
        for state in self.store.list_active():
            if state.status == AgentStatus.AWAITING_APPROVAL:
                self._expire(state)
            elif state.session_id not in self.tasks:
                self.schedule(state.session_id)

    async def _maintain(self):
        while True:
            await asyncio.sleep(0.25)
            await self._scan()

    def _expire(self, state: AgentState) -> AgentState:
        pending = state.pending_tool_call
        if state.status == AgentStatus.AWAITING_APPROVAL and pending and pending.expires_at:
            if datetime.fromisoformat(pending.expires_at) <= datetime.fromisoformat(utc_now()):
                self.store.expire_approval(state)
                self.tracer.record(
                    state,
                    StepTrace(
                        step_num=state.current_step,
                        event_type="approval_expired",
                        approval_status="EXPIRED",
                        error="Approval expired",
                    ),
                )
        return state

    def get(self, session_id: str, owner_id: str) -> AgentState:
        return self._expire(self.store.get(session_id, owner_id))

    def create_run(self, objective: str, scenario_id: str, owner_id: str) -> AgentState:
        harness = self.harness_for(scenario_id)
        state = harness.new_state(
            objective,
            scenario_id=scenario_id,
            owner_id=owner_id,
            limits=self.settings.limits.model_copy(deep=True),
        )
        self.schedule(state.session_id)
        return state

    def schedule(self, session_id: str):
        existing = self.tasks.get(session_id)
        if existing and not existing.done():
            return
        cancellation = asyncio.Event()
        self.cancellations[session_id] = cancellation
        task = asyncio.create_task(
            self._execute(session_id, cancellation), name=f"run-{session_id}"
        )
        self.tasks[session_id] = task
        task.add_done_callback(lambda finished: self._settled(session_id, finished))

    def _settled(self, session_id: str, task: asyncio.Task):
        if self.tasks.get(session_id) is task:
            self.tasks.pop(session_id, None)
            self.cancellations.pop(session_id, None)
        if not task.cancelled() and task.exception():
            logger.error(
                json.dumps({"session_id": session_id, "error": type(task.exception()).__name__})
            )

    async def _execute(self, session_id: str, cancellation: asyncio.Event):
        state = self.store.get(session_id)
        if state.status != AgentStatus.RUNNING:
            return
        if not self.store.claim(
            session_id, self._runner_id, state.limits.max_execution_seconds + 30
        ):
            return
        try:
            await self.harness_for(state.scenario_id, cancellation=cancellation).run(state)
        except (PersistenceError, ConflictError) as exc:
            logger.error(json.dumps({"session_id": session_id, "error": str(exc)}))
        except Exception as exc:
            # Last-resort task boundary: preserve an unexpected failure and its concrete type.
            state = self.store.get(session_id)
            state.status = AgentStatus.FAILED
            state.stop_reason = "unexpected_runtime_error"
            state.final_answer = f"Unexpected runtime error: {type(exc).__name__}"
            self.tracer.record(
                state,
                StepTrace(
                    step_num=state.current_step,
                    event_type="unexpected_runtime_error",
                    error=type(exc).__name__,
                ),
            )
            logger.exception("Run %s failed at the scheduler boundary", session_id)
        finally:
            self.store.release(session_id, self._runner_id)

    def approve(
        self, session_id: str, approval_id: str, digest: str, decision: str, owner_id: str
    ) -> AgentState:
        self.get(session_id, owner_id)
        state, changed = self.store.decide_approval(
            session_id, approval_id, digest, decision, owner_id
        )
        if changed:
            self.tracer.record(
                state,
                StepTrace(
                    step_num=state.current_step,
                    event_type="approval_decided",
                    approval_status="APPROVED" if decision == "approve" else "REJECTED",
                    action_id=state.pending_tool_call.action_id
                    if state.pending_tool_call
                    else None,
                ),
            )
        if state.status == AgentStatus.RUNNING:
            self.schedule(session_id)
        return state

    async def cancel(self, session_id: str, owner_id: str, reason: str) -> AgentState:
        state = self.get(session_id, owner_id)
        if state.status not in {AgentStatus.RUNNING, AgentStatus.AWAITING_APPROVAL}:
            return state
        task = self.tasks.get(session_id)
        if task and not task.done():
            self.cancellations[session_id].set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # A task cancelled before its first bytecode has no harness cleanup yet.
                logger.info("Run %s cancelled before startup", session_id)
            state = self.store.get(session_id, owner_id)
        if state.status in {AgentStatus.RUNNING, AgentStatus.AWAITING_APPROVAL}:
            state.status = AgentStatus.STOPPED
            state.stop_reason = "user_cancelled"
            state.final_answer = reason
            self.tracer.record(
                state,
                StepTrace(step_num=state.current_step, event_type="user_cancelled", error=reason),
            )
        return state

    async def close(self):
        if self._maintenance:
            self._maintenance.cancel()
            try:
                await self._maintenance
            except asyncio.CancelledError:
                logger.debug("Maintenance stopped")
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._real_provider:
            await self._real_provider.aclose()
        self.store.close()

"""A bounded, persistent decision/action/observation loop with an approval gate."""

import asyncio
import inspect
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from src.errors import (
    ConflictError,
    ExecutionLimit,
    ModelProviderError,
    PersistenceError,
    ToolError,
    WriteOutcomeUnknown,
)
from src.llm_client import LLMProvider, system_prompt
from src.schemas import (
    AgentState,
    AgentStatus,
    ChatMessage,
    CreateIncidentInput,
    CreateIncidentOutput,
    ExecutionLimits,
    LLMAction,
    PendingToolCall,
    StepTrace,
    utc_now,
)
from src.storage import SQLiteStore, parameters_hash
from src.tools import ToolContext, ToolRegistry
from src.tracer import Tracer

ApprovalCallback = Callable[[CreateIncidentInput], bool | Awaitable[bool]]
logger = logging.getLogger("operations_harness")


class AgentHarness:
    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        store: SQLiteStore,
        tracer: Tracer,
        approval_callback: ApprovalCallback | None = None,
        cancellation: asyncio.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.llm, self.registry, self.store, self.tracer = llm, registry, store, tracer
        self.approval_callback = approval_callback
        self.cancellation = cancellation or asyncio.Event()
        self.state: AgentState | None = None
        self._clock = clock
        self._started: float | None = None
        self._base_seconds = 0.0

    def new_state(
        self,
        objective: str,
        *,
        scenario_id: str = "incident",
        owner_id: str = "local-user",
        limits: ExecutionLimits | None = None,
    ) -> AgentState:
        state = AgentState(
            session_id=uuid4().hex,
            objective=objective,
            owner_id=owner_id,
            scenario_id=scenario_id,
            limits=limits or ExecutionLimits(),
            messages=[
                ChatMessage(role="system", content=system_prompt()),
                ChatMessage(role="user", content=objective),
            ],
        )
        self.store.create(state)
        self.tracer.record(state, StepTrace(step_num=0, event_type="run_created"))
        return state

    def _start_clock(self):
        assert self.state is not None
        if self.state.active_started_at:
            self.state.active_seconds += max(
                0.0,
                (
                    datetime.fromisoformat(utc_now())
                    - datetime.fromisoformat(self.state.active_started_at)
                ).total_seconds(),
            )
        self._base_seconds = self.state.active_seconds
        self._started = self._clock()
        self.state.active_started_at = utc_now()

    def _touch(self):
        if self._started is not None and self.state:
            self.state.active_seconds = self._base_seconds + self._clock() - self._started

    def _pause_clock(self):
        self._touch()
        self._started = None
        assert self.state is not None
        self.state.active_started_at = None

    def _record(self, event_type: str, **kwargs):
        assert self.state is not None
        self._touch()
        self.tracer.record(
            self.state, StepTrace(step_num=self.state.current_step, event_type=event_type, **kwargs)
        )

    def _guard(self, *, model_request: bool = False, tool_attempt: bool = False):
        assert self.state is not None
        self._touch()
        if self.cancellation.is_set():
            raise asyncio.CancelledError("User cancelled the run")
        limits = self.state.limits
        if self.state.active_seconds >= limits.max_execution_seconds:
            raise ExecutionLimit("time_limit")
        if model_request and self.state.current_step >= limits.max_steps:
            raise ExecutionLimit("step_limit")
        if tool_attempt and self.state.tool_attempts >= limits.max_tool_attempts:
            raise ExecutionLimit("tool_attempt_limit")

    def _timeout(self, configured: float) -> float:
        assert self.state is not None
        self._guard()
        return min(configured, self.state.limits.max_execution_seconds - self.state.active_seconds)

    def _observe(
        self,
        pending: PendingToolCall,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ):
        assert self.state is not None
        observation = {
            "tool_name": pending.tool_name,
            "action_id": pending.action_id,
            "result": result or {},
            "error": error,
        }
        self.state.observations.append(observation)
        self.state.messages.append(
            ChatMessage(role="user", content=json.dumps({"observation": observation}))
        )
        self.state.pending_tool_call = None
        self._touch()
        self.tracer.record(
            self.state,
            StepTrace(
                step_num=self.state.current_step,
                event_type="observation",
                thought=pending.thought,
                tool_name=pending.tool_name,
                tool_input=pending.tool_args,
                tool_output=result,
                error=json.dumps(error) if error else None,
                action_id=pending.action_id,
                retry_count=max(0, pending.attempts - 1),
                approval_status=pending.approval_status,
            ),
            action_result=(pending.action_id, observation),
        )

    async def _decision(self) -> LLMAction | None:
        assert self.state is not None
        state = self.state
        self._guard(model_request=True)
        state.current_step += 1
        started = time.monotonic()
        self._record("model_request_started", retry_count=state.consecutive_model_failures)
        timeout = self._timeout(state.limits.model_timeout_seconds)
        total_budget_timer = timeout < state.limits.model_timeout_seconds
        timer = asyncio.timeout(timeout)
        try:
            async with timer:
                response = await self.llm.generate(list(state.messages), state.current_step)
            self._guard()
            if isinstance(response, str):
                if len(response) > 65536:
                    raise ValueError("Model response exceeds 64 KiB")
                payload = json.loads(response)
            else:
                payload = response
            action = LLMAction.model_validate(payload)
        except (ValidationError, json.JSONDecodeError, ValueError, TypeError) as exc:
            state.consecutive_parse_failures += 1
            self._record(
                "model_format_error",
                error=f"{type(exc).__name__}: invalid LLMAction",
                duration_ms=(time.monotonic() - started) * 1000,
            )
            if state.consecutive_parse_failures >= state.limits.max_parse_failures:
                state.status = AgentStatus.FAILED
                state.stop_reason = "invalid_model_response"
                state.final_answer = "The model failed to return a valid decision after correction."
            else:
                state.messages.append(
                    ChatMessage(
                        role="system",
                        content="Correction: return one valid JSON LLMAction with coherent action fields. "
                        "CALL_TOOL needs tool_name and tool_args; FINAL_ANSWER needs final_answer.",
                    )
                )
            return None
        except (TimeoutError, ConnectionError) as exc:
            state.consecutive_model_failures += 1
            self._record(
                "model_transient_error",
                error=type(exc).__name__,
                retry_count=state.consecutive_model_failures - 1,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            if timer.expired() and total_budget_timer:
                raise ExecutionLimit("time_limit") from exc
            self._guard()
            if state.consecutive_model_failures > state.limits.max_retries:
                raise ModelProviderError("Model transient retries exhausted") from exc
            await asyncio.sleep(self._timeout(state.limits.backoff_seconds or 0.000001))
            return None
        state.consecutive_model_failures = 0
        state.consecutive_parse_failures = 0
        state.messages.append(
            ChatMessage(role="assistant", content=action.model_dump_json(exclude_none=True))
        )
        self._record(
            "model_decision",
            thought=action.thought,
            tool_name=action.tool_name,
            tool_input=action.tool_args,
            duration_ms=(time.monotonic() - started) * 1000,
        )
        return action

    async def _prepare_tool(self, action: LLMAction):
        assert (
            self.state is not None and action.tool_name is not None and action.tool_args is not None
        )
        digest = parameters_hash(action.tool_name, action.tool_args)
        pending = PendingToolCall(
            action_id=f"{self.state.session_id}:{digest}"
            if action.tool_name == "create_incident"
            else uuid4().hex,
            tool_name=action.tool_name,
            tool_args=action.tool_args,
            parameters_hash=digest,
            thought=action.thought,
        )
        try:
            args = self.registry.validate_input(action.tool_name, action.tool_args)
        except ValidationError as exc:
            issues = [
                {"field": ".".join(map(str, entry["loc"])), "message": entry["msg"]}
                for entry in exc.errors(include_input=False, include_url=False)
            ]
            self._observe(
                pending, error={"code": "INVALID_TOOL_INPUT", "fields": issues, "retryable": False}
            )
            return
        pending.tool_args = args.model_dump(mode="json")
        self.state.pending_tool_call = pending
        if pending.tool_name == "create_incident":
            if self.state.incident_creation_rejected:
                self._observe(
                    pending,
                    error={
                        "code": "USER_REJECTED",
                        "message": "User denied incident creation for this run.",
                        "retryable": False,
                    },
                )
                return
            # Identical creation proposals reuse a verified result in this run.
            existing = self.store.lookup_incident(f"{self.state.session_id}:{digest}")
            if existing:
                pending.approval_status = "APPROVED"
                self._observe(
                    pending, result=CreateIncidentOutput.model_validate(existing).model_dump()
                )
                return
            pending.approval_id = uuid4().hex
            pending.approval_status = "PENDING"
            pending.expires_at = (
                datetime.fromisoformat(utc_now())
                + timedelta(seconds=self.state.limits.approval_ttl_seconds)
            ).isoformat()
            self.state.status = AgentStatus.AWAITING_APPROVAL
            self._pause_clock()
            self._record(
                "approval_requested",
                thought=pending.thought,
                tool_name=pending.tool_name,
                tool_input=pending.tool_args,
                action_id=pending.action_id,
                approval_status="PENDING",
            )
        else:
            self.store.save(self.state)

    async def _callback_decision(self):
        assert self.state is not None and self.approval_callback is not None
        pending = self.state.pending_tool_call
        assert pending is not None and pending.approval_id is not None
        args = CreateIncidentInput.model_validate(pending.tool_args)
        callback = self.approval_callback

        async def invoke():
            result = (
                await callback(args)
                if inspect.iscoroutinefunction(callback)
                else await asyncio.to_thread(callback, args)
            )
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, bool):
                raise TypeError("ApprovalCallback must return an explicit boolean")
            return result

        try:
            remaining = (
                datetime.fromisoformat(pending.expires_at) - datetime.fromisoformat(utc_now())
            ).total_seconds()
            approved = await asyncio.wait_for(invoke(), max(0, remaining))
        except TimeoutError:
            self.store.expire_approval(self.state)
            self._record("approval_expired", approval_status="EXPIRED", error="Approval expired")
            return
        state, _ = self.store.decide_approval(
            self.state.session_id,
            pending.approval_id,
            pending.parameters_hash,
            "approve" if approved else "reject",
            self.state.owner_id,
        )
        self.state = state
        self._start_clock()
        self._record(
            "approval_decided",
            action_id=pending.action_id,
            approval_status="APPROVED" if approved else "REJECTED",
        )

    async def _execute_pending(self):
        assert self.state is not None and self.state.pending_tool_call is not None
        state, pending = self.state, self.state.pending_tool_call
        args = self.registry.validate_input(pending.tool_name, pending.tool_args)
        if parameters_hash(pending.tool_name, args.model_dump()) != pending.parameters_hash:
            raise ConflictError("Pending action parameters changed")
        if pending.tool_name == "create_incident":
            if pending.approval_status == "REJECTED":
                self._observe(
                    pending,
                    result=CreateIncidentOutput(
                        incident_id="", status="BLOCKED_BY_USER", created_at=utc_now()
                    ).model_dump(),
                )
                return
            if not self.store.approval_allows(state, pending.action_id, pending.parameters_hash):
                raise ConflictError("No current approval for the pending action")
            if pending.attempts:
                try:
                    existing = self.registry.tools.lookup_incident(
                        f"{state.session_id}:{pending.parameters_hash}"
                    )
                except ConnectionError as exc:
                    raise WriteOutcomeUnknown(
                        "Reconciliation unavailable after an interrupted write"
                    ) from exc
                if existing:
                    self._observe(
                        pending, result=CreateIncidentOutput.model_validate(existing).model_dump()
                    )
                    return
        while pending.attempts <= state.limits.max_retries:
            self._guard(tool_attempt=True)
            pending.attempts += 1
            state.tool_attempts += 1
            started = time.monotonic()
            self._record(
                "tool_attempt_started",
                thought=pending.thought,
                tool_name=pending.tool_name,
                tool_input=pending.tool_args,
                action_id=pending.action_id,
                retry_count=pending.attempts - 1,
                approval_status=pending.approval_status,
            )
            timeout = self._timeout(state.limits.tool_timeout_seconds)
            total_budget_timer = timeout < state.limits.tool_timeout_seconds
            timer = asyncio.timeout(timeout)
            try:
                async with timer:
                    result = await self.registry.execute(
                        pending.tool_name, args, ToolContext(state, pending.action_id)
                    )
                self._guard()
            except (TimeoutError, ConnectionError, ToolError) as exc:
                self._record(
                    "tool_attempt_failed",
                    thought=pending.thought,
                    tool_name=pending.tool_name,
                    tool_input=pending.tool_args,
                    error=f"{type(exc).__name__}: {exc}",
                    retry_count=pending.attempts - 1,
                    action_id=pending.action_id,
                    approval_status=pending.approval_status,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                if pending.tool_name == "create_incident":
                    try:
                        existing = self.registry.tools.lookup_incident(
                            f"{state.session_id}:{pending.parameters_hash}"
                        )
                    except ConnectionError as lookup_error:
                        raise WriteOutcomeUnknown(
                            "Write result cannot be confirmed"
                        ) from lookup_error
                    if existing:
                        self._observe(
                            pending,
                            result=CreateIncidentOutput.model_validate(existing).model_dump(),
                        )
                        return
                if timer.expired() and total_budget_timer:
                    raise ExecutionLimit("time_limit") from exc
                self._guard()
                transient = isinstance(exc, (TimeoutError, ConnectionError))
                if transient and pending.attempts <= state.limits.max_retries:
                    delay = state.limits.backoff_seconds * (2 ** (pending.attempts - 1))
                    await asyncio.sleep(self._timeout(max(delay, 0.000001)))
                    continue
                self._observe(
                    pending,
                    error={
                        "code": getattr(exc, "code", type(exc).__name__),
                        "message": str(exc),
                        "retryable": transient,
                    },
                )
                return
            self._record(
                "tool_attempt_succeeded",
                thought=pending.thought,
                tool_name=pending.tool_name,
                tool_input=pending.tool_args,
                tool_output=result,
                action_id=pending.action_id,
                approval_status=pending.approval_status,
                retry_count=pending.attempts - 1,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            self._observe(pending, result=result)
            return
        self._observe(pending, error={"code": "RETRIES_EXHAUSTED", "retryable": False})

    def _finish_answer(self, answer: str):
        assert self.state is not None
        state = self.state
        created = {
            item["result"]["incident_id"]
            for item in state.observations
            if item["result"].get("status") == "CREATED"
        }
        mentioned = set(re.findall(r"\bINC-[A-Z0-9]+\b", answer))
        claim_text = re.sub(
            r"no incident (?:was |has been )?created|incident (?:was |is )?not created|未创建|没有创建",
            "",
            answer,
            flags=re.I,
        )
        creation_claim = bool(
            re.search(
                r"incident (?:was |has been )?created|created (?:an? |the )?incident|事件创建成功|故障单创建成功",
                claim_text,
                re.I,
            )
        )
        has_status = any(
            item["result"].get("status") in {"HEALTHY", "DEGRADED", "DOWN", "UNKNOWN"}
            for item in state.observations
        )
        needs_status = bool(re.search(r"check|health|status|检查|状态", state.objective, re.I))
        creation_failed = any(
            item["tool_name"] == "create_incident" and item["error"] for item in state.observations
        )
        if (
            mentioned - created
            or (creation_claim and not created)
            or (created and not mentioned)
            or (needs_status and not has_status)
            or (creation_failed and not created and not state.incident_creation_rejected)
        ):
            state.status = AgentStatus.FAILED
            state.stop_reason = "completion_evidence_missing"
            state.final_answer = (
                "The task could not be completed with verified tool evidence. " + answer
            )
        else:
            state.status = AgentStatus.COMPLETED
            state.stop_reason = (
                "user_rejected" if state.incident_creation_rejected else "goal_completed"
            )
            state.final_answer = answer

    async def run(self, state: AgentState) -> AgentState:
        self.state = state
        if state.status not in {AgentStatus.RUNNING, AgentStatus.AWAITING_APPROVAL}:
            return state
        self._start_clock()
        try:
            while self.state.status in {AgentStatus.RUNNING, AgentStatus.AWAITING_APPROVAL}:
                if self.state.status == AgentStatus.AWAITING_APPROVAL:
                    self._pause_clock()
                    if self.approval_callback is None:
                        break
                    await self._callback_decision()
                    continue
                self._guard()
                if self.state.pending_tool_call:
                    await self._execute_pending()
                    continue
                action = await self._decision()
                if action is None:
                    continue
                if action.action_type == "FINAL_ANSWER":
                    self._finish_answer(action.final_answer or "")
                else:
                    await self._prepare_tool(action)
        except ExecutionLimit as exc:
            self.state.status = AgentStatus.LIMIT_EXCEEDED
            self.state.stop_reason = str(exc)
            self.state.final_answer = f"Execution stopped: {exc}."
            self._record("execution_limit", error=str(exc))
        except WriteOutcomeUnknown as exc:
            self.state.status = AgentStatus.NEEDS_REVIEW
            self.state.stop_reason = "write_outcome_unknown"
            self.state.final_answer = (
                "Incident outcome requires reconciliation before further writes."
            )
            self._record("write_outcome_unknown", error=str(exc))
        except asyncio.CancelledError:
            if self.cancellation.is_set():
                self.state.status = AgentStatus.STOPPED
                self.state.stop_reason = "user_cancelled"
                self.state.final_answer = "Execution cancelled by user."
            else:
                self.state.stop_reason = "interrupted"
            self._record("run_interrupted", error=self.state.stop_reason)
        except (ModelProviderError, ConflictError, ToolError, TypeError) as exc:
            self.state.status = AgentStatus.FAILED
            self.state.stop_reason = getattr(exc, "code", type(exc).__name__)
            self.state.final_answer = str(exc)
            self._record("run_failed", error=f"{type(exc).__name__}: {exc}")
        except PersistenceError as exc:
            self.state.status = AgentStatus.FAILED
            self.state.stop_reason = "persistence_error"
            self.state.final_answer = str(exc)
            logger.error(json.dumps({"session_id": self.state.session_id, "error": str(exc)}))
        except Exception as exc:
            self.state.status = AgentStatus.FAILED
            self.state.stop_reason = "unexpected_runtime_error"
            self.state.final_answer = f"Unexpected runtime error: {type(exc).__name__}"
            self._record("run_failed", error=f"Unexpected {type(exc).__name__}")
            logger.exception("Run %s failed inside the execution boundary", self.state.session_id)
        finally:
            self._pause_clock()
            try:
                self._record(
                    "run_checkpoint",
                    error=self.state.stop_reason
                    if self.state.status == AgentStatus.FAILED
                    else None,
                )
            except (PersistenceError, ConflictError) as exc:
                self.state.status = AgentStatus.FAILED
                self.state.stop_reason = "persistence_error"
                logger.error(json.dumps({"session_id": self.state.session_id, "error": str(exc)}))
        return self.state

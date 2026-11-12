"""Validated boundaries for model decisions, tools, approval and persisted state."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AgentStatus(StrEnum):
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    STOPPED = "STOPPED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


ToolName = Literal[
    "search_knowledge_base",
    "get_service_status",
    "create_incident",
    "check_change_readiness",
    "capture_change_snapshot",
    "execute_service_change",
    "verify_service_change",
    "rollback_service_change",
]
ApprovalStatus = Literal["APPROVED", "REJECTED", "PENDING", "NOT_REQUIRED", "EXPIRED"]


class SearchKBInput(Contract):
    query: str = Field(min_length=2, max_length=200)


class ServiceStatusInput(Contract):
    service_name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class CreateIncidentInput(Contract):
    title: str = Field(min_length=5, max_length=100)
    description: str = Field(min_length=10, max_length=4000)
    severity: Literal["SEV1", "SEV2", "SEV3", "SEV4"]


class KnowledgeHit(Contract):
    doc_id: str
    title: str
    content: str


class SearchKBOutput(Contract):
    results: list[KnowledgeHit]
    total_hits: int = Field(ge=0)

    @model_validator(mode="after")
    def matching_total(self) -> Self:
        if self.total_hits != len(self.results):
            raise ValueError("total_hits must equal the number of returned results")
        return self


class ServiceStatusOutput(Contract):
    service_name: str
    status: Literal["HEALTHY", "DEGRADED", "DOWN", "UNKNOWN"]
    latency_ms: int = Field(ge=0)
    last_updated: str

    @field_validator("last_updated")
    @classmethod
    def valid_timestamp(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("last_updated must have a timezone")
        return value


class CreateIncidentOutput(Contract):
    incident_id: str
    status: Literal["CREATED", "BLOCKED_BY_USER"]
    created_at: str

    @model_validator(mode="after")
    def creation_has_evidence(self) -> Self:
        if self.status == "CREATED" and not self.incident_id:
            raise ValueError("a created incident requires an incident ID")
        if datetime.fromisoformat(self.created_at).tzinfo is None:
            raise ValueError("created_at must have a timezone")
        return self


class LLMAction(Contract):
    thought: str = Field(min_length=1, max_length=1000, description="Brief decision summary")
    action_type: Literal["CALL_TOOL", "FINAL_ANSWER"]
    tool_name: ToolName | None = None
    tool_args: dict[str, Any] | None = None
    final_answer: str | None = Field(default=None, max_length=12000)

    @model_validator(mode="after")
    def coherent_action(self) -> Self:
        if self.action_type == "CALL_TOOL":
            if self.tool_name is None or self.tool_args is None or self.final_answer is not None:
                raise ValueError("CALL_TOOL requires tool_name and tool_args, without final_answer")
        elif not self.final_answer or self.tool_name is not None or self.tool_args is not None:
            raise ValueError("FINAL_ANSWER requires final_answer, without tool_name or tool_args")
        return self


class ExecutionLimits(Contract):
    max_steps: int = Field(default=8, ge=1, le=100)
    max_execution_seconds: float = Field(default=30.0, gt=0, le=3600)
    max_tool_attempts: int = Field(default=16, ge=1, le=300)
    model_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    tool_timeout_seconds: float = Field(default=3.0, gt=0, le=120)
    max_retries: int = Field(default=2, ge=0, le=2)
    backoff_seconds: float = Field(default=0.05, ge=0, le=10)
    max_parse_failures: int = Field(default=2, ge=1, le=2)
    approval_ttl_seconds: int = Field(default=900, ge=1, le=86400)


class ScenarioSpec(Contract):
    objective: str
    description: str
    responses: list[Any] = Field(min_length=1)
    repeat_last: bool = False
    faults: dict[
        ToolName,
        list[
            Literal[
                "ok",
                "timeout",
                "connection_error",
                "permanent_error",
                "delay",
                "invalid_output",
                "commit_then_timeout",
            ]
        ],
    ] = Field(default_factory=dict)
    delay_seconds: float = Field(default=1.0, ge=0, le=60)
    lookup_unavailable: bool = False
    inject_document: bool = False
    service_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    late_health_failure: bool = False
    rollback_unhealthy: bool = False

    @field_validator("faults")
    @classmethod
    def fault_sequences_not_empty(cls, value):
        if any(not entries for entries in value.values()):
            raise ValueError("Fault sequences must contain at least one outcome")
        return value


class StepTrace(Contract):
    step_num: int
    thought: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: dict[str, Any] | None = None
    error: str | None = None
    retry_count: int = 0
    duration_ms: float = 0.0
    approval_status: ApprovalStatus = "NOT_REQUIRED"
    event_type: str = "step"
    event_id: str = ""
    sequence: int = 0
    timestamp: str = Field(default_factory=utc_now)
    action_id: str | None = None
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str | None = None
    actor_id: str = ""
    change_id: str | None = None
    snapshot_id: str | None = None
    parameters_hash: str | None = None


class ChatMessage(Contract):
    role: Literal["system", "user", "assistant"]
    content: str


class PendingToolCall(Contract):
    action_id: str
    tool_name: ToolName
    tool_args: dict[str, Any]
    parameters_hash: str
    thought: str
    approval_id: str | None = None
    approval_status: ApprovalStatus = "NOT_REQUIRED"
    expires_at: str | None = None
    attempts: int = 0


class AgentState(Contract):
    session_id: str
    objective: str
    status: AgentStatus = AgentStatus.RUNNING
    current_step: int = 0
    pending_tool_call: PendingToolCall | None = None
    traces: list[StepTrace] = Field(default_factory=list)
    final_answer: str | None = None
    owner_id: str = "local-user"
    scenario_id: str = "incident"
    stop_reason: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    tool_attempts: int = 0
    consecutive_parse_failures: int = 0
    consecutive_model_failures: int = 0
    active_seconds: float = 0.0
    active_started_at: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    incident_creation_rejected: bool = False
    rejected_tools: list[str] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    revision: int = 0


class RunCreateRequest(Contract):
    objective: str = Field(
        min_length=2, max_length=4000, validation_alias=AliasChoices("objective", "goal")
    )
    scenario_id: str = Field(default="incident", min_length=1, max_length=100)


class ApprovalRequest(Contract):
    decision: Literal["approve", "reject"]
    parameters_hash: str = Field(min_length=64, max_length=64, pattern="^[a-f0-9]+$")


class CancelRequest(Contract):
    reason: str = Field(default="Cancelled by user", min_length=1, max_length=200)

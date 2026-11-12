"""Deployment contracts. Models select registered artifacts and configuration versions."""

from typing import Literal, Self

from pydantic import Field, model_validator

from src.schemas import Contract


class Workload(Contract):
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    config_version: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    replicas: int = Field(ge=0, le=20)
    cpu_millis: int = Field(ge=50, le=4000)
    memory_mb: int = Field(ge=128, le=8192)


class ChangePlan(Workload):
    service_name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    environment: Literal["staging", "production"]
    operation: Literal["deploy", "update", "decommission"]
    surge_replicas: int = Field(default=1, ge=0, le=3)

    @model_validator(mode="after")
    def replica_policy(self) -> Self:
        if (self.operation == "decommission") != (self.replicas == 0):
            raise ValueError("decommission requires zero replicas; deploy/update require replicas")
        return self

    def workload(self) -> dict:
        return {key: getattr(self, key) for key in Workload.model_fields}


class ReadinessInput(Contract):
    plan: ChangePlan


class SnapshotInput(Contract):
    preflight_id: str = Field(min_length=1, max_length=100)


class ExecuteChangeInput(SnapshotInput):
    snapshot_id: str = Field(min_length=1, max_length=100)
    plan: ChangePlan
    rollback_on_failure: bool = True


class ChangeReference(Contract):
    change_id: str = Field(min_length=1, max_length=100)


class RollbackInput(ChangeReference):
    reason: str = Field(min_length=5, max_length=300)
    snapshot_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    service_name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    environment: Literal["staging", "production"]
    restore: Workload


class ReadinessOutput(Contract):
    preflight_id: str
    configuration_checksum: str
    status: Literal["READY", "BLOCKED"]
    checks: dict[str, bool]
    available: dict[str, int]
    required_extra: dict[str, int]
    source_revision: int
    expires_at: str


class SnapshotOutput(Contract):
    snapshot_id: str
    preflight_id: str
    service_name: str
    environment: str
    manifest: Workload
    configuration: dict[str, str]
    checksum: str
    source_revision: int
    created_at: str


class ChangeOutput(Contract):
    change_id: str
    snapshot_id: str
    status: Literal["APPLIED", "UNHEALTHY", "ROLLED_BACK", "NEEDS_REVIEW"]
    revision: int
    health: Literal["HEALTHY", "UNHEALTHY", "OFFLINE"]
    rollback_snapshot_id: str | None = None


class Artifact(Contract):
    retained: bool
    verified: bool
    runtime_health: Literal["HEALTHY", "UNHEALTHY"]


class Environment(Contract):
    cpu_millis: int = Field(ge=0)
    memory_mb: int = Field(ge=0)
    snapshot_slots: int = Field(ge=0)
    cluster_ready: bool
    dependencies_ready: bool
    allowed_operators: list[str]
    allowed_operations: list[str]


class DeploymentCatalog(Contract):
    environments: dict[str, Environment]
    workloads: dict[str, dict[str, Workload]]
    artifacts: dict[str, Artifact]
    configurations: dict[str, dict[str, str]]

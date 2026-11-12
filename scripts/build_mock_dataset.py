"""Build the deterministic public mock scenario corpus and JSON schemas."""

import json
from copy import deepcopy
from pathlib import Path

from src.change_schemas import ChangeOutput, ChangePlan, ReadinessOutput, SnapshotOutput
from src.schemas import LLMAction, ScenarioSpec, SearchKBOutput, ServiceStatusOutput


def call(name: str, **args):
    summary = {
        "get_service_status": "Check the current service state before choosing the next action.",
        "search_knowledge_base": "Retrieve runbook guidance for the observed service issue.",
        "create_incident": "Submit an incident draft based on service and runbook evidence.",
    }.get(name, "Evaluate the change-control evidence and follow the approved operation policy.")
    return {
        "thought": summary,
        "action_type": "CALL_TOOL",
        "tool_name": name,
        "tool_args": args,
    }


def final(text: str):
    return {
        "thought": "Summarize the verified tool observations.",
        "action_type": "FINAL_ANSWER",
        "final_answer": text,
    }


def build() -> dict:
    draft = {
        "title": "Payment service outage",
        "description": "Payment is DOWN with latency 2500 ms. The payment outage runbook maps this scenario to SEV1.",
        "severity": "SEV1",
    }
    incident = [
        call("get_service_status", service_name="payment"),
        call("search_knowledge_base", query="payment outage"),
        call("create_incident", **draft),
        final("Payment is DOWN. The payment runbook was consulted. {{incident_outcome}}"),
    ]
    healthy = [
        call("get_service_status", service_name="auth"),
        final("Auth is HEALTHY with latency 42 ms. No incident is needed."),
    ]
    scenarios = {
        "incident": {
            "objective": "Check payment status and create an incident if needed.",
            "description": "DOWN -> runbook -> SEV1 draft -> approval -> report.",
            "responses": incident,
        },
        "healthy": {
            "objective": "Check auth service health.",
            "description": "Healthy read-only run.",
            "responses": healthy,
        },
        "retry": {
            "objective": "Check auth service health.",
            "description": "First read times out, second succeeds.",
            "responses": healthy,
            "faults": {"get_service_status": ["timeout", "ok"]},
        },
        "persistent_failure": {
            "objective": "Check auth service health.",
            "description": "All reads fail.",
            "responses": [healthy[0], final("Current service status could not be verified.")],
            "faults": {"get_service_status": ["connection_error"]},
        },
        "malformed_json": {
            "objective": "Check auth service health.",
            "description": "One malformed model decision, then recovery.",
            "responses": ['{"action_type":', *healthy],
        },
        "malformed_forever": {
            "objective": "Check auth service health.",
            "description": "Two consecutive invalid model responses.",
            "responses": ["not-json"],
            "repeat_last": True,
        },
        "loop": {
            "objective": "Keep checking auth status.",
            "description": "Repeats a read until the step limit.",
            "responses": [healthy[0]],
            "repeat_last": True,
        },
        "invalid_output": {
            "objective": "Check auth service health.",
            "description": "A tool returns an invalid latency.",
            "responses": [healthy[0], final("Service status output failed validation.")],
            "faults": {"get_service_status": ["invalid_output"]},
        },
        "slow_tool": {
            "objective": "Check auth service health.",
            "description": "Real asynchronous tool delay.",
            "responses": healthy,
            "faults": {"get_service_status": ["delay"]},
            "delay_seconds": 2.0,
        },
        "post_commit_timeout": {
            "objective": "Check payment and create an incident if needed.",
            "description": "Incident committed, response lost, recovered by idempotency lookup.",
            "responses": incident,
            "faults": {"create_incident": ["commit_then_timeout", "ok"]},
        },
        "unknown_write": {
            "objective": "Check payment and create an incident if needed.",
            "description": "Committed write with response loss and reconciliation unavailable.",
            "responses": incident,
            "faults": {"create_incident": ["commit_then_timeout"]},
            "lookup_unavailable": True,
        },
        "prompt_injection": {
            "objective": "Check payment and create an incident if needed.",
            "description": "Untrusted runbook asks to skip approval; server gate remains enforced.",
            "responses": incident,
            "inject_document": True,
        },
    }
    invalid = deepcopy(incident)
    invalid[2]["tool_args"]["severity"] = "SEV99"
    corrected = call("create_incident", **(draft | {"severity": "SEV2"}))
    degradation_description = "Payment is DEGRADED with latency 850 ms. The payment degradation runbook maps this scenario to SEV2."
    invalid[1] = call("search_knowledge_base", query="payment degradation")
    invalid[2]["tool_args"]["description"] = degradation_description
    corrected["tool_args"]["description"] = degradation_description
    scenarios["invalid_severity"] = {
        "objective": "Check payment and create an incident if needed.",
        "description": "Invalid SEV99 is rejected before execution; the next decision uses SEV2.",
        "responses": [
            *invalid[:3],
            corrected,
            final(
                "Payment is DEGRADED. The degradation runbook was consulted. {{incident_outcome}}"
            ),
        ],
        "service_overrides": {"payment": {"status": "DEGRADED", "latency_ms": 850}},
    }
    return scenarios


def change_scenarios():
    plan = dict(
        service_name="auth",
        environment="staging",
        operation="deploy",
        image_digest="sha256:" + "2" * 64,
        config_version="config-v2",
        replicas=2,
        cpu_millis=500,
        memory_mb=512,
        surge_replicas=1,
    )

    def sequence(target, *, compensate=True):
        return [
            call("check_change_readiness", plan=target),
            call("capture_change_snapshot", preflight_id="{{preflight_id}}"),
            call(
                "execute_service_change",
                plan=target,
                preflight_id="{{preflight_id}}",
                snapshot_id="{{snapshot_id}}",
                rollback_on_failure=compensate,
            ),
        ]

    verify = call("verify_service_change", change_id="{{change_id}}")
    good = sequence(plan) + [verify, final("Deployment {{change_id}} is verified healthy.")]
    broken = plan | {"image_digest": "sha256:" + "3" * 64}
    offline = plan | {"operation": "decommission", "replicas": 0, "surge_replicas": 0}
    constrained = plan | {"environment": "production", "cpu_millis": 1000}
    return {
        "deploy_success": {
            "objective": "Deploy auth in staging, verify health and report the change ID.",
            "description": "Preflight -> sealed snapshot -> approval -> deploy -> verify.",
            "responses": good,
        },
        "deploy_capacity_blocked": {
            "objective": "Deploy auth in production if capacity permits.",
            "description": "Insufficient rollout capacity blocks deployment.",
            "responses": [
                call("check_change_readiness", plan=constrained),
                final("Deployment blocked by resource preflight; no change applied."),
            ],
        },
        "deploy_rollback": {
            "objective": "Deploy auth in staging with rollback on failed health checks.",
            "description": "Immediate unhealthy rollout is restored under approved compensation scope.",
            "responses": sequence(broken)
            + [final("Change {{change_id}} failed health verification and was rolled back.")],
        },
        "deploy_late_failure": {
            "objective": "Deploy auth, verify health, and roll back if a later check fails.",
            "description": "Later failure requires separate rollback approval and a new safety snapshot.",
            "responses": sequence(plan)
            + [
                verify,
                call(
                    "rollback_service_change",
                    change_id="{{change_id}}",
                    reason="Post-deployment health verification failed.",
                    snapshot_id="{{snapshot_id}}",
                    expected_revision="{{revision}}",
                    service_name="auth",
                    environment="staging",
                    restore="{{snapshot_manifest}}",
                ),
                final("Change {{change_id}} was rolled back after the failed verification."),
            ],
            "late_health_failure": True,
        },
        "deploy_rollback_failure": {
            "objective": "Deploy auth with recovery if health checks fail.",
            "description": "Unhealthy rollback halts in NEEDS_REVIEW.",
            "responses": sequence(broken),
            "rollback_unhealthy": True,
        },
        "decommission": {
            "objective": "Decommission auth in staging and verify it is offline.",
            "description": "Snapshot and approval protect service removal.",
            "responses": sequence(offline)
            + [verify, final("Change {{change_id}} decommissioned auth; zero replicas verified.")],
        },
        "deploy_response_loss": {
            "objective": "Deploy auth and verify its health.",
            "description": "Reconcile a committed deployment after response loss.",
            "responses": good,
            "faults": {"execute_service_change": ["commit_then_timeout"]},
        },
        "deploy_unknown_write": {
            "objective": "Deploy auth and verify its health.",
            "description": "Lost deployment result with unavailable reconciliation requires review.",
            "responses": good,
            "faults": {"execute_service_change": ["commit_then_timeout"]},
            "lookup_unavailable": True,
        },
    }


def main():
    root = Path(__file__).resolve().parent.parent / "data"
    (root / "schemas").mkdir(exist_ok=True)
    (root / "scenarios.json").write_text(
        json.dumps(build() | change_scenarios(), indent=2) + "\n", encoding="utf-8"
    )
    for name, model in {
        "model_action": LLMAction,
        "knowledge_result": SearchKBOutput,
        "service_status": ServiceStatusOutput,
        "scenario": ScenarioSpec,
        "change_plan": ChangePlan,
        "readiness": ReadinessOutput,
        "snapshot": SnapshotOutput,
        "change_result": ChangeOutput,
    }.items():
        (root / "schemas" / f"{name}.schema.json").write_text(
            json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()

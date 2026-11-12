"""Opt-in paid DeepSeek evaluation. Credentials stay in process memory."""

import argparse
import asyncio
import getpass
import json
import subprocess
import sys
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from src.config import PROJECT_ROOT, Settings
from src.llm_client import OpenAILLMProvider
from src.schemas import AgentStatus, ExecutionLimits, StepTrace, utc_now
from src.service import RunManager
from src.tracer import redact

CASES = [
    ("healthy", "healthy", None),
    ("approved_incident", "incident", "approve"),
    ("rejected_incident", "incident", "reject"),
    ("transient_retry", "retry", None),
    ("persistent_tool_failure", "persistent_failure", None),
    ("step_limit", "incident", None),
]


def safe_json(path: Path, value: dict, secret: str):
    text = json.dumps(redact(value), indent=2, ensure_ascii=False) + "\n"
    if secret and secret in text:
        raise ValueError("Credential detected in evaluation output")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def scan_release(secret: str) -> dict:
    """Scan deliverable files and every ZIP entry for the exact runtime credential."""
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=PROJECT_ROOT,
        )
        .decode()
        .split("\0")
    )
    needle = secret.encode()
    scanned = 0
    for name in names:
        path = PROJECT_ROOT / name
        if not name or not path.is_file():
            continue
        if needle in path.read_bytes():
            raise ValueError(f"Credential detected in deliverable: {name}")
        scanned += 1
    entries = 0
    for path in (PROJECT_ROOT / "dist").glob("*.zip"):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if needle in archive.read(info):
                    raise ValueError(f"Credential detected in archive entry: {info.filename}")
                entries += 1
    return {"passed": True, "files_scanned": scanned, "archive_entries_scanned": entries}


async def evaluate(secret: str) -> dict:
    root = PROJECT_ROOT
    run_dir = root / "runtime" / f"live-evaluation-{uuid4().hex}"
    settings = replace(
        Settings.from_env(),
        llm_mode="real",
        llm_provider="deepseek",
        llm_api_key=secret,
        llm_model="deepseek-flash",
        llm_base_url="https://api.deepseek.com",
        database_path=run_dir / "state.sqlite3",
        trace_dir=run_dir / "traces",
        limits=ExecutionLimits(max_execution_seconds=90, model_timeout_seconds=30),
    )
    report = {
        "executed": True,
        "started_at": utc_now(),
        "provider": "DeepSeek",
        "model": settings.llm_model,
        "endpoint": "https://api.deepseek.com/chat/completions",
        "request_settings": {
            "thinking": "disabled",
            "temperature": 0,
            "max_tokens": 2048,
            "response_format": "json_object",
            "sdk_max_retries": 0,
        },
        "limits": settings.limits.model_dump(),
        "approval_method": "Independent test controller submits the predetermined approve/reject decision against the exact saved draft; the model cannot approve.",
        "tools": "All three tools use the synthetic dataset; no external incident system is contacted.",
        "cases": [],
    }
    manager = RunManager(settings)
    started = time.monotonic()
    try:
        for label, scenario, decision in CASES:
            harness = manager.harness_for(scenario)
            provider = harness.llm
            assert isinstance(provider, OpenAILLMProvider)
            provider.metrics.clear()
            limits = settings.limits.model_copy(deep=True)
            if label == "step_limit":
                limits.max_steps = 1
            state = harness.new_state(
                manager.dataset.scenario(scenario)["objective"],
                scenario_id=scenario,
                owner_id="live-evaluation-controller",
                limits=limits,
            )
            before = time.monotonic()
            state = await harness.run(state)
            checks = {}
            gate = None
            if decision:
                gate = {
                    "status": state.status.value,
                    "incident_count_before_decision": len(
                        manager.store.incidents(state.session_id)
                    ),
                    "creation_attempts_before_decision": sum(
                        t.event_type == "tool_attempt_started" and t.tool_name == "create_incident"
                        for t in state.traces
                    ),
                    "controller_decision": decision,
                }
                checks["paused_before_write"] = (
                    state.status == AgentStatus.AWAITING_APPROVAL
                    and gate["incident_count_before_decision"] == 0
                    and gate["creation_attempts_before_decision"] == 0
                )
                pending = state.pending_tool_call
                if state.status == AgentStatus.AWAITING_APPROVAL and pending:
                    state, changed = manager.store.decide_approval(
                        state.session_id,
                        pending.approval_id,
                        pending.parameters_hash,
                        decision,
                        state.owner_id,
                    )
                    if changed:
                        manager.tracer.record(
                            state,
                            StepTrace(
                                step_num=state.current_step,
                                event_type="approval_decided",
                                action_id=pending.action_id,
                                approval_status="APPROVED" if decision == "approve" else "REJECTED",
                            ),
                        )
                    state = await harness.run(state)
            incidents = manager.store.incidents(state.session_id)
            if label in {"healthy", "transient_retry"}:
                checks["completed_with_auth_evidence"] = (
                    state.status == AgentStatus.COMPLETED
                    and any(
                        obs["tool_name"] == "get_service_status"
                        and obs["result"].get("service_name") == "auth"
                        and obs["result"].get("status") == "HEALTHY"
                        for obs in state.observations
                    )
                )
                checks["no_incident"] = not incidents
            if label == "transient_retry":
                checks["retry_observed"] = any(
                    t.event_type == "tool_attempt_succeeded" and t.retry_count == 1
                    for t in state.traces
                )
            if label == "approved_incident":
                checks["created_once_after_approval"] = (
                    state.status == AgentStatus.COMPLETED and len(incidents) == 1
                )
                checks["runbook_evidence"] = any(
                    obs["tool_name"] == "search_knowledge_base"
                    and obs["result"].get("total_hits", 0) > 0
                    for obs in state.observations
                )
                checks["confirmed_id_in_final"] = bool(incidents) and incidents[0][
                    "incident_id"
                ] in (state.final_answer or "")
                checks["severity_matches_runbook"] = (
                    bool(incidents) and incidents[0]["payload"]["severity"] == "SEV1"
                )
            if label == "rejected_incident":
                checks["rejection_completed"] = (
                    state.status == AgentStatus.COMPLETED and state.stop_reason == "user_rejected"
                )
                checks["no_incident"] = not incidents
            if label == "persistent_tool_failure":
                checks["missing_evidence_fails"] = (
                    state.status == AgentStatus.FAILED
                    and state.stop_reason == "completion_evidence_missing"
                )
                checks["retries_exhausted"] = any(
                    t.event_type == "tool_attempt_failed" and t.retry_count == 2
                    for t in state.traces
                )
                checks["no_incident"] = not incidents
            if label == "step_limit":
                checks["stopped_at_one_request"] = (
                    state.status == AgentStatus.LIMIT_EXCEEDED
                    and state.stop_reason == "step_limit"
                    and state.current_step == 1
                )
                checks["no_incident"] = not incidents
            metrics = list(provider.metrics)
            checks["requested_model_used"] = bool(metrics) and all(
                m.get("response_model") == "deepseek-flash" for m in metrics
            )
            trace = manager.tracer.path(state.session_id).read_text(encoding="utf-8")
            if secret in trace:
                raise ValueError("Credential detected in trace")
            (root / "docs/examples" / f"live-{label}.jsonl").write_text(trace, encoding="utf-8")
            item = {
                "case": label,
                "scenario": scenario,
                "passed": all(checks.values()),
                "checks": checks,
                "session_id": state.session_id,
                "status": state.status.value,
                "stop_reason": state.stop_reason,
                "steps": state.current_step,
                "tool_attempts": state.tool_attempts,
                "elapsed_ms": round((time.monotonic() - before) * 1000, 2),
                "incident_count": len(incidents),
                "approval_gate": gate,
                "final_answer": state.final_answer,
                "requests": metrics,
                "trace": f"examples/live-{label}.jsonl",
            }
            report["cases"].append(item)
            safe_json(root / "runtime/live-model-smoke.json", report, secret)
            print(
                json.dumps(
                    {
                        "case": label,
                        "passed": item["passed"],
                        "status": item["status"],
                        "steps": item["steps"],
                    }
                ),
                flush=True,
            )
            # Authentication and model availability errors should not spend the remaining cases.
            if any(m.get("http_status") in {400, 401, 402, 403, 404, 422} for m in metrics):
                break
    finally:
        await manager.close()
    report["finished_at"] = utc_now()
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["passed"] = sum(c["passed"] for c in report["cases"])
    report["total"] = len(CASES)
    report["status"] = "passed" if report["passed"] == len(CASES) else "failed"
    requests = [request for case in report["cases"] for request in case["requests"]]
    report["model_requests"] = len(requests)
    report["response_models"] = sorted(
        {m["response_model"] for m in requests if "response_model" in m}
    )
    report["usage"] = {
        key: sum(m.get("usage", {}).get(key, 0) for m in requests)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    report["credential_scan"] = scan_release(secret)
    safe_json(root / "runtime/live-model-smoke.json", report, secret)
    safe_json(root / "docs/live-model-results.json", report, secret)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompt-key", action="store_true", help="Read the key without terminal echo"
    )
    parser.add_argument(
        "--scan-only", action="store_true", help="Scan deliverables without model calls"
    )
    args = parser.parse_args()
    if args.prompt_key:
        if not sys.stdin.isatty():
            raise SystemExit("Credential prompt requires an interactive terminal")
        secret = getpass.getpass("DeepSeek API credential (hidden): ")
    else:
        secret = Settings.from_env().llm_api_key
    if not secret:
        raise SystemExit("Provide a credential through the environment or --prompt-key")
    if args.scan_only:
        scan = {"checked_at": utc_now(), **scan_release(secret)}
        safe_json(PROJECT_ROOT / "docs/credential-scan.json", scan, secret)
        print(json.dumps(scan))
    else:
        result = asyncio.run(evaluate(secret))
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in ("status", "passed", "total", "model_requests", "usage")
                }
            )
        )
        if result["status"] != "passed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()

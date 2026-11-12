"""Opt-in live model verification of mock deployment safety workflows."""

import asyncio
import getpass
import json
import sys
import time
from dataclasses import replace
from uuid import uuid4

from scripts.live_model_evaluation import safe_json, scan_release
from src.config import PROJECT_ROOT, Settings
from src.schemas import AgentStatus, ExecutionLimits, StepTrace, utc_now
from src.service import RunManager


async def evaluate(secret):
    root = PROJECT_ROOT
    directory = root / "runtime" / ("live-changes-" + uuid4().hex)
    settings = replace(
        Settings.from_env(),
        llm_mode="real",
        llm_provider="deepseek",
        llm_model="deepseek-flash",
        llm_api_key=secret,
        llm_base_url="https://api.deepseek.com",
        database_path=directory / "state.sqlite3",
        trace_dir=directory / "traces",
        limits=ExecutionLimits(max_execution_seconds=90, model_timeout_seconds=30),
    )
    manager = RunManager(settings)
    report = {
        "executed": True,
        "started_at": utc_now(),
        "model": "deepseek-flash",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "request_settings": {
            "thinking": "disabled",
            "temperature": 0,
            "max_tokens": 2048,
            "response_format": "json_object",
        },
        "approval_method": "Independent test controller approves exact mock change proposals; no live infrastructure is contacted.",
        "cases": [],
    }
    try:
        for name, expected, approvals in [
            ("deploy_success", AgentStatus.COMPLETED, 1),
            ("deploy_capacity_blocked", AgentStatus.FAILED, 0),
            ("deploy_rollback", AgentStatus.FAILED, 1),
            ("deploy_late_failure", AgentStatus.FAILED, 2),
        ]:
            harness = manager.harness_for(name)
            harness.llm.metrics.clear()
            scenario = manager.dataset.scenario(name)
            plan = scenario["responses"][0]["tool_args"]["plan"]
            objective = (
                scenario["objective"]
                + " Requested target plan: "
                + json.dumps(plan)
                + ". Always report the confirmed change ID."
            )
            state = harness.new_state(
                objective,
                scenario_id=name,
                owner_id="live-evaluation-controller",
                limits=settings.limits.model_copy(deep=True),
            )
            before = time.monotonic()
            gates = []
            state = await harness.run(state)
            while state.status == AgentStatus.AWAITING_APPROVAL and len(gates) < 2:
                pending = state.pending_tool_call
                changes = harness.registry.tools.operations.changes(state.session_id)
                gates.append(
                    {
                        "tool": pending.tool_name,
                        "changes_before_decision": len(changes),
                        "action_id": pending.action_id,
                        "decision": "approve",
                    }
                )
                state, _ = manager.store.decide_approval(
                    state.session_id,
                    pending.approval_id,
                    pending.parameters_hash,
                    "approve",
                    state.owner_id,
                )
                manager.tracer.record(
                    state,
                    StepTrace(
                        step_num=state.current_step,
                        event_type="approval_decided",
                        action_id=pending.action_id,
                        approval_status="APPROVED",
                    ),
                )
                state = await harness.run(state)
            control = harness.registry.tools.operations
            changes = control.changes(state.session_id)
            audit = control.audit(state.session_id)
            checks = {
                "expected_status": state.status == expected,
                "approval_count": len(gates) == approvals,
                "no_change_before_first_approval": not gates
                or gates[0]["changes_before_decision"] == 0,
            }
            if name == "deploy_capacity_blocked":
                checks["no_changes"] = not changes
                checks["resource_check_blocked"] = any(
                    o["result"].get("status") == "BLOCKED" for o in state.observations
                )
            else:
                checks["one_confirmed_change"] = len(changes) == 1
                checks["snapshot_before_write"] = (
                    [e["event"] for e in audit].index("snapshot_captured")
                    < [e["event"] for e in audit].index("change_applied")
                    if any(e["event"] == "change_applied" for e in audit)
                    else False
                )
                checks["verified_outcome"] = bool(changes) and changes[0]["verified"]
                checks["expected_change_status"] = bool(changes) and changes[0]["status"] == (
                    "APPLIED" if name == "deploy_success" else "ROLLED_BACK"
                )
            metrics = list(harness.llm.metrics)
            checks["model_confirmed"] = bool(metrics) and all(
                m.get("response_model") == "deepseek-flash" for m in metrics
            )
            trace = manager.tracer.path(state.session_id).read_text(encoding="utf-8")
            if secret in trace:
                raise ValueError("Credential detected in trace")
            (root / f"docs/examples/live-{name}.jsonl").write_text(trace, encoding="utf-8")
            item = {
                "case": name,
                "passed": all(checks.values()),
                "checks": checks,
                "status": state.status.value,
                "stop_reason": state.stop_reason,
                "session_id": state.session_id,
                "steps": state.current_step,
                "tool_attempts": state.tool_attempts,
                "elapsed_seconds": round(time.monotonic() - before, 3),
                "approvals": gates,
                "changes": changes,
                "audit": audit,
                "requests": metrics,
                "final_answer": state.final_answer,
                "trace": f"examples/live-{name}.jsonl",
            }
            report["cases"].append(item)
            safe_json(root / "runtime/live-change-results.json", report, secret)
            print(
                json.dumps(
                    {
                        "case": name,
                        "passed": item["passed"],
                        "status": item["status"],
                        "steps": item["steps"],
                    }
                ),
                flush=True,
            )
            if any(m.get("http_status") in {400, 401, 402, 403, 404, 422} for m in metrics):
                break
    finally:
        await manager.close()
    report.update(finished_at=utc_now(), passed=sum(c["passed"] for c in report["cases"]), total=4)
    requests = [m for c in report["cases"] for m in c["requests"]]
    report["model_requests"] = len(requests)
    report["usage"] = {
        key: sum(m.get("usage", {}).get(key, 0) for m in requests)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    report["credential_scan"] = scan_release(secret)
    safe_json(root / "docs/live-change-results.json", report, secret)
    print(json.dumps({key: report[key] for key in ("passed", "total", "model_requests", "usage")}))
    return report["passed"] == report["total"]


if __name__ == "__main__":
    if not sys.stdin.isatty():
        raise SystemExit("Use an interactive terminal for the hidden credential prompt")
    if not asyncio.run(evaluate(getpass.getpass("DeepSeek credential (hidden): "))):
        raise SystemExit(1)

"""Generate the importable Postman collection and a secret-free environment template."""

import json
from pathlib import Path


def request(name, method, path, *, body=None, tests=None):
    result = {
        "name": name,
        "request": {"method": method, "header": [], "url": "{{base_url}}" + path},
    }
    if body is not None:
        result["request"]["header"] = [{"key": "Content-Type", "value": "application/json"}]
        result["request"]["body"] = {
            "mode": "raw",
            "raw": json.dumps(body, indent=2),
            "options": {"raw": {"language": "json"}},
        }
    if tests:
        result["event"] = [{"listen": "test", "script": {"type": "text/javascript", "exec": tests}}]
    return result


def start(name, scenario):
    return request(
        name,
        "POST",
        "/runs",
        body={"objective": "Check service health and handle incidents.", "scenario_id": scenario},
        tests=[
            'pm.test("Run accepted", () => pm.response.to.have.status(202));',
            'pm.collectionVariables.set("run_id", pm.response.json().session_id);',
            'pm.collectionVariables.set("poll_count", "0");',
        ],
    )


def poll(name, expected, extra=None):
    tests = [
        'pm.test("Status query succeeds", () => pm.response.to.have.status(200));',
        "const state = pm.response.json();",
        f'const expected = "{expected}";',
        'if (state.status === "RUNNING" && state.status !== expected) {',
        '  const count = Number(pm.collectionVariables.get("poll_count") || "0") + 1;',
        '  pm.collectionVariables.set("poll_count", String(count));',
        "  if (count < 100) { pm.execution.setNextRequest(pm.info.requestName); }",
        '  else { pm.test("Polling completes within 100 requests", () => pm.expect.fail("Timed out")); pm.execution.setNextRequest(null); }',
        "} else {",
        '  pm.collectionVariables.set("poll_count", "0");',
        '  pm.test("Expected run state", () => pm.expect(state.status).to.eql(expected));',
        "  if (state.pending_tool_call) {",
        '    pm.collectionVariables.set("approval_id", state.pending_tool_call.approval_id);',
        '    pm.collectionVariables.set("parameters_hash", state.pending_tool_call.parameters_hash);',
        "  }",
    ]
    tests.extend("  " + line for line in (extra or []))
    tests.append("}")
    return request(name, "GET", "/runs/{{run_id}}", tests=tests)


def approve(name, decision):
    return request(
        name,
        "POST",
        "/runs/{{run_id}}/approvals/{{approval_id}}",
        body={"decision": decision, "parameters_hash": "{{parameters_hash}}"},
        tests=['pm.test("Decision accepted", () => pm.response.to.have.status(202));'],
    )


def incident_count(name, count):
    return request(
        name,
        "GET",
        "/runs/{{run_id}}/incidents",
        tests=[
            'pm.test("Incident query succeeds", () => pm.response.to.have.status(200));',
            f'pm.test("Exactly {count} incident records", () => pm.expect(pm.response.json()).to.have.lengthOf({count}));',
        ],
    )


def main():
    folders = [
        {
            "name": "01 Health and contracts",
            "item": [
                request(
                    "Health",
                    "GET",
                    "/health",
                    tests=['pm.test("Healthy API", () => pm.response.to.have.status(200));'],
                ),
                request(
                    "List deterministic scenarios",
                    "GET",
                    "/scenarios",
                    tests=['pm.test("Scenario catalog", () => pm.response.to.have.status(200));'],
                ),
            ],
        },
        {
            "name": "02 Healthy service",
            "item": [
                start("Create healthy run", "healthy"),
                poll("Poll healthy completion", "COMPLETED"),
                incident_count("Verify no incident", 0),
            ],
        },
        {
            "name": "03 Approve an incident",
            "item": [
                start("Create outage run", "incident"),
                poll("Poll pending approval", "AWAITING_APPROVAL"),
                incident_count("Verify zero writes before approval", 0),
                approve("Approve exact saved incident", "approve"),
                poll("Poll approved completion", "COMPLETED"),
                incident_count("Verify one created incident", 1),
                request(
                    "Export trace",
                    "GET",
                    "/runs/{{run_id}}/trace.jsonl",
                    tests=[
                        'pm.test("Trace export", () => pm.response.to.have.status(200));',
                        'pm.test("Trace contains approval", () => pm.expect(pm.response.text()).to.include("approval_decided"));',
                    ],
                ),
            ],
        },
        {
            "name": "04 Reject an incident",
            "item": [
                start("Create rejected run", "incident"),
                poll("Poll rejection approval", "AWAITING_APPROVAL"),
                approve("Reject saved incident", "reject"),
                poll(
                    "Poll cancellation summary",
                    "COMPLETED",
                    [
                        'pm.test("Rejection observed", () => pm.expect(state.stop_reason).to.eql("user_rejected"));'
                    ],
                ),
                incident_count("Verify rejection prevents creation", 0),
            ],
        },
        {
            "name": "05 Retry and model correction",
            "item": [
                start("Create transient failure run", "retry"),
                poll(
                    "Poll retry recovery",
                    "COMPLETED",
                    ['pm.test("Retry counted", () => pm.expect(state.tool_attempts).to.eql(2));'],
                ),
                start("Create malformed JSON run", "malformed_json"),
                poll(
                    "Poll model correction",
                    "COMPLETED",
                    [
                        'pm.test("Correction consumes a step", () => pm.expect(state.current_step).to.eql(3));'
                    ],
                ),
                start("Create permanent failure run", "persistent_failure"),
                poll(
                    "Poll permanent failure",
                    "FAILED",
                    [
                        'pm.test("Three bounded attempts", () => pm.expect(state.tool_attempts).to.eql(3));'
                    ],
                ),
            ],
        },
        {
            "name": "06 Execution limit",
            "item": [
                start("Create looping run", "loop"),
                poll(
                    "Poll bounded loop",
                    "LIMIT_EXCEEDED",
                    [
                        'pm.test("Step limit", () => pm.expect(state.stop_reason).to.eql("step_limit"));'
                    ],
                ),
                request(
                    "Read bounded execution events",
                    "GET",
                    "/runs/{{run_id}}/events?after=0",
                    tests=[
                        'pm.test("Execution limit event", () => pm.expect(pm.response.json().some(e => e.event_type === "execution_limit")).to.eql(true));'
                    ],
                ),
            ],
        },
    ]

    def change_query(name, assertions):
        return request(
            name,
            "GET",
            "/runs/{{run_id}}/changes",
            tests=[
                'pm.test("Change audit query succeeds", () => pm.response.to.have.status(200));',
                "const changeAudit = pm.response.json();",
                *[line.replace("data.", "changeAudit.") for line in assertions],
            ],
        )

    folders.extend(
        [
            {
                "name": "07 Safe deployment",
                "item": [
                    start("Create deployment run", "deploy_success"),
                    poll("Poll deployment approval", "AWAITING_APPROVAL"),
                    change_query(
                        "Verify snapshot without deployment",
                        [
                            'pm.test("No deployment before approval", () => pm.expect(data.changes).to.have.lengthOf(0));',
                            'pm.test("Snapshot already captured", () => pm.expect(data.audit.some(e => e.event === "snapshot_captured")).to.eql(true));',
                        ],
                    ),
                    approve("Approve exact deployment", "approve"),
                    poll("Poll verified deployment", "COMPLETED"),
                    change_query(
                        "Inspect deployed revision and audit",
                        [
                            'pm.test("One verified deployment", () => {pm.expect(data.changes).to.have.lengthOf(1); pm.expect(data.changes[0].verified).to.eql(true);});',
                            'pm.test("Audit links present", () => pm.expect(data.audit.every(e => e.entry_hash && e.trace_id && e.actor_id)).to.eql(true));',
                        ],
                    ),
                ],
            },
            {
                "name": "08 Automatic failure rollback",
                "item": [
                    start("Create unhealthy deployment", "deploy_rollback"),
                    poll("Poll compensation approval", "AWAITING_APPROVAL"),
                    approve("Approve deployment with compensation", "approve"),
                    poll(
                        "Poll safely rolled back failure",
                        "FAILED",
                        [
                            'pm.test("Deployment failure reported", () => pm.expect(state.stop_reason).to.eql("change_rolled_back"));'
                        ],
                    ),
                    change_query(
                        "Verify automatic rollback",
                        [
                            'pm.test("Restored saved revision", () => pm.expect(data.changes[0].status).to.eql("ROLLED_BACK"));'
                        ],
                    ),
                ],
            },
            {
                "name": "09 Capacity preflight",
                "item": [
                    start("Create constrained deployment", "deploy_capacity_blocked"),
                    poll("Poll capacity refusal", "FAILED"),
                    change_query(
                        "Verify capacity blocks changes",
                        [
                            'pm.test("No capacity no deployment", () => pm.expect(data.changes).to.have.lengthOf(0));',
                            'pm.test("CPU check blocks", () => pm.expect(data.audit[0].result.checks.cpu_capacity).to.eql(false));',
                        ],
                    ),
                ],
            },
            {
                "name": "10 Later health failure",
                "item": [
                    start("Create later failure deployment", "deploy_late_failure"),
                    poll("Poll initial release approval", "AWAITING_APPROVAL"),
                    approve("Approve initial release", "approve"),
                    poll("Poll separate rollback approval", "AWAITING_APPROVAL"),
                    change_query(
                        "Inspect unhealthy release before rollback",
                        [
                            'pm.test("Rollback awaits a separate decision", () => pm.expect(data.changes[0].status).to.eql("UNHEALTHY"));'
                        ],
                    ),
                    approve("Approve exact rollback target", "approve"),
                    poll("Poll later failure recovery", "FAILED"),
                    change_query(
                        "Verify rollback safety snapshot",
                        [
                            'pm.test("Rollback captured current state", () => pm.expect(data.changes[0].rollback_snapshot_id).to.be.a("string"));'
                        ],
                    ),
                ],
            },
        ]
    )
    collection = {
        "info": {
            "name": "Operations Agent Harness",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            "description": "Scripted-mode acceptance collection. Run with 100 ms request delay; default server budgets apply. Approval is tied to the fetched action hash.",
        },
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{api_token}}", "type": "string"}],
        },
        "variable": [
            {"key": "base_url", "value": "http://127.0.0.1:8000"},
            {"key": "api_token", "value": "local-demo-token"},
        ],
        "item": folders,
    }
    root = Path(__file__).resolve().parent.parent
    (root / "postman_collection.json").write_text(
        json.dumps(collection, indent=2) + "\n", encoding="utf-8"
    )
    environment = {
        "name": "Operations Harness Local",
        "values": [
            {"key": "base_url", "value": "http://127.0.0.1:8000", "enabled": True},
            {"key": "api_token", "value": "local-demo-token", "enabled": True, "type": "secret"},
        ],
        "_postman_variable_scope": "environment",
    }
    (root / "postman_environment.json").write_text(
        json.dumps(environment, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()

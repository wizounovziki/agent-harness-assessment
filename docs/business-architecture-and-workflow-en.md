# Operations Agent Harness: Business Architecture

Updated: 2026-11-13 (Asia/Shanghai)

The assistant supports incident response and controlled service changes. ReAct supplies evidence-driven decisions; the harness enforces authorization, contracts, approval, recovery prerequisites and execution limits.

## Layered architecture

![Layered business architecture](./images/operations-layered-architecture-en-v3.png)

| Layer | Modules and responsibilities |
| --- | --- |
| Access & interaction | CLI/HTTP goals, exact user decisions, run/change/audit queries |
| Operations business logic | Service assessment, runbook diagnosis, incident planning, change planning, verification and recovery |
| Execution & safety | Resource preflight, sealed snapshots, exact approval, revision checks, bounded execution and write reconciliation |
| Model & tool adapters | DeepSeek/scripted model; three incident tools; five change-control tools |
| Data & evidence | SQLite state/claims, versioned snapshots, linked audit hashes, mock environments and regression evidence |

The architecture groups responsibilities in stacked layers. Agent-state snapshots support resuming execution; service snapshots support restoring workloads. Both are persisted independently.

## Business workflow

![Controlled operations workflow](./images/operations-business-workflow-en-v2.png)

Incident response gathers service/runbook evidence, proposes an incident, waits for approval and reports the confirmed result.

Service changes follow **preflight → service snapshot → exact approval → environment/revision recheck → execution → health verification**. A blocked check prevents deployment. Immediate failure compensation uses the approved recovery scope. Later health failure requires separately approved rollback, a fresh capacity/version check and a snapshot of the state being reverted.

Unverified changes and failed recovery require operator review. Restoring an earlier release is recorded as a failed deployment with confirmed recovery. Critical operations retain operator, approval, parameters, versions, snapshots and trace links.

## Implementation mapping

- src/harness.py: bounded loop, approval and final evidence checks.
- src/change_schemas.py: workload plans, resource, snapshot and recovery contracts.
- src/operations.py: transactional mock preflight, changes, recovery and audit ledger.
- src/tools.py: eight registered tools with validated boundaries and injected faults.
- src/storage.py and src/tracer.py: ownership, approvals, correlated events and JSONL exports.
- data/mock_deployments.json: trusted capacities, operator policy and immutable mock artifacts.

Detailed contracts and production adapter requirements: [operations-safety.md](./operations-safety.md).
Image prompts and versions: [image-generation-prompts-en.md](./image-generation-prompts-en.md).

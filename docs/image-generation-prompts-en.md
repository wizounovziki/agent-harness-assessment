# English Diagram Generation Record

Updated: 2026-11-13 (Asia/Shanghai)

Method: built-in image generation. Existing English images were edit targets. The architecture remains five stacked layers; the workflow retains incident response and adds controlled service changes. Business capabilities dominate the architecture; ReAct is a supporting decision method.

Selected assets:

- [Layered architecture v3](./images/operations-layered-architecture-en-v3.png)
- [Controlled business workflow v2](./images/operations-business-workflow-en-v2.png)

The workflow was visually inspected. A follow-up edit corrected the incident approval branches so rejection terminates and approval leads directly to creation. Both final images were inspected for legibility and implemented business semantics.

## Architecture prompt

```text
Use case: infographic-diagram. Asset type: English architecture slide for an operations Agent Harness assessment. Edit target: the supplied layered architecture image. Keep the clean professional style, dark navy type, white background, teal and pale-blue panels, and orange approval highlights. Redesign the content to reflect implemented change safety. This MUST be an architecture with FIVE HORIZONTAL STACKED LAYERS and modular cards, no flow arrows, no flowchart diamonds. Wide landscape, crisp readable typography.
Title: "Operations Agent Harness"
Subtitle: "Incident response and controlled service changes"
Layer 1 label: "ACCESS & INTERACTION". Three cards: "Goal Intake" / "CLI, HTTP API & Postman"; "User Decisions" / "Approve, reject or cancel"; "Run & Audit Queries" / "Status, changes and evidence".
Layer 2 label: "OPERATIONS BUSINESS LOGIC". Five cards: "Service Assessment", "Runbook Diagnosis", "Incident Planning", "Change Planning", "Verification & Recovery". A small secondary badge says "Decision method: ReAct". A bottom strip says "Service catalog | SEV rules | Approved change plans | Completion evidence".
Layer 3 label: "EXECUTION & SAFETY". Six cards: "Resource Preflight", "Sealed Snapshots", "Exact Approval", "Revision Checks", "Bounded Execution", "Write Reconciliation". Small line below: "Recheck policy and environment before every change".
Layer 4 label: "MODEL & TOOL ADAPTERS". Three cards: "LLM Adapter" / "deepseek-flash / scripted"; "Incident Tools (3)" / "Knowledge | Status | Create"; "Change Tools (5)" / "Readiness | Snapshot | Execute | Verify | Rollback". Orange small badge under the last two cards: "Approval required for critical writes".
Layer 5 label: "DATA & EVIDENCE". Four cards: "SQLite State & Leases", "Versioned Snapshots", "Linked Audit & Hashes", "Mock Data & Tests".
Footer: "Python | FastAPI | Pydantic | SQLite | Mock infrastructure".
All text in English. Preserve exact model spelling. Business modules are most prominent; ReAct is a small supporting note. No logos, no watermark.
```

## Workflow prompt

```text
Use case: infographic-diagram. Asset type: English business workflow slide. Edit target: supplied previous operations workflow image. Update the workflow for BOTH incident response and controlled service changes. Clean professional white background, navy typography, pale blue process boxes, orange approvals, green confirmed results, red blocked/failed outcomes. Landscape high resolution, readable text, tidy orthogonal arrows without crossing labels.
Title: "Operations Assistant: Controlled Business Workflow".
At top "Goal & service context" splits into two lanes.
Smaller left lane titled "Incident Response": "Gather status & runbook evidence" -> "Draft incident" -> orange "User approval" -> "Create & confirm incident" -> green "Evidence report". A rejected approval points to "Cancelled; no write".
Larger right lane titled "Service Changes": "Permission & resource preflight" -> diamond "Ready?". No goes to red "Blocked; no change". Yes goes to "Capture sealed snapshot" -> orange "Approve exact plan & recovery scope" -> "Recheck environment & revision" -> "Apply change" -> "Verify service health". A rejected approval goes to "Cancelled; no write".
The health verification decision has a green "Healthy" arrow to "Verified change report". The unhealthy branch leads to a clearly bounded recovery panel: "Immediate failure: use approved rollback scope" OR "Later failure: obtain separate rollback approval". Both then go to "Recheck capacity & revision" -> "Snapshot current state" -> "Restore approved snapshot" -> diamond "Recovery healthy?". Yes goes to red "FAILED: change rolled back" (successful recovery does not mean successful deployment). No goes to orange "NEEDS_REVIEW".
Do not imply unconditional automatic rollback. Small caption in recovery panel: "Never overwrite a newer revision".
Across the bottom, a full-width band: "Every critical operation retains actor, approval, parameters, versions, snapshot and linked audit records".
Bottom small note: "Validated tools | Timeouts and retry budgets | Persistent state | Mock infrastructure".
All words must be English. Keep the incident path compact so change safety is the focus. Show business flow, not source code. No watermark.
```

## Workflow correction

```text
Edit ONLY the Incident Response lane on the left of this workflow image to correct the approval branches. Preserve the title, service-change lane, recovery panel, colors, typography, and all other content exactly.
The orange "User approval" box must have TWO SEPARATE paths:
1. Red "Rejected" arrow goes to a red terminal "Cancelled; no write" box and STOPS THERE. There must be NO arrow from the cancelled box to "Create & confirm incident".
2. Green "Approved" arrow must bypass the cancelled box entirely and go DIRECTLY to the blue "Create & confirm incident" box, then continue to "Evidence report".
Make the red cancellation box narrower and align it to the left within the incident lane, leaving enough room along its right side for a vertical green approved path. Remove the current blue arrow that incorrectly connects cancellation to creation. Make the branch geometry unambiguous and labels readable. Do not change any service-change or recovery logic. All text remains English.
```

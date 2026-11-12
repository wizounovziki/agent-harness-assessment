"""CLI and FastAPI server entry point."""

import argparse
import asyncio
import json
import logging

import uvicorn

from src.config import Settings
from src.errors import HarnessError
from src.schemas import AgentStatus, CreateIncidentInput
from src.service import RunManager


async def interactive_approval(draft: CreateIncidentInput) -> bool:
    print("\nApproval requested for this exact incident:")
    print(draft.model_dump_json(indent=2))
    try:
        answer = await asyncio.to_thread(
            input, 'Type "approve" to create it; any other response rejects: '
        )
    except EOFError:
        return False
    return answer.strip() == "approve"


async def execute_cli(args) -> int:
    manager = RunManager(Settings.from_env())
    try:
        if args.command in {"run", "resume"}:
            callback = None if args.pause else interactive_approval
            if args.command == "run":
                harness = manager.harness_for(args.scenario, approval_callback=callback)
                state = harness.new_state(
                    args.objective,
                    scenario_id=args.scenario,
                    owner_id="demo-user",
                    limits=manager.settings.limits.model_copy(deep=True),
                )
            else:
                state = manager.get(args.session_id, "demo-user")
                harness = manager.harness_for(state.scenario_id, approval_callback=callback)
            state = await harness.run(state)
            print(
                json.dumps(
                    {
                        "session_id": state.session_id,
                        "status": state.status.value,
                        "stop_reason": state.stop_reason,
                        "current_step": state.current_step,
                        "tool_attempts": state.tool_attempts,
                        "final_answer": state.final_answer,
                        "pending_tool_call": state.pending_tool_call.model_dump()
                        if state.pending_tool_call
                        else None,
                        "trace_path": str(manager.tracer.path(state.session_id)),
                    },
                    indent=2,
                )
            )
            return {
                AgentStatus.COMPLETED: 0,
                AgentStatus.AWAITING_APPROVAL: 3,
                AgentStatus.LIMIT_EXCEEDED: 4,
            }.get(state.status, 2)
        if args.command == "show":
            print(manager.get(args.session_id, "demo-user").model_dump_json(indent=2))
        elif args.command == "export":
            manager.get(args.session_id, "demo-user")
            print(manager.tracer.rebuild_export(args.session_id))
        return 0
    finally:
        await manager.close()


def main():
    parser = argparse.ArgumentParser(description="Operations Agent Harness")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="Run an objective; print exact incident details before approval"
    )
    run.add_argument("objective")
    run.add_argument("--scenario", default="incident")
    run.add_argument("--pause", action="store_true", help="Persist approval request and exit")
    resume = commands.add_parser(
        "resume", help="Resume a persisted run, including pending approval"
    )
    resume.add_argument("session_id")
    resume.add_argument("--pause", action="store_true")
    for name in ("show", "export"):
        commands.add_parser(name).add_argument("session_id")
    serve = commands.add_parser("serve", help="Start the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "serve":
        uvicorn.run("src.api:create_app", factory=True, host=args.host, port=args.port, workers=1)
        return
    try:
        raise SystemExit(asyncio.run(execute_cli(args)))
    except (HarnessError, ValueError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()

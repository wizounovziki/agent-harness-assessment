"""Optional paid live-model verification, using only the read-only healthy scenario."""

import asyncio
import json
from dataclasses import replace

from src.config import PROJECT_ROOT, Settings
from src.schemas import AgentStatus
from src.service import RunManager


async def main():
    settings = replace(
        Settings.from_env(),
        llm_mode="real",
        database_path=PROJECT_ROOT / "runtime/live-smoke.sqlite3",
        trace_dir=PROJECT_ROOT / "runtime/live-traces",
    )
    manager = RunManager(settings)
    try:
        harness = manager.harness_for("healthy")
        state = harness.new_state(
            "Check auth service health, report the observed status and latency. "
            "Create no incident for a healthy service.",
            scenario_id="healthy",
        )
        state = await harness.run(state)
        result = {
            "executed": True,
            "model": settings.llm_model,
            "status": state.status.value,
            "stop_reason": state.stop_reason,
            "session_id": state.session_id,
            "steps": state.current_step,
            "tool_attempts": state.tool_attempts,
        }
        (PROJECT_ROOT / "runtime/live-model-smoke.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, indent=2))
        if state.status != AgentStatus.COMPLETED:
            raise SystemExit(1)
    finally:
        await manager.close()


if __name__ == "__main__":
    asyncio.run(main())

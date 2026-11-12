"""Durable step events and redacted, flushed JSONL exports."""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.errors import ConflictError, PersistenceError
from src.schemas import AgentState, StepTrace
from src.storage import SQLiteStore

logger = logging.getLogger("operations_harness")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(
                word in key.lower()
                for word in ("api_key", "authorization", "password", "secret", "access_token")
            )
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", value)
    return value


class Tracer:
    def __init__(self, store: SQLiteStore, directory: Path):
        self.store = store
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, session_id: str) -> Path:
        # Session IDs are server-generated UUID hex values, never file names supplied by clients.
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValueError("Invalid session ID for trace export")
        return self.directory / f"{session_id}.jsonl"

    def record(
        self,
        state: AgentState,
        event: StepTrace,
        action_result: tuple[str, dict[str, Any]] | None = None,
    ) -> None:
        event.event_id = uuid4().hex
        event.sequence = len(state.traces) + 1
        # Persist and export the same sanitized event.
        event = StepTrace.model_validate(redact(event.model_dump()))
        state.traces.append(event)
        try:
            self.store.save(state, event, action_result)
        except (PersistenceError, ConflictError):
            state.traces.pop()
            raise
        payload = {"session_id": state.session_id, **event.model_dump()}
        try:
            with self.path(state.session_id).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise PersistenceError(
                "JSONL trace flush failed; SQLite retains the committed event"
            ) from exc
        logger.info(
            json.dumps(
                {
                    "session_id": state.session_id,
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "error": event.error,
                }
            )
        )

    def rebuild_export(self, session_id: str) -> Path:
        path = self.path(session_id)
        temporary = path.with_suffix(".jsonl.new")
        with temporary.open("w", encoding="utf-8") as stream:
            for event in self.store.events(session_id):
                stream.write(
                    json.dumps({"session_id": session_id, **event}, ensure_ascii=False) + "\n"
                )
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        return path

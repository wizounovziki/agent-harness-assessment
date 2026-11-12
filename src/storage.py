"""SQLite snapshots, atomic approval decisions and idempotent mock writes."""

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from src.errors import ConflictError, NotFoundError, PersistenceError
from src.schemas import AgentState, AgentStatus, CreateIncidentInput, StepTrace, utc_now


def parameters_hash(tool_name: str, args: dict[str, Any]) -> str:
    canonical = json.dumps([tool_name, args], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class SQLiteStore:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS runs (
                session_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                status TEXT NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
                claimed_by TEXT, lease_until REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                session_id TEXT NOT NULL REFERENCES runs(session_id),
                sequence INTEGER NOT NULL, event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (session_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES runs(session_id),
                tool_name TEXT NOT NULL, parameters_hash TEXT NOT NULL,
                arguments TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, result TEXT
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES runs(session_id),
                action_id TEXT NOT NULL REFERENCES actions(action_id), parameters_hash TEXT NOT NULL,
                status TEXT NOT NULL, expires_at TEXT NOT NULL,
                decided_by TEXT, decided_at TEXT
            );
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES runs(session_id),
                action_id TEXT UNIQUE NOT NULL, idempotency_key TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tool_counters (
                session_id TEXT NOT NULL, tool_name TEXT NOT NULL, attempts INTEGER NOT NULL,
                PRIMARY KEY (session_id, tool_name)
            );
        """)

    def create(self, state: AgentState) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO runs(session_id,owner_id,status,revision,snapshot) VALUES(?,?,?,?,?)",
                    (
                        state.session_id,
                        state.owner_id,
                        state.status.value,
                        0,
                        state.model_dump_json(),
                    ),
                )
        except sqlite3.Error as exc:
            raise PersistenceError("Could not create run snapshot") from exc

    def _snapshot(self, state: AgentState, event: StepTrace | None = None) -> None:
        next_revision = state.revision + 1
        saved = state.model_copy(update={"revision": next_revision, "updated_at": utc_now()})
        cursor = self._conn.execute(
            "UPDATE runs SET status=?,revision=?,snapshot=? WHERE session_id=? AND revision=?",
            (
                saved.status.value,
                next_revision,
                saved.model_dump_json(),
                state.session_id,
                state.revision,
            ),
        )
        if cursor.rowcount != 1:
            raise ConflictError("Run changed concurrently; reload its persisted state")
        pending = state.pending_tool_call
        if pending:
            self._conn.execute(
                """INSERT INTO actions VALUES(?,?,?,?,?,?,NULL)
                   ON CONFLICT(action_id) DO UPDATE SET attempts=excluded.attempts""",
                (
                    pending.action_id,
                    state.session_id,
                    pending.tool_name,
                    pending.parameters_hash,
                    json.dumps(pending.tool_args),
                    pending.attempts,
                ),
            )
            if pending.approval_id:
                self._conn.execute(
                    """INSERT INTO approvals(approval_id,session_id,action_id,parameters_hash,status,expires_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(approval_id) DO NOTHING""",
                    (
                        pending.approval_id,
                        state.session_id,
                        pending.action_id,
                        pending.parameters_hash,
                        pending.approval_status,
                        pending.expires_at,
                    ),
                )
        if event:
            self._conn.execute(
                "INSERT INTO events VALUES(?,?,?,?)",
                (state.session_id, event.sequence, event.event_id, event.model_dump_json()),
            )
        state.revision = next_revision
        state.updated_at = saved.updated_at

    def save(
        self,
        state: AgentState,
        event: StepTrace | None = None,
        action_result: tuple[str, dict[str, Any]] | None = None,
    ) -> None:
        old_revision = state.revision
        try:
            with self._lock, self._conn:
                self._snapshot(state, event)
                if action_result:
                    self._conn.execute(
                        "UPDATE actions SET result=? WHERE action_id=?",
                        (json.dumps(action_result[1]), action_result[0]),
                    )
        except sqlite3.Error as exc:
            state.revision = old_revision
            raise PersistenceError("Could not persist run transition") from exc

    def get(self, session_id: str, owner_id: str | None = None) -> AgentState:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM runs WHERE session_id=?", (session_id,)
                ).fetchone()
            if not row or (owner_id is not None and row["owner_id"] != owner_id):
                raise NotFoundError("Run not found")
            return AgentState.model_validate_json(row["snapshot"])
        except sqlite3.Error as exc:
            raise PersistenceError("Could not read run") from exc

    def list_active(self) -> list[AgentState]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT snapshot FROM runs WHERE status IN ('RUNNING','AWAITING_APPROVAL')"
            ).fetchall()
        return [AgentState.model_validate_json(row[0]) for row in rows]

    def claim(self, session_id: str, runner_id: str, seconds: float) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """UPDATE runs SET claimed_by=?,lease_until=? WHERE session_id=?
                   AND (claimed_by IS NULL OR lease_until<?)""",
                (runner_id, time.time() + seconds, session_id, time.time()),
            )
            return cursor.rowcount == 1

    def release(self, session_id: str, runner_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET claimed_by=NULL,lease_until=0 WHERE session_id=? AND claimed_by=?",
                (session_id, runner_id),
            )

    def decide_approval(
        self, session_id: str, approval_id: str, digest: str, decision: str, owner_id: str
    ) -> tuple[AgentState, bool]:
        with self._lock, self._conn:
            state = self.get(session_id, owner_id)
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE approval_id=? AND session_id=?",
                (approval_id, session_id),
            ).fetchone()
            if not row:
                raise NotFoundError("Approval not found")
            target = "APPROVED" if decision == "approve" else "REJECTED"
            if row["parameters_hash"] != digest:
                raise ConflictError("Approval parameters do not match the saved action")
            if row["status"] == target:
                return state, False
            if row["status"] != "PENDING" or state.status != AgentStatus.AWAITING_APPROVAL:
                raise ConflictError("Approval is no longer pending")
            if datetime.fromisoformat(row["expires_at"]) <= datetime.fromisoformat(utc_now()):
                raise ConflictError("Approval expired")
            pending = state.pending_tool_call
            if not pending or pending.approval_id != approval_id:
                raise ConflictError("Approval does not match the pending action")
            if parameters_hash(pending.tool_name, pending.tool_args) != digest:
                raise ConflictError("Saved action parameters changed")
            self._conn.execute(
                "UPDATE approvals SET status=?,decided_by=?,decided_at=? WHERE approval_id=?",
                (target, owner_id, utc_now(), approval_id),
            )
            pending.approval_status = target
            state.status = AgentStatus.RUNNING
            if decision == "reject":
                state.incident_creation_rejected = True
            self._snapshot(state)
            return state, True

    def expire_approval(self, state: AgentState) -> None:
        pending = state.pending_tool_call
        if not pending or not pending.approval_id:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE approvals SET status='EXPIRED' WHERE approval_id=? AND status='PENDING'",
                (pending.approval_id,),
            )
            pending.approval_status = "EXPIRED"
            state.status = AgentStatus.STOPPED
            state.stop_reason = "approval_expired"
            state.final_answer = "Approval expired. No incident was created."
            self._snapshot(state)

    def next_tool_attempt(self, session_id: str, tool_name: str) -> int:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO tool_counters VALUES(?,?,1) ON CONFLICT(session_id,tool_name)
                   DO UPDATE SET attempts=attempts+1""",
                (session_id, tool_name),
            )
            return self._conn.execute(
                "SELECT attempts FROM tool_counters WHERE session_id=? AND tool_name=?",
                (session_id, tool_name),
            ).fetchone()[0]

    def approval_allows(self, state: AgentState, action_id: str, digest: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """SELECT a.* FROM approvals a JOIN runs r ON a.session_id=r.session_id
                   WHERE a.session_id=? AND a.action_id=? AND a.parameters_hash=?
                   AND a.status='APPROVED' AND r.status='RUNNING' AND r.revision=?""",
                (state.session_id, action_id, digest, state.revision),
            ).fetchone()
            return bool(
                row
                and datetime.fromisoformat(row["expires_at"]) > datetime.fromisoformat(utc_now())
            )

    def lookup_incident(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if not row:
                return None
            return {
                "incident_id": row["incident_id"],
                "status": "CREATED",
                "created_at": row["created_at"],
            }

    def create_incident(
        self, state: AgentState, action_id: str, args: CreateIncidentInput
    ) -> dict[str, Any]:
        digest = parameters_hash("create_incident", args.model_dump())
        key = f"{state.session_id}:{digest}"
        with self._lock, self._conn:
            if not self.approval_allows(state, action_id, digest):
                raise ConflictError(
                    "Incident creation requires a current approval for these parameters"
                )
            existing = self.lookup_incident(key)
            if existing:
                return existing
            incident_id = "INC-" + hashlib.sha256(key.encode()).hexdigest()[:10].upper()
            created_at = utc_now()
            self._conn.execute(
                "INSERT INTO incidents VALUES(?,?,?,?,?,?)",
                (incident_id, state.session_id, action_id, key, args.model_dump_json(), created_at),
            )
            return {"incident_id": incident_id, "status": "CREATED", "created_at": created_at}

    def incidents(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM incidents WHERE session_id=?", (session_id,)
            ).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]

    def events(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM events WHERE session_id=? AND sequence>? ORDER BY sequence",
                (session_id, after),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def close(self) -> None:
        self._conn.close()

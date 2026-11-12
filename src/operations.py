"""Transactional mock deployment control: preflight, snapshots, changes and audit."""

import hashlib
import json
from datetime import datetime, timedelta

from src.change_schemas import ChangePlan, DeploymentCatalog
from src.errors import ToolError
from src.schemas import utc_now
from src.storage import parameters_hash
from src.tracer import redact


class ChangeSafetyError(ToolError):
    code = "CHANGE_BLOCKED"


def checksum(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ChangeControl:
    def __init__(self, store, catalog: DeploymentCatalog):
        self.store, self.catalog = store, catalog
        with store.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ops_resources (resource_key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ops_environments (name TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ops_objects (kind TEXT NOT NULL, id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES runs(session_id), payload TEXT NOT NULL, PRIMARY KEY(kind,id))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ops_audit (sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES runs(session_id), payload TEXT NOT NULL, previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL)"
            )
            for name, env in catalog.environments.items():
                conn.execute(
                    "INSERT OR IGNORE INTO ops_environments VALUES(?,?)",
                    (name, env.model_dump_json()),
                )
            for env, services in catalog.workloads.items():
                for name, manifest in services.items():
                    payload = {
                        "service_name": name,
                        "environment": env,
                        "revision": 1,
                        "manifest": manifest.model_dump(),
                        "configuration": catalog.configurations[manifest.config_version],
                        "health": "HEALTHY",
                    }
                    conn.execute(
                        "INSERT OR IGNORE INTO ops_resources VALUES(?,?)",
                        (f"{env}/{name}", json.dumps(payload)),
                    )

    def _resource(self, conn, plan):
        row = conn.execute(
            "SELECT payload FROM ops_resources WHERE resource_key=?",
            (f"{plan.environment}/{plan.service_name}",),
        ).fetchone()
        if not row:
            raise ChangeSafetyError("Service/environment is not registered")
        return json.loads(row[0])

    def _put_resource(self, conn, resource):
        conn.execute(
            "UPDATE ops_resources SET payload=? WHERE resource_key=?",
            (json.dumps(resource), f"{resource['environment']}/{resource['service_name']}"),
        )

    def _owned(self, conn, kind, identity, context):
        row = conn.execute(
            "SELECT payload,session_id FROM ops_objects WHERE kind=? AND id=?", (kind, identity)
        ).fetchone()
        if not row or row[1] != context.state.session_id:
            raise ChangeSafetyError("Change-control record is unavailable for this run")
        item = json.loads(row[0])
        if item["owner_id"] != context.state.owner_id:
            raise ChangeSafetyError("Change-control record is unavailable for this operator")
        return item

    def _put(self, conn, kind, identity, context, item):
        item = item | {"owner_id": context.state.owner_id}
        conn.execute(
            "INSERT INTO ops_objects VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload",
            (kind, identity, context.state.session_id, json.dumps(item)),
        )

    def _audit(self, conn, context, event, **details):
        previous = conn.execute(
            "SELECT entry_hash FROM ops_audit WHERE session_id=? ORDER BY sequence DESC LIMIT 1",
            (context.state.session_id,),
        ).fetchone()
        previous_hash = previous[0] if previous else "0" * 64
        payload = redact(
            {
                "event": event,
                "timestamp": utc_now(),
                "trace_id": context.state.session_id,
                "span_id": hashlib.sha256(context.action_id.encode()).hexdigest()[:16],
                "parent_span_id": context.state.session_id[:16],
                "actor_id": context.state.owner_id,
                "action_id": context.action_id,
                **details,
            }
        )
        entry_hash = checksum({"previous_hash": previous_hash, "payload": payload})
        conn.execute(
            "INSERT INTO ops_audit(session_id,payload,previous_hash,entry_hash) VALUES(?,?,?,?)",
            (context.state.session_id, json.dumps(payload), previous_hash, entry_hash),
        )

    def _checks(self, conn, plan, context, *, slots=2, permission=None):
        resource = self._resource(conn, plan)
        env = json.loads(
            conn.execute(
                "SELECT payload FROM ops_environments WHERE name=?", (plan.environment,)
            ).fetchone()[0]
        )
        resources = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM ops_resources")]
        used = {
            key: sum(
                r["manifest"][key] * r["manifest"]["replicas"]
                for r in resources
                if r["environment"] == plan.environment
            )
            for key in ("cpu_millis", "memory_mb")
        }
        available = {key: env[key] - used[key] for key in used}
        old = resource["manifest"]
        required = {}
        for key in used:
            old_total, new_total = old[key] * old["replicas"], getattr(plan, key) * plan.replicas
            surge = 0 if plan.operation == "decommission" else plan.surge_replicas
            peak = max(old_total + surge * getattr(plan, key), new_total + surge * old[key])
            required[key] = max(0, peak - old_total)
        snapshots = sum(
            json.loads(r[0])["environment"] == plan.environment
            for r in conn.execute("SELECT payload FROM ops_objects WHERE kind='snapshot'")
        )
        available["snapshot_slots"] = env["snapshot_slots"] - snapshots
        required["snapshot_slots"] = slots
        artifact = self.catalog.artifacts.get(plan.image_digest)
        old_artifact = self.catalog.artifacts.get(old["image_digest"])
        checks = {
            "recovery_authorized": "rollback" in env["allowed_operations"],
            "operator_authorized": context.state.owner_id in env["allowed_operators"]
            and (permission or plan.operation) in env["allowed_operations"],
            "cluster_ready": env["cluster_ready"],
            "dependencies_ready": env["dependencies_ready"],
            "artifact_available_and_verified": bool(
                artifact and artifact.retained and artifact.verified
            ),
            "rollback_artifact_retained": bool(
                old_artifact and old_artifact.retained and old_artifact.verified
            ),
            "configuration_registered": plan.config_version in self.catalog.configurations,
            "cpu_capacity": available["cpu_millis"] >= required["cpu_millis"],
            "memory_capacity": available["memory_mb"] >= required["memory_mb"],
            "snapshot_capacity": available["snapshot_slots"] >= slots,
        }
        return resource, checks, available, required

    def preflight(self, args, context):
        with self.store.transaction() as conn:
            identity = "pre-" + hashlib.sha256(context.action_id.encode()).hexdigest()[:24]
            existing = conn.execute(
                "SELECT payload FROM ops_objects WHERE kind='preflight' AND id=?", (identity,)
            ).fetchone()
            if existing:
                return json.loads(existing[0])["result"]
            resource, checks, available, required = self._checks(conn, args.plan, context)
            expires = (datetime.fromisoformat(utc_now()) + timedelta(seconds=300)).isoformat()
            result = {
                "preflight_id": identity,
                "configuration_checksum": checksum(
                    self.catalog.configurations.get(args.plan.config_version, {})
                ),
                "status": "READY" if all(checks.values()) else "BLOCKED",
                "checks": checks,
                "available": available,
                "required_extra": required,
                "source_revision": resource["revision"],
                "expires_at": expires,
            }
            self._put(
                conn,
                "preflight",
                identity,
                context,
                {"plan": args.plan.model_dump(), "result": result},
            )
            self._audit(
                conn,
                context,
                "preflight_completed",
                preflight_id=identity,
                plan=args.plan.model_dump(),
                result=result,
            )
            return result

    def _fresh(self, conn, preflight, context, slots):
        result = preflight["result"]
        if result["status"] != "READY" or datetime.fromisoformat(
            result["expires_at"]
        ) <= datetime.fromisoformat(utc_now()):
            raise ChangeSafetyError("A fresh successful preflight is required")
        plan = ChangePlan.model_validate(preflight["plan"])
        if checksum(self.catalog.configurations.get(plan.config_version, {})) != result.get(
            "configuration_checksum"
        ):
            raise ChangeSafetyError("Target configuration changed after preflight")
        current, checks, available, required = self._checks(conn, plan, context, slots=slots)
        if not all(checks.values()) or current["revision"] != result["source_revision"]:
            raise ChangeSafetyError(
                "Readiness or service revision changed; repeat preflight and approval"
            )
        self._audit(
            conn,
            context,
            "preflight_revalidated",
            preflight_id=result["preflight_id"],
            checks=checks,
            available=available,
            required_extra=required,
            revision=current["revision"],
        )
        return current

    def _snapshot(self, conn, context, identity, preflight_id, current):
        content = {
            "preflight_id": preflight_id,
            "service_name": current["service_name"],
            "environment": current["environment"],
            "manifest": current["manifest"],
            "configuration": current["configuration"],
            "source_revision": current["revision"],
        }
        result = {
            "snapshot_id": identity,
            "preflight_id": preflight_id,
            **content,
            "checksum": checksum(content),
            "created_at": utc_now(),
        }
        self._put(
            conn,
            "snapshot",
            identity,
            context,
            {
                "environment": current["environment"],
                "service_name": current["service_name"],
                "result": result,
            },
        )
        self._audit(
            conn,
            context,
            "snapshot_captured",
            snapshot_id=identity,
            checksum=result["checksum"],
            source_revision=current["revision"],
        )
        return result

    def snapshot(self, args, context):
        with self.store.transaction() as conn:
            preflight = self._owned(conn, "preflight", args.preflight_id, context)
            identity = "snap-" + args.preflight_id
            existing = conn.execute(
                "SELECT payload FROM ops_objects WHERE kind='snapshot' AND id=?", (identity,)
            ).fetchone()
            if existing:
                return self._owned(conn, "snapshot", identity, context)["result"]
            current = self._fresh(conn, preflight, context, 2)
            return self._snapshot(conn, context, identity, args.preflight_id, current)

    def _sealed_snapshot(self, conn, identity, context):
        snapshot = self._owned(conn, "snapshot", identity, context)
        result = snapshot["result"]
        if (
            checksum(
                {
                    k: result[k]
                    for k in (
                        "preflight_id",
                        "service_name",
                        "environment",
                        "manifest",
                        "configuration",
                        "source_revision",
                    )
                }
            )
            != result["checksum"]
        ):
            raise ChangeSafetyError("Snapshot integrity check failed")
        return snapshot

    def _approved(self, context, tool, args):
        digest = parameters_hash(tool, args.model_dump(mode="json"))
        if not self.store.approval_allows(context.state, context.action_id, digest):
            raise ChangeSafetyError("Current approval for the exact operation is required")
        return digest

    def _output(self, change):
        return {
            key: change[key]
            for key in (
                "change_id",
                "snapshot_id",
                "status",
                "revision",
                "health",
                "rollback_snapshot_id",
            )
        }

    def execute(self, args, context, *, rollback_unhealthy=False):
        with self.store.transaction() as conn:
            digest = self._approved(context, "execute_service_change", args)
            existing = self.lookup(context.action_id, context.state.session_id)
            if existing:
                return existing
            preflight = self._owned(conn, "preflight", args.preflight_id, context)
            if preflight["plan"] != args.plan.model_dump():
                raise ChangeSafetyError("Approved plan differs from preflight")
            snapshot = self._sealed_snapshot(conn, args.snapshot_id, context)
            if snapshot["result"]["preflight_id"] != args.preflight_id:
                raise ChangeSafetyError("Snapshot is bound to another preflight")
            current = self._fresh(conn, preflight, context, 1)
            if snapshot["result"]["source_revision"] != current["revision"]:
                raise ChangeSafetyError("Snapshot revision is stale")
            current.update(
                manifest=args.plan.workload(),
                configuration=self.catalog.configurations[args.plan.config_version],
                revision=current["revision"] + 1,
            )
            current["health"] = (
                "OFFLINE"
                if args.plan.operation == "decommission"
                else self.catalog.artifacts[args.plan.image_digest].runtime_health
            )
            change = {
                "change_id": "chg-" + args.preflight_id,
                "snapshot_id": args.snapshot_id,
                "plan": args.plan.model_dump(),
                "status": "UNHEALTHY" if current["health"] == "UNHEALTHY" else "APPLIED",
                "revision": current["revision"],
                "health": current["health"],
                "rollback_snapshot_id": None,
                "verified": False,
            }
            self._put_resource(conn, current)
            self._audit(
                conn,
                context,
                "change_applied",
                change_id=change["change_id"],
                snapshot_id=args.snapshot_id,
                parameters_hash=digest,
                approval_id=context.state.pending_tool_call.approval_id,
                before_revision=snapshot["result"]["source_revision"],
                after_revision=current["revision"],
                plan=change["plan"],
                rollback_on_failure=args.rollback_on_failure,
            )
            if current["health"] == "UNHEALTHY" and args.rollback_on_failure:
                self._restore(
                    conn,
                    change,
                    current,
                    snapshot,
                    context,
                    rollback_unhealthy,
                    "approved_failure_compensation",
                )
            self._put(conn, "change", change["change_id"], context, change)
            output = self._output(change)
            self._put(conn, "result", context.action_id, context, {"result": output})
            return output

    def _restore(self, conn, change, current, snapshot, context, unhealthy, reason):
        plan = ChangePlan.model_validate(
            change["plan"]
            | snapshot["result"]["manifest"]
            | {
                "operation": "update"
                if snapshot["result"]["manifest"]["replicas"]
                else "decommission",
                "surge_replicas": 0,
            }
        )
        # Snapshot of the state being reverted is committed with the rollback itself.
        _, checks, _, _ = self._checks(conn, plan, context, slots=1, permission="rollback")
        if not all(checks.values()):
            change["status"] = "NEEDS_REVIEW"
            self._audit(
                conn, context, "rollback_blocked", change_id=change["change_id"], checks=checks
            )
            return
        backup = self._snapshot(
            conn,
            context,
            "rollback-" + change["change_id"],
            "rollback:" + change["change_id"],
            current,
        )
        current.update(
            manifest=snapshot["result"]["manifest"],
            configuration=snapshot["result"]["configuration"],
            revision=current["revision"] + 1,
        )
        current["health"] = (
            "OFFLINE"
            if not current["manifest"]["replicas"]
            else (
                "UNHEALTHY"
                if unhealthy
                else self.catalog.artifacts[current["manifest"]["image_digest"]].runtime_health
            )
        )
        unhealthy = current["health"] == "UNHEALTHY"
        self._put_resource(conn, current)
        change.update(
            status="NEEDS_REVIEW" if unhealthy else "ROLLED_BACK",
            revision=current["revision"],
            health=current["health"],
            rollback_snapshot_id=backup["snapshot_id"],
            verified=not unhealthy,
        )
        self._audit(
            conn,
            context,
            "rollback_failed" if unhealthy else "rollback_completed",
            change_id=change["change_id"],
            snapshot_id=change["snapshot_id"],
            rollback_snapshot_id=backup["snapshot_id"],
            reason=reason,
            after_revision=current["revision"],
            health=current["health"],
        )

    def rollback(self, args, context, *, unhealthy=False):
        with self.store.transaction() as conn:
            digest = self._approved(context, "rollback_service_change", args)
            existing = self.lookup(context.action_id, context.state.session_id)
            if existing:
                return existing
            change = self._owned(conn, "change", args.change_id, context)
            current = self._resource(conn, ChangePlan.model_validate(change["plan"]))
            if current["revision"] != change["revision"]:
                raise ChangeSafetyError(
                    "A newer change exists; rollback requires operator reconciliation"
                )
            if change["status"] == "ROLLED_BACK":
                return self._output(change)
            snapshot = self._sealed_snapshot(conn, change["snapshot_id"], context)
            if (
                args.snapshot_id != change["snapshot_id"]
                or args.expected_revision != change["revision"]
                or args.service_name != change["plan"]["service_name"]
                or args.environment != change["plan"]["environment"]
                or args.restore.model_dump() != snapshot["result"]["manifest"]
            ):
                raise ChangeSafetyError(
                    "Rollback proposal does not match the saved target and revision"
                )
            self._audit(
                conn,
                context,
                "rollback_authorized",
                change_id=args.change_id,
                parameters_hash=digest,
                approval_id=context.state.pending_tool_call.approval_id,
                reason=args.reason,
            )
            self._restore(conn, change, current, snapshot, context, unhealthy, args.reason)
            self._put(conn, "change", args.change_id, context, change)
            output = self._output(change)
            self._put(conn, "result", context.action_id, context, {"result": output})
            return output

    def verify(self, args, context, *, late_failure=False):
        with self.store.transaction() as conn:
            change = self._owned(conn, "change", args.change_id, context)
            current = self._resource(conn, ChangePlan.model_validate(change["plan"]))
            if current["revision"] != change["revision"]:
                raise ChangeSafetyError("Service changed after this operation")
            if late_failure and change["status"] == "APPLIED":
                current["health"] = "UNHEALTHY"
                self._put_resource(conn, current)
            change["health"] = current["health"]
            if current["health"] == "UNHEALTHY":
                change["status"] = "UNHEALTHY"
            change["verified"] = current["health"] in {"HEALTHY", "OFFLINE"}
            self._put(conn, "change", args.change_id, context, change)
            self._audit(
                conn,
                context,
                "change_verified",
                change_id=args.change_id,
                snapshot_id=change["snapshot_id"],
                health=change["health"],
                revision=change["revision"],
            )
            return self._output(change)

    def lookup(self, action_id, session_id):
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT payload FROM ops_objects WHERE kind='result' AND id=? AND session_id=?",
                (action_id, session_id),
            ).fetchone()
            return json.loads(row[0])["result"] if row else None

    def changes(self, session_id):
        with self.store._lock:
            return [
                json.loads(row[0])
                for row in self.store._conn.execute(
                    "SELECT payload FROM ops_objects WHERE kind='change' AND session_id=?",
                    (session_id,),
                )
            ]

    def audit(self, session_id):
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM ops_audit WHERE session_id=? ORDER BY sequence", (session_id,)
            ).fetchall()
        previous = "0" * 64
        results = []
        for row in rows:
            payload = json.loads(row["payload"])
            if (
                row["previous_hash"] != previous
                or checksum({"previous_hash": previous, "payload": payload}) != row["entry_hash"]
            ):
                raise ChangeSafetyError("Change audit integrity check failed")
            results.append(
                payload
                | {
                    "sequence": row["sequence"],
                    "previous_hash": previous,
                    "entry_hash": row["entry_hash"],
                }
            )
            previous = row["entry_hash"]
        return results

"""SQLite state machine, checkpoints, release ledger, and benchmark store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUN_STATES = (
    "NEW",
    "ADMITTED",
    "GENERATED",
    "TESTED",
    "EVALUATED",
    "TRIAGED",
    "PATCH_PROPOSED",
    "RETEST",
    "RELEASE_CANDIDATE",
    "APPROVED",
    "PUBLISHED",
    "BLOCKED",
    "CANCELLED",
    "PAUSED",
)

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "NEW": {"ADMITTED", "BLOCKED", "CANCELLED"},
    "ADMITTED": {"GENERATED", "BLOCKED", "CANCELLED"},
    "GENERATED": {"TESTED", "TRIAGED", "BLOCKED", "CANCELLED"},
    "TESTED": {"EVALUATED", "TRIAGED", "BLOCKED", "CANCELLED"},
    "EVALUATED": {"RELEASE_CANDIDATE", "TRIAGED", "BLOCKED", "CANCELLED"},
    "TRIAGED": {"PATCH_PROPOSED", "BLOCKED", "CANCELLED"},
    "PATCH_PROPOSED": {"RETEST", "BLOCKED", "CANCELLED"},
    "RETEST": {"TESTED", "EVALUATED", "BLOCKED", "CANCELLED"},
    "RELEASE_CANDIDATE": {"APPROVED", "TRIAGED", "BLOCKED", "PAUSED"},
    "APPROVED": {"PUBLISHED", "BLOCKED"},
    "PUBLISHED": {"PUBLISHED"},
    "BLOCKED": {"NEW"},
    "PAUSED": {"ADMITTED", "GENERATED", "TESTED", "EVALUATED", "CANCELLED"},
    "CANCELLED": set(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_json(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class StateStore:
    """Transactional SQLite store for pipeline runs, checkpoints, releases, and benchmarks."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    tool_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    input_hash TEXT NOT NULL,
                    contract_version TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    parent_run_id TEXT,
                    artifact_refs_json TEXT NOT NULL DEFAULT '{}',
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS releases (
                    release_id TEXT PRIMARY KEY,
                    tool_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    manifest_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS benchmarks (
                    benchmark_id TEXT PRIMARY KEY,
                    tool_id TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def create_run(
        self,
        *,
        tool_id: str,
        input_hash: str,
        contract_version: str,
        model_id: str = "local_deterministic",
        prompt_version: str = "v1",
        parent_run_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        rid = run_id or f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs(
                    run_id, tool_id, stage, status, attempt, input_hash,
                    contract_version, model_id, prompt_version, parent_run_id,
                    artifact_refs_json, summary_json, created_at, updated_at
                ) VALUES (?, ?, 'admission', 'NEW', 0, ?, ?, ?, ?, ?, '{}', '{}', ?, ?)
                """,
                (rid, tool_id, input_hash, contract_version, model_id, prompt_version, parent_run_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO checkpoints(run_id, stage, status, attempt, payload_json, created_at)
                VALUES (?, 'admission', 'NEW', 0, '{}', ?)
                """,
                (rid, now),
            )
        return rid

    def transition(
        self,
        run_id: str,
        *,
        stage: str,
        status: str,
        attempt: int | None = None,
        payload: dict[str, Any] | None = None,
        artifact_refs: dict[str, str] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        if status not in RUN_STATES:
            raise ValueError(f"invalid state: {status}")
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute("SELECT status, attempt, artifact_refs_json, summary_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown run_id: {run_id}")
            cur_status = row["status"]
            if status != cur_status and status not in ALLOWED_TRANSITIONS.get(cur_status, set()):
                raise ValueError(f"illegal state transition {cur_status} -> {status}")

            new_attempt = row["attempt"] if attempt is None else attempt
            merged_refs = {**json.loads(row["artifact_refs_json"]), **(artifact_refs or {})}
            merged_summary = {**json.loads(row["summary_json"]), **(summary or {})}

            conn.execute(
                """
                UPDATE runs
                SET stage = ?, status = ?, attempt = ?, artifact_refs_json = ?, summary_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    stage,
                    status,
                    new_attempt,
                    json.dumps(merged_refs, ensure_ascii=False),
                    json.dumps(merged_summary, ensure_ascii=False),
                    now,
                    run_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO checkpoints(run_id, stage, status, attempt, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, stage, status, new_attempt, json.dumps(payload or {}, ensure_ascii=False), now),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            cps = conn.execute(
                "SELECT stage, status, attempt, payload_json, created_at FROM checkpoints WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
        out = dict(row)
        out["artifact_refs"] = json.loads(out.pop("artifact_refs_json"))
        out["summary"] = json.loads(out.pop("summary_json"))
        out["checkpoints"] = [
            {
                "stage": c["stage"],
                "status": c["status"],
                "attempt": c["attempt"],
                "payload": json.loads(c["payload_json"]),
                "created_at": c["created_at"],
            }
            for c in cps
        ]
        return out

    def list_runs(self, tool_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if tool_id:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE tool_id = ? ORDER BY created_at DESC LIMIT ?",
                    (tool_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        results = []
        for r in rows:
            item = dict(r)
            item["artifact_refs"] = json.loads(item.pop("artifact_refs_json"))
            item["summary"] = json.loads(item.pop("summary_json"))
            results.append(item)
        return results

    def save_release(
        self,
        *,
        release_id: str,
        tool_id: str,
        version: str,
        run_id: str,
        manifest_hash: str,
        status: str,
        target_path: str,
        manifest: dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO releases(
                    release_id, tool_id, version, run_id, manifest_hash, status, target_path, manifest_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    release_id,
                    tool_id,
                    version,
                    run_id,
                    manifest_hash,
                    status,
                    target_path,
                    json.dumps(manifest, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def update_release_status(self, release_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE releases SET status = ? WHERE release_id = ?", (status, release_id))

    def list_releases(self, tool_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if tool_id:
                rows = conn.execute(
                    "SELECT * FROM releases WHERE tool_id = ? ORDER BY created_at DESC",
                    (tool_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM releases ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["manifest"] = json.loads(item.pop("manifest_json"))
            out.append(item)
        return out

    def save_benchmark(
        self,
        *,
        benchmark_id: str,
        tool_id: str,
        condition: str,
        summary: dict[str, Any],
        details: dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO benchmarks(benchmark_id, tool_id, condition, summary_json, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    benchmark_id,
                    tool_id,
                    condition,
                    json.dumps(summary, ensure_ascii=False),
                    json.dumps(details, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def list_benchmarks(self, tool_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if tool_id:
                rows = conn.execute(
                    "SELECT * FROM benchmarks WHERE tool_id = ? ORDER BY created_at DESC",
                    (tool_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM benchmarks ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["summary"] = json.loads(item.pop("summary_json"))
            item["details"] = json.loads(item.pop("details_json"))
            out.append(item)
        return out

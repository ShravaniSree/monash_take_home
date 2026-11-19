"""SQLite audit log and idempotent ledger.

The ADK session is the workflow checkpoint. This file is the system of
record for posts and for the events a reviewer can replay after a restart.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ap_agent.domain.models import AuditEvent
from ap_agent.domain.models import SubmitResult


class AuditStore:
    """Decisions and audit events that outlive the process."""

    def __init__(self, path: str | Path | None = None) -> None:
        """Open a database file, or an in-memory database when path is omitted.

        Args:
            path: SQLite file. Parent directories are created. ``None``
                keeps the ledger inside this process only.
        """
        if path is None:
            self._db = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(target), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._create()

    def close(self) -> None:
        """Close the SQLite connection."""
        self._db.close()

    def _create(self) -> None:
        """Create the decision and event tables when they are missing."""
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                idempotency_key TEXT PRIMARY KEY,
                ledger_ref TEXT NOT NULL,
                decision TEXT NOT NULL,
                replayed INTEGER NOT NULL,
                body_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                type TEXT NOT NULL,
                outcome TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                detail_json TEXT NOT NULL
            );
            """
        )
        self._db.commit()

    def save_decision(self, key: str, result: SubmitResult, body: str) -> None:
        """Insert a ledger row. A duplicate key leaves the original row in place.

        Args:
            key: Idempotency key.
            result: Ledger acknowledgement, stored with ``replayed`` false.
            body: Canonical JSON of the decision that was posted.
        """
        self._db.execute(
            """
            INSERT OR IGNORE INTO decisions (
                idempotency_key, ledger_ref, decision, replayed, body_json
            ) VALUES (?, ?, ?, 0, ?)
            """,
            (key, result.ledger_ref, result.decision.value, body),
        )
        self._db.commit()

    def load_decisions(self) -> list[tuple[str, SubmitResult, str]]:
        """Read every ledger row.

        Returns:
            Key, acknowledgement, and the original request JSON.
        """
        rows = self._db.execute(
            "SELECT idempotency_key, ledger_ref, decision, body_json FROM decisions"
        ).fetchall()
        loaded: list[tuple[str, SubmitResult, str]] = []
        for row in rows:
            loaded.append(
                (
                    row["idempotency_key"],
                    SubmitResult(ledger_ref=row["ledger_ref"], decision=row["decision"], replayed=False),
                    row["body_json"],
                )
            )
        return loaded

    def save_event(self, event: AuditEvent) -> None:
        """Append one audit event.

        Args:
            event: Redacted event. The caller supplies the run id and outcome.
        """
        self._db.execute(
            """
            INSERT INTO events (run_id, ts, type, outcome, duration_ms, detail_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.run_id,
                event.ts,
                event.type,
                event.outcome,
                event.duration_ms,
                json.dumps(event.detail, sort_keys=True),
            ),
        )
        self._db.commit()

    def count_decisions(self) -> int:
        """Return how many distinct ledger keys have been stored."""
        row = self._db.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()
        return int(row["n"])

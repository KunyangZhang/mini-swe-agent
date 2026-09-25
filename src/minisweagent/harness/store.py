"""Single-writer SQLite journal; an unfinished external action is never auto-retried."""

import fcntl
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def fingerprint(value: object) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


class AmbiguousActionError(RuntimeError):
    """An action started but no durable outcome is available."""


@dataclass
class RunStore:
    """Own a run directory for the lifetime of an agent invocation (Linux/WSL)."""

    directory: Path

    def __enter__(self) -> "RunStore":
        self.directory = self.directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.directory / "writer.lock").open("a")
        entered = False
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(self.directory / "journal.sqlite3")
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported journal version: {version}")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actions (
                    step INTEGER NOT NULL, ordinal INTEGER NOT NULL, action TEXT NOT NULL,
                    result TEXT, PRIMARY KEY(step, ordinal));
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    kind TEXT NOT NULL, payload TEXT NOT NULL);
                PRAGMA user_version=1;
            """)
            entered = True
            return self
        finally:
            # __exit__ is not called when __enter__ fails.
            if not entered:
                if hasattr(self, "db"):
                    self.db.close()
                self.lock.close()

    def __exit__(self, kind: type | None, value: BaseException | None, tb: TracebackType | None) -> None:
        self.db.close()
        self.lock.close()

    def event(self, kind: str, payload: dict) -> None:
        self.db.execute("INSERT INTO events(kind,payload) VALUES (?,?)", (kind, encode(payload)))

    def load(self) -> dict | None:
        row = self.db.execute("SELECT payload FROM snapshot WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def checkpoint(self, state: dict, kind: str = "checkpoint") -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO snapshot VALUES (1,?)", (encode(state),))
            self.event(kind, {"phase": state["phase"], "calls": state["n_calls"], "cost": state["cost"]})

    def begin(self, step: int, ordinal: int, action: dict) -> dict | None:
        row = self.db.execute("SELECT * FROM actions WHERE step=? AND ordinal=?", (step, ordinal)).fetchone()
        if row:
            if row["action"] != encode(action):
                raise ValueError("Action identity changed during resume")
            if row["result"] is None:
                raise AmbiguousActionError(
                    f"Action {step}:{ordinal} has an unknown outcome. Inspect side effects and resolve explicitly."
                )
            return json.loads(row["result"])
        with self.db:
            self.db.execute("INSERT INTO actions VALUES (?,?,?,NULL)", (step, ordinal, encode(action)))
            self.event("action_started", {"step": step, "ordinal": ordinal, "action": action})
        return None

    def complete(self, step: int, ordinal: int, result: dict) -> None:
        with self.db:
            if (
                self.db.execute(
                    "UPDATE actions SET result=? WHERE step=? AND ordinal=? AND result IS NULL",
                    (encode(result), step, ordinal),
                ).rowcount
                != 1
            ):
                raise ValueError("Action is missing or already completed")
            self.event("action_completed", {"step": step, "ordinal": ordinal})

    def resolve(self, step: int, ordinal: int, result: dict, reason: str) -> None:
        if not reason.strip() or not isinstance(result.get("output"), str) or type(result.get("returncode")) is not int:
            raise ValueError("Resolution needs a reason, string output, and integer returncode")
        with self.db:
            if (
                self.db.execute(
                    "UPDATE actions SET result=? WHERE step=? AND ordinal=? AND result IS NULL",
                    (encode(result), step, ordinal),
                ).rowcount
                != 1
            ):
                raise ValueError("Only an unresolved action can be resolved")
            self.event("action_resolved", {"step": step, "ordinal": ordinal, "reason": reason, "result": result})

    def status(self) -> dict:
        state = self.load()
        return {
            "phase": state["phase"] if state else "new",
            "model_calls": state["n_calls"] if state else 0,
            "known_cost": state["cost"] if state else 0,
            "actions": [dict(row) for row in self.db.execute("SELECT * FROM actions ORDER BY step, ordinal")],
            "events": [dict(row) for row in self.db.execute("SELECT * FROM events ORDER BY sequence")],
        }

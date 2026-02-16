"""SQLite database layer for PaneldeContolOpenClaw.

This module wraps a simple SQLite database used to record runs and
associated metadata.  Each run corresponds to a call to an AI model
through the proxy.  Storing run metadata allows the UI to present
histories, compute statistics and audit past calls without retaining
full log contents once they are purged.

The schema is created automatically on initialisation.  Using a
database file on disk ensures persistence across restarts.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class Database:
    """Lightweight wrapper around SQLite for storing run metadata."""

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        # Use a lock to guard writes from multiple threads
        self.lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()
        # Perform an initial VACUUM to compact the database and ensure
        # journal files are cleaned up.  VACUUM can help reclaim
        # space when logs are purged.  This is safe to run at
        # startup because no other threads are accessing the DB yet.
        try:
            self._conn.execute("VACUUM")
        except Exception:
            pass

    def _create_schema(self) -> None:
        """Create tables if they do not exist."""
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    start_time REAL,
                    end_time REAL,
                    provider TEXT,
                    model TEXT,
                    status TEXT,
                    tokens_in INTEGER,
                    tokens_out INTEGER,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    cost_estimate REAL,
                    log_file TEXT,
                    error_message TEXT
                )
                """
            )
            # Table of timeline events per run
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT,
                    timestamp REAL,
                    event TEXT,
                    details TEXT
                )
                """
            )
            # Table of denied commands attempts (for future use)
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS denied_commands (
                    timestamp REAL,
                    run_id TEXT,
                    command TEXT
                )
                """
            )

    def add_run(
        self,
        run_id: str,
        provider: str,
        model: str,
        start_time: float,
        log_file: str,
    ) -> None:
        """Insert a new run into the database.

        Parameters
        ----------
        run_id : str
            Unique identifier for the run.
        provider : str
            Name of the AI provider.
        model : str
            Name of the model used.
        start_time : float
            UNIX timestamp when the run started.
        log_file : str
            Path to the log file for this run.
        """
        with self.lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO runs (
                    id, provider, model, start_time, log_file, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, provider, model, start_time, log_file, "running"),
            )

    # Event handling
    def add_event(self, run_id: str, timestamp: float, event: str, details: Optional[str] = None) -> None:
        """Record a timeline event for a run.

        Parameters
        ----------
        run_id : str
            Identifier of the run the event relates to.
        timestamp : float
            UNIX timestamp when the event occurred.
        event : str
            A short string describing the event type.
        details : str, optional
            Additional textual details for the event.
        """
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO events (run_id, timestamp, event, details) VALUES (?, ?, ?, ?)",
                (run_id, timestamp, event, details),
            )

    def get_events_for_run(self, run_id: str) -> List[sqlite3.Row]:
        """Return all events for a run ordered by timestamp ascending."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY timestamp ASC",
            (run_id,),
        )
        return cur.fetchall()

    # Denied commands logging (for future use)
    def add_denied_command(self, run_id: str, command: str) -> None:
        """Record a denied command attempt for audit."""
        ts = time.time()
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO denied_commands (timestamp, run_id, command) VALUES (?, ?, ?)",
                (ts, run_id, command),
            )

    def get_denied_commands(self, run_id: Optional[str] = None) -> List[sqlite3.Row]:
        """Return denied commands, optionally filtered by run."""
        cur = self._conn.cursor()
        if run_id:
            cur.execute(
                "SELECT * FROM denied_commands WHERE run_id = ? ORDER BY timestamp ASC",
                (run_id,),
            )
        else:
            cur.execute(
                "SELECT * FROM denied_commands ORDER BY timestamp DESC"
            )
        return cur.fetchall()

    # Backup and maintenance
    def backup(self, backup_path: Optional[str] = None) -> str:
        """Create a copy of the current database file for backup purposes.

        If ``backup_path`` is provided it will be used as the destination
        filename.  Otherwise a new file with a timestamp will be created
        in the same directory as the database.  Returns the path to the
        backup file.  Note that this method does not lock the database
        while copying; for robust hot backups consider using the
        SQLite backup API or copy when the proxy is idle.
        """
        import shutil
        import datetime
        src = self.db_path
        if backup_path:
            dest = Path(backup_path)
        else:
            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            dest = src.with_name(f"{src.stem}.bak.{timestamp}{src.suffix}")
        try:
            shutil.copy2(src, dest)
        except Exception:
            # ignore errors silently
            dest = src.with_name(f"{src.stem}.bak.error")
        return str(dest)

    def update_run(
        self,
        run_id: str,
        end_time: Optional[float] = None,
        status: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        cost_estimate: Optional[float] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update fields for a run.

        Any argument that is None will not update the corresponding
        column.  Callers should specify only the fields that changed.
        """
        # Build dynamic query based on provided arguments
        fields: List[str] = []
        values: List[Any] = []
        if end_time is not None:
            fields.append("end_time = ?")
            values.append(end_time)
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if tokens_in is not None:
            fields.append("tokens_in = ?")
            values.append(tokens_in)
        if tokens_out is not None:
            fields.append("tokens_out = ?")
            values.append(tokens_out)
        if prompt_tokens is not None:
            fields.append("prompt_tokens = ?")
            values.append(prompt_tokens)
        if completion_tokens is not None:
            fields.append("completion_tokens = ?")
            values.append(completion_tokens)
        if total_tokens is not None:
            fields.append("total_tokens = ?")
            values.append(total_tokens)
        if cost_estimate is not None:
            fields.append("cost_estimate = ?")
            values.append(cost_estimate)
        if error_message is not None:
            fields.append("error_message = ?")
            values.append(error_message)
        if not fields:
            return
        values.append(run_id)
        with self.lock, self._conn:
            self._conn.execute(
                f"UPDATE runs SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def get_recent_runs(self, limit: int = 100) -> List[sqlite3.Row]:
        """Return the most recent runs up to the given limit."""
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT * FROM runs
            ORDER BY start_time DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
        return rows

    def get_all_runs(self) -> List[sqlite3.Row]:
        """Return all runs, ordered by start time descending."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM runs ORDER BY start_time DESC"
        )
        return cur.fetchall()

    def get_run(self, run_id: str) -> Optional[sqlite3.Row]:
        """Return a single run by ID, or None if not found."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM runs WHERE id = ?",
            (run_id,),
        )
        return cur.fetchone()

"""
Audit logging for permission tier events.

Provides append-only SQLite-backed audit trail for tier changes, command
denials, rate limit events, and promotion requests. Thread-safe with
per-call connections following the RuntimeUserStore pattern.

When ``permission_tiers.audit`` is not configured, ``NullAuditLog`` is used
as a no-op stand-in — zero overhead, zero behavior change.
"""

import logging
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AuditLog:
    """SQLite-backed append-only audit log for permission tier events.

    Thread-safe. Per-call connections. WAL mode. Rotation by row count.
    """

    def __init__(self, db_path: Optional[str] = None, max_rows: int = 100_000):
        if db_path is None:
            from hermes_constants import get_hermes_home

            db_path = str(get_hermes_home() / "audit.db")
        self._db_path = db_path
        self._max_rows = max_rows
        self._lock = threading.Lock()
        self._create_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _create_tables(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS audit_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        platform TEXT,
                        user_id TEXT,
                        tier_name TEXT,
                        details TEXT,
                        actor_id TEXT
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                    ON audit_events(timestamp)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_audit_event_type
                    ON audit_events(event_type, timestamp)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_audit_user
                    ON audit_events(platform, user_id, timestamp)
                """)
                conn.commit()
            finally:
                conn.close()

    def log(
        self,
        event_type: str,
        platform: Optional[str] = None,
        user_id: Optional[str] = None,
        tier_name: Optional[str] = None,
        details: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> None:
        """Append an audit event. Non-blocking on errors — logs warning and continues."""
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO audit_events "
                    "(timestamp, event_type, platform, user_id, tier_name, details, actor_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (now, event_type, platform, user_id, tier_name, details, actor_id),
                )
                conn.commit()
            except Exception as exc:
                logger.warning("Audit log write failed: %s", exc)
            finally:
                conn.close()

        # Rotation check (low-frequency, no lock needed for the count check)
        self._maybe_rotate()

    def _maybe_rotate(self) -> None:
        """Rotate old events if we exceed max_rows."""
        try:
            conn = self._connect()
            try:
                count = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
                if count > self._max_rows:
                    # Keep newest half
                    cutoff = conn.execute(
                        "SELECT id FROM audit_events ORDER BY id DESC LIMIT 1 OFFSET ?",
                        (self._max_rows // 2,),
                    ).fetchone()
                    if cutoff:
                        conn.execute(
                            "DELETE FROM audit_events WHERE id < ?", (cutoff[0],)
                        )
                        conn.commit()
                        logger.info(
                            "Audit log rotated: removed events older than id %d",
                            cutoff[0],
                        )
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("Audit rotation failed: %s", exc)

    def query(
        self,
        event_type: Optional[str] = None,
        platform: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Query audit events with optional filters."""
        conditions = []
        params: list = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.extend([limit, offset])

        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT id, timestamp, event_type, platform, user_id, "
                    f"tier_name, details, actor_id "
                    f"FROM audit_events WHERE {where} "
                    f"ORDER BY id DESC LIMIT ? OFFSET ?",
                    params,
                ).fetchall()
                return [
                    {
                        "id": r[0],
                        "timestamp": r[1],
                        "event_type": r[2],
                        "platform": r[3],
                        "user_id": r[4],
                        "tier_name": r[5],
                        "details": r[6],
                        "actor_id": r[7],
                    }
                    for r in rows
                ]
            finally:
                conn.close()

    def count(
        self,
        event_type: Optional[str] = None,
        platform: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> int:
        """Count audit events matching filters."""
        conditions = []
        params: list = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)

        where = " AND ".join(conditions) if conditions else "1=1"

        with self._lock:
            conn = self._connect()
            try:
                return conn.execute(
                    f"SELECT COUNT(*) FROM audit_events WHERE {where}", params
                ).fetchone()[0]
            finally:
                conn.close()


class NullAuditLog:
    """No-op audit log — used when audit is not configured.

    All methods are safe to call but do nothing and return empty results.
    """

    def log(self, **kwargs) -> None:
        pass

    def query(self, **kwargs) -> List[Dict[str, Any]]:
        return []

    def count(self, **kwargs) -> int:
        return 0

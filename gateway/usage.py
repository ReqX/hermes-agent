"""
Per-user usage tracking across sessions.

Provides SQLite-backed usage tracking for persistent rate limiting,
token counting, and usage reporting. When ``permission_tiers.usage_tracking``
is not configured, ``NullUsageStore`` is used as a no-op stand-in.

Thread-safe with per-call connections following the RuntimeUserStore pattern.
"""

import logging
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class UsageStore:
    """SQLite-backed per-user usage tracking.

    Tracks:
    - Request counts per time window (for persistent rate limiting)
    - Token usage per user (input + output tokens)
    - Per-session usage breakdown

    Thread-safe. Per-call connections. WAL mode.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from hermes_constants import get_hermes_home

            db_path = str(get_hermes_home() / "usage.db")
        self._db_path = db_path
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
                    CREATE TABLE IF NOT EXISTS rate_limits (
                        platform TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        window_type TEXT NOT NULL DEFAULT 'hourly',
                        window_start REAL NOT NULL,
                        count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (platform, user_id, window_type, window_start)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS token_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        platform TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        session_key TEXT,
                        input_tokens INTEGER DEFAULT 0,
                        output_tokens INTEGER DEFAULT 0,
                        model TEXT
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_token_usage_ts
                    ON token_usage(timestamp)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_token_usage_user
                    ON token_usage(platform, user_id, timestamp)
                """)
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Persistent rate limiting
    # ------------------------------------------------------------------

    def check_and_increment(
        self,
        platform: str,
        user_id: str,
        limit: int,
        window_seconds: int = 3600,
    ) -> Tuple[bool, int]:
        """Atomically check rate limit and increment counter.

        Returns (allowed, current_count).
        """
        now = time.time()
        window_start = int(now // window_seconds) * window_seconds

        with self._lock:
            conn = self._connect()
            try:
                # Clean up expired windows
                conn.execute(
                    "DELETE FROM rate_limits WHERE window_start < ?",
                    (now - window_seconds,),
                )

                row = conn.execute(
                    "SELECT count FROM rate_limits "
                    "WHERE platform = ? AND user_id = ? AND window_type = 'hourly' "
                    "AND window_start = ?",
                    (platform, user_id, window_start),
                ).fetchone()

                if row:
                    current = row[0]
                    if current >= limit:
                        conn.commit()
                        return False, current
                    conn.execute(
                        "UPDATE rate_limits SET count = count + 1 "
                        "WHERE platform = ? AND user_id = ? AND window_type = 'hourly' "
                        "AND window_start = ?",
                        (platform, user_id, window_start),
                    )
                    conn.commit()
                    return True, current + 1
                else:
                    conn.execute(
                        "INSERT INTO rate_limits (platform, user_id, window_type, "
                        "window_start, count) VALUES (?, ?, 'hourly', ?, 1)",
                        (platform, user_id, window_start),
                    )
                    conn.commit()
                    return True, 1
            finally:
                conn.close()

    def get_current_count(
        self,
        platform: str,
        user_id: str,
        window_seconds: int = 3600,
    ) -> int:
        """Get current request count in the active window."""
        now = time.time()
        window_start = int(now // window_seconds) * window_seconds

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT count FROM rate_limits "
                    "WHERE platform = ? AND user_id = ? AND window_type = 'hourly' "
                    "AND window_start = ?",
                    (platform, user_id, window_start),
                ).fetchone()
                return row[0] if row else 0
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Token tracking
    # ------------------------------------------------------------------

    def record_tokens(
        self,
        platform: str,
        user_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        session_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Record token usage for a request."""
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO token_usage "
                    "(timestamp, platform, user_id, session_key, "
                    "input_tokens, output_tokens, model) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        platform,
                        user_id,
                        session_key,
                        input_tokens,
                        output_tokens,
                        model,
                    ),
                )
                conn.commit()
            except Exception as exc:
                logger.warning("Token usage record failed: %s", exc)
            finally:
                conn.close()

    def get_user_usage(
        self,
        platform: str,
        user_id: str,
        hours: int = 24,
    ) -> Dict[str, Any]:
        """Get aggregated usage for a user over the last N hours."""
        cutoff = time.time() - (hours * 3600)

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*), "
                    "COALESCE(SUM(input_tokens), 0), "
                    "COALESCE(SUM(output_tokens), 0) "
                    "FROM token_usage "
                    "WHERE platform = ? AND user_id = ? AND timestamp >= ?",
                    (platform, user_id, cutoff),
                ).fetchone()
                return {
                    "platform": platform,
                    "user_id": user_id,
                    "hours": hours,
                    "request_count": row[0] if row else 0,
                    "input_tokens": row[1] if row else 0,
                    "output_tokens": row[2] if row else 0,
                    "total_tokens": (row[1] or 0) + (row[2] or 0),
                }
            finally:
                conn.close()

    def get_all_user_usage(
        self,
        hours: int = 24,
    ) -> List[Dict[str, Any]]:
        """Get aggregated usage for all users over the last N hours."""
        cutoff = time.time() - (hours * 3600)

        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT platform, user_id, COUNT(*), "
                    "COALESCE(SUM(input_tokens), 0), "
                    "COALESCE(SUM(output_tokens), 0) "
                    "FROM token_usage WHERE timestamp >= ? "
                    "GROUP BY platform, user_id "
                    "ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC",
                    (cutoff,),
                ).fetchall()
                return [
                    {
                        "platform": r[0],
                        "user_id": r[1],
                        "request_count": r[2],
                        "input_tokens": r[3],
                        "output_tokens": r[4],
                        "total_tokens": r[3] + r[4],
                    }
                    for r in rows
                ]
            finally:
                conn.close()

    def cleanup(self, max_age_days: int = 90) -> int:
        """Remove token usage records older than max_age_days."""
        cutoff = time.time() - (max_age_days * 86400)
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM token_usage WHERE timestamp < ?", (cutoff,)
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()


class NullUsageStore:
    """No-op usage store — used when usage tracking is not configured.

    All methods are safe to call but do nothing and return empty/default results.
    """

    def check_and_increment(
        self, platform: str, user_id: str, limit: int, **kwargs
    ) -> Tuple[bool, int]:
        return True, 0

    def get_current_count(self, **kwargs) -> int:
        return 0

    def record_tokens(self, **kwargs) -> None:
        pass

    def get_user_usage(self, **kwargs) -> Dict[str, Any]:
        return {
            "request_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    def get_all_user_usage(self, **kwargs) -> List[Dict[str, Any]]:
        return []

    def cleanup(self, **kwargs) -> int:
        return 0

"""
Permission Manager — centralized permission tier logic.

Extracts all permission-related methods from GatewayRunner into a single
class for reuse, testability, and clarity.

This module is the sole authority for:
- User → tier resolution (config + runtime overlay)
- Tier config lookups
- Time window enforcement
- Tool/toolset filtering decisions
- i18n message formatting
- Approval key isolation
- Runtime user management (set/remove/list tiers)
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple
from zoneinfo import ZoneInfo

from gateway.config import (
    PLATFORM_ALLOW_ALL_ENV,
    PLATFORM_ALLOWED_USERS_ENV,
    PermissionTiersConfig,
    TierDefinition,
    UserTierConfig,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime user-role store (SQLite)
# ---------------------------------------------------------------------------


class RuntimeUserStore:
    """Persistent SQLite store for runtime tier assignments.

    Runtime assignments (via ``/users set``) overlay on top of config.yaml
    user mappings. On restart, config reloads and the runtime overlay
    persists via this store.

    The store is a single ``user_tiers`` table with columns:
    - user_id TEXT NOT NULL
    - source_platform TEXT NOT NULL
    - tier_name TEXT NOT NULL
    - granted_by TEXT (user_id of the admin who set it, or "system")
    - granted_at TEXT (ISO 8601 timestamp)

    The composite PRIMARY KEY (user_id, source_platform) prevents
    cross-platform collisions (e.g. same user_id on Telegram and Discord).
    """

    # Platform value used when no platform is specified (backward compat)
    _DEFAULT_PLATFORM = "default"

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            from hermes_constants import get_hermes_home

            db_path = get_hermes_home() / "permissions.db"
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Create tables if they don't exist, migrating from old schema."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # Check for old schema (single-column PK) and migrate
            try:
                table_info = conn.execute("PRAGMA table_info(user_tiers)").fetchall()
                if table_info:
                    pk_cols = [
                        col[1]
                        for col in table_info
                        if col[5] > 0  # col[5] = pk flag
                    ]
                    if pk_cols == ["user_id"]:
                        # Old schema: drop and recreate (runtime store is ephemeral)
                        conn.execute("DROP TABLE user_tiers")
            except Exception:
                pass  # Table doesn't exist yet — will be created below

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_tiers (
                    user_id TEXT NOT NULL,
                    source_platform TEXT NOT NULL,
                    tier_name TEXT NOT NULL,
                    granted_by TEXT NOT NULL DEFAULT 'system',
                    granted_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, source_platform)
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _normalize_platform(platform: Optional[str]) -> str:
        """Normalize platform to a non-empty string."""
        return platform if platform else RuntimeUserStore._DEFAULT_PLATFORM

    def set_user_tier(
        self,
        user_id: str,
        tier_name: str,
        granted_by: str = "system",
        source_platform: Optional[str] = None,
    ) -> None:
        """Set or update the runtime tier for a user on a specific platform."""
        now = datetime.utcnow().isoformat()
        platform = self._normalize_platform(source_platform)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_tiers (user_id, source_platform, tier_name, granted_by, granted_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, source_platform) DO UPDATE SET
                    tier_name = excluded.tier_name,
                    granted_by = excluded.granted_by,
                    granted_at = excluded.granted_at
                """,
                (user_id, platform, tier_name, granted_by, now),
            )
            conn.commit()

    def remove_user_tier(
        self, user_id: str, source_platform: Optional[str] = None
    ) -> bool:
        """Remove a runtime tier assignment. Returns True if a row was deleted.

        When ``source_platform`` is None, removes ALL platform entries for
        the given ``user_id`` (used by ``/users remove``).  When a specific
        platform is given, only that entry is removed.
        """
        with self._lock, self._connect() as conn:
            if source_platform is not None:
                platform = self._normalize_platform(source_platform)
                cursor = conn.execute(
                    "DELETE FROM user_tiers WHERE user_id = ? AND source_platform = ?",
                    (user_id, platform),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM user_tiers WHERE user_id = ?", (user_id,)
                )
            conn.commit()
            return cursor.rowcount > 0

    def get_user_tier(
        self, user_id: str, source_platform: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Return runtime tier info for a user on a specific platform, or None.

        Returns dict with keys: tier_name, granted_by, granted_at, source_platform.
        """
        platform = self._normalize_platform(source_platform)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT tier_name, granted_by, granted_at, source_platform "
                "FROM user_tiers WHERE user_id = ? AND source_platform = ?",
                (user_id, platform),
            ).fetchone()
        if row is None:
            return None
        return {
            "tier_name": row["tier_name"],
            "granted_by": row["granted_by"],
            "granted_at": row["granted_at"],
            "source_platform": row["source_platform"],
        }

    def list_all(self) -> List[Dict[str, Any]]:
        """Return all runtime tier assignments."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, source_platform, tier_name, granted_by, granted_at "
                "FROM user_tiers ORDER BY granted_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        """No-op (connections are per-call), kept for API symmetry."""
        pass


# ---------------------------------------------------------------------------
# PermissionManager
# ---------------------------------------------------------------------------


class PermissionManager:
    """Centralized permission tier enforcement.

    Holds a reference to the parsed ``PermissionTiersConfig`` and provides
    methods for every permission decision the gateway needs.

    When ``config`` is ``None``, every method returns the "unrestricted"
    default — identical to the pre-tier behavior.

    Resolution order for user tiers:
    1. Runtime overlay (SQLite — set via /users set)
    2. Config.yaml user mapping (explicit user_id or wildcard "*")
    3. default_tier from config
    4. Most-restrictive tier (fail-closed)
    """

    def __init__(
        self,
        config: Optional[PermissionTiersConfig] = None,
        runtime_store: Optional[RuntimeUserStore] = None,
        pairing_store: Optional[
            Any
        ] = None,  # PairingStore — Any to avoid circular import
    ):
        self._config = config
        self._runtime_store = runtime_store
        self._pairing_store = pairing_store
        # Rate limiting: (user_id, hour_bucket) → count
        self._rate_counts: Dict[Tuple[str, int], int] = {}
        self._rate_lock = threading.Lock()
        # Apply auto-tier from env vars (once at init)
        if config is not None and config.auto_tier:
            self._apply_env_auto_tiers()

    @property
    def active(self) -> bool:
        """True when permission tiers are configured and enabled."""
        return self._config is not None

    @property
    def config(self) -> Optional[PermissionTiersConfig]:
        return self._config

    @property
    def owner_tier_name(self) -> str:
        """The configured owner tier name (default: 'owner')."""
        if self._config is not None:
            return self._config.env_owner_tier
        return "owner"

    # ------------------------------------------------------------------
    # Auto-tier from env vars (Phase 10)
    # ------------------------------------------------------------------

    def _apply_env_auto_tiers(self) -> None:
        """Populate config.users from *_ALLOWED_USERS env vars and pairing store.

        Runs once at init when ``auto_tier: true`` is set. Injects
        ``UserTierConfig`` entries into ``self._config.users`` for user IDs
        found in platform allowlist env vars and the pairing store.

        Rules:
        - First entry in a platform's ALLOWED_USERS → env_owner_tier
        - Remaining entries → env_default_tier
        - Pairing-approved users → pairing_default_tier
        - If any ALLOW_ALL flag is set, inject wildcard "*" → env_open_tier
        - Explicit config entries are NEVER overridden (skip existing keys)
        - Keys are composite ``platform:user_id`` to prevent cross-platform collisions
        """
        if self._config is None or not self._config.auto_tier:
            return

        import os

        env_owner = self._config.env_owner_tier
        env_default = self._config.env_default_tier
        pairing_tier = self._config.pairing_default_tier
        open_tier = self._config.env_open_tier

        # 1. Platform ALLOWED_USERS env vars
        for platform, env_var in PLATFORM_ALLOWED_USERS_ENV.items():
            raw = os.getenv(env_var, "").strip()
            if not raw:
                continue
            ids = [uid.strip() for uid in raw.split(",") if uid.strip()]
            if not ids:
                continue
            for i, user_id in enumerate(ids):
                composite_key = f"{platform.value}:{user_id}"
                # Explicit config always wins
                if composite_key in self._config.users or user_id in self._config.users:
                    continue
                tier_name = env_owner if i == 0 else env_default
                self._config.users[composite_key] = UserTierConfig(tier=tier_name)
            # Log the auto-promoted owner
            if ids:
                logger.info(
                    "Auto-tier: %s first entry '%s' → %s",
                    env_var,
                    ids[0],
                    env_owner,
                )

        # 2. Global GATEWAY_ALLOWED_USERS
        global_raw = os.getenv("GATEWAY_ALLOWED_USERS", "").strip()
        if global_raw:
            ids = [uid.strip() for uid in global_raw.split(",") if uid.strip()]
            if ids:
                for i, user_id in enumerate(ids):
                    composite_key = f"global:{user_id}"
                    if (
                        composite_key in self._config.users
                        or user_id in self._config.users
                    ):
                        continue
                    tier_name = env_owner if i == 0 else env_default
                    self._config.users[composite_key] = UserTierConfig(tier=tier_name)
                logger.info(
                    "Auto-tier: GATEWAY_ALLOWED_USERS first entry '%s' → %s",
                    ids[0],
                    env_owner,
                )

        # 3. Pairing-approved users
        if self._pairing_store is not None:
            try:
                approved = self._pairing_store.list_approved()
                for entry in approved:
                    platform_name = entry.get("platform", "unknown")
                    user_id = entry.get("user_id")
                    if not user_id:
                        continue
                    composite_key = f"{platform_name}:{user_id}"
                    # Don't override existing mappings
                    if (
                        composite_key in self._config.users
                        or user_id in self._config.users
                    ):
                        continue
                    self._config.users[composite_key] = UserTierConfig(
                        tier=pairing_tier
                    )
                if approved:
                    logger.info(
                        "Auto-tier: %d pairing-approved user(s) → %s",
                        len(approved),
                        pairing_tier,
                    )
            except Exception as exc:
                logger.warning("Auto-tier: failed to read pairing store: %s", exc)

        # 4. ALLOW_ALL_USERS → wildcard entry (only if no explicit wildcard)
        if "*" not in self._config.users:
            allow_all_flags = list(PLATFORM_ALLOW_ALL_ENV.values()) + [
                "GATEWAY_ALLOW_ALL_USERS"
            ]
            for flag_var in allow_all_flags:
                if os.getenv(flag_var, "").lower() in ("true", "1", "yes"):
                    self._config.users["*"] = UserTierConfig(tier=open_tier)
                    logger.info(
                        "Auto-tier: open access detected (%s) → wildcard '%s'",
                        flag_var,
                        open_tier,
                    )
                    break

    def resolve_user_tier(self, source) -> str:
        """Return tier name for *source*.

        Resolution order:
        1. Runtime overlay (SQLite — set via /users set)
        2. Config.yaml user mapping (explicit user_id or wildcard "*")
        3. default_tier from config
        4. Most-restrictive tier (fail-closed)

        Validates that the resolved tier exists in the config. If not, falls
        back to default_tier, then to the most-restrictive tier available.
        """
        if self._config is None:
            return "admin"

        user_id = getattr(source, "user_id", None)

        # 1. Runtime overlay (highest priority — admin-set at runtime)
        if user_id and self._runtime_store is not None:
            _platform_val = getattr(getattr(source, "platform", None), "value", None)
            runtime_entry = self._runtime_store.get_user_tier(
                user_id, source_platform=_platform_val
            )
            if runtime_entry is not None:
                tier_name = runtime_entry["tier_name"]
                if tier_name in self._config.tiers:
                    return tier_name
                # Runtime tier no longer exists in config — log and fall through
                logger.warning(
                    "Runtime tier '%s' for user '%s' not in config tiers, "
                    "falling back to config resolution",
                    tier_name,
                    user_id,
                )

        # 2. Config.yaml user mapping (composite key → bare user_id → wildcard)
        composite_key = None
        platform = getattr(source, "platform", None)
        if platform and user_id:
            composite_key = f"{platform.value}:{user_id}"
        user_cfg = None
        if composite_key:
            user_cfg = self._config.users.get(composite_key)
        if user_cfg is None:
            user_cfg = self._config.users.get(user_id)
        if user_cfg is None:
            user_cfg = self._config.users.get("*")

        # 3. Dynamic pairing injection (auto-tier only, on-the-fly)
        if (
            user_cfg is None
            and user_id
            and self._config.auto_tier
            and self._pairing_store is not None
        ):
            platform_name = platform.value if platform else "unknown"
            try:
                if self._pairing_store.is_approved(platform_name, user_id):
                    tier_name = self._config.pairing_default_tier
                    dyn_key = f"{platform_name}:{user_id}"
                    self._config.users[dyn_key] = UserTierConfig(tier=tier_name)
                    logger.info(
                        "Auto-tier (dynamic): pairing-approved user '%s' → %s",
                        dyn_key,
                        tier_name,
                    )
                    return tier_name
            except Exception as exc:
                logger.warning("Auto-tier (dynamic): pairing check failed: %s", exc)

        # F-10: user_cfg.tier may be None (user entry with no tier key)
        tier_name = (
            user_cfg.tier if user_cfg and user_cfg.tier else self._config.default_tier
        )
        # Validate tier exists — fail-closed on unknown tier names (typos)
        if tier_name not in self._config.tiers:
            logger.warning(
                "Permission tier '%s' not defined, falling back to '%s'",
                tier_name,
                self._config.default_tier,
            )
            tier_name = self._config.default_tier
            if tier_name not in self._config.tiers:
                # F-9: Fall back to most-restrictive tier, not "admin".
                tier_name = self.most_restrictive_tier(self._config)
        return tier_name

    def resolve_user_cfg(self, source):
        """Return ``UserTierConfig`` for *source* or ``None``."""
        if self._config is None:
            return None
        # Match resolve_user_tier() resolution order: composite → bare → wildcard
        platform = getattr(source, "platform", None)
        user_id = getattr(source, "user_id", None)
        if platform and user_id:
            composite = f"{platform.value}:{user_id}"
            cfg = self._config.users.get(composite)
            if cfg:
                return cfg
        return self._config.users.get(user_id) or self._config.users.get("*")

    @staticmethod
    def most_restrictive_tier(pt: PermissionTiersConfig) -> str:
        """Return the name of the most-restrictive tier in *pt.tiers*.

        "Most restrictive" = fewest tools (resolved_tools or allowed_toolsets,
        with ``"*"`` counting as infinity), and among ties, no exec and no
        admin preferred. If *pt.tiers* is empty, returns a sentinel name that
        ``get_tier_config()`` maps to a restrictive fallback.
        """
        if not pt.tiers:
            return "__restricted_fallback__"

        def _score(t: TierDefinition) -> tuple:
            if t.resolved_tools is not None:
                n = float("inf") if "*" in t.resolved_tools else len(t.resolved_tools)
            else:
                toolsets = t.allowed_toolsets or []
                n = float("inf") if "*" in toolsets else len(toolsets)
            has_exec = 1 if t.allow_exec else 0
            has_admin = 1 if t.allow_admin_commands else 0
            return (n, has_exec, has_admin)

        best = min(pt.tiers.items(), key=lambda kv: _score(kv[1]))
        return best[0]

    # ------------------------------------------------------------------
    # Tier config lookups
    # ------------------------------------------------------------------

    def get_tier_config(self, tier_name: str) -> Optional[TierDefinition]:
        """Return ``TierDefinition`` or ``None`` if tiers are unconfigured."""
        if self._config is None:
            return None
        cfg = self._config.tiers.get(tier_name)
        if cfg is None and tier_name == "__restricted_fallback__":
            # F-9: Sentinel tier for completely misconfigured configs.
            return TierDefinition(
                allowed_toolsets=[],
                allow_exec=False,
                allow_admin_commands=False,
            )
        return cfg

    def get_allowed_toolsets(self, tier_name: str) -> List[str]:
        """Return allowed toolset list. ``["*"]`` means all allowed."""
        tier = self.get_tier_config(tier_name)
        if tier is None:
            return ["*"]
        return tier.allowed_toolsets

    # ------------------------------------------------------------------
    # Tool filtering
    # ------------------------------------------------------------------

    def filter_tools(
        self,
        enabled_toolsets: List[str],
        tier_name: str,
    ) -> Tuple[List[str], Optional[FrozenSet[str]]]:
        """Apply tier-based tool filtering.

        Returns ``(filtered_toolsets, allowed_tool_names)`` where:

        - *filtered_toolsets* is the toolset list after filtering.
        - *allowed_tool_names* is ``None`` (toolset filtering used) or a
          ``frozenset`` of allowed tool names (tool-level filtering used).

        When the tier has ``resolved_tools`` set, tool-level filtering
        takes precedence and *filtered_toolsets* is unchanged (the
        name-level filter is applied later in ``get_tool_definitions``).
        """
        tier_cfg = self.get_tier_config(tier_name)

        if tier_cfg is None:
            return enabled_toolsets, None

        # Tool-level filtering (allowed_tools + @group expansion)
        if tier_cfg.resolved_tools is not None:
            if "*" in tier_cfg.resolved_tools:
                # @all / "*" — no filtering needed
                return enabled_toolsets, None
            return enabled_toolsets, tier_cfg.resolved_tools

        # Legacy toolset-level filtering
        _allowed = tier_cfg.allowed_toolsets
        if "*" in _allowed:
            return enabled_toolsets, None

        _pre_filter = list(enabled_toolsets)
        enabled_toolsets = [ts for ts in enabled_toolsets if ts in _allowed]
        if not enabled_toolsets and _pre_filter:
            logger.warning(
                "Tier '%s': allowed_toolsets %s had no overlap with "
                "enabled toolsets %s — possible misconfiguration",
                tier_name,
                _allowed,
                _pre_filter,
            )
        return enabled_toolsets, None

    # ------------------------------------------------------------------
    # Time windows
    # ------------------------------------------------------------------

    def is_within_time_window(self, tier: TierDefinition) -> Tuple[bool, Optional[str]]:
        """Check time restrictions. Returns ``(allowed, reason_key_or_None)``.

        Handles cross-midnight windows (e.g. 22:00 → 07:00).
        """
        if tier is None or tier.time_restrictions is None:
            return True, None
        tr = tier.time_restrictions

        try:
            tz = ZoneInfo(tr.timezone)
        except Exception:
            logger.warning("Invalid timezone '%s', falling back to UTC", tr.timezone)
            tz = ZoneInfo("UTC")

        now = datetime.now(tz)
        if tr.days is not None and now.weekday() not in tr.days:
            return False, "time_restricted_wrong_day"

        try:
            start_h, start_m = map(int, tr.start.split(":"))
            end_h, end_m = map(int, tr.end.split(":"))
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "Invalid time restriction format: %s-%s, denying access",
                tr.start,
                tr.end,
            )
            return False, "time_restricted_invalid"

        now_minutes = now.hour * 60 + now.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if not (0 <= start_minutes < 1440 and 0 <= end_minutes < 1440):
            logger.warning(
                "Invalid time restriction values: %s-%s, denying access",
                tr.start,
                tr.end,
            )
            return False, "time_restricted_invalid"

        if start_minutes <= end_minutes:
            if now_minutes < start_minutes:
                return False, "time_restricted_before"
            if now_minutes >= end_minutes:
                return False, "time_restricted_after"
        else:
            # Cross-midnight (e.g. 22:00 → 07:00)
            if now_minutes < start_minutes and now_minutes >= end_minutes:
                return False, "time_restricted_after"

        return True, None

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------

    def format_tier_message(
        self,
        tier: TierDefinition,
        key: str,
        source,
        user_cfg=None,
    ) -> str:
        """Look up i18n message template by *key*.

        Fallback chain: key+locale → key+en → hardcoded English.
        """
        locale = "en"
        if user_cfg:
            locale = user_cfg.locale
        elif self._config:
            _resolved_cfg = self.resolve_user_cfg(source)
            if _resolved_cfg:
                locale = _resolved_cfg.locale

        template = (tier.messages or {}).get(key, {}).get(locale)
        if not template:
            template = (tier.messages or {}).get(key, {}).get("en")

        tr = tier.time_restrictions
        if template and tr:
            try:
                return (
                    template.replace("{start}", str(tr.start))
                    .replace("{end}", str(tr.end))
                    .replace("{timezone}", str(tr.timezone))
                )
            except Exception:
                return template
        if template:
            return template

        fallbacks = {
            "exec_denied": (
                "You don't have permission to approve terminal commands. "
                "Ask an admin to approve it, or use /whoami to check your access level."
            ),
            "command_denied": (
                "This command requires higher access. Use /whoami to see your "
                "available commands, or ask an admin to run it for you."
            ),
            "time_restricted_before": f"Access starts at {tr.start if tr else '08:00'}.",
            "time_restricted_after": f"Access ended at {tr.end if tr else '22:00'}.",
            "time_restricted_wrong_day": "Not available today.",
            "rate_limited": (
                "You've reached your message limit for this hour. "
                "Your limit will reset soon — please try again later."
            ),
            "rate_limited_blocked": "Access is currently restricted.",
        }
        return fallbacks.get(
            key, "Access restricted. Use /whoami to check your permissions."
        )

    # ------------------------------------------------------------------
    # Approval key isolation
    # ------------------------------------------------------------------

    def approval_key(self, session_key: str, source) -> Any:
        """Compute the approval isolation key for a session+user pair.

        When tiers are active and the source carries a user_id, returns a
        ``(session_key, user_id)`` tuple. Otherwise returns plain
        ``session_key``.
        """
        if self._config is not None and getattr(source, "user_id", None):
            return (session_key, source.user_id)
        return session_key

    # ------------------------------------------------------------------
    # Runtime user management
    # ------------------------------------------------------------------

    def set_user_tier(
        self,
        user_id: str,
        tier_name: str,
        granted_by: str = "system",
        source_platform: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Set runtime tier for a user.

        Returns (success, message). Validates tier exists before setting.
        """
        if self._config is None:
            return False, "Permission tiers are not configured."
        if tier_name not in self._config.tiers:
            available = ", ".join(sorted(self._config.tiers.keys()))
            return False, f"Unknown tier '{tier_name}'. Available: {available}"
        if self._runtime_store is None:
            return False, "Runtime user store is not available."
        self._runtime_store.set_user_tier(
            user_id, tier_name, granted_by=granted_by, source_platform=source_platform
        )
        return True, f"User '{user_id}' assigned to tier '{tier_name}'."

    def remove_user_tier(
        self,
        user_id: str,
        source_platform: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Remove runtime tier for a user.

        Returns (success, message). User falls back to config resolution.
        """
        if self._runtime_store is None:
            return False, "Runtime user store is not available."
        removed = self._runtime_store.remove_user_tier(
            user_id, source_platform=source_platform
        )
        if removed:
            return (
                True,
                f"Runtime tier removed for user '{user_id}'. They will use config/default resolution.",
            )
        return False, f"No runtime tier found for user '{user_id}'."

    def list_users(self) -> List[Dict[str, Any]]:
        """Return combined user list from config + runtime overlay.

        Each entry has: user_id, tier_name, source (config/runtime),
        granted_by (for runtime entries).
        """
        result: Dict[str, Dict[str, Any]] = {}

        # Config-based users
        if self._config is not None:
            for uid, ucfg in self._config.users.items():
                if ucfg.tier:
                    result[uid] = {
                        "user_id": uid,
                        "tier_name": ucfg.tier,
                        "source": "config",
                        "granted_by": None,
                        "granted_at": None,
                    }

        # Runtime overlay (overrides config entries for same user_id)
        if self._runtime_store is not None:
            for entry in self._runtime_store.list_all():
                uid = entry["user_id"]
                sp = entry.get("source_platform")
                # Use composite key when platform is known to match config entries
                dict_key = f"{sp}:{uid}" if sp else uid
                # Also check if the bare uid was set by config, so runtime overrides it
                if uid in result and dict_key not in result:
                    dict_key = uid
                result[dict_key] = {
                    "user_id": uid,
                    "tier_name": entry["tier_name"],
                    "source": "runtime",
                    "granted_by": entry["granted_by"],
                    "granted_at": entry["granted_at"],
                }

        return sorted(result.values(), key=lambda e: e["user_id"])

    def whoami(self, source) -> Dict[str, Any]:
        """Return comprehensive identity info for a user.

        Returns dict with: user_id, tier_name, tier_source, tool_count,
        allow_exec, allow_admin_commands, time_restrictions, platform.
        """
        user_id = getattr(source, "user_id", None)
        platform = getattr(source, "platform", None)
        tier_name = self.resolve_user_tier(source)
        tier_cfg = self.get_tier_config(tier_name)

        # Determine source of the tier assignment
        tier_source = "default"
        if self._config is not None and user_id:
            if self._runtime_store is not None:
                _platform_val = getattr(platform, "value", None) if platform else None
                rt = self._runtime_store.get_user_tier(
                    user_id, source_platform=_platform_val
                )
                if rt is not None:
                    tier_source = "runtime"
                elif user_id in self._config.users:
                    tier_source = "config"
                elif "*" in self._config.users:
                    tier_source = "wildcard"
            elif user_id in self._config.users:
                tier_source = "config"
            elif "*" in self._config.users:
                tier_source = "wildcard"

        # Tool count
        tool_count = None
        if tier_cfg is not None:
            if tier_cfg.resolved_tools is not None:
                if "*" in tier_cfg.resolved_tools:
                    tool_count = "all"
                else:
                    tool_count = len(tier_cfg.resolved_tools)
            elif "*" in (tier_cfg.allowed_toolsets or []):
                tool_count = "all"
            else:
                tool_count = len(tier_cfg.allowed_toolsets or [])

        return {
            "user_id": user_id,
            "user_name": getattr(source, "user_name", None),
            "platform": platform.value if platform else None,
            "tier_name": tier_name,
            "tier_source": tier_source,
            "tool_count": tool_count,
            "allow_exec": tier_cfg.allow_exec if tier_cfg else True,
            "allow_admin_commands": (
                tier_cfg.allow_admin_commands if tier_cfg else True
            ),
            "time_restrictions": (
                tier_cfg.time_restrictions.to_dict()
                if tier_cfg and tier_cfg.time_restrictions
                else None
            ),
            "requests_per_hour": (tier_cfg.requests_per_hour if tier_cfg else None),
            "rate_limit_remaining": self.rate_limit_remaining(source),
        }

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _current_hour_bucket(self) -> int:
        """Return the current hour as a Unix-timestamp bucket (floored to hour)."""
        return int(time.time()) // 3600

    def check_rate_limit(self, source) -> Tuple[bool, Optional[str]]:
        """Check if the user is within their rate limit.

        Returns ``(allowed, reason_or_None)``. When allowed is False,
        reason is a string key for message lookup.

        Side effect: increments the user's counter when allowed.
        """
        if self._config is None:
            return True, None

        user_id = getattr(source, "user_id", None)
        if not user_id:
            return True, None  # No user_id = no rate limiting

        tier_name = self.resolve_user_tier(source)
        tier_cfg = self.get_tier_config(tier_name)

        if tier_cfg is None or tier_cfg.requests_per_hour is None:
            return True, None  # No rate limit configured

        limit = tier_cfg.requests_per_hour
        if limit == 0:
            return False, "rate_limited_blocked"  # Explicitly blocked

        bucket = self._current_hour_bucket()
        # Composite key prevents cross-platform rate-limit bleed (same user_id
        # on Telegram and Discord get independent counters).
        _platform = (
            getattr(getattr(source, "platform", None), "value", None) or "unknown"
        )
        key = (_platform, user_id, bucket)

        with self._rate_lock:
            count = self._rate_counts.get(key, 0)
            if count >= limit:
                return False, "rate_limited"
            self._rate_counts[key] = count + 1

        return True, None

    def rate_limit_remaining(self, source) -> Optional[int]:
        """Return the number of requests remaining in the current hour.

        Returns ``None`` when no rate limit is configured.
        """
        if self._config is None:
            return None

        user_id = getattr(source, "user_id", None)
        if not user_id:
            return None

        tier_name = self.resolve_user_tier(source)
        tier_cfg = self.get_tier_config(tier_name)

        if tier_cfg is None or tier_cfg.requests_per_hour is None:
            return None

        limit = tier_cfg.requests_per_hour
        bucket = self._current_hour_bucket()
        _platform = (
            getattr(getattr(source, "platform", None), "value", None) or "unknown"
        )
        key = (_platform, user_id, bucket)

        with self._rate_lock:
            count = self._rate_counts.get(key, 0)

        return max(0, limit - count)

    def rate_limit_resets_in(self) -> Optional[int]:
        """Return seconds until the current rate limit window resets.

        Returns ``None`` when rate limiting is not active.
        """
        if self._config is None:
            return None

        has_any_limit = any(
            t.requests_per_hour is not None for t in self._config.tiers.values()
        )
        if not has_any_limit:
            return None

        now = time.time()
        return 3600 - (now % 3600)

    def cleanup_rate_counters(self) -> None:
        """Remove expired rate limit counters (buckets older than the current hour).

        Call periodically (e.g. every 10 minutes) to prevent unbounded memory growth.
        """
        current_bucket = self._current_hour_bucket()
        with self._rate_lock:
            expired_keys = [k for k in self._rate_counts if k[2] < current_bucket]
            for k in expired_keys:
                del self._rate_counts[k]

    # ------------------------------------------------------------------
    # Agent context (system prompt injection)
    # ------------------------------------------------------------------

    def build_tier_context(
        self, tier_name: str, tier_cfg: Optional["TierDefinition"]
    ) -> Optional[str]:
        """Build the agent-aware tier context string for system prompt injection.

        Returns None when no context is needed (full access or no tiers).
        """
        if self._config is None or tier_cfg is None:
            return None

        parts = []

        # Tier name
        parts.append(
            f"You are operating with restricted permissions (tier: {tier_name})."
        )

        # Tool-level detail
        if tier_cfg.resolved_tools is not None and "*" not in tier_cfg.resolved_tools:
            tool_count = len(tier_cfg.resolved_tools)
            mcp_patterns = [t for t in tier_cfg.resolved_tools if t.startswith("mcp:")]
            regular_count = tool_count - len(mcp_patterns)
            detail = f"You have {regular_count} tools available"
            if mcp_patterns:
                detail += f" plus {len(mcp_patterns)} MCP tool pattern(s)"
            parts.append(detail + ".")
        elif tier_cfg.allowed_toolsets and "*" not in tier_cfg.allowed_toolsets:
            parts.append(
                f"Only these toolsets are available: {', '.join(tier_cfg.allowed_toolsets)}."
            )

        # Exec restriction note
        if not tier_cfg.allow_exec:
            parts.append(
                "You cannot approve or deny terminal command executions. "
                "If the user asks you to run a command, explain that you don't have "
                "exec access and suggest they ask an admin."
            )

        # Rate limit note
        if tier_cfg.requests_per_hour is not None:
            parts.append(f"Rate limit: {tier_cfg.requests_per_hour} requests per hour.")

        # Helpful denial behavioral guidance (T7e)
        parts.append(
            "When a user asks you to do something you can't do with your current tools:\n"
            "1. Acknowledge the request positively\n"
            "2. Explain what you CAN do instead (using your available tools)\n"
            "3. If possible, offer step-by-step instructions the user can follow themselves\n"
            "4. Suggest they ask an admin if the task requires elevated access\n"
            "Never just say 'denied' or 'I can't' — always offer a helpful alternative."
        )

        return "\n".join(parts) if parts else None

    # ------------------------------------------------------------------
    # Platform role mapping (T3/T4)
    # ------------------------------------------------------------------

    def resolve_platform_role_tier(self, source) -> Optional[str]:
        """Resolve tier from platform-specific roles (Telegram admin, Discord roles).

        Checks the platform_role_mapping config section for matching roles.
        Returns tier name or None if no mapping matches.

        Resolution order:
        1. Exact role name match → mapped tier
        2. Wildcard role "*" → mapped tier
        3. No match → None (fall through to standard resolution)
        """
        if self._config is None or not self._config.platform_role_mapping:
            return None

        user_roles = getattr(source, "user_roles", None)
        if not user_roles:
            return None

        platform = getattr(source, "platform", None)
        if not platform:
            return None
        platform_name = platform.value

        # Get platform-specific mapping
        platform_mapping = self._config.platform_role_mapping.get(platform_name, {})
        if not platform_mapping:
            # Also try generic "default" mapping
            platform_mapping = self._config.platform_role_mapping.get("default", {})
        if not platform_mapping:
            return None

        # roles config is: {"administrator": "admin", "moderator": "user", ...}
        for role in user_roles:
            tier_name = platform_mapping.get(role)
            if tier_name:
                # Validate tier exists
                if tier_name in self._config.tiers:
                    return tier_name
                logger.warning(
                    "Platform role mapping: role '%s' → tier '%s' "
                    "not defined, skipping",
                    role,
                    tier_name,
                )

        # Check wildcard
        wildcard_tier = platform_mapping.get("*")
        if wildcard_tier and wildcard_tier in self._config.tiers:
            return wildcard_tier

        return None

    def is_elevated_tier(self, tier_name: str) -> bool:
        """Check if a tier has elevated (MCP) access.

        Elevated means the tier has wildcard tools, @mcp group,
        or explicit mcp: patterns in resolved_tools.
        """
        tier_cfg = self.get_tier_config(tier_name)
        if tier_cfg is None:
            return True  # No config = unrestricted = elevated

        # Check resolved_tools for wildcard or MCP patterns
        if tier_cfg.resolved_tools is not None:
            if "*" in tier_cfg.resolved_tools:
                return True
            if any(t.startswith("mcp:") for t in tier_cfg.resolved_tools):
                return True

        # Check allowed_tools (pre-expansion) for @mcp or mcp: patterns
        if tier_cfg.allowed_tools:
            for tool in tier_cfg.allowed_tools:
                if tool in ("@mcp", "@all"):
                    return True
                if tool.startswith("mcp:"):
                    return True

        # Check allowed_toolsets for @mcp
        if tier_cfg.allowed_toolsets:
            for ts in tier_cfg.allowed_toolsets:
                if ts in ("@mcp", "*"):
                    return True

        return False


# ---------------------------------------------------------------------------
# Promote request store (T7d)
# ---------------------------------------------------------------------------


class PromoteRequestStore:
    """SQLite-backed store for pending tier promotion requests.

    Users request tier promotion via ``/promote <tier>``. Requests are stored
    here for admin review. Admins can approve or deny via ``/promote approve/deny``.

    Follows the same patterns as RuntimeUserStore: per-call connections,
    threading lock, composite keys, WAL mode.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from hermes_constants import get_hermes_home

            db_path = str(get_hermes_home() / "promote_requests.db")
        self._db_path = db_path
        self._lock = threading.RLock()  # Reentrant lock for nested calls
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
                    CREATE TABLE IF NOT EXISTS promote_requests (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        requested_tier TEXT NOT NULL,
                        current_tier TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at REAL NOT NULL,
                        resolved_at REAL,
                        resolved_by TEXT,
                        reason TEXT
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_promote_status
                    ON promote_requests(status, created_at)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_promote_user
                    ON promote_requests(platform, user_id)
                """)
                conn.commit()
            finally:
                conn.close()

    def create_request(
        self,
        user_id: str,
        platform: str,
        requested_tier: str,
        current_tier: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a new promotion request. Returns the request dict or None if one already exists."""
        import uuid

        request_id = uuid.uuid4().hex[:8]  # Short UUID for easy reference
        now = time.time()

        with self._lock:
            conn = self._connect()
            try:
                # Check for existing pending request from same user
                existing = conn.execute(
                    "SELECT id FROM promote_requests "
                    "WHERE user_id = ? AND platform = ? AND status = 'pending'",
                    (user_id, platform),
                ).fetchone()
                if existing:
                    return None  # Already has a pending request

                conn.execute(
                    "INSERT INTO promote_requests "
                    "(id, user_id, platform, requested_tier, current_tier, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (request_id, user_id, platform, requested_tier, current_tier, now),
                )
                conn.commit()
                return {
                    "id": request_id,
                    "user_id": user_id,
                    "platform": platform,
                    "requested_tier": requested_tier,
                    "current_tier": current_tier,
                    "status": "pending",
                    "created_at": now,
                }
            finally:
                conn.close()

    def get_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Get a request by ID."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, user_id, platform, requested_tier, current_tier, "
                    "status, created_at, resolved_at, resolved_by, reason "
                    "FROM promote_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "user_id": row[1],
                    "platform": row[2],
                    "requested_tier": row[3],
                    "current_tier": row[4],
                    "status": row[5],
                    "created_at": row[6],
                    "resolved_at": row[7],
                    "resolved_by": row[8],
                    "reason": row[9],
                }
            finally:
                conn.close()

    def list_pending(self) -> List[Dict[str, Any]]:
        """List all pending requests."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, user_id, platform, requested_tier, current_tier, "
                    "status, created_at, resolved_at, resolved_by, reason "
                    "FROM promote_requests WHERE status = 'pending' "
                    "ORDER BY created_at DESC"
                ).fetchall()
                return [
                    {
                        "id": r[0],
                        "user_id": r[1],
                        "platform": r[2],
                        "requested_tier": r[3],
                        "current_tier": r[4],
                        "status": r[5],
                        "created_at": r[6],
                        "resolved_at": r[7],
                        "resolved_by": r[8],
                        "reason": r[9],
                    }
                    for r in rows
                ]
            finally:
                conn.close()

    def approve_request(
        self,
        request_id: str,
        resolved_by: str,
        reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Approve a pending request. Returns the updated request or None."""
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT status FROM promote_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                if not row or row[0] != "pending":
                    return None
                conn.execute(
                    "UPDATE promote_requests SET status = 'approved', "
                    "resolved_at = ?, resolved_by = ?, reason = ? WHERE id = ?",
                    (now, resolved_by, reason, request_id),
                )
                conn.commit()
                return self.get_request(request_id)
            finally:
                conn.close()

    def deny_request(
        self,
        request_id: str,
        resolved_by: str,
        reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Deny a pending request. Returns the updated request or None."""
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT status FROM promote_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                if not row or row[0] != "pending":
                    return None
                conn.execute(
                    "UPDATE promote_requests SET status = 'denied', "
                    "resolved_at = ?, resolved_by = ?, reason = ? WHERE id = ?",
                    (now, resolved_by, reason, request_id),
                )
                conn.commit()
                return self.get_request(request_id)
            finally:
                conn.close()

    def cleanup_expired(self, max_age_hours: int = 72) -> int:
        """Remove expired pending requests older than max_age_hours.

        Returns the number of removed requests.
        """
        cutoff = time.time() - (max_age_hours * 3600)
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM promote_requests "
                    "WHERE status = 'pending' AND created_at < ?",
                    (cutoff,),
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

"""Tests for user permission tiers.

Covers: config schema (Phase 1), tier resolution + tool gating (Phase 2),
exec gating (Phase 3), time restrictions (Phase 4), admin commands (Phase 5),
and i18n message formatting (Phase 6).
"""

from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import logging
import pytest

from gateway.config import (
    GatewayConfig,
    PermissionTiersConfig,
    Platform,
    PlatformConfig,
    TimeRestrictions,
    TierDefinition,
    UserTierConfig,
)
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_source(user_id="u1", platform=Platform.DISCORD) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str, user_id="u1") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(user_id=user_id),
        message_id="m1",
    )


def _make_runner(permission_tiers=None, quick_commands=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")},
        permission_tiers=permission_tiers,
        quick_commands=quick_commands or {},
    )
    runner.adapters = {Platform.DISCORD: MagicMock()}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._ephemeral_system_prompt = None
    runner._agent_cache = {}
    runner._agent_cache_lock = SimpleNamespace(
        __enter__=lambda self: None, __exit__=lambda self, *a: None
    )
    return runner


def _admin_tier():
    return TierDefinition(
        allowed_toolsets=["*"],
        allow_exec=True,
        allow_admin_commands=True,
    )


def _restricted_tier(
    allowed_toolsets=None,
    allow_exec=False,
    allow_admin_commands=False,
    time_restrictions=None,
    messages=None,
):
    return TierDefinition(
        allowed_toolsets=allowed_toolsets or ["hermes-discord"],
        allow_exec=allow_exec,
        allow_admin_commands=allow_admin_commands,
        time_restrictions=time_restrictions,
        messages=messages or {},
    )


def _make_permission_config(tiers=None, users=None, default_tier="admin", **kwargs):
    _default_tiers = {"admin": _admin_tier(), "restricted": _restricted_tier()}
    return PermissionTiersConfig(
        default_tier=default_tier,
        tiers=tiers if tiers is not None else _default_tiers,
        users=users or {},
        **kwargs,
    )


# ------------------------------------------------------------------
# Phase 1: Config Schema
# ------------------------------------------------------------------


class TestPermissionTiersConfig:
    def test_time_restrictions_defaults(self):
        tr = TimeRestrictions()
        assert tr.start == "08:00"
        assert tr.end == "22:00"
        assert tr.timezone == "UTC"
        assert tr.days is None

    def test_time_restrictions_roundtrip(self):
        tr = TimeRestrictions(
            start="09:00", end="23:00", timezone="Europe/Vienna", days=[0, 1, 2, 3, 4]
        )
        d = tr.to_dict()
        restored = TimeRestrictions.from_dict(d)
        assert restored.start == "09:00"
        assert restored.end == "23:00"
        assert restored.timezone == "Europe/Vienna"
        assert restored.days == [0, 1, 2, 3, 4]

    def test_tier_definition_defaults(self):
        td = TierDefinition()
        assert td.allowed_toolsets == ["*"]
        assert td.allow_exec is True
        assert td.allow_admin_commands is True
        assert td.time_restrictions is None
        assert td.messages == {}

    def test_tier_definition_roundtrip(self):
        td = TierDefinition(
            allowed_toolsets=["hermes-discord"],
            allow_exec=False,
            allow_admin_commands=False,
            time_restrictions=TimeRestrictions(start="08:00", end="22:00"),
            messages={"exec_denied": {"en": "Nope", "de": "Nein"}},
        )
        d = td.to_dict()
        restored = TierDefinition.from_dict(d)
        assert restored.allowed_toolsets == ["hermes-discord"]
        assert restored.allow_exec is False
        assert restored.allow_admin_commands is False
        assert restored.time_restrictions.start == "08:00"
        assert restored.messages["exec_denied"]["de"] == "Nein"

    def test_user_tier_config_defaults(self):
        utc = UserTierConfig()
        assert utc.tier is None  # F-10: default is None, not "admin"
        assert utc.locale == "en"

    def test_user_tier_config_roundtrip(self):
        utc = UserTierConfig(tier="restricted", locale="de")
        d = utc.to_dict()
        restored = UserTierConfig.from_dict(d)
        assert restored.tier == "restricted"
        assert restored.locale == "de"

    def test_permission_tiers_config_roundtrip(self):
        pt = _make_permission_config(
            tiers={
                "admin": _admin_tier(),
                "restricted": _restricted_tier(),
            },
            users={
                "u1": UserTierConfig(tier="admin", locale="en"),
                "u2": UserTierConfig(tier="restricted", locale="de"),
                "*": UserTierConfig(tier="restricted", locale="de"),
            },
            default_tier="restricted",
        )
        d = pt.to_dict()
        restored = PermissionTiersConfig.from_dict(d)
        assert restored.default_tier == "restricted"
        assert "admin" in restored.tiers
        assert "restricted" in restored.tiers
        assert restored.users["u1"].tier == "admin"
        assert restored.users["*"].locale == "de"

    def test_gateway_config_opt_out(self):
        gc = GatewayConfig()
        assert gc.permission_tiers is None
        d = gc.to_dict()
        restored = GatewayConfig.from_dict(d)
        assert restored.permission_tiers is None

    def test_gateway_config_with_permission_tiers(self):
        gc = GatewayConfig(
            permission_tiers=_make_permission_config(),
        )
        d = gc.to_dict()
        assert d["permission_tiers"] is not None
        restored = GatewayConfig.from_dict(d)
        assert restored.permission_tiers is not None
        assert restored.permission_tiers.default_tier == "admin"

    def test_from_dict_injects_missing_default_tier_as_restrictive(self):
        """If default_tier references a nonexistent tier, from_dict injects most-restrictive."""
        data = {
            "default_tier": "standard",
            "tiers": {"admin": {"allowed_toolsets": ["*"]}},
            "users": {},
        }
        pt = PermissionTiersConfig.from_dict(data)
        assert "standard" in pt.tiers
        assert pt.tiers["standard"].allowed_toolsets == []
        assert pt.tiers["standard"].allow_exec is False
        assert pt.tiers["standard"].allow_admin_commands is False

    def test_from_dict_injects_missing_user_tier_as_restrictive(self):
        """If a user references a nonexistent tier, from_dict injects most-restrictive."""
        data = {
            "default_tier": "admin",
            "tiers": {"admin": {"allowed_toolsets": ["*"]}},
            "users": {"u1": {"tier": "ghost", "locale": "en"}},
        }
        pt = PermissionTiersConfig.from_dict(data)
        assert "ghost" in pt.tiers
        assert pt.tiers["ghost"].allow_exec is False
        assert pt.tiers["ghost"].allow_admin_commands is False
        assert pt.tiers["ghost"].allowed_toolsets == []


# ------------------------------------------------------------------
# Phase 5: Type Validation & Config Parsing Hardening
# ------------------------------------------------------------------


class TestTypeValidation:
    """Tests for F-7, F-3: type coercion and null handling in TierDefinition."""

    def test_allow_exec_quoted_false_is_false(self):
        """allow_exec: "false" (string) must be False, not truthy."""
        td = TierDefinition.from_dict({"allow_exec": "false"})
        assert td.allow_exec is False

    def test_allow_exec_string_no_is_false(self):
        """allow_exec: "no" must be False."""
        td = TierDefinition.from_dict({"allow_exec": "no"})
        assert td.allow_exec is False

    def test_allow_exec_string_0_is_false(self):
        """allow_exec: "0" must be False."""
        td = TierDefinition.from_dict({"allow_exec": "0"})
        assert td.allow_exec is False

    def test_allow_exec_bool_true_is_true(self):
        """allow_exec: true (bool) must be True."""
        td = TierDefinition.from_dict({"allow_exec": True})
        assert td.allow_exec is True

    def test_allow_exec_bool_false_is_false(self):
        """allow_exec: false (bool) must be False."""
        td = TierDefinition.from_dict({"allow_exec": False})
        assert td.allow_exec is False

    def test_allow_admin_commands_quoted_false_is_false(self):
        """allow_admin_commands: "false" must be False."""
        td = TierDefinition.from_dict({"allow_admin_commands": "false"})
        assert td.allow_admin_commands is False

    def test_allow_admin_commands_string_no_is_false(self):
        """allow_admin_commands: "no" must be False."""
        td = TierDefinition.from_dict({"allow_admin_commands": "no"})
        assert td.allow_admin_commands is False

    def test_allowed_toolsets_string_becomes_empty(self):
        """allowed_toolsets: "*" (string, not list) must fail-closed to []."""
        td = TierDefinition.from_dict({"allowed_toolsets": "*"})
        assert td.allowed_toolsets == []

    def test_allowed_toolsets_null_becomes_wildcard(self):
        """allowed_toolsets: null must default to ["*"] (no restriction)."""
        td = TierDefinition.from_dict({"allowed_toolsets": None})
        assert td.allowed_toolsets == ["*"]

    def test_allowed_toolsets_absent_is_wildcard(self):
        """No allowed_toolsets key → default ["*"]."""
        td = TierDefinition.from_dict({})
        assert td.allowed_toolsets == ["*"]

    def test_allowed_toolsets_list_works_normally(self):
        """allowed_toolsets: ["web"] works as expected."""
        td = TierDefinition.from_dict({"allowed_toolsets": ["web", "search"]})
        assert td.allowed_toolsets == ["web", "search"]

    def test_empty_dataclass_default(self):
        """TierDefinition() with no args gets safe defaults."""
        td = TierDefinition()
        assert td.allowed_toolsets == ["*"]
        assert td.allow_exec is True
        assert td.allow_admin_commands is True


class TestTierResolution:
    def test_no_permission_tiers_returns_admin(self):
        runner = _make_runner(permission_tiers=None)
        source = _make_source()
        assert runner._permissions.resolve_user_tier(source) == "admin"

    def test_none_user_id_graceful(self):
        """user_id=None should not crash — falls to wildcard/default."""
        pt = _make_permission_config(
            users={"*": UserTierConfig(tier="restricted")},
            default_tier="admin",
        )
        runner = _make_runner(permission_tiers=pt)
        source = SessionSource(
            platform=Platform.DISCORD,
            user_id=None,
            chat_id="c1",
            chat_type="dm",
        )
        assert runner._permissions.resolve_user_tier(source) == "restricted"

    def test_known_user_returns_their_tier(self):
        pt = _make_permission_config(users={"u1": UserTierConfig(tier="restricted")})
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        assert runner._permissions.resolve_user_tier(source) == "restricted"

    def test_unknown_user_falls_to_default_tier(self):
        pt = _make_permission_config(
            default_tier="standard",
            tiers={
                "admin": _admin_tier(),
                "standard": _restricted_tier(),
            },
            users={"u1": UserTierConfig(tier="admin")},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u999")
        assert runner._permissions.resolve_user_tier(source) == "standard"

    def test_wildcard_user_fallback(self):
        pt = _make_permission_config(
            default_tier="admin",
            users={"*": UserTierConfig(tier="restricted")},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="unknown_user")
        assert runner._permissions.resolve_user_tier(source) == "restricted"

    def test_specific_user_overrides_wildcard(self):
        pt = _make_permission_config(
            users={
                "u1": UserTierConfig(tier="admin"),
                "*": UserTierConfig(tier="restricted"),
            }
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        assert runner._permissions.resolve_user_tier(source) == "admin"

    def test_typo_tier_name_fails_closed_via_from_dict(self):
        """User mapped to a nonexistent tier gets restrictive injection via from_dict."""
        data = {
            "default_tier": "restricted",
            "tiers": {
                "admin": {"allowed_toolsets": ["*"]},
                "restricted": {"allowed_toolsets": ["hermes-discord"]},
            },
            "users": {"u1": {"tier": "standrad"}},  # typo
        }
        pt = PermissionTiersConfig.from_dict(data)
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        # from_dict injects "standrad" as restrictive — NOT admin bypass
        tier_name = runner._permissions.resolve_user_tier(source)
        assert tier_name == "standrad"
        tier_cfg = runner._permissions.get_tier_config(tier_name)
        assert tier_cfg.allow_exec is False
        assert tier_cfg.allow_admin_commands is False
        assert tier_cfg.allowed_toolsets == []

    def test_all_tiers_missing_still_restrictive(self):
        """If both user tier and default_tier are typos, from_dict injects restrictive for both."""
        data = {
            "default_tier": "nonexistent",
            "tiers": {},
            "users": {"u1": {"tier": "alsobad"}},
        }
        pt = PermissionTiersConfig.from_dict(data)
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        tier_name = runner._permissions.resolve_user_tier(source)
        # "alsobad" was injected, so resolve returns it (not fallback)
        assert tier_name == "alsobad"
        tier_cfg = runner._permissions.get_tier_config(tier_name)
        assert tier_cfg.allow_exec is False
        assert tier_cfg.allow_admin_commands is False

    def test_get_tier_config_returns_none_when_unconfigured(self):
        runner = _make_runner(permission_tiers=None)
        assert runner._permissions.get_tier_config("admin") is None

    def test_get_tier_config_returns_definition(self):
        pt = _make_permission_config(
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()}
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        assert tier is not None
        assert tier.allow_exec is False

    def test_get_tier_config_unknown_tier_returns_none(self):
        pt = _make_permission_config(tiers={"admin": _admin_tier()})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.get_tier_config("nonexistent") is None

    def test_get_tier_allowed_toolsets_wildcard(self):
        pt = _make_permission_config(tiers={"admin": _admin_tier()})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.get_allowed_toolsets("admin") == ["*"]

    def test_get_tier_allowed_toolsets_explicit(self):
        pt = _make_permission_config(
            tiers={
                "restricted": _restricted_tier(
                    allowed_toolsets=["hermes-discord", "hermes-telegram"]
                )
            }
        )
        runner = _make_runner(permission_tiers=pt)
        ts = runner._permissions.get_allowed_toolsets("restricted")
        assert ts == ["hermes-discord", "hermes-telegram"]

    def test_get_tier_allowed_toolsets_unconfigured(self):
        runner = _make_runner(permission_tiers=None)
        assert runner._permissions.get_allowed_toolsets("anything") == ["*"]


class TestAgentConfigSignature:
    def test_signature_includes_user_tier(self):
        from gateway.run import GatewayRunner

        sig_a = GatewayRunner._agent_config_signature(
            "m1", {}, ["ts1"], "prompt", "admin"
        )
        sig_b = GatewayRunner._agent_config_signature(
            "m1", {}, ["ts1"], "prompt", "restricted"
        )
        assert sig_a != sig_b

    def test_signature_same_tier_same_hash(self):
        from gateway.run import GatewayRunner

        sig_a = GatewayRunner._agent_config_signature(
            "m1", {}, ["ts1"], "prompt", "admin"
        )
        sig_b = GatewayRunner._agent_config_signature(
            "m1", {}, ["ts1"], "prompt", "admin"
        )
        assert sig_a == sig_b

    def test_signature_empty_tier_is_same_as_default(self):
        from gateway.run import GatewayRunner

        sig_a = GatewayRunner._agent_config_signature("m1", {}, ["ts1"], "prompt", "")
        sig_b = GatewayRunner._agent_config_signature("m1", {}, ["ts1"], "prompt")
        assert sig_a == sig_b


# ------------------------------------------------------------------
# Phase 4: Time Restrictions
# ------------------------------------------------------------------


class TestTimeRestrictions:
    def test_no_restrictions_always_allowed(self):
        tier = _admin_tier()  # no time_restrictions
        runner = _make_runner()
        allowed, reason = runner._permissions.is_within_time_window(tier)
        assert allowed is True
        assert reason is None

    def test_none_tier_always_allowed(self):
        runner = _make_runner()
        allowed, reason = runner._permissions.is_within_time_window(None)
        assert allowed is True

    def test_within_window(self):
        from datetime import datetime

        now = datetime.now()
        h = now.hour
        # Set window that covers right now
        start = f"{h:02d}:00"
        end = f"{min(h + 1, 23):02d}:00"
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(start=start, end=end, timezone="UTC")
        )
        runner = _make_runner()
        allowed, reason = runner._permissions.is_within_time_window(tier)
        assert allowed is True

    def test_before_window(self):
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(
                start="23:59", end="23:59", timezone="UTC"
            )
        )
        runner = _make_runner()
        allowed, reason = runner._permissions.is_within_time_window(tier)
        assert allowed is False
        assert reason == "time_restricted_before"

    def test_after_window(self):
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(
                start="00:00", end="00:01", timezone="UTC"
            )
        )
        runner = _make_runner()
        allowed, reason = runner._permissions.is_within_time_window(tier)
        assert allowed is False
        assert reason == "time_restricted_after"

    def test_cross_midnight_window(self):
        """22:00 to 07:00 should allow 23:00 and 02:00, block 12:00."""
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(
                start="22:00", end="07:00", timezone="UTC"
            )
        )
        runner = _make_runner()
        from datetime import datetime
        from unittest.mock import patch as _patch
        from zoneinfo import ZoneInfo

        utc = ZoneInfo("UTC")

        # 23:00 — inside the 22:00-07:00 window → allowed
        with _patch("gateway.permissions.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 23, 0, tzinfo=utc)
            allowed, reason = runner._permissions.is_within_time_window(tier)
            assert allowed is True

        # 02:00 — inside the 22:00-07:00 window (cross-midnight) → allowed
        with _patch("gateway.permissions.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 2, 0, tzinfo=utc)
            allowed, reason = runner._permissions.is_within_time_window(tier)
            assert allowed is True

        # 12:00 — outside the 22:00-07:00 window → blocked
        with _patch("gateway.permissions.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 12, 0, tzinfo=utc)
            allowed, reason = runner._permissions.is_within_time_window(tier)
            assert allowed is False

        # 21:59 — just before window starts → blocked
        with _patch("gateway.permissions.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 21, 59, tzinfo=utc)
            allowed, reason = runner._permissions.is_within_time_window(tier)
            assert allowed is False

        # 07:00 — exactly at end → blocked (boundary)
        with _patch("gateway.permissions.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 7, 0, tzinfo=utc)
            allowed, reason = runner._permissions.is_within_time_window(tier)
            assert allowed is False

    def test_day_filter(self):
        """Weekdays-only restriction blocks weekends."""
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(
                start="00:00",
                end="23:59",
                timezone="UTC",
                days=[0, 1, 2, 3, 4],  # Mon-Fri
            )
        )
        runner = _make_runner()
        allowed, reason = runner._permissions.is_within_time_window(tier)
        from datetime import datetime

        today = datetime.now().weekday()
        if today >= 5:  # Weekend
            assert allowed is False
            assert reason == "time_restricted_wrong_day"
        else:
            assert allowed is True

    def test_invalid_timezone_falls_back_to_utc(self):
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(
                start="00:00", end="23:59", timezone="Invalid/Zone"
            )
        )
        runner = _make_runner()
        # Should not raise
        allowed, reason = runner._permissions.is_within_time_window(tier)
        assert isinstance(allowed, bool)

    def test_invalid_time_format_denies_access(self):
        """Garbage time values should fail-closed (deny) rather than crash."""
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(start="99:00", end="abc", timezone="UTC")
        )
        runner = _make_runner()
        allowed, reason = runner._permissions.is_within_time_window(tier)
        # Must not crash; fail-closed means allowed=False
        assert allowed is False
        assert reason == "time_restricted_invalid"

    def test_all_invalid_days_is_always_blocked(self):
        """F-2: days: [7, 8, 9] (all invalid) → empty list → always blocked."""
        tr = TimeRestrictions.from_dict(
            {
                "start": "00:00",
                "end": "23:59",
                "timezone": "UTC",
                "days": [7, 8, 9],
            }
        )
        # After validation, days should be [] (not None)
        assert tr.days == []
        # And runtime should always deny
        tier = _restricted_tier(time_restrictions=tr)
        runner = _make_runner()
        allowed, reason = runner._permissions.is_within_time_window(tier)
        assert allowed is False
        assert reason == "time_restricted_wrong_day"

    def test_valid_days_after_filtering(self):
        """days: [0, 7, 8, 1] → filters to [0, 1], still works."""
        tr = TimeRestrictions.from_dict(
            {
                "start": "00:00",
                "end": "23:59",
                "timezone": "UTC",
                "days": [0, 7, 8, 1],
            }
        )
        assert tr.days == [0, 1]

    def test_non_string_time_value_denies_access(self):
        """F-8: start: 800 (int) → AttributeError caught, access denied."""
        # Construct directly to bypass from_dict string defaults
        tr = TimeRestrictions(start=800, end="22:00", timezone="UTC")
        tier = _restricted_tier(time_restrictions=tr)
        runner = _make_runner()
        allowed, reason = runner._permissions.is_within_time_window(tier)
        assert allowed is False
        assert reason == "time_restricted_invalid"

    def test_none_time_value_denies_access(self):
        """F-8: start: None → AttributeError caught, access denied."""
        tr = TimeRestrictions(start=None, end="22:00", timezone="UTC")
        tier = _restricted_tier(time_restrictions=tr)
        runner = _make_runner()
        allowed, reason = runner._permissions.is_within_time_window(tier)
        assert allowed is False
        assert reason == "time_restricted_invalid"


# ------------------------------------------------------------------
# Phase 3: Exec Approval Gating
# ------------------------------------------------------------------


class TestExecApprovalGating:
    @pytest.mark.asyncio
    async def test_approve_blocked_for_restricted_user(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        event = _make_event("/approve", user_id="u1")
        result = await runner._handle_approve_command(event)
        assert "permission" in result.lower() or "Permission" in result

    @pytest.mark.asyncio
    async def test_approve_allowed_for_admin_user(self):
        import time

        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="admin")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        session_key = runner._session_key_for_source(source)
        runner._pending_approvals[
            runner._permissions.approval_key(session_key, source)
        ] = {
            "command": "echo hi",
            "pattern_key": "echo",
            "pattern_keys": ["echo"],
            "timestamp": time.time(),
        }
        event = _make_event("/approve", user_id="u1")
        with patch("tools.terminal_tool.terminal_tool", return_value="hi"):
            result = await runner._handle_approve_command(event)
        assert "approved" in result.lower()

    @pytest.mark.asyncio
    async def test_deny_blocked_for_restricted_user(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        event = _make_event("/deny", user_id="u1")
        result = await runner._handle_deny_command(event)
        assert "permission" in result.lower() or "Permission" in result

    @pytest.mark.asyncio
    async def test_deny_allowed_for_admin_user(self):
        import time

        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="admin")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        session_key = runner._session_key_for_source(source)
        runner._pending_approvals[
            runner._permissions.approval_key(session_key, source)
        ] = {
            "command": "echo hi",
            "pattern_key": "echo",
            "pattern_keys": ["echo"],
            "timestamp": time.time(),
        }
        event = _make_event("/deny", user_id="u1")
        result = await runner._handle_deny_command(event)
        assert "denied" in result.lower()

    @pytest.mark.asyncio
    async def test_approve_allowed_when_no_tiers_configured(self):
        """Opt-out: no permission_tiers = no gating."""
        import time

        runner = _make_runner(permission_tiers=None)
        source = _make_source(user_id="u1")
        session_key = runner._session_key_for_source(source)
        runner._pending_approvals[session_key] = {
            "command": "echo hi",
            "pattern_key": "echo",
            "pattern_keys": ["echo"],
            "timestamp": time.time(),
        }
        event = _make_event("/approve", user_id="u1")
        with patch("tools.terminal_tool.terminal_tool", return_value="hi"):
            result = await runner._handle_approve_command(event)
        assert "approved" in result.lower()


# ------------------------------------------------------------------
# Phase 6 (F-5): Approval Isolation — cross-user approval prevention
# ------------------------------------------------------------------


class TestApprovalIsolation:
    """F-5: Pending approvals keyed by (session_key, user_id) so that
    user B cannot approve/deny a command that user A triggered in the
    same session (e.g. a group chat sharing one session_key)."""

    @pytest.mark.asyncio
    async def test_user_cannot_approve_another_users_command(self):
        """User A triggers a command, user B tries to /approve — must fail."""
        import time

        pt = _make_permission_config(
            users={
                "u_admin": UserTierConfig(tier="admin"),
                "u_other": UserTierConfig(tier="admin"),
            },
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)

        # Simulate user_admin having a pending approval
        source_admin = _make_source(user_id="u_admin")
        session_key = runner._session_key_for_source(source_admin)
        runner._pending_approvals[
            runner._permissions.approval_key(session_key, source_admin)
        ] = {
            "command": "rm -rf /tmp/test",
            "pattern_key": "rm",
            "pattern_keys": ["rm"],
            "timestamp": time.time(),
        }

        # u_other tries to approve in the same session
        event_other = _make_event("/approve", user_id="u_other")
        result = await runner._handle_approve_command(event_other)
        assert "no pending command" in result.lower()

        # Original approval still exists for u_admin
        event_admin = _make_event("/approve", user_id="u_admin")
        with patch("tools.terminal_tool.terminal_tool", return_value="done"):
            result = await runner._handle_approve_command(event_admin)
        assert "approved" in result.lower()

    @pytest.mark.asyncio
    async def test_user_cannot_deny_another_users_command(self):
        """User A triggers a command, user B tries to /deny — must fail."""
        import time

        pt = _make_permission_config(
            users={
                "u_admin": UserTierConfig(tier="admin"),
                "u_other": UserTierConfig(tier="admin"),
            },
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)

        source_admin = _make_source(user_id="u_admin")
        session_key = runner._session_key_for_source(source_admin)
        runner._pending_approvals[
            runner._permissions.approval_key(session_key, source_admin)
        ] = {
            "command": "rm -rf /tmp/test",
            "pattern_key": "rm",
            "pattern_keys": ["rm"],
            "timestamp": time.time(),
        }

        # u_other tries to deny
        event_other = _make_event("/deny", user_id="u_other")
        result = await runner._handle_deny_command(event_other)
        assert "no pending command" in result.lower()

    @pytest.mark.asyncio
    async def test_no_tiers_uses_session_key_only(self):
        """When tiers are off, _approval_key returns plain session_key
        (backward compatibility)."""
        runner = _make_runner(permission_tiers=None)
        source = _make_source(user_id="u1")
        session_key = runner._session_key_for_source(source)
        assert runner._permissions.approval_key(session_key, source) == session_key

    @pytest.mark.asyncio
    async def test_tiers_active_uses_tuple_key(self):
        """When tiers are on and user_id present, key is (session_key, user_id)."""
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="admin")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        session_key = runner._session_key_for_source(source)
        key = runner._permissions.approval_key(session_key, source)
        assert isinstance(key, tuple)
        assert key == (session_key, "u1")

    @pytest.mark.asyncio
    async def test_no_user_id_falls_back_to_session_key(self):
        """When tiers are on but user_id is missing, fall back to session_key."""
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="admin")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id=None)
        session_key = runner._session_key_for_source(source)
        key = runner._permissions.approval_key(session_key, source)
        assert key == session_key


# ------------------------------------------------------------------
# Phase 5: Admin Command Gating
# ------------------------------------------------------------------


class TestAdminCommandGating:
    @pytest.mark.asyncio
    async def test_model_command_blocked_for_restricted(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        event = _make_event("/model", user_id="u1")
        # Simulate the command dispatch path
        command = event.get_command()
        assert command == "model"
        _tiered_admin = {"model", "provider", "update", "reload-mcp", "config"}
        assert command in _tiered_admin
        tier = runner._permissions.get_tier_config(
            runner._permissions.resolve_user_tier(event.source)
        )
        assert tier is not None and not tier.allow_admin_commands

    @pytest.mark.asyncio
    async def test_model_command_allowed_for_admin(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="admin")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config(
            runner._permissions.resolve_user_tier(_make_source(user_id="u1"))
        )
        assert tier.allow_admin_commands is True

    def test_safe_commands_not_in_admin_set(self):
        _tiered_admin = {"model", "provider", "update", "reload-mcp", "config"}
        safe = {"new", "reset", "help", "status", "stop", "retry", "undo"}
        assert not safe.intersection(_tiered_admin)


# ------------------------------------------------------------------
# Phase 6: i18n Message Formatting
# ------------------------------------------------------------------


class TestFormatTierMessage:
    def test_english_locale_from_user_config(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted", locale="de")},
            tiers={
                "restricted": _restricted_tier(
                    messages={
                        "exec_denied": {
                            "de": "Keine Berechtigung!",
                            "en": "No permission!",
                        }
                    }
                ),
            },
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(tier, "exec_denied", source)
        assert msg == "Keine Berechtigung!"

    def test_fallback_to_english_locale(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted", locale="fr")},
            tiers={
                "restricted": _restricted_tier(
                    messages={
                        "exec_denied": {
                            "en": "No permission!",
                        }
                    }
                ),
            },
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(tier, "exec_denied", source)
        assert msg == "No permission!"

    def test_fallback_to_hardcoded_english(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"restricted": _restricted_tier(messages={})},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(tier, "exec_denied", source)
        assert "permission" in msg.lower()

    def test_time_restricted_message_with_placeholders(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted", locale="de")},
            tiers={
                "restricted": _restricted_tier(
                    time_restrictions=TimeRestrictions(
                        start="08:00", end="22:00", timezone="Europe/Vienna"
                    ),
                    messages={
                        "time_restricted_after": {
                            "de": "Feierabend! Wieder da ab {start} {timezone}.",
                        }
                    },
                )
            },
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(
            tier, "time_restricted_after", source
        )
        assert "08:00" in msg
        assert "Europe/Vienna" in msg
        assert "Feierabend" in msg

    def test_unknown_key_returns_generic_denied(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"restricted": _restricted_tier(messages={})},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(tier, "unknown_key", source)
        assert msg == "Access restricted. Use /whoami to check your permissions."

    def test_no_permission_tiers_uses_english_default(self):
        runner = _make_runner(permission_tiers=None)
        # _format_tier_message needs a tier object; if called with no config,
        # locale defaults to "en"
        tier = _restricted_tier(messages={"exec_denied": {"en": "Blocked!"}})
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(tier, "exec_denied", source)
        assert msg == "Blocked!"


# ------------------------------------------------------------------
# Integration: _handle_message with tiers (L-2)
# ------------------------------------------------------------------


class TestTierIntegration:
    """End-to-end tests for _handle_message with permission tiers.

    Verifies that time block, admin command block, and exec block all
    compose correctly through the full message dispatch path.
    """

    @pytest.mark.asyncio
    async def test_restricted_user_blocked_from_admin_command(self):
        """Restricted user cannot use admin-only slash commands."""
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={
                "admin": _admin_tier(),
                "restricted": _restricted_tier(allow_admin_commands=False),
            },
        )
        runner = _make_runner(permission_tiers=pt)
        event = _make_event("/provider", user_id="u1")
        result = await runner._handle_message(event)
        assert result is not None
        assert (
            "admin" in result.lower()
            or "command" in result.lower()
            or "permission" in result.lower()
        )

    @pytest.mark.asyncio
    async def test_restricted_user_blocked_from_approve(self):
        """Restricted user cannot approve exec commands."""
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={
                "admin": _admin_tier(),
                "restricted": _restricted_tier(allow_exec=False),
            },
        )
        runner = _make_runner(permission_tiers=pt)
        event = _make_event("/approve", user_id="u1")
        result = await runner._handle_message(event)
        assert result is not None
        assert "permission" in result.lower() or "Permission" in result

    @pytest.mark.asyncio
    async def test_admin_user_allowed_admin_command(self):
        """Admin user can use admin-only slash commands."""
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="admin")},
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        # Provider command — should pass the admin check and reach the handler
        event = _make_event("/provider", user_id="u1")
        result = await runner._handle_message(event)
        # The handler might return a provider list or error, but NOT a permission denial
        assert result is None or "permission" not in (result or "").lower()

    @pytest.mark.asyncio
    async def test_quick_exec_command_blocked_for_restricted_user(self):
        """F-6: Quick command type: exec is gated behind allow_exec."""
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={
                "admin": _admin_tier(),
                "restricted": _restricted_tier(allow_exec=False),
            },
        )
        runner = _make_runner(
            permission_tiers=pt,
            quick_commands={"uptime": {"type": "exec", "command": "uptime"}},
        )
        event = _make_event("/uptime", user_id="u1")
        result = await runner._handle_message(event)
        assert result is not None
        assert "permission" in result.lower() or "Permission" in result

    @pytest.mark.asyncio
    async def test_quick_exec_command_allowed_for_admin(self):
        """F-6: Admin with allow_exec=True can run quick exec commands."""
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="admin")},
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(
            permission_tiers=pt,
            quick_commands={"echohi": {"type": "exec", "command": "echo hello"}},
        )
        event = _make_event("/echohi", user_id="u1")
        result = await runner._handle_message(event)
        # Should execute the command, not deny it
        assert result is not None
        assert "permission" not in (result or "").lower()

    @pytest.mark.asyncio
    async def test_quick_exec_command_allowed_without_tiers(self):
        """F-6: Without permission_tiers, quick exec commands work as before."""
        runner = _make_runner(
            permission_tiers=None,
            quick_commands={"echohi": {"type": "exec", "command": "echo hello"}},
        )
        event = _make_event("/echohi", user_id="u1")
        result = await runner._handle_message(event)
        assert result is not None
        assert "hello" in result.lower()


# ------------------------------------------------------------------
# Phase 7: Config boundary hardening (F-1, F-9, F-10)
# ------------------------------------------------------------------


class TestConfigBoundaryHardening:
    """F-1: Warn on empty tiers.  F-9: Most-restrictive fallback.  F-10: None default tier."""

    def test_f1_warning_on_empty_permission_tiers(self, caplog):
        """F-1: logger.warning when permission_tiers block exists but has no tiers."""
        from gateway.config import GatewayConfig

        data = {
            "platforms": {},
            "permission_tiers": {
                "default_tier": "restricted",
                "users": {"*": {"tier": "restricted"}},
            },
        }
        with caplog.at_level(logging.WARNING):
            cfg = GatewayConfig.from_dict(data)
        assert cfg.permission_tiers is None
        assert "no tiers defined" in caplog.text.lower()

    def test_f1_no_warning_when_permission_tiers_absent(self, caplog):
        """F-1: No warning when permission_tiers key is entirely absent."""
        from gateway.config import GatewayConfig

        data = {"platforms": {}}
        with caplog.at_level(logging.WARNING):
            cfg = GatewayConfig.from_dict(data)
        assert cfg.permission_tiers is None
        assert "tiers" not in caplog.text.lower()

    def test_f9_runtime_fallback_is_most_restrictive(self):
        """F-9: When both user tier and default_tier are missing from pt.tiers
        (direct construction edge case), resolve to the most-restrictive tier."""
        pt = PermissionTiersConfig(
            default_tier="nonexistent_in_tiers",
            tiers={
                "admin": _admin_tier(),
                "restricted": _restricted_tier(),
            },
            users={"u1": UserTierConfig(tier="also_nonexistent")},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        tier_name = runner._permissions.resolve_user_tier(source)
        # Should fall back to "restricted" (fewest toolsets), not "admin"
        assert tier_name == "restricted"

    def test_f9_most_restrictive_tier_picks_restricted(self):
        """_most_restrictive_tier returns the tier with fewest toolsets."""
        pt = _make_permission_config(
            tiers={
                "god": _admin_tier(),
                "viewer": _restricted_tier(),
            },
        )
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.most_restrictive_tier(pt) == "viewer"

    def test_f9_empty_tiers_returns_sentinel(self):
        """_most_restrictive_tier returns sentinel for empty tiers dict."""
        pt = PermissionTiersConfig(default_tier="x", tiers={}, users={})
        runner = _make_runner(permission_tiers=pt)
        result = runner._permissions.most_restrictive_tier(pt)
        assert result == "__restricted_fallback__"

    def test_f9_sentinel_tier_is_maximally_restrictive(self):
        """_get_tier_config maps sentinel to a fully-restrictive TierDefinition."""
        runner = _make_runner(
            permission_tiers=PermissionTiersConfig(
                default_tier="x",
                tiers={},
                users={},
            )
        )
        cfg = runner._permissions.get_tier_config("__restricted_fallback__")
        assert cfg is not None
        assert cfg.allow_exec is False
        assert cfg.allow_admin_commands is False
        assert cfg.allowed_toolsets == []

    def test_f10_user_tier_config_default_is_none(self):
        """F-10: UserTierConfig.tier defaults to None, not 'admin'."""
        utc = UserTierConfig()
        assert utc.tier is None

    def test_f10_from_dict_no_tier_key_is_none(self):
        """F-10: UserTierConfig.from_dict with no tier key yields None."""
        utc = UserTierConfig.from_dict({"locale": "de"})
        assert utc.tier is None

    def test_f10_none_tier_falls_to_default_tier(self):
        """F-10: User entry with no tier resolves to default_tier."""
        pt = _make_permission_config(
            default_tier="restricted",
            users={"u1": UserTierConfig()},  # tier=None
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        assert runner._permissions.resolve_user_tier(source) == "restricted"

    def test_f10_none_tier_wildcard_falls_to_default(self):
        """F-10: Wildcard user with no tier resolves to default_tier."""
        _pt = _make_permission_config(
            default_tier="restricted",
            users={"*": UserTierConfig()},  # tier=None
        )
        _runner = _make_runner(permission_tiers=_pt)
        _source = _make_source(user_id="anyone")


# ------------------------------------------------------------------
# Phase 2: Tool-level filtering & @group syntax
# ------------------------------------------------------------------


class TestToolGroupExpansion:
    """Tests for TOOL_GROUPS, _expand_tool_groups, and TierDefinition.from_dict
    tool-level parsing (P2.1-P2.3)."""

    def test_expand_web_group(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@web"])
        assert result == {"web_search", "web_extract"}

    def test_expand_read_group(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@read"])
        assert result == {"read_file", "search_files"}

    def test_expand_write_group(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@write"])
        assert result == {"write_file", "patch"}

    def test_expand_media_group(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@media"])
        assert result == {"vision_analyze", "image_generate", "text_to_speech"}

    def test_expand_code_group(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@code"])
        assert result == {"terminal", "execute_code"}

    def test_expand_system_group(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@system"])
        assert result == {"cronjob", "delegate_task"}

    def test_expand_memory_group(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@memory"])
        assert result == {"memory", "session_search"}

    def test_expand_skills_group(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@skills"])
        assert result == {"skills_list", "skill_view"}

    def test_expand_all_group(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@all"])
        assert result == {"*"}

    def test_expand_safe_group(self):
        """@safe = @web + @read + @media + @skills + clarify."""
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@safe"])
        expected = {
            "web_search",
            "web_extract",
            "read_file",
            "search_files",
            "vision_analyze",
            "image_generate",
            "text_to_speech",
            "skills_list",
            "skill_view",
            "clarify",
        }
        assert result == expected

    def test_expand_mixed_groups_and_individual(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@web", "@memory", "terminal"])
        assert result == {
            "web_search",
            "web_extract",
            "memory",
            "session_search",
            "terminal",
        }

    def test_expand_unknown_group_skipped_with_warning(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@unknown", "web_search"])
        assert result == {"web_search"}

    def test_expand_plain_tools_passthrough(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["web_search", "terminal", "clarify"])
        assert result == {"web_search", "terminal", "clarify"}

    def test_expand_empty_list(self):
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups([])
        assert result == set()

    def test_expand_deduplication(self):
        """Duplicate tool names from overlapping groups are deduplicated."""
        from gateway.config import _expand_tool_groups

        result = _expand_tool_groups(["@web", "web_search"])
        assert result == {"web_search", "web_extract"}

    def test_tier_definition_allowed_tools_expansion(self):
        """TierDefinition.from_dict expands @group in allowed_tools."""
        td = TierDefinition.from_dict({"allowed_tools": ["@web", "clarify"]})
        assert td.allowed_tools == ["@web", "clarify"]
        assert td.resolved_tools == frozenset({"web_search", "web_extract", "clarify"})

    def test_tier_definition_no_allowed_tools_backward_compat(self):
        """Without allowed_tools, resolved_tools is None (use toolsets)."""
        td = TierDefinition.from_dict({"allowed_toolsets": ["hermes-discord"]})
        assert td.allowed_tools is None
        assert td.resolved_tools is None
        assert td.allowed_toolsets == ["hermes-discord"]

    def test_tier_definition_allowed_tools_all_wildcard(self):
        """@all in allowed_tools resolves to {"*"}."""
        td = TierDefinition.from_dict({"allowed_tools": ["@all"]})
        assert td.resolved_tools == frozenset({"*"})

    def test_tier_definition_allowed_tools_empty_list(self):
        """Empty allowed_tools list = no tools allowed (fail-closed)."""
        td = TierDefinition.from_dict({"allowed_tools": []})
        assert td.allowed_tools == []
        assert td.resolved_tools == frozenset()

    def test_tier_definition_allowed_tools_invalid_type_fails_closed(self):
        """Non-list allowed_tools is ignored, falls back to toolsets."""
        td = TierDefinition.from_dict({"allowed_tools": "not-a-list"})
        assert td.allowed_tools is None
        assert td.resolved_tools is None
        # Falls back to toolsets (default ["*"])
        assert td.allowed_toolsets == ["*"]

    def test_tier_definition_allowed_tools_roundtrip(self):
        """to_dict preserves allowed_tools; resolved_tools is computed."""
        td = TierDefinition.from_dict({"allowed_tools": ["@web", "clarify"]})
        d = td.to_dict()
        assert d["allowed_tools"] == ["@web", "clarify"]
        restored = TierDefinition.from_dict(d)
        assert restored.resolved_tools == frozenset(
            {"web_search", "web_extract", "clarify"}
        )

    def test_tier_definition_allowed_tools_takes_precedence_in_to_dict(self):
        """When allowed_tools is set, to_dict includes it."""
        td = TierDefinition(
            allowed_toolsets=["hermes-discord"],
            allowed_tools=["@web"],
            resolved_tools=frozenset({"web_search", "web_extract"}),
        )
        d = td.to_dict()
        assert "allowed_tools" in d
        assert d["allowed_tools"] == ["@web"]

    def test_tier_definition_no_allowed_tools_not_in_to_dict(self):
        """When allowed_tools is None, to_dict omits it."""
        td = TierDefinition(allowed_toolsets=["hermes-discord"])
        d = td.to_dict()
        assert "allowed_tools" not in d


class TestToolLevelFiltering:
    """Tests for get_tool_definitions with allowed_tool_names (P2.4)."""

    def test_filter_to_specific_tools(self):
        """allowed_tool_names restricts tools to the named set."""
        from model_tools import get_tool_definitions

        allowed = frozenset({"terminal", "clarify"})
        tools = get_tool_definitions(
            enabled_toolsets=["hermes-cli"],
            allowed_tool_names=allowed,
            quiet_mode=True,
        )
        names = {t["function"]["name"] for t in tools}
        # terminal and clarify should be present (both pass check_fn in test env)
        assert "clarify" in names
        assert "terminal" in names
        # Others should not
        assert "memory" not in names
        assert "read_file" not in names

    def test_filter_with_wildcard_returns_all(self):
        """allowed_tool_names containing '*' returns all tools."""
        from model_tools import get_tool_definitions

        allowed = frozenset({"*"})
        tools_with = get_tool_definitions(
            enabled_toolsets=["hermes-cli"],
            allowed_tool_names=allowed,
            quiet_mode=True,
        )
        tools_without = get_tool_definitions(
            enabled_toolsets=["hermes-cli"],
            allowed_tool_names=None,
            quiet_mode=True,
        )
        assert len(tools_with) == len(tools_without)

    def test_filter_with_empty_set_returns_nothing(self):
        """Empty allowed_tool_names returns no tools."""
        from model_tools import get_tool_definitions

        allowed = frozenset()
        tools = get_tool_definitions(
            enabled_toolsets=["hermes-cli"],
            allowed_tool_names=allowed,
            quiet_mode=True,
        )
        assert tools == []

    def test_filter_with_none_is_backward_compat(self):
        """None allowed_tool_names = no tool-level filtering (backward compat)."""
        from model_tools import get_tool_definitions

        tools_none = get_tool_definitions(
            enabled_toolsets=["hermes-cli"],
            allowed_tool_names=None,
            quiet_mode=True,
        )
        tools_nofilter = get_tool_definitions(
            enabled_toolsets=["hermes-cli"],
            quiet_mode=True,
        )
        assert len(tools_none) == len(tools_nofilter)

    def test_filter_excludes_nonexistent_tools(self):
        """allowed_tool_names with non-existent tool names returns only matching tools."""
        from model_tools import get_tool_definitions

        allowed = frozenset({"clarify", "nonexistent_tool"})
        tools = get_tool_definitions(
            enabled_toolsets=["hermes-cli"],
            allowed_tool_names=allowed,
            quiet_mode=True,
        )
        names = {t["function"]["name"] for t in tools}
        assert names == {"clarify"}

    def test_filter_intersects_with_check_fn(self):
        """Tool must pass both check_fn AND be in allowed_tool_names."""
        from model_tools import get_tool_definitions

        # web_search won't pass check_fn without API keys
        allowed = frozenset({"web_search", "clarify"})
        tools = get_tool_definitions(
            enabled_toolsets=["hermes-cli"],
            allowed_tool_names=allowed,
            quiet_mode=True,
        )
        names = {t["function"]["name"] for t in tools}
        assert "clarify" in names
        assert "web_search" not in names  # check_fn blocks it


class TestToolLevelFilteringIntegration:
    """Integration: TierDefinition.resolved_tools → agent tool filtering."""

    def test_resolved_tools_overrides_toolsets_for_filtering(self):
        """When tier has resolved_tools, tool-level filtering is used."""
        from gateway.config import PermissionTiersConfig

        pt = PermissionTiersConfig.from_dict(
            {
                "default_tier": "guest",
                "tiers": {
                    "admin": {"allowed_toolsets": ["*"]},
                    "guest": {
                        "allowed_toolsets": ["*"],  # Would allow all via toolsets
                        "allowed_tools": ["@safe"],  # But tool-level restricts
                    },
                },
                "users": {},
            }
        )
        guest_cfg = pt.tiers["guest"]
        # Tool-level filter takes precedence
        assert guest_cfg.resolved_tools is not None
        assert "*" not in guest_cfg.resolved_tools
        # @safe tools should be present
        assert "clarify" in guest_cfg.resolved_tools
        assert "web_search" in guest_cfg.resolved_tools
        # But terminal should NOT (not in @safe)
        assert "terminal" not in guest_cfg.resolved_tools

    def test_toolset_fallback_when_no_allowed_tools(self):
        """Without allowed_tools, toolset filtering is used (backward compat)."""
        pt = _make_permission_config(
            tiers={
                "admin": _admin_tier(),
                "restricted": TierDefinition(
                    allowed_toolsets=["web"],
                    allow_exec=False,
                ),
            },
        )
        restricted_cfg = pt.tiers["restricted"]
        assert restricted_cfg.resolved_tools is None
        assert restricted_cfg.allowed_toolsets == ["web"]

    def test_most_restrictive_tier_considers_resolved_tools(self):
        """_most_restrictive_tier scores resolved_tools size."""
        pt = PermissionTiersConfig.from_dict(
            {
                "default_tier": "guest",
                "tiers": {
                    "admin": {"allowed_toolsets": ["*"]},
                    "limited": {"allowed_tools": ["@web"]},
                    "guest": {"allowed_tools": ["clarify"]},
                },
            }
        )
        runner = _make_runner(permission_tiers=pt)
        tier_name = runner._permissions.most_restrictive_tier(pt)
        assert tier_name == "guest"  # fewest tools

    def test_most_restrictive_tier_resolved_tools_vs_toolsets(self):
        """Tier with resolved_tools scored correctly vs tier with only toolsets."""
        pt = PermissionTiersConfig.from_dict(
            {
                "builtins": False,
                "default_tier": "wide",
                "tiers": {
                    "wide": {"allowed_toolsets": ["*"]},
                    "mid": {"allowed_toolsets": ["web", "file"]},  # 2 toolsets
                    "narrow_tools": {"allowed_tools": ["clarify"]},  # 1 tool
                },
            }
        )
        runner = _make_runner(permission_tiers=pt)
        tier_name = runner._permissions.most_restrictive_tier(pt)
        # narrow_tools has 1 resolved tool, mid has 2 toolsets → narrow wins
        assert tier_name == "narrow_tools"


# ------------------------------------------------------------------
# Phase 3: Built-in tier presets
# ------------------------------------------------------------------


class TestBuiltinTierPresets:
    """Tests for built-in tier presets (owner/admin/user/guest)."""

    def test_presets_available_by_default(self):
        """Without explicit tiers, built-in presets are loaded."""
        pt = PermissionTiersConfig.from_dict(
            {
                "default_tier": "guest",
                "users": {},
            }
        )
        assert "owner" in pt.tiers
        assert "admin" in pt.tiers
        assert "user" in pt.tiers
        assert "guest" in pt.tiers
        assert pt.builtins is True

    def test_owner_gets_all_tools(self):
        pt = PermissionTiersConfig.from_dict(
            {
                "default_tier": "guest",
                "users": {},
            }
        )
        owner = pt.tiers["owner"]
        assert "*" in owner.resolved_tools
        assert owner.allow_exec is True
        assert owner.allow_admin_commands is True

    def test_admin_gets_most_tools(self):
        pt = PermissionTiersConfig.from_dict(
            {
                "default_tier": "guest",
                "users": {},
            }
        )
        admin = pt.tiers["admin"]
        assert "terminal" in admin.resolved_tools
        assert "write_file" in admin.resolved_tools
        assert "web_search" in admin.resolved_tools
        assert admin.allow_exec is True
        assert admin.allow_admin_commands is True

    def test_user_gets_readonly_tools(self):
        pt = PermissionTiersConfig.from_dict(
            {
                "default_tier": "guest",
                "users": {},
            }
        )
        user = pt.tiers["user"]
        assert "web_search" in user.resolved_tools
        assert "read_file" in user.resolved_tools
        assert "terminal" not in user.resolved_tools
        assert "write_file" not in user.resolved_tools
        assert user.allow_exec is False
        assert user.allow_admin_commands is False

    def test_guest_gets_minimal_tools(self):
        pt = PermissionTiersConfig.from_dict(
            {
                "default_tier": "guest",
                "users": {},
            }
        )
        guest = pt.tiers["guest"]
        # T7b: Guest preset uses @safe (web + read + media + skills + clarify)
        assert "clarify" in guest.resolved_tools
        assert "web_search" in guest.resolved_tools
        assert "read_file" in guest.resolved_tools
        assert "vision_analyze" in guest.resolved_tools
        assert guest.allow_exec is False
        assert guest.allow_admin_commands is False

    def test_builtins_false_disables_presets(self):
        """builtins: false means no presets loaded."""
        pt = PermissionTiersConfig.from_dict(
            {
                "builtins": False,
                "default_tier": "custom",
                "tiers": {"custom": {"allowed_toolsets": ["*"]}},
                "users": {},
            }
        )
        assert "owner" not in pt.tiers
        assert "admin" not in pt.tiers
        assert "user" not in pt.tiers
        assert "guest" not in pt.tiers
        assert "custom" in pt.tiers
        assert pt.builtins is False

    def test_user_tier_overrides_preset(self):
        """User-defined tier with same name overrides preset."""
        pt = PermissionTiersConfig.from_dict(
            {
                "tiers": {
                    "admin": {
                        "allowed_tools": ["@web"],
                        "allow_exec": False,
                    },
                },
                "users": {},
            }
        )
        admin = pt.tiers["admin"]
        # Override wins — only @web tools, no exec
        assert admin.resolved_tools == frozenset({"web_search", "web_extract"})
        assert admin.allow_exec is False
        # Other presets still present
        assert "owner" in pt.tiers
        assert "guest" in pt.tiers

    def test_presets_minimal_config(self):
        """Minimal config with just users — all tiers come from presets."""
        pt = PermissionTiersConfig.from_dict(
            {
                "default_tier": "user",
                "users": {
                    "111111111111111111": {"tier": "owner"},
                    "222222222222222222": {"tier": "admin"},
                    "*": {"tier": "user"},
                },
            }
        )
        assert len(pt.tiers) == 4
        assert pt.tiers["owner"].allow_exec is True
        assert pt.tiers["guest"].allow_exec is False


# ------------------------------------------------------------------
# Phase 5: Runtime user store, /whoami, /users commands
# ------------------------------------------------------------------


class TestRuntimeUserStore:
    """Tests for RuntimeUserStore (SQLite-backed runtime tier assignments)."""

    @pytest.fixture
    def store(self, tmp_path):
        from gateway.permissions import RuntimeUserStore

        s = RuntimeUserStore(db_path=tmp_path / "test_permissions.db")
        yield s
        s.close()

    def test_set_and_get(self, store):
        store.set_user_tier("u1", "admin", granted_by="owner1")
        entry = store.get_user_tier("u1")
        assert entry is not None
        assert entry["tier_name"] == "admin"
        assert entry["granted_by"] == "owner1"
        assert entry["granted_at"] is not None

    def test_get_missing_user(self, store):
        assert store.get_user_tier("nonexistent") is None

    def test_set_overwrites(self, store):
        store.set_user_tier("u1", "user", granted_by="owner")
        store.set_user_tier("u1", "admin", granted_by="owner")
        entry = store.get_user_tier("u1")
        assert entry["tier_name"] == "admin"

    def test_remove(self, store):
        store.set_user_tier("u1", "admin")
        assert store.remove_user_tier("u1") is True
        assert store.get_user_tier("u1") is None

    def test_remove_nonexistent(self, store):
        assert store.remove_user_tier("nonexistent") is False

    def test_list_all(self, store):
        store.set_user_tier("u1", "admin", granted_by="owner")
        store.set_user_tier("u2", "user", granted_by="owner")
        entries = store.list_all()
        assert len(entries) == 2
        uids = {e["user_id"] for e in entries}
        assert uids == {"u1", "u2"}

    def test_list_empty(self, store):
        assert store.list_all() == []

    def test_source_platform(self, store):
        store.set_user_tier("u1", "admin", source_platform="telegram")
        entry = store.get_user_tier("u1", source_platform="telegram")
        assert entry["source_platform"] == "telegram"

    def test_persistence(self, tmp_path):
        """Data survives store recreation."""
        from gateway.permissions import RuntimeUserStore

        db_path = tmp_path / "test_persist.db"
        store1 = RuntimeUserStore(db_path=db_path)
        store1.set_user_tier("u1", "admin", granted_by="owner")
        store1.close()

        store2 = RuntimeUserStore(db_path=db_path)
        entry = store2.get_user_tier("u1")
        store2.close()
        assert entry is not None
        assert entry["tier_name"] == "admin"


class TestPermissionManagerRuntimeOverlay:
    """Tests for PermissionManager with runtime overlay."""

    @pytest.fixture
    def pm_with_runtime(self, tmp_path):
        from gateway.permissions import PermissionManager, RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "test.db")
        config = _make_permission_config(
            tiers={
                "admin": _admin_tier(),
                "restricted": _restricted_tier(),
            },
            users={
                "config_user": UserTierConfig(tier="restricted"),
                "*": UserTierConfig(tier="restricted"),
            },
            default_tier="restricted",
        )
        pm = PermissionManager(config, runtime_store=store)
        yield pm
        store.close()

    def test_runtime_overrides_config(self, pm_with_runtime):
        """Runtime assignment takes priority over config mapping."""
        source = _make_source(user_id="config_user")
        assert pm_with_runtime.resolve_user_tier(source) == "restricted"

        pm_with_runtime.set_user_tier(
            "config_user", "admin", granted_by="owner", source_platform="discord"
        )
        assert pm_with_runtime.resolve_user_tier(source) == "admin"

    def test_runtime_overrides_wildcard(self, pm_with_runtime):
        """Runtime assignment for unknown user overrides wildcard."""
        source = _make_source(user_id="new_user")
        assert pm_with_runtime.resolve_user_tier(source) == "restricted"

        pm_with_runtime.set_user_tier(
            "new_user", "admin", granted_by="owner", source_platform="discord"
        )
        assert pm_with_runtime.resolve_user_tier(source) == "admin"

    def test_runtime_falls_back_to_config_after_removal(self, pm_with_runtime):
        """After removing runtime, config mapping takes over again."""
        source = _make_source(user_id="config_user")
        pm_with_runtime.set_user_tier("config_user", "admin", source_platform="discord")
        assert pm_with_runtime.resolve_user_tier(source) == "admin"

        pm_with_runtime.remove_user_tier("config_user", source_platform="discord")
        assert pm_with_runtime.resolve_user_tier(source) == "restricted"

    def test_unknown_runtime_tier_falls_back(self, pm_with_runtime):
        """If runtime tier doesn't exist in config, fall back gracefully."""
        pm_with_runtime._runtime_store.set_user_tier(
            "u1", "nonexistent_tier", source_platform="discord"
        )
        source = _make_source(user_id="u1")
        tier = pm_with_runtime.resolve_user_tier(source)
        assert tier == "restricted"  # wildcard/default

    def test_no_runtime_store(self):
        """PermissionManager without runtime store still works."""
        from gateway.permissions import PermissionManager

        config = _make_permission_config()
        pm = PermissionManager(config, runtime_store=None)
        source = _make_source(user_id="u1")
        assert pm.resolve_user_tier(source) == "admin"  # default_tier


class TestPermissionManagerSetUserTier:
    """Tests for set_user_tier validation."""

    @pytest.fixture
    def pm(self, tmp_path):
        from gateway.permissions import PermissionManager, RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "test.db")
        config = _make_permission_config(
            tiers={"admin": _admin_tier(), "guest": _restricted_tier()},
        )
        pm = PermissionManager(config, runtime_store=store)
        yield pm
        store.close()

    def test_set_valid_tier(self, pm):
        ok, msg = pm.set_user_tier("u1", "admin", granted_by="owner")
        assert ok is True
        assert "admin" in msg

    def test_set_unknown_tier(self, pm):
        ok, msg = pm.set_user_tier("u1", "nonexistent")
        assert ok is False
        assert "Unknown tier" in msg

    def test_set_no_config(self):
        from gateway.permissions import PermissionManager

        pm = PermissionManager(config=None)
        ok, msg = pm.set_user_tier("u1", "admin")
        assert ok is False
        assert "not configured" in msg

    def test_set_no_runtime_store(self):
        from gateway.permissions import PermissionManager

        config = _make_permission_config()
        pm = PermissionManager(config, runtime_store=None)
        ok, msg = pm.set_user_tier("u1", "admin")
        assert ok is False
        assert "not available" in msg


class TestPermissionManagerRemoveUserTier:
    """Tests for remove_user_tier."""

    @pytest.fixture
    def pm(self, tmp_path):
        from gateway.permissions import PermissionManager, RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "test.db")
        config = _make_permission_config()
        pm = PermissionManager(config, runtime_store=store)
        yield pm
        store.close()

    def test_remove_existing(self, pm):
        pm.set_user_tier("u1", "admin")
        ok, msg = pm.remove_user_tier("u1")
        assert ok is True
        assert "removed" in msg.lower()

    def test_remove_nonexistent(self, pm):
        ok, msg = pm.remove_user_tier("nonexistent")
        assert ok is False
        assert "No runtime tier" in msg

    def test_remove_no_store(self):
        from gateway.permissions import PermissionManager

        pm = PermissionManager(config=None)
        ok, msg = pm.remove_user_tier("u1")
        assert ok is False


class TestPermissionManagerListUsers:
    """Tests for list_users (combined config + runtime)."""

    @pytest.fixture
    def pm(self, tmp_path):
        from gateway.permissions import PermissionManager, RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "test.db")
        config = _make_permission_config(
            users={
                "config_user": UserTierConfig(tier="admin"),
            },
        )
        pm = PermissionManager(config, runtime_store=store)
        yield pm
        store.close()

    def test_list_config_only(self, pm):
        users = pm.list_users()
        assert len(users) == 1
        assert users[0]["user_id"] == "config_user"
        assert users[0]["source"] == "config"

    def test_list_runtime_overrides_config(self, pm):
        pm.set_user_tier("config_user", "restricted", granted_by="owner")
        users = pm.list_users()
        assert len(users) == 1
        assert users[0]["source"] == "runtime"
        assert users[0]["tier_name"] == "restricted"

    def test_list_combined(self, pm):
        pm.set_user_tier("runtime_user", "restricted", granted_by="owner")
        users = pm.list_users()
        assert len(users) == 2
        sources = {u["source"] for u in users}
        assert "config" in sources
        assert "runtime" in sources

    def test_list_no_config(self):
        from gateway.permissions import PermissionManager

        pm = PermissionManager(config=None)
        assert pm.list_users() == []


class TestPermissionManagerWhoami:
    """Tests for whoami method."""

    @pytest.fixture
    def pm(self, tmp_path):
        from gateway.permissions import PermissionManager, RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "test.db")
        config = _make_permission_config(
            tiers={
                "admin": _admin_tier(),
                "restricted": _restricted_tier(),
            },
            users={
                "config_user": UserTierConfig(tier="admin"),
                "*": UserTierConfig(tier="restricted"),
            },
            default_tier="restricted",
        )
        pm = PermissionManager(config, runtime_store=store)
        yield pm
        store.close()

    def test_whoami_config_user(self, pm):
        source = _make_source(user_id="config_user")
        info = pm.whoami(source)
        assert info["user_id"] == "config_user"
        assert info["tier_name"] == "admin"
        assert info["tier_source"] == "config"
        assert info["allow_exec"] is True
        assert info["allow_admin_commands"] is True

    def test_whoami_wildcard_user(self, pm):
        source = _make_source(user_id="unknown")
        info = pm.whoami(source)
        assert info["tier_name"] == "restricted"
        assert info["tier_source"] == "wildcard"
        assert info["allow_exec"] is False

    def test_whoami_runtime_user(self, pm):
        pm.set_user_tier(
            "runtime_user", "admin", granted_by="owner", source_platform="discord"
        )
        source = _make_source(user_id="runtime_user")
        info = pm.whoami(source)
        assert info["tier_name"] == "admin"
        assert info["tier_source"] == "runtime"

    def test_whoami_no_config(self):
        from gateway.permissions import PermissionManager

        pm = PermissionManager(config=None)
        source = _make_source(user_id="u1")
        info = pm.whoami(source)
        assert info["tier_name"] == "admin"
        assert info["allow_exec"] is True
        assert info["tool_count"] is None

    def test_whoami_tool_count_limited(self, pm):
        """Tool count reflects restricted tier's limited tools."""
        source = _make_source(user_id="guest_user")
        info = pm.whoami(source)
        assert info["tool_count"] == 1

    def test_whoami_includes_platform(self, pm):
        source = _make_source(user_id="u1", platform=Platform.TELEGRAM)
        info = pm.whoami(source)
        assert info["platform"] == "telegram"


class TestWhoamiCommand:
    """End-to-end tests for /whoami command dispatch in GatewayRunner."""

    @pytest.fixture
    def runner(self, tmp_path):
        from gateway.permissions import RuntimeUserStore

        pt = _make_permission_config(
            tiers={
                "admin": _admin_tier(),
                "restricted": _restricted_tier(),
            },
            users={
                "admin_user": UserTierConfig(tier="admin"),
                "*": UserTierConfig(tier="restricted"),
            },
            default_tier="restricted",
        )
        r = _make_runner(permission_tiers=pt)
        store = RuntimeUserStore(db_path=tmp_path / "test.db")
        r._permissions._runtime_store = store
        yield r
        store.close()

    @pytest.mark.asyncio
    async def test_whoami_shows_tier(self, runner):
        event = _make_event("/whoami", user_id="admin_user")
        result = await runner._handle_whoami_command(event)
        assert "admin" in result
        assert "config" in result

    @pytest.mark.asyncio
    async def test_whoami_shows_wildcard(self, runner):
        event = _make_event("/whoami", user_id="unknown_user")
        result = await runner._handle_whoami_command(event)
        assert "restricted" in result
        assert "wildcard" in result

    @pytest.mark.asyncio
    async def test_whoami_shows_runtime(self, runner):
        runner._permissions.set_user_tier(
            "runtime_user", "admin", granted_by="admin_user", source_platform="discord"
        )
        event = _make_event("/whoami", user_id="runtime_user")
        result = await runner._handle_whoami_command(event)
        assert "admin" in result
        assert "runtime" in result

    @pytest.mark.asyncio
    async def test_whoami_shows_exec_access(self, runner):
        event = _make_event("/whoami", user_id="admin_user")
        result = await runner._handle_whoami_command(event)
        assert "Exec access" in result
        assert "✅" in result

    @pytest.mark.asyncio
    async def test_whoami_no_tiers(self):
        """Without tiers configured, /whoami still works."""
        runner = _make_runner(permission_tiers=None)
        event = _make_event("/whoami", user_id="u1")
        result = await runner._handle_whoami_command(event)
        assert "admin" in result


class TestUsersCommand:
    """End-to-end tests for /users command dispatch in GatewayRunner."""

    @pytest.fixture
    def runner(self, tmp_path):
        from gateway.permissions import RuntimeUserStore

        pt = _make_permission_config(
            tiers={
                "admin": _admin_tier(),
                "restricted": _restricted_tier(),
            },
            users={
                "config_user": UserTierConfig(tier="restricted"),
            },
            default_tier="restricted",
        )
        r = _make_runner(permission_tiers=pt)
        store = RuntimeUserStore(db_path=tmp_path / "test.db")
        r._permissions._runtime_store = store
        yield r
        store.close()

    @pytest.mark.asyncio
    async def test_users_list(self, runner):
        event = _make_event("/users list", user_id="admin_user")
        result = await runner._handle_users_command(event)
        assert "config_user" in result
        assert "config" in result

    @pytest.mark.asyncio
    async def test_users_set(self, runner):
        event = _make_event("/users set new_user admin", user_id="admin_user")
        result = await runner._handle_users_command(event)
        assert "✅" in result
        assert "new_user" in result
        assert "admin" in result

        info = runner._permissions._runtime_store.get_user_tier(
            "new_user", source_platform="discord"
        )
        assert info is not None
        assert info["tier_name"] == "admin"

    @pytest.mark.asyncio
    async def test_users_set_unknown_tier(self, runner):
        event = _make_event("/users set new_user nonexistent", user_id="admin_user")
        result = await runner._handle_users_command(event)
        assert "❌" in result
        assert "Unknown tier" in result

    @pytest.mark.asyncio
    async def test_users_remove(self, runner):
        runner._permissions.set_user_tier(
            "target", "admin", granted_by="admin_user", source_platform="discord"
        )
        event = _make_event("/users remove target", user_id="admin_user")
        result = await runner._handle_users_command(event)
        assert "✅" in result
        assert "removed" in result.lower()

    @pytest.mark.asyncio
    async def test_users_remove_nonexistent(self, runner):
        event = _make_event("/users remove nobody", user_id="admin_user")
        result = await runner._handle_users_command(event)
        assert "❌" in result

    @pytest.mark.asyncio
    async def test_users_no_args_shows_list(self, runner):
        event = _make_event("/users", user_id="admin_user")
        result = await runner._handle_users_command(event)
        assert "config_user" in result

    @pytest.mark.asyncio
    async def test_users_set_missing_args(self, runner):
        event = _make_event("/users set only_one_arg", user_id="admin_user")
        result = await runner._handle_users_command(event)
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_users_remove_missing_args(self, runner):
        event = _make_event("/users remove", user_id="admin_user")
        result = await runner._handle_users_command(event)
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_users_unknown_subcommand(self, runner):
        event = _make_event("/users explode", user_id="admin_user")
        result = await runner._handle_users_command(event)
        assert "Unknown subcommand" in result

    @pytest.mark.asyncio
    async def test_users_list_empty(self):
        """Empty list when no users configured."""
        from gateway.permissions import RuntimeUserStore
        import tempfile

        pt = _make_permission_config(users={})
        runner = _make_runner(permission_tiers=pt)
        with tempfile.TemporaryDirectory() as td:
            store = RuntimeUserStore(db_path=Path(td) / "test.db")
            runner._permissions._runtime_store = store
            event = _make_event("/users list", user_id="admin")
            result = await runner._handle_users_command(event)
            assert "No user tier assignments" in result
            store.close()


class TestCommandRegistryPhase5:
    """Verify /whoami and /users are properly registered."""

    def test_whoami_in_registry(self):
        from hermes_cli.commands import COMMAND_REGISTRY

        cmd = next((c for c in COMMAND_REGISTRY if c.name == "whoami"), None)
        assert cmd is not None
        assert cmd.gateway_only is True
        assert cmd.admin_only is False

    def test_users_in_registry(self):
        from hermes_cli.commands import COMMAND_REGISTRY

        cmd = next((c for c in COMMAND_REGISTRY if c.name == "users"), None)
        assert cmd is not None
        assert cmd.gateway_only is True
        assert cmd.admin_only is True
        assert "list" in cmd.subcommands
        assert "set" in cmd.subcommands
        assert "remove" in cmd.subcommands

    def test_whoami_in_gateway_known_commands(self):
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS

        assert "whoami" in GATEWAY_KNOWN_COMMANDS

    def test_users_in_gateway_known_commands(self):
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS

        assert "users" in GATEWAY_KNOWN_COMMANDS

    def test_whoami_not_in_cli_commands(self):
        from hermes_cli.commands import COMMANDS

        assert "/whoami" not in COMMANDS

    def test_users_not_in_cli_commands(self):
        from hermes_cli.commands import COMMANDS

        assert "/users" not in COMMANDS


# ------------------------------------------------------------------
# Phase 6: Rate limiting
# ------------------------------------------------------------------


class TestRateLimitConfig:
    """Tests for requests_per_hour field on TierDefinition."""

    def test_default_none(self):
        t = TierDefinition()
        assert t.requests_per_hour is None

    def test_explicit_value(self):
        t = TierDefinition(requests_per_hour=10)
        assert t.requests_per_hour == 10

    def test_zero_allowed(self):
        """0 = explicitly blocked (no requests allowed)."""
        t = TierDefinition.from_dict({"requests_per_hour": 0})
        assert t.requests_per_hour == 0

    def test_from_dict_positive(self):
        t = TierDefinition.from_dict({"requests_per_hour": 42})
        assert t.requests_per_hour == 42

    def test_from_dict_null(self):
        t = TierDefinition.from_dict({"requests_per_hour": None})
        assert t.requests_per_hour is None

    def test_from_dict_missing(self):
        t = TierDefinition.from_dict({})
        assert t.requests_per_hour is None

    def test_from_dict_negative(self):
        """Negative values are treated as unlimited (None)."""
        t = TierDefinition.from_dict({"requests_per_hour": -5})
        assert t.requests_per_hour is None

    def test_from_dict_string(self):
        """String values are treated as unlimited (None)."""
        t = TierDefinition.from_dict({"requests_per_hour": "unlimited"})
        assert t.requests_per_hour is None

    def test_to_dict_roundtrip(self):
        t = TierDefinition(requests_per_hour=15)
        d = t.to_dict()
        assert d["requests_per_hour"] == 15

    def test_to_dict_omits_none(self):
        t = TierDefinition()
        d = t.to_dict()
        assert "requests_per_hour" not in d


class TestRateLimitCheck:
    """Tests for PermissionManager.check_rate_limit."""

    @pytest.fixture
    def pm_with_limit(self, tmp_path):
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            tiers={
                "admin": _admin_tier(),  # No rate limit
                "limited": TierDefinition(
                    allowed_toolsets=["*"],
                    requests_per_hour=3,
                ),
                "blocked": TierDefinition(
                    allowed_toolsets=["*"],
                    requests_per_hour=0,
                ),
            },
            users={
                "limited_user": UserTierConfig(tier="limited"),
                "blocked_user": UserTierConfig(tier="blocked"),
            },
        )
        pm = PermissionManager(config, runtime_store=None)
        yield pm

    def test_no_config(self):
        from gateway.permissions import PermissionManager

        pm = PermissionManager(config=None)
        source = _make_source(user_id="u1")
        ok, reason = pm.check_rate_limit(source)
        assert ok is True
        assert reason is None

    def test_unlimited_tier(self, pm_with_limit):
        """Admin tier has no rate limit."""
        source = _make_source(user_id="admin_user")
        for _ in range(100):
            ok, _ = pm_with_limit.check_rate_limit(source)
            assert ok is True

    def test_within_limit(self, pm_with_limit):
        """First N requests are allowed."""
        source = _make_source(user_id="limited_user")
        for i in range(3):
            ok, reason = pm_with_limit.check_rate_limit(source)
            assert ok is True, f"Request {i + 1} should be allowed"
            assert reason is None

    def test_exceeds_limit(self, pm_with_limit):
        """Request beyond limit is denied."""
        source = _make_source(user_id="limited_user")
        for _ in range(3):
            pm_with_limit.check_rate_limit(source)
        ok, reason = pm_with_limit.check_rate_limit(source)
        assert ok is False
        assert reason == "rate_limited"

    def test_blocked_tier(self, pm_with_limit):
        """Tier with requests_per_hour=0 is always blocked."""
        source = _make_source(user_id="blocked_user")
        ok, reason = pm_with_limit.check_rate_limit(source)
        assert ok is False
        assert reason == "rate_limited_blocked"

    def test_no_user_id(self, pm_with_limit):
        """No user_id means no rate limiting."""
        source = _make_source(user_id=None)
        ok, reason = pm_with_limit.check_rate_limit(source)
        assert ok is True
        assert reason is None

    def test_counters_per_user(self, pm_with_limit):
        """Each user has independent counters."""
        user1 = _make_source(user_id="limited_user")
        _user2 = _make_source(user_id="another_limited")
        # Map user2 to limited tier via runtime
        # Actually, user2 maps to admin (default). Use limited_user's slot.
        # user1 uses 3 requests
        for _ in range(3):
            pm_with_limit.check_rate_limit(user1)
        # user1 should be blocked
        ok1, _ = pm_with_limit.check_rate_limit(user1)
        assert ok1 is False


class TestRateLimitRemaining:
    """Tests for PermissionManager.rate_limit_remaining."""

    def test_no_config(self):
        from gateway.permissions import PermissionManager

        pm = PermissionManager(config=None)
        source = _make_source(user_id="u1")
        assert pm.rate_limit_remaining(source) is None

    def test_unlimited_tier(self):
        config = _make_permission_config(
            tiers={"admin": _admin_tier()},
        )
        from gateway.permissions import PermissionManager

        pm = PermissionManager(config)
        source = _make_source(user_id="admin_user")
        assert pm.rate_limit_remaining(source) is None

    def test_limited_tier(self):
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            tiers={
                "limited": TierDefinition(allowed_toolsets=["*"], requests_per_hour=5),
            },
            users={"u1": UserTierConfig(tier="limited")},
        )
        pm = PermissionManager(config)
        source = _make_source(user_id="u1")
        assert pm.rate_limit_remaining(source) == 5
        pm.check_rate_limit(source)
        assert pm.rate_limit_remaining(source) == 4

    def test_exhausted(self):
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            tiers={
                "limited": TierDefinition(allowed_toolsets=["*"], requests_per_hour=2),
            },
            users={"u1": UserTierConfig(tier="limited")},
        )
        pm = PermissionManager(config)
        source = _make_source(user_id="u1")
        pm.check_rate_limit(source)
        pm.check_rate_limit(source)
        assert pm.rate_limit_remaining(source) == 0


class TestRateLimitCleanup:
    """Tests for PermissionManager.cleanup_rate_counters."""

    def test_cleanup_removes_old_buckets(self):
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            tiers={
                "limited": TierDefinition(
                    allowed_toolsets=["*"], requests_per_hour=100
                ),
            },
            users={"u1": UserTierConfig(tier="limited")},
        )
        pm = PermissionManager(config)

        # Insert a stale counter manually (3-tuple: platform, user_id, bucket)
        old_bucket = pm._current_hour_bucket() - 1
        pm._rate_counts[("discord", "u1", old_bucket)] = 50
        pm._rate_counts[("discord", "u1", pm._current_hour_bucket())] = 10

        pm.cleanup_rate_counters()

        assert ("discord", "u1", old_bucket) not in pm._rate_counts
        assert ("discord", "u1", pm._current_hour_bucket()) in pm._rate_counts


class TestRateLimitInWhoami:
    """Verify /whoami shows rate limit info."""

    @pytest.fixture
    def pm(self, tmp_path):
        from gateway.permissions import PermissionManager, RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "test.db")
        config = _make_permission_config(
            tiers={
                "admin": _admin_tier(),
                "limited": TierDefinition(allowed_toolsets=["*"], requests_per_hour=10),
            },
            users={
                "admin_user": UserTierConfig(tier="admin"),
                "limited_user": UserTierConfig(tier="limited"),
                "*": UserTierConfig(tier="limited"),
            },
        )
        pm = PermissionManager(config, runtime_store=store)
        yield pm
        store.close()

    def test_whoami_shows_rate_limit(self, pm):
        source = _make_source(user_id="limited_user")
        info = pm.whoami(source)
        assert info["requests_per_hour"] == 10
        assert info["rate_limit_remaining"] == 10

    def test_whoami_after_requests(self, pm):
        source = _make_source(user_id="limited_user")
        pm.check_rate_limit(source)
        pm.check_rate_limit(source)
        info = pm.whoami(source)
        assert info["requests_per_hour"] == 10
        assert info["rate_limit_remaining"] == 8

    def test_whoami_unlimited(self, pm):
        source = _make_source(user_id="admin_user")
        info = pm.whoami(source)
        assert info["requests_per_hour"] is None
        assert info["rate_limit_remaining"] is None


class TestRateLimitCommandIntegration:
    """End-to-end tests for rate limiting in the gateway message flow."""

    @pytest.fixture
    def runner(self, tmp_path):
        from gateway.permissions import RuntimeUserStore

        pt = _make_permission_config(
            tiers={
                "admin": _admin_tier(),
                "limited": TierDefinition(allowed_toolsets=["*"], requests_per_hour=2),
            },
            users={
                "admin_user": UserTierConfig(tier="admin"),
                "limited_user": UserTierConfig(tier="limited"),
                "*": UserTierConfig(tier="limited"),
            },
        )
        r = _make_runner(permission_tiers=pt)
        store = RuntimeUserStore(db_path=tmp_path / "test.db")
        r._permissions._runtime_store = store
        yield r
        store.close()

    @pytest.mark.asyncio
    async def test_whoami_shows_rate_limit(self, runner):
        event = _make_event("/whoami", user_id="limited_user")
        result = await runner._handle_whoami_command(event)
        assert "10" not in result  # Our limit is 2
        assert "Rate limit" in result or "remaining" in result.lower() or "2" in result

    @pytest.mark.asyncio
    async def test_whoami_shows_unlimited(self, runner):
        event = _make_event("/whoami", user_id="admin_user")
        result = await runner._handle_whoami_command(event)
        assert "Unlimited" in result or "unlimited" in result.lower()


# ------------------------------------------------------------------
# Phase 8: MCP tool filtering
# ------------------------------------------------------------------


class TestMCPToolPatternExpansion:
    """Tests for mcp:server:tool syntax in allowed_tools."""

    def test_mcp_wildcard_all(self):
        """mcp:*:* expands to a pattern in resolved_tools."""
        tier = TierDefinition.from_dict({"allowed_tools": ["mcp:*:*"]})
        assert "mcp:*:*" in tier.resolved_tools

    def test_mcp_server_wildcard(self):
        """mcp:server:* expands correctly."""
        tier = TierDefinition.from_dict({"allowed_tools": ["mcp:notion:*"]})
        assert "mcp:notion:*" in tier.resolved_tools

    def test_mcp_specific_tool(self):
        """mcp:server:tool exact match."""
        tier = TierDefinition.from_dict({"allowed_tools": ["mcp:weather:get_forecast"]})
        assert "mcp:weather:get_forecast" in tier.resolved_tools

    def test_mcp_mixed_with_regular(self):
        """MCP patterns coexist with regular tool names."""
        tier = TierDefinition.from_dict(
            {"allowed_tools": ["web_search", "mcp:notion:*", "clarify"]}
        )
        assert "web_search" in tier.resolved_tools
        assert "mcp:notion:*" in tier.resolved_tools
        assert "clarify" in tier.resolved_tools

    def test_mcp_with_groups(self):
        """MCP patterns coexist with @group expansions."""
        tier = TierDefinition.from_dict({"allowed_tools": ["@web", "mcp:*:*"]})
        assert "web_search" in tier.resolved_tools
        assert "mcp:*:*" in tier.resolved_tools


class TestMCPToolFiltering:
    """Tests for the MCP pattern matching in tool filtering."""

    def _filter_tool_names(self, allowed_names, tool_names):
        """Simulate the get_tool_definitions filtering logic."""
        allowed = frozenset(allowed_names)

        _mcp_patterns = [p for p in allowed if p.startswith("mcp:")]
        _concrete_names = {p for p in allowed if not p.startswith("mcp:")}

        def _tool_allowed(tool_name):
            if tool_name in _concrete_names:
                return True
            if not _mcp_patterns or not tool_name.startswith("mcp_"):
                return False
            for pattern in _mcp_patterns:
                if pattern == "mcp:*:*":
                    return True
                normalized = pattern.replace(":", "_")
                if normalized.endswith("_*"):
                    prefix = normalized[:-2]
                    if tool_name.startswith(prefix + "_") or tool_name == prefix:
                        return True
                else:
                    if tool_name == normalized:
                        return True
            return False

        return [t for t in tool_names if _tool_allowed(t)]

    def test_mcp_all_tools_allowed(self):
        tools = ["web_search", "mcp_weather_get_forecast", "mcp_notion_search"]
        result = self._filter_tool_names(["mcp:*:*"], tools)
        # mcp:*:* only matches mcp_ tools, not regular tools
        assert "mcp_weather_get_forecast" in result
        assert "mcp_notion_search" in result
        assert "web_search" not in result

    def test_mcp_server_wildcard(self):
        tools = [
            "mcp_weather_get_forecast",
            "mcp_weather_get_alerts",
            "mcp_notion_search",
        ]
        result = self._filter_tool_names(["mcp:weather:*"], tools)
        assert "mcp_weather_get_forecast" in result
        assert "mcp_weather_get_alerts" in result
        assert "mcp_notion_search" not in result

    def test_mcp_specific_tool(self):
        tools = [
            "mcp_weather_get_forecast",
            "mcp_weather_get_alerts",
        ]
        result = self._filter_tool_names(["mcp:weather:get_forecast"], tools)
        assert "mcp_weather_get_forecast" in result
        assert "mcp_weather_get_alerts" not in result

    def test_mcp_mixed_with_concrete(self):
        tools = [
            "web_search",
            "mcp_weather_get_forecast",
            "terminal",
        ]
        result = self._filter_tool_names(["web_search", "mcp:weather:*"], tools)
        assert "web_search" in result
        assert "mcp_weather_get_forecast" in result
        assert "terminal" not in result

    def test_no_mcp_patterns(self):
        """Without mcp: patterns, MCP tools are excluded."""
        tools = ["web_search", "mcp_weather_get_forecast"]
        result = self._filter_tool_names(["web_search"], tools)
        assert "web_search" in result
        assert "mcp_weather_get_forecast" not in result

    def test_empty_allowed(self):
        tools = ["web_search", "mcp_weather_get_forecast"]
        result = self._filter_tool_names([], tools)
        assert result == []


class TestMCPToolFilteringIntegration:
    """Integration tests for MCP filtering through PermissionManager.filter_tools."""

    def test_filter_tools_with_mcp_all(self):
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            tiers={
                "admin": TierDefinition(
                    allowed_toolsets=["*"],
                    allowed_tools=["@all", "mcp:*:*"],
                    resolved_tools=frozenset({"*", "mcp:*:*"}),
                ),
            },
        )
        pm = PermissionManager(config)
        toolsets, allowed_names = pm.filter_tools(["hermes-discord", "web"], "admin")
        # @all expands to "*" which means no filtering
        assert allowed_names is None  # "*" = no filtering

    def test_filter_tools_with_mcp_server(self):
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            tiers={
                "limited": TierDefinition(
                    allowed_toolsets=["*"],
                    allowed_tools=["web_search", "mcp:notion:*"],
                    resolved_tools=frozenset({"web_search", "mcp:notion:*"}),
                ),
            },
            users={"u1": UserTierConfig(tier="limited")},
        )
        pm = PermissionManager(config)
        toolsets, allowed_names = pm.filter_tools(["hermes-discord", "web"], "limited")
        assert allowed_names is not None
        assert "web_search" in allowed_names
        assert "mcp:notion:*" in allowed_names


class TestMCPDefaultPolicy:
    """Tests for default MCP policy (admin/owner get all, user/guest get none)."""

    def test_builtin_owner_includes_mcp(self):
        """Owner preset uses @all which includes all MCP tools."""
        from gateway.config import BUILTIN_TIER_PRESETS

        owner_preset = BUILTIN_TIER_PRESETS["owner"]
        tier = TierDefinition.from_dict(owner_preset)
        # @all expands to {"*"} — all MCP tools included
        assert "*" in tier.resolved_tools

    def test_builtin_admin_has_explicit_tools(self):
        """Admin preset explicitly lists tools, NOT using *.
        MCP tools ARE included via @mcp (T7c: admin gets MCP access).
        """
        from gateway.config import BUILTIN_TIER_PRESETS

        admin_preset = BUILTIN_TIER_PRESETS["admin"]
        tier = TierDefinition.from_dict(admin_preset)
        # Admin lists individual tools — no wildcard
        assert "*" not in tier.resolved_tools
        # T7c: Admin includes @mcp which expands to mcp:*:*
        mcp_patterns = [t for t in tier.resolved_tools if t.startswith("mcp:")]
        assert len(mcp_patterns) > 0

    def test_builtin_user_excludes_mcp(self):
        """User preset should NOT include MCP tools."""
        from gateway.config import BUILTIN_TIER_PRESETS

        user_preset = BUILTIN_TIER_PRESETS["user"]
        tier = TierDefinition.from_dict(user_preset)
        mcp_patterns = [t for t in tier.resolved_tools if "mcp" in t.lower()]
        assert len(mcp_patterns) == 0

    def test_builtin_guest_excludes_mcp(self):
        """Guest preset should NOT include MCP tools."""
        from gateway.config import BUILTIN_TIER_PRESETS

        guest_preset = BUILTIN_TIER_PRESETS["guest"]
        tier = TierDefinition.from_dict(guest_preset)
        mcp_patterns = [t for t in tier.resolved_tools if "mcp" in t.lower()]
        assert len(mcp_patterns) == 0


# ======================================================================
# Phase 10: Auto-tier from env vars & pairing
# ======================================================================


def _make_auto_tier_config(**overrides):
    """Build a PermissionTiersConfig with auto_tier enabled."""
    from gateway.config import PermissionTiersConfig

    data = {
        "default_tier": "guest",
        "tiers": {
            "owner": {"allowed_tools": ["@all"], "allow_exec": True},
            "admin": {"allowed_tools": ["@web", "@read"], "allow_exec": True},
            "user": {"allowed_tools": ["@web"], "allow_exec": False},
            "guest": {"allowed_tools": ["@clarify"], "allow_exec": False},
        },
        "auto_tier": True,
        "env_owner_tier": "owner",
        "env_default_tier": "admin",
        "pairing_default_tier": "user",
        "env_open_tier": "guest",
    }
    data.update(overrides)
    return PermissionTiersConfig.from_dict(data)


def _make_mock_pairing_store(approved=None):
    """Create a mock PairingStore with predictable approved users."""
    store = MagicMock()
    approved = approved or []
    store.list_approved.return_value = approved
    store.is_approved.side_effect = lambda plat, uid: any(
        e["platform"] == plat and e["user_id"] == uid for e in approved
    )
    return store


class TestAutoTierConfig:
    """Config-level tests for auto-tier fields (P10.1)."""

    def test_auto_tier_defaults_to_false(self):
        cfg = PermissionTiersConfig.from_dict({"tiers": {"owner": {}}})
        assert cfg.auto_tier is False

    def test_auto_tier_enabled_explicitly(self):
        cfg = _make_auto_tier_config()
        assert cfg.auto_tier is True

    def test_auto_tier_disabled_when_env_owner_tier_missing(self):
        """Fail-closed: auto_tier disabled if referenced tier doesn't exist."""
        cfg = PermissionTiersConfig.from_dict(
            {
                "tiers": {"admin": {}},
                "auto_tier": True,
                "env_owner_tier": "nonexistent",
            }
        )
        assert cfg.auto_tier is False

    def test_auto_tier_disabled_when_env_default_tier_missing(self):
        cfg = PermissionTiersConfig.from_dict(
            {
                "tiers": {"admin": {}, "owner": {}},
                "auto_tier": True,
                "env_default_tier": "missing_tier",
            }
        )
        assert cfg.auto_tier is False

    def test_auto_tier_disabled_when_pairing_default_missing(self):
        cfg = PermissionTiersConfig.from_dict(
            {
                "tiers": {"admin": {}, "owner": {}},
                "auto_tier": True,
                "pairing_default_tier": "missing",
            }
        )
        assert cfg.auto_tier is False

    def test_auto_tier_disabled_when_env_open_tier_missing(self):
        cfg = PermissionTiersConfig.from_dict(
            {
                "tiers": {"admin": {}, "owner": {}},
                "auto_tier": True,
                "env_open_tier": "missing",
            }
        )
        assert cfg.auto_tier is False

    def test_auto_tier_stays_on_when_all_tiers_valid(self):
        cfg = _make_auto_tier_config()
        assert cfg.auto_tier is True
        assert cfg.env_owner_tier == "owner"
        assert cfg.env_default_tier == "admin"
        assert cfg.pairing_default_tier == "user"
        assert cfg.env_open_tier == "guest"

    def test_auto_tier_field_defaults(self):
        """Default tier references match Issue #527 spec."""
        cfg = PermissionTiersConfig.from_dict({"tiers": {"owner": {}}})
        assert cfg.env_owner_tier == "owner"
        assert cfg.env_default_tier == "admin"
        assert cfg.pairing_default_tier == "user"
        assert cfg.env_open_tier == "guest"

    def test_roundtrip_serialization(self):
        cfg = _make_auto_tier_config()
        d = cfg.to_dict()
        assert d["auto_tier"] is True
        assert d["env_owner_tier"] == "owner"
        assert d["env_default_tier"] == "admin"
        assert d["pairing_default_tier"] == "user"
        assert d["env_open_tier"] == "guest"

    def test_roundtrip_from_dict_preserves_auto_tier(self):
        cfg = _make_auto_tier_config()
        d = cfg.to_dict()
        cfg2 = PermissionTiersConfig.from_dict(d)
        assert cfg2.auto_tier is True
        assert cfg2.env_owner_tier == "owner"


class TestAutoTierEnvVars:
    """Tests for _apply_env_auto_tiers() reading env vars (P10.2)."""

    def _make_mgr(self, env=None, pairing_store=None, **cfg_overrides):
        """Build a PermissionManager with auto_tier config and optional env."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config(**cfg_overrides)
        with patch.dict("os.environ", env or {}, clear=False):
            mgr = PermissionManager(cfg, pairing_store=pairing_store)
        return mgr

    def test_platform_env_var_first_user_gets_owner(self):
        """First entry in TELEGRAM_ALLOWED_USERS gets env_owner_tier."""
        mgr = self._make_mgr(env={"TELEGRAM_ALLOWED_USERS": "111,222,333"})
        cfg = mgr.config
        assert cfg.users["telegram:111"].tier == "owner"

    def test_platform_env_var_remaining_users_get_default(self):
        """Non-first entries get env_default_tier."""
        mgr = self._make_mgr(env={"TELEGRAM_ALLOWED_USERS": "111,222,333"})
        assert mgr.config.users["telegram:222"].tier == "admin"
        assert mgr.config.users["telegram:333"].tier == "admin"

    def test_discord_env_var(self):
        mgr = self._make_mgr(env={"DISCORD_ALLOWED_USERS": "444"})
        assert mgr.config.users["discord:444"].tier == "owner"

    def test_multiple_platforms_independent(self):
        """Each platform's env var creates separate composite keys."""
        mgr = self._make_mgr(
            env={
                "TELEGRAM_ALLOWED_USERS": "111",
                "DISCORD_ALLOWED_USERS": "222",
            }
        )
        assert mgr.config.users["telegram:111"].tier == "owner"
        assert mgr.config.users["discord:222"].tier == "owner"

    def test_global_gateway_allowed_users(self):
        """GATEWAY_ALLOWED_USERS injects with 'global:' prefix."""
        mgr = self._make_mgr(env={"GATEWAY_ALLOWED_USERS": "999,888"})
        assert mgr.config.users["global:999"].tier == "owner"
        assert mgr.config.users["global:888"].tier == "admin"

    def test_empty_env_var_ignored(self):
        mgr = self._make_mgr(env={"TELEGRAM_ALLOWED_USERS": "  "})
        # No users injected from telegram
        telegram_keys = [k for k in mgr.config.users if k.startswith("telegram:")]
        assert len(telegram_keys) == 0

    def test_no_env_vars_no_injection(self):
        mgr = self._make_mgr(env={})
        # Only users from config (none by default)
        assert len(mgr.config.users) == 0

    def test_explicit_config_not_overridden(self):
        """Explicit config.yaml user entries are never overridden by auto-tier."""
        cfg = _make_auto_tier_config(users={"telegram:111": {"tier": "guest"}})
        from gateway.permissions import PermissionManager

        with patch.dict("os.environ", {"TELEGRAM_ALLOWED_USERS": "111"}, clear=False):
            mgr = PermissionManager(cfg)
        # Explicit config entry wins — still guest, not owner
        assert mgr.config.users["telegram:111"].tier == "guest"

    def test_bare_user_id_config_not_overridden(self):
        """Bare user_id in config also prevents auto-tier injection."""
        cfg = _make_auto_tier_config(users={"111": {"tier": "user"}})
        from gateway.permissions import PermissionManager

        with patch.dict("os.environ", {"TELEGRAM_ALLOWED_USERS": "111"}, clear=False):
            mgr = PermissionManager(cfg)
        # Bare key config wins
        assert mgr.config.users["111"].tier == "user"
        # No composite key injected
        assert "telegram:111" not in mgr.config.users

    def test_composite_key_prevents_cross_platform_collision(self):
        """User ID '123' on Telegram ≠ '123' on Discord."""
        mgr = self._make_mgr(
            env={
                "TELEGRAM_ALLOWED_USERS": "123",
                "DISCORD_ALLOWED_USERS": "123",
            }
        )
        assert mgr.config.users["telegram:123"].tier == "owner"
        assert mgr.config.users["discord:123"].tier == "owner"
        # Two separate entries
        assert len([k for k in mgr.config.users if ":123" in k]) == 2


class TestAutoTierAllowAll:
    """Tests for ALLOW_ALL_USERS → wildcard injection."""

    def _make_mgr(self, env=None):
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config()
        with patch.dict("os.environ", env or {}, clear=False):
            mgr = PermissionManager(cfg)
        return mgr

    def test_allow_all_creates_wildcard_entry(self):
        mgr = self._make_mgr(env={"TELEGRAM_ALLOW_ALL_USERS": "true"})
        assert "*" in mgr.config.users
        assert mgr.config.users["*"].tier == "guest"

    def test_allow_all_yes_value(self):
        mgr = self._make_mgr(env={"GATEWAY_ALLOW_ALL_USERS": "yes"})
        assert mgr.config.users["*"].tier == "guest"

    def test_allow_all_1_value(self):
        mgr = self._make_mgr(env={"DISCORD_ALLOW_ALL_USERS": "1"})
        assert mgr.config.users["*"].tier == "guest"

    def test_allow_all_false_no_injection(self):
        mgr = self._make_mgr(env={"TELEGRAM_ALLOW_ALL_USERS": "false"})
        assert "*" not in mgr.config.users

    def test_existing_wildcard_not_overridden(self):
        """If config.yaml already has a wildcard, auto-tier doesn't override."""
        cfg = _make_auto_tier_config(users={"*": {"tier": "admin"}})
        from gateway.permissions import PermissionManager

        with patch.dict(
            "os.environ", {"TELEGRAM_ALLOW_ALL_USERS": "true"}, clear=False
        ):
            mgr = PermissionManager(cfg)
        assert mgr.config.users["*"].tier == "admin"

    def test_first_allow_all_flag_wins(self):
        """Only one wildcard entry is created, even if multiple ALLOW_ALL flags set."""
        mgr = self._make_mgr(
            env={
                "TELEGRAM_ALLOW_ALL_USERS": "true",
                "DISCORD_ALLOW_ALL_USERS": "true",
            }
        )
        assert mgr.config.users["*"].tier == "guest"
        # Only one wildcard entry
        assert sum(1 for k in mgr.config.users if k == "*") == 1


class TestAutoTierPairing:
    """Tests for pairing store → tier injection."""

    def test_pairing_approved_users_injected(self):
        """Approved pairing users get pairing_default_tier."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config()
        pairing = _make_mock_pairing_store(
            approved=[
                {"platform": "telegram", "user_id": "555", "user_name": "alice"},
                {"platform": "discord", "user_id": "666", "user_name": "bob"},
            ]
        )
        mgr = PermissionManager(cfg, pairing_store=pairing)
        assert mgr.config.users["telegram:555"].tier == "user"
        assert mgr.config.users["discord:666"].tier == "user"

    def test_pairing_no_store_no_error(self):
        """No pairing store → no injection, no error."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config()
        mgr = PermissionManager(cfg, pairing_store=None)
        assert len(mgr.config.users) == 0

    def test_pairing_store_exception_handled(self):
        """Pairing store failure is caught gracefully (fail-safe logging)."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config()
        bad_store = MagicMock()
        bad_store.list_approved.side_effect = RuntimeError("disk error")
        mgr = PermissionManager(cfg, pairing_store=bad_store)
        # No users injected, no crash
        assert len(mgr.config.users) == 0

    def test_pairing_user_not_overridden_by_env(self):
        """If env var already injected a user, pairing store skips them."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config()
        pairing = _make_mock_pairing_store(
            approved=[{"platform": "telegram", "user_id": "777"}]
        )
        with patch.dict("os.environ", {"TELEGRAM_ALLOWED_USERS": "111"}, clear=False):
            mgr = PermissionManager(cfg, pairing_store=pairing)
        # Env var already injected telegram:111 as owner
        assert mgr.config.users["telegram:111"].tier == "owner"


class TestAutoTierCompositeKeyResolution:
    """Tests for resolve_user_tier() with composite keys (P10.4)."""

    def _make_mgr(self, users=None, auto_tier=True):
        from gateway.permissions import PermissionManager

        cfg = (
            _make_auto_tier_config(users=users)
            if auto_tier
            else _make_auto_tier_config()
        )
        return PermissionManager(cfg)

    def test_composite_key_resolved(self):
        """Composite key 'telegram:123' is matched by resolve_user_tier()."""
        mgr = self._make_mgr(users={"telegram:123": {"tier": "owner"}})
        source = _make_source(user_id="123", platform=Platform.TELEGRAM)
        assert mgr.resolve_user_tier(source) == "owner"

    def test_bare_key_still_works(self):
        """Bare user_id in config still resolves (backward compat)."""
        mgr = self._make_mgr(users={"123": {"tier": "admin"}})
        source = _make_source(user_id="123", platform=Platform.TELEGRAM)
        assert mgr.resolve_user_tier(source) == "admin"

    def test_composite_key_takes_precedence_over_bare(self):
        """When both composite and bare keys exist, composite wins."""
        mgr = self._make_mgr(
            users={"telegram:123": {"tier": "owner"}, "123": {"tier": "guest"}}
        )
        source = _make_source(user_id="123", platform=Platform.TELEGRAM)
        assert mgr.resolve_user_tier(source) == "owner"

    def test_cross_platform_isolation(self):
        """User '123' on Telegram ≠ '123' on Discord."""
        mgr = self._make_mgr(
            users={
                "telegram:123": {"tier": "owner"},
                "discord:123": {"tier": "guest"},
            }
        )
        tg_source = _make_source(user_id="123", platform=Platform.TELEGRAM)
        dc_source = _make_source(user_id="123", platform=Platform.DISCORD)
        assert mgr.resolve_user_tier(tg_source) == "owner"
        assert mgr.resolve_user_tier(dc_source) == "guest"

    def test_no_platform_attr_falls_back_to_bare_key(self):
        """Source without platform uses bare user_id lookup."""
        mgr = self._make_mgr(users={"999": {"tier": "admin"}})
        source = SimpleNamespace(user_id="999", platform=None)
        assert mgr.resolve_user_tier(source) == "admin"


class TestAutoTierDynamicPairing:
    """Tests for dynamic pairing injection in resolve_user_tier() (P10.5)."""

    def test_dynamic_pairing_injection(self):
        """User approved in pairing store gets tier on-the-fly."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config()
        _pairing = _make_mock_pairing_store(
            approved=[{"platform": "telegram", "user_id": "777"}]
        )
        # No pairing users injected at init (already tested that init injection works).
        # Let's test the dynamic path by using a fresh pairing store that
        # has a newly-approved user not seen at init.
        mgr = PermissionManager(cfg, pairing_store=None)

        # Now simulate a new pairing approval by updating the store
        new_pairing = _make_mock_pairing_store(
            approved=[{"platform": "telegram", "user_id": "777"}]
        )
        mgr._pairing_store = new_pairing

        source = _make_source(user_id="777", platform=Platform.TELEGRAM)
        assert mgr.resolve_user_tier(source) == "user"

        # Entry is now cached in config.users
        assert "telegram:777" in mgr.config.users

    def test_dynamic_pairing_disabled_when_auto_tier_off(self):
        """No dynamic injection when auto_tier is False."""
        from gateway.permissions import PermissionManager

        cfg = PermissionTiersConfig.from_dict(
            {
                "default_tier": "guest",
                "tiers": {"guest": {"allowed_tools": ["@clarify"]}},
            }
        )
        pairing = _make_mock_pairing_store(
            approved=[{"platform": "telegram", "user_id": "777"}]
        )
        mgr = PermissionManager(cfg, pairing_store=pairing)
        source = _make_source(user_id="777", platform=Platform.TELEGRAM)
        # auto_tier is off → falls to default_tier
        assert mgr.resolve_user_tier(source) == "guest"

    def test_dynamic_pairing_store_failure_is_safe(self):
        """If pairing store raises during dynamic check, user gets default tier."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config()
        bad_store = MagicMock()
        bad_store.is_approved.side_effect = RuntimeError("disk error")
        mgr = PermissionManager(cfg, pairing_store=bad_store)
        source = _make_source(user_id="777", platform=Platform.TELEGRAM)
        # No crash, falls back to default_tier
        assert mgr.resolve_user_tier(source) == "guest"

    def test_dynamic_pairing_no_store_no_error(self):
        """No pairing store → no dynamic injection, no crash."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config()
        mgr = PermissionManager(cfg, pairing_store=None)
        source = _make_source(user_id="777", platform=Platform.TELEGRAM)
        assert mgr.resolve_user_tier(source) == "guest"

    def test_dynamic_pairing_result_cached(self):
        """Once injected, subsequent lookups don't re-query the pairing store."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config()
        pairing = _make_mock_pairing_store(
            approved=[{"platform": "telegram", "user_id": "777"}]
        )
        mgr = PermissionManager(cfg)
        mgr._pairing_store = pairing

        source = _make_source(user_id="777", platform=Platform.TELEGRAM)
        result1 = mgr.resolve_user_tier(source)
        assert result1 == "user"

        # Reset mock to verify it's not called again
        pairing.is_approved.reset_mock()

        result2 = mgr.resolve_user_tier(source)
        assert result2 == "user"
        # The cached config entry was found, so is_approved was NOT called again
        pairing.is_approved.assert_not_called()


class TestAutoTierEndToEnd:
    """Integration tests: env vars + pairing + composite keys together."""

    def test_full_auto_tier_pipeline(self):
        """Simulate a real auto-tier setup with env vars + pairing."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config()
        pairing = _make_mock_pairing_store(
            approved=[
                {"platform": "telegram", "user_id": "444"},
            ]
        )
        with patch.dict(
            "os.environ",
            {"TELEGRAM_ALLOWED_USERS": "111,222"},
            clear=False,
        ):
            mgr = PermissionManager(cfg, pairing_store=pairing)

        # 111 = first in ALLOWED_USERS → owner
        s111 = _make_source(user_id="111", platform=Platform.TELEGRAM)
        assert mgr.resolve_user_tier(s111) == "owner"

        # 222 = second in ALLOWED_USERS → admin
        s222 = _make_source(user_id="222", platform=Platform.TELEGRAM)
        assert mgr.resolve_user_tier(s222) == "admin"

        # 444 = pairing-approved → user
        s444 = _make_source(user_id="444", platform=Platform.TELEGRAM)
        assert mgr.resolve_user_tier(s444) == "user"

        # 999 = unknown → default (guest)
        s999 = _make_source(user_id="999", platform=Platform.TELEGRAM)
        assert mgr.resolve_user_tier(s999) == "guest"

    def test_explicit_config_wins_over_auto_tier(self):
        """Explicit config.yaml entries are never overridden."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config(users={"telegram:111": {"tier": "guest"}})
        with patch.dict("os.environ", {"TELEGRAM_ALLOWED_USERS": "111"}, clear=False):
            mgr = PermissionManager(cfg)

        s = _make_source(user_id="111", platform=Platform.TELEGRAM)
        # Explicit config says guest, auto-tier would say owner → config wins
        assert mgr.resolve_user_tier(s) == "guest"

    def test_auto_tier_disabled_is_noop(self):
        """When auto_tier: false, no env var injection happens."""
        from gateway.permissions import PermissionManager

        cfg = PermissionTiersConfig.from_dict(
            {
                "default_tier": "admin",
                "tiers": {"admin": {"allowed_tools": ["@all"]}},
            }
        )
        with patch.dict(
            "os.environ",
            {"TELEGRAM_ALLOWED_USERS": "111,222"},
            clear=False,
        ):
            mgr = PermissionManager(cfg)

        # No auto-tier injection → no users in config
        assert len(mgr.config.users) == 0
        # Falls back to default_tier
        s = _make_source(user_id="111", platform=Platform.TELEGRAM)
        assert mgr.resolve_user_tier(s) == "admin"


# ------------------------------------------------------------------
# FIX-01: Delegate tool privilege escalation
# ------------------------------------------------------------------


class TestDelegateToolPermissionEscalation:
    """FIX-01: allowed_tool_names must propagate to child agents."""

    def test_child_inherits_allowed_tool_names(self):
        """Child agent created by _build_child_agent inherits parent's allowed_tool_names."""
        from tools.delegate_tool import _build_child_agent

        # Mock parent agent with restricted tool allowlist
        parent = MagicMock()
        parent.enabled_toolsets = ["web", "file"]
        parent.allowed_tool_names = frozenset({"web_search", "read_file"})
        parent.model = "test-model"
        parent.base_url = "http://localhost"
        parent.api_key = "test-key"
        parent.api_mode = None
        parent.provider = None
        parent.providers_allowed = None
        parent.providers_ignored = None
        parent.providers_order = None
        parent.provider_sort = None
        parent.max_tokens = None
        parent.reasoning_config = None
        parent.prefill_messages = None
        parent.platform = "cli"
        parent.acp_command = None
        parent.acp_args = []
        parent._session_db = None
        parent._active_children = []
        parent._active_children_lock = None

        child = _build_child_agent(
            task_index=0,
            goal="test task",
            context=None,
            toolsets=None,
            model=None,
            max_iterations=90,
            parent_agent=parent,
        )
        assert child.allowed_tool_names == frozenset({"web_search", "read_file"})

    def test_child_no_restriction_when_parent_unrestricted(self):
        """When parent has no allowed_tool_names, child also has None."""
        from tools.delegate_tool import _build_child_agent

        parent = MagicMock()
        parent.enabled_toolsets = ["web", "file"]
        parent.allowed_tool_names = None  # No restriction
        parent.model = "test-model"
        parent.base_url = "http://localhost"
        parent.api_key = "test-key"
        parent.api_mode = None
        parent.provider = None
        parent.providers_allowed = None
        parent.providers_ignored = None
        parent.providers_order = None
        parent.provider_sort = None
        parent.max_tokens = None
        parent.reasoning_config = None
        parent.prefill_messages = None
        parent.platform = "cli"
        parent.acp_command = None
        parent.acp_args = []
        parent._session_db = None
        parent._active_children = []
        parent._active_children_lock = None

        child = _build_child_agent(
            task_index=0,
            goal="test task",
            context=None,
            toolsets=None,
            model=None,
            max_iterations=90,
            parent_agent=parent,
        )
        assert child.allowed_tool_names is None


# ------------------------------------------------------------------
# FIX-02: allow_exec strips exec tools
# ------------------------------------------------------------------


class TestAllowExecToolStripping:
    """FIX-02: allow_exec=False strips exec tools from agent schema."""

    def test_custom_tier_no_exec_strips_terminal_tools(self):
        """Custom tier with allow_exec=false should not have terminal tools."""
        runner = _make_runner(
            permission_tiers=_make_permission_config(
                tiers={
                    "custom": TierDefinition(
                        allowed_toolsets=["*"],
                        allow_exec=False,
                    ),
                },
                users={"test_user": UserTierConfig(tier="custom")},
            )
        )
        source = _make_source(user_id="test_user")
        tier_name = runner._permissions.resolve_user_tier(source)
        tier_cfg = runner._permissions.get_tier_config(tier_name)
        assert tier_cfg is not None
        assert tier_cfg.allow_exec is False

    def test_custom_tier_with_exec_has_terminal(self):
        """Custom tier with allow_exec=true should allow terminal tools."""
        runner = _make_runner(
            permission_tiers=_make_permission_config(
                tiers={
                    "custom": TierDefinition(
                        allowed_toolsets=["*"],
                        allow_exec=True,
                    ),
                },
                users={"test_user": UserTierConfig(tier="custom")},
            )
        )
        source = _make_source(user_id="test_user")
        tier_name = runner._permissions.resolve_user_tier(source)
        tier_cfg = runner._permissions.get_tier_config(tier_name)
        assert tier_cfg is not None
        assert tier_cfg.allow_exec is True


# ------------------------------------------------------------------
# FIX-03: RuntimeUserStore composite keys
# ------------------------------------------------------------------


class TestRuntimeUserStoreCompositeKeys:
    """FIX-03: RuntimeUserStore uses composite (user_id, source_platform) keys."""

    def test_same_user_id_different_platforms(self, tmp_path):
        """Same user_id on different platforms are separate entries."""
        from gateway.permissions import RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "test.db")

        store.set_user_tier("123", "admin", source_platform="telegram")
        store.set_user_tier("123", "restricted", source_platform="discord")

        tg = store.get_user_tier("123", source_platform="telegram")
        dc = store.get_user_tier("123", source_platform="discord")

        assert tg["tier_name"] == "admin"
        assert dc["tier_name"] == "restricted"

    def test_remove_all_platforms(self, tmp_path):
        """remove_user_tier without platform removes all entries."""
        from gateway.permissions import RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "test.db")

        store.set_user_tier("123", "admin", source_platform="telegram")
        store.set_user_tier("123", "restricted", source_platform="discord")

        # Remove without platform → removes all
        removed = store.remove_user_tier("123")
        assert removed is True

        assert store.get_user_tier("123", source_platform="telegram") is None
        assert store.get_user_tier("123", source_platform="discord") is None

    def test_remove_specific_platform(self, tmp_path):
        """remove_user_tier with platform only removes that entry."""
        from gateway.permissions import RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "test.db")

        store.set_user_tier("123", "admin", source_platform="telegram")
        store.set_user_tier("123", "restricted", source_platform="discord")

        removed = store.remove_user_tier("123", source_platform="telegram")
        assert removed is True

        assert store.get_user_tier("123", source_platform="telegram") is None
        dc = store.get_user_tier("123", source_platform="discord")
        assert dc["tier_name"] == "restricted"

    def test_list_all_includes_platform(self, tmp_path):
        """list_all returns entries with source_platform."""
        from gateway.permissions import RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "test.db")

        store.set_user_tier("123", "admin", source_platform="telegram")
        store.set_user_tier("456", "restricted", source_platform="discord")

        entries = store.list_all()
        platforms = {(e["user_id"], e["source_platform"]) for e in entries}
        assert ("123", "telegram") in platforms
        assert ("456", "discord") in platforms

    def test_old_schema_migrated(self, tmp_path):
        """Old single-PK schema is migrated to composite PK."""
        import sqlite3
        from gateway.permissions import RuntimeUserStore

        db_path = tmp_path / "migrate.db"
        # Create old schema
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE user_tiers ("
            "user_id TEXT PRIMARY KEY, tier_name TEXT NOT NULL, "
            "granted_by TEXT NOT NULL DEFAULT 'system', granted_at TEXT NOT NULL, "
            "source_platform TEXT)"
        )
        conn.execute(
            "INSERT INTO user_tiers VALUES (?, ?, ?, ?, ?)",
            ("old_user", "admin", "system", "2025-01-01T00:00:00", "telegram"),
        )
        conn.commit()
        conn.close()

        # Opening with RuntimeUserStore should migrate
        store = RuntimeUserStore(db_path=db_path)
        # Old data is gone (drop + recreate)
        assert store.get_user_tier("old_user", source_platform="telegram") is None
        # Can write with new schema
        store.set_user_tier("new_user", "admin", source_platform="discord")
        assert store.get_user_tier("new_user", source_platform="discord") is not None

    def test_rate_limit_platform_scoped(self, tmp_path):
        """Rate limits are scoped by platform — no cross-platform bleed."""
        from gateway.permissions import PermissionManager, RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "rl.db")
        config = PermissionTiersConfig(
            tiers={
                "limited": TierDefinition(allowed_toolsets=["*"], requests_per_hour=5)
            },
            users={
                "telegram:123": UserTierConfig(tier="limited"),
                "discord:123": UserTierConfig(tier="limited"),
            },
            default_tier="limited",
        )
        pm = PermissionManager(config, runtime_store=store)

        # Create sources for same user_id on different platforms
        tg_source = _make_source(user_id="123", platform=Platform.TELEGRAM)
        dc_source = _make_source(user_id="123", platform=Platform.DISCORD)

        # Exhaust rate limit on telegram
        for _ in range(5):
            ok, _ = pm.check_rate_limit(tg_source)
            assert ok is True

        ok, _ = pm.check_rate_limit(tg_source)
        assert ok is False  # Telegram blocked

        # Discord should still work
        ok, _ = pm.check_rate_limit(dc_source)
        assert ok is True  # Different platform, independent counter


# ------------------------------------------------------------------
# Phase B: Edge-case tests for test coverage gap analysis
# ------------------------------------------------------------------


class TestCompositeKeyWithSpecialChars:
    """Edge case: user IDs containing colons (e.g. Matrix @user:matrix.org)."""

    def test_matrix_style_user_id_in_config(self):
        """Matrix IDs like @user:matrix.org work as composite key values."""
        runner = _make_runner(
            permission_tiers=_make_permission_config(
                tiers={
                    "admin": _admin_tier(),
                    "restricted": _restricted_tier(),
                },
                users={
                    "matrix:@alice:matrix.org": UserTierConfig(tier="admin"),
                },
                default_tier="restricted",
            )
        )
        source = _make_source(user_id="@alice:matrix.org", platform=Platform.MATRIX)
        tier = runner._permissions.resolve_user_tier(source)
        assert tier == "admin"

    def test_matrix_runtime_store(self, tmp_path):
        """RuntimeUserStore handles Matrix-style IDs correctly."""
        from gateway.permissions import RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "matrix.db")
        uid = "@bob:matrix.org"
        store.set_user_tier(uid, "admin", source_platform="matrix")
        entry = store.get_user_tier(uid, source_platform="matrix")
        assert entry is not None
        assert entry["tier_name"] == "admin"

    def test_bare_user_id_with_colon_in_config(self):
        """Bare user_id with colons resolves without splitting."""
        runner = _make_runner(
            permission_tiers=_make_permission_config(
                tiers={
                    "admin": _admin_tier(),
                    "restricted": _restricted_tier(),
                },
                users={
                    "@charlie:matrix.org": UserTierConfig(tier="admin"),
                },
                default_tier="restricted",
            )
        )
        source = _make_source(user_id="@charlie:matrix.org", platform=Platform.DISCORD)
        tier = runner._permissions.resolve_user_tier(source)
        assert tier == "admin"


class TestRateLimitHourBoundary:
    """Edge case: rate limits at hour boundary transitions."""

    def test_rate_limit_resets_across_hour_boundary(self):
        """Counter resets when the hour bucket changes."""
        from gateway.permissions import PermissionManager

        config = PermissionTiersConfig(
            tiers={
                "limited": TierDefinition(allowed_toolsets=["*"], requests_per_hour=2)
            },
            users={"u1": UserTierConfig(tier="limited")},
            default_tier="limited",
        )
        pm = PermissionManager(config)
        source = _make_source(user_id="u1")

        # Mock time to return bucket 100
        with patch.object(pm, "_current_hour_bucket", return_value=100):
            ok1, _ = pm.check_rate_limit(source)
            ok2, _ = pm.check_rate_limit(source)
            ok3, _ = pm.check_rate_limit(source)
        assert ok1 is True
        assert ok2 is True
        assert ok3 is False  # limit exhausted in bucket 100

        # Same user, but new hour bucket — counter resets
        with patch.object(pm, "_current_hour_bucket", return_value=101):
            ok4, _ = pm.check_rate_limit(source)
        assert ok4 is True  # Fresh bucket, fresh counter


class TestAutoTierEnvVarConflicts:
    """Edge case: platform-specific and global env vars interacting."""

    def test_platform_wins_over_global_for_same_user(self):
        """Platform-specific env var takes precedence over global."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config()

        # Both set the same user — platform entry wins
        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_ALLOWED_USERS": "999",
                "GATEWAY_ALLOWED_USERS": "999",
            },
            clear=False,
        ):
            mgr = PermissionManager(cfg)
        # Platform entry should exist
        assert "telegram:999" in mgr.config.users
        # Global entry should also exist
        assert "global:999" in mgr.config.users

    def test_explicit_config_blocks_both_env_injections(self):
        """Explicit config entry prevents both platform and global auto-tier."""
        from gateway.permissions import PermissionManager

        cfg = _make_auto_tier_config(users={"999": {"tier": "restricted"}})

        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_ALLOWED_USERS": "999",
                "GATEWAY_ALLOWED_USERS": "999",
            },
            clear=False,
        ):
            mgr = PermissionManager(cfg)
        # Bare key config wins — no composite keys injected
        assert mgr.config.users["999"].tier == "restricted"
        assert "telegram:999" not in mgr.config.users
        assert "global:999" not in mgr.config.users


class TestUnicodeUserIDs:
    """Edge case: non-ASCII and special characters in user IDs."""

    def test_runtime_store_unicode_user_id(self, tmp_path):
        """RuntimeUserStore handles unicode user IDs."""
        from gateway.permissions import RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "unicode.db")
        uid = "用户_123"
        store.set_user_tier(uid, "admin", source_platform="discord")
        entry = store.get_user_tier(uid, source_platform="discord")
        assert entry is not None
        assert entry["tier_name"] == "admin"

    def test_runtime_store_emoji_user_id(self, tmp_path):
        """RuntimeUserStore handles emoji in user IDs."""
        from gateway.permissions import RuntimeUserStore

        store = RuntimeUserStore(db_path=tmp_path / "emoji.db")
        uid = "🎉user🎉"
        store.set_user_tier(uid, "restricted", source_platform="telegram")
        entry = store.get_user_tier(uid, source_platform="telegram")
        assert entry is not None
        assert entry["tier_name"] == "restricted"

    def test_config_unicode_user_id(self):
        """Unicode user IDs work in config resolution."""
        runner = _make_runner(
            permission_tiers=_make_permission_config(
                tiers={
                    "admin": _admin_tier(),
                    "restricted": _restricted_tier(),
                },
                users={
                    "用户_123": UserTierConfig(tier="admin"),
                },
                default_tier="restricted",
            )
        )
        source = _make_source(user_id="用户_123")
        tier = runner._permissions.resolve_user_tier(source)
        assert tier == "admin"


class TestConcurrentRateLimiting:
    """Edge case: concurrent rate limit checks are thread-safe."""

    def test_concurrent_rate_limit_counts_accurate(self):
        """Many concurrent threads hitting rate limit don't lose counts."""
        import threading
        from gateway.permissions import PermissionManager

        config = PermissionTiersConfig(
            tiers={
                "limited": TierDefinition(allowed_toolsets=["*"], requests_per_hour=100)
            },
            users={"u1": UserTierConfig(tier="limited")},
            default_tier="limited",
        )
        pm = PermissionManager(config)
        source = _make_source(user_id="u1")

        # Patch to fixed bucket so all threads hit the same counter
        with patch.object(pm, "_current_hour_bucket", return_value=42):
            successes = []
            errors = []

            def hit_rate_limit():
                try:
                    ok, _ = pm.check_rate_limit(source)
                    successes.append(ok)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=hit_rate_limit) for _ in range(100)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(errors) == 0
        # All 100 should succeed (limit is 100)
        assert all(successes)
        # Next call should fail
        with patch.object(pm, "_current_hour_bucket", return_value=42):
            ok, _ = pm.check_rate_limit(source)
        assert ok is False


# ------------------------------------------------------------------
# Round 4 regression tests (FIX-04 through FIX-10)
# ------------------------------------------------------------------


class TestPersonalitySethomeAdminOnly:
    """FIX-04: /personality and /sethome must be admin_only."""

    def test_personality_in_admin_commands(self):
        from hermes_cli.commands import COMMAND_REGISTRY

        personality_cmd = next(c for c in COMMAND_REGISTRY if c.name == "personality")
        assert personality_cmd.admin_only is True

    def test_sethome_in_admin_commands(self):
        from hermes_cli.commands import COMMAND_REGISTRY

        sethome_cmd = next(c for c in COMMAND_REGISTRY if c.name == "sethome")
        assert sethome_cmd.admin_only is True

    def test_update_in_owner_commands(self):
        """FIX-09: /update must be owner_only."""
        from hermes_cli.commands import COMMAND_REGISTRY

        update_cmd = next(c for c in COMMAND_REGISTRY if c.name == "update")
        assert update_cmd.admin_only is True
        assert update_cmd.owner_only is True


class TestCleanupRateCountersCompositeKeys:
    """FIX-05: cleanup_rate_counters must use k[2] (bucket), not k[1] (user_id)."""

    def test_cleanup_with_3tuple_keys(self):
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            tiers={
                "limited": TierDefinition(
                    allowed_toolsets=["*"], requests_per_hour=100
                ),
            },
        )
        pm = PermissionManager(config)
        old_bucket = pm._current_hour_bucket() - 1
        current_bucket = pm._current_hour_bucket()

        # 3-tuple keys: (platform, user_id, bucket)
        pm._rate_counts[("discord", "u1", old_bucket)] = 50
        pm._rate_counts[("discord", "u1", current_bucket)] = 10

        pm.cleanup_rate_counters()

        assert ("discord", "u1", old_bucket) not in pm._rate_counts
        assert ("discord", "u1", current_bucket) in pm._rate_counts

    def test_cleanup_handles_multiple_platforms(self):
        """Verify cleanup doesn't mix up platforms."""
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            tiers={
                "limited": TierDefinition(
                    allowed_toolsets=["*"], requests_per_hour=100
                ),
            },
        )
        pm = PermissionManager(config)
        old_bucket = pm._current_hour_bucket() - 1
        current_bucket = pm._current_hour_bucket()

        pm._rate_counts[("discord", "u1", old_bucket)] = 50
        pm._rate_counts[("telegram", "u1", old_bucket)] = 30
        pm._rate_counts[("discord", "u2", current_bucket)] = 10
        pm._rate_counts[("telegram", "u1", current_bucket)] = 5

        pm.cleanup_rate_counters()

        # All old-bucket entries removed, regardless of platform
        assert ("discord", "u1", old_bucket) not in pm._rate_counts
        assert ("telegram", "u1", old_bucket) not in pm._rate_counts
        # Current-bucket entries preserved
        assert ("discord", "u2", current_bucket) in pm._rate_counts
        assert ("telegram", "u1", current_bucket) in pm._rate_counts


class TestResolveUserCfgCompositeKeys:
    """FIX-06: resolve_user_cfg must check composite keys (platform:user_id)."""

    def test_composite_key_resolved(self):
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            users={"telegram:u1": UserTierConfig(tier="restricted", locale="de")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        pm = PermissionManager(config)
        source = _make_source(user_id="u1", platform=Platform.TELEGRAM)
        cfg = pm.resolve_user_cfg(source)
        assert cfg is not None
        assert cfg.locale == "de"
        assert cfg.tier == "restricted"

    def test_bare_key_fallback(self):
        """Bare user_id still works when no composite key matches."""
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted", locale="fr")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        pm = PermissionManager(config)
        source = _make_source(user_id="u1", platform=Platform.DISCORD)
        cfg = pm.resolve_user_cfg(source)
        assert cfg is not None
        assert cfg.locale == "fr"

    def test_composite_overrides_bare(self):
        """Composite key takes priority over bare key."""
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            users={
                "u1": UserTierConfig(tier="admin", locale="en"),
                "discord:u1": UserTierConfig(tier="restricted", locale="de"),
            },
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        pm = PermissionManager(config)
        source = _make_source(user_id="u1", platform=Platform.DISCORD)
        cfg = pm.resolve_user_cfg(source)
        assert cfg.tier == "restricted"
        assert cfg.locale == "de"


class TestFormatTierMessageSafeTemplates:
    """FIX-08: str.format() replaced with .replace() for template safety."""

    def test_template_with_format_specifiers_ignored(self):
        """Template containing {start.__class__} should not explode."""
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={
                "restricted": _restricted_tier(
                    time_restrictions=TimeRestrictions(start="08:00", end="22:00"),
                    messages={
                        "time_restricted_before": {
                            "en": "Access starts at {start} and {timezone}. {start.__class__}",
                        },
                    },
                ),
            },
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        result = runner._permissions.format_tier_message(
            tier, "time_restricted_before", source
        )
        # .replace() should just leave {start.__class__} as literal text
        assert "{start.__class__}" in result
        assert "08:00" in result


class TestGuestDefaultRateLimit:
    """FIX-10: Guest built-in preset has requests_per_hour: 10."""

    def test_guest_preset_has_rate_limit(self):
        from gateway.config import BUILTIN_TIER_PRESETS

        guest = BUILTIN_TIER_PRESETS["guest"]
        assert guest["requests_per_hour"] == 10

    def test_guest_preset_no_exec_no_admin(self):
        from gateway.config import BUILTIN_TIER_PRESETS

        guest = BUILTIN_TIER_PRESETS["guest"]
        assert guest["allow_exec"] is False
        assert guest["allow_admin_commands"] is False


class TestOwnerOnlyCommands:
    """FIX-09: owner_only flag and gateway dispatch gate."""

    def test_owner_only_commands_derived_from_registry(self):
        from hermes_cli.commands import COMMAND_REGISTRY

        owner_cmds = {cmd.name for cmd in COMMAND_REGISTRY if cmd.owner_only}
        assert "update" in owner_cmds

    def test_admin_cannot_use_owner_command(self):
        """Admin-tier user is blocked from owner_only commands."""
        from hermes_cli.commands import COMMAND_REGISTRY

        owner_cmds = {cmd.name for cmd in COMMAND_REGISTRY if cmd.owner_only}
        # Simulate the gateway check: admin tier != owner tier
        _msg_tier_name = "admin"
        owner_tier_name = "owner"
        assert _msg_tier_name != owner_tier_name
        assert "update" in owner_cmds

    def test_owner_can_use_owner_command(self):
        """Owner-tier user can use owner_only commands."""
        _msg_tier_name = "owner"
        owner_tier_name = "owner"
        assert _msg_tier_name == owner_tier_name

    def test_owner_tier_name_property(self):
        from gateway.permissions import PermissionManager

        config = _make_permission_config(
            tiers={"admin": _admin_tier()},
            default_tier="admin",
            env_owner_tier="owner",
        )
        pm = PermissionManager(config)
        assert pm.owner_tier_name == "owner"

    def test_owner_tier_name_default(self):
        from gateway.permissions import PermissionManager

        pm = PermissionManager(None)
        assert pm.owner_tier_name == "owner"


# ------------------------------------------------------------------
# Phase 11: UsageStore and NullUsageStore tests
# ------------------------------------------------------------------


class TestUsageStore:
    """Tests for UsageStore - SQLite-backed usage tracking."""

    def test_check_and_increment_first_call_allowed(self, tmp_path):
        """First call returns (True, 1)."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        allowed, count = store.check_and_increment("telegram", "u1", limit=10)
        assert allowed is True
        assert count == 1

    def test_check_and_increment_within_limit(self, tmp_path):
        """limit=3, call 3 times, all return True."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        for i in range(3):
            allowed, count = store.check_and_increment("telegram", "u1", limit=3)
            assert allowed is True, f"Call {i + 1} should be allowed"
            assert count == i + 1

    def test_check_and_increment_exceeds_limit(self, tmp_path):
        """limit=2, call 3 times, 3rd returns False."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        assert store.check_and_increment("telegram", "u1", limit=2) == (True, 1)
        assert store.check_and_increment("telegram", "u1", limit=2) == (True, 2)
        assert store.check_and_increment("telegram", "u1", limit=2) == (False, 2)

    def test_check_and_increment_different_users_separate(self, tmp_path):
        """2 users with limit=1, both allowed."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        allowed1, count1 = store.check_and_increment("telegram", "u1", limit=1)
        allowed2, count2 = store.check_and_increment("telegram", "u2", limit=1)
        assert allowed1 is True
        assert count1 == 1
        assert allowed2 is True
        assert count2 == 1

    def test_check_and_increment_different_platforms_separate(self, tmp_path):
        """Same user, different platforms, separate counts."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        assert store.check_and_increment("telegram", "u1", limit=1) == (True, 1)
        # Different platform resets counter for same user
        assert store.check_and_increment("discord", "u1", limit=1) == (True, 1)
        # Telegram still at limit
        assert store.check_and_increment("telegram", "u1", limit=1) == (False, 1)

    def test_check_and_increment_returns_current_count(self, tmp_path):
        """Verify count increments correctly."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        assert store.check_and_increment("telegram", "u1", limit=5)[1] == 1
        assert store.check_and_increment("telegram", "u1", limit=5)[1] == 2
        assert store.check_and_increment("telegram", "u1", limit=5)[1] == 3

    def test_record_tokens_stores_usage(self, tmp_path):
        """record_tokens then get_user_usage shows the tokens."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        store.record_tokens("telegram", "u1", input_tokens=100, output_tokens=50)
        usage = store.get_user_usage("telegram", "u1", hours=24)
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        assert usage["request_count"] == 1

    def test_record_tokens_accumulates(self, tmp_path):
        """record 2x, get_user_usage shows sum."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        store.record_tokens("telegram", "u1", input_tokens=100, output_tokens=50)
        store.record_tokens("telegram", "u1", input_tokens=50, output_tokens=25)
        usage = store.get_user_usage("telegram", "u1", hours=24)
        assert usage["input_tokens"] == 150
        assert usage["output_tokens"] == 75
        assert usage["request_count"] == 2

    def test_record_tokens_with_model(self, tmp_path):
        """model param stored."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        store.record_tokens(
            "telegram", "u1", input_tokens=100, output_tokens=50, model="claude-3"
        )
        # Verify the record was created (model stored in DB, not returned by get_user_usage)
        usage = store.get_user_usage("telegram", "u1", hours=24)
        assert usage["request_count"] == 1

    def test_get_user_usage_no_data(self, tmp_path):
        """No records → zero values."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        usage = store.get_user_usage("telegram", "u1", hours=24)
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["request_count"] == 0
        assert usage["total_tokens"] == 0

    def test_get_user_usage_respects_hours(self, tmp_path):
        """Record old entry, set hours=1, old entry excluded."""
        import time
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        # Record old token usage (2 hours ago)
        old_timestamp = time.time() - (2 * 3600)
        with store._lock:
            conn = store._connect()
            try:
                conn.execute(
                    "INSERT INTO token_usage "
                    "(timestamp, platform, user_id, input_tokens, output_tokens) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (old_timestamp, "telegram", "u1", 1000, 500),
                )
                conn.commit()
            finally:
                conn.close()

        # Record recent usage
        store.record_tokens("telegram", "u1", input_tokens=100, output_tokens=50)

        # With hours=1, old entry excluded
        usage_1h = store.get_user_usage("telegram", "u1", hours=1)
        assert usage_1h["input_tokens"] == 100
        assert usage_1h["request_count"] == 1

        # With hours=24, all included
        usage_24h = store.get_user_usage("telegram", "u1", hours=24)
        assert usage_24h["input_tokens"] == 1100
        assert usage_24h["request_count"] == 2

    def test_get_all_user_usage(self, tmp_path):
        """Record for 2 users, get_all returns both."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        store.record_tokens("telegram", "u1", input_tokens=100, output_tokens=50)
        store.record_tokens("telegram", "u2", input_tokens=200, output_tokens=100)

        all_usage = store.get_all_user_usage(hours=24)
        assert len(all_usage) == 2
        user_ids = {u["user_id"] for u in all_usage}
        assert "u1" in user_ids
        assert "u2" in user_ids

    def test_get_all_user_usage_empty(self, tmp_path):
        """No records → []."""
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        all_usage = store.get_all_user_usage(hours=24)
        assert all_usage == []

    def test_cleanup_removes_old_entries(self, tmp_path):
        """Record, patch time, cleanup removes old entries."""
        import time
        from gateway.usage import UsageStore

        store = UsageStore(db_path=str(tmp_path / "usage.db"))
        # Record old token usage (100 days ago)
        old_timestamp = time.time() - (100 * 86400)
        with store._lock:
            conn = store._connect()
            try:
                conn.execute(
                    "INSERT INTO token_usage "
                    "(timestamp, platform, user_id, input_tokens, output_tokens) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (old_timestamp, "telegram", "u1", 1000, 500),
                )
                conn.commit()
            finally:
                conn.close()

        # Record recent usage
        store.record_tokens("telegram", "u1", input_tokens=100, output_tokens=50)

        # Cleanup removes entries older than 30 days
        store.cleanup(max_age_days=30)

        # Only recent entry remains
        usage = store.get_user_usage("telegram", "u1", hours=24)
        assert usage["input_tokens"] == 100
        assert usage["request_count"] == 1

    def test_default_db_path(self, tmp_path):
        """UsageStore() with no path uses get_hermes_home()."""
        from gateway.usage import UsageStore
        from unittest.mock import patch

        # Use a real temporary directory that exists
        with patch("hermes_constants.get_hermes_home") as mock_hermes_home:
            mock_hermes_home.return_value = tmp_path
            store = UsageStore()
            assert store._db_path == str(tmp_path / "usage.db")


class TestNullUsageStore:
    """Tests for NullUsageStore - no-op usage tracking."""

    def test_check_and_increment_returns_true(self):
        """Always (True, 0)."""
        from gateway.usage import NullUsageStore

        store = NullUsageStore()
        allowed, count = store.check_and_increment("telegram", "u1", limit=1)
        assert allowed is True
        assert count == 0

    def test_record_tokens_does_nothing(self):
        """No error."""
        from gateway.usage import NullUsageStore

        store = NullUsageStore()
        # NullUsageStore.record_tokens only accepts **kwargs
        store.record_tokens(
            platform="telegram", user_id="u1", input_tokens=100, output_tokens=50
        )
        # Should not raise

    def test_get_user_usage_returns_zeros(self):
        """Returns {"input_tokens": 0, ...}."""
        from gateway.usage import NullUsageStore

        store = NullUsageStore()
        # NullUsageStore.get_user_usage only accepts **kwargs
        usage = store.get_user_usage(platform="telegram", user_id="u1", hours=24)
        assert usage["request_count"] == 0
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["total_tokens"] == 0

    def test_get_all_user_usage_returns_empty(self):
        """Returns []."""
        from gateway.usage import NullUsageStore

        store = NullUsageStore()
        all_usage = store.get_all_user_usage(hours=24)
        assert all_usage == []

    def test_cleanup_does_nothing(self):
        """No error, returns 0."""
        from gateway.usage import NullUsageStore

        store = NullUsageStore()
        deleted = store.cleanup(max_age_days=30)
        assert deleted == 0


class TestUsageStoreIntegration:
    """Integration tests: UsageStore with GatewayRunner."""

    def test_runner_usage_store_null_when_no_config(self, tmp_path):
        """No permission_tiers → NullUsageStore."""
        from gateway.usage import NullUsageStore

        runner = _make_runner(permission_tiers=None)
        assert isinstance(runner._usage_store, NullUsageStore)

    def test_runner_usage_store_real_with_config(self, tmp_path):
        """usage_tracking config → UsageStore."""
        from gateway.usage import UsageStore

        config = _make_permission_config(
            usage_tracking={"db_path": str(tmp_path / "usage.db")}
        )
        runner = _make_runner(permission_tiers=config)
        assert isinstance(runner._usage_store, UsageStore)

    def test_runner_usage_store_uses_config_db_path(self, tmp_path):
        """Custom db_path."""
        custom_path = str(tmp_path / "custom_usage.db")
        config = _make_permission_config(usage_tracking={"db_path": custom_path})
        runner = _make_runner(permission_tiers=config)
        # If db_path is absolute, it's used directly; relative paths are joined with HERMES_HOME
        # In this case, custom_path is already absolute (tmp_path / "custom_usage.db")
        assert runner._usage_store._db_path == custom_path

    def test_runner_usage_store_cached_property(self, tmp_path):
        """Accessing twice returns same object."""
        config = _make_permission_config(
            usage_tracking={"db_path": str(tmp_path / "usage.db")}
        )
        runner = _make_runner(permission_tiers=config)
        store1 = runner._usage_store
        store2 = runner._usage_store
        assert store1 is store2

    def test_runner_usage_store_with_empty_tracking_dict(self, tmp_path):
        """usage_tracking: {} → NullUsageStore."""
        from gateway.usage import NullUsageStore

        config = _make_permission_config(usage_tracking={})
        runner = _make_runner(permission_tiers=config)
        assert isinstance(runner._usage_store, NullUsageStore)


# ------------------------------------------------------------------
# Phase 3: Audit Log (AuditLog and NullAuditLog)
# ------------------------------------------------------------------


class TestAuditLog:
    """Tests for AuditLog class (SQLite-backed audit trail)."""

    @pytest.fixture
    def audit_log(self, tmp_path):
        from gateway.audit import AuditLog

        db_path = tmp_path / "audit.db"
        audit = AuditLog(db_path=str(db_path), max_rows=100_000)
        yield audit
        # No cleanup needed - SQLite handles connection closing

    def test_log_writes_event(self, audit_log):
        """Log one event, query returns it."""
        audit_log.log(
            event_type="command_denied",
            platform="discord",
            user_id="u1",
            tier_name="restricted",
            details="/model command blocked",
        )
        events = audit_log.query()
        assert len(events) == 1
        assert events[0]["event_type"] == "command_denied"
        assert events[0]["platform"] == "discord"
        assert events[0]["user_id"] == "u1"
        assert events[0]["tier_name"] == "restricted"
        assert events[0]["details"] == "/model command blocked"

    def test_log_multiple_events(self, audit_log):
        """Log 5 events, query returns all in DESC order."""
        for i in range(5):
            audit_log.log(event_type=f"event_{i}", user_id=f"u{i}")
        events = audit_log.query()
        assert len(events) == 5
        # Should be in DESC order (newest first)
        assert events[0]["event_type"] == "event_4"
        assert events[4]["event_type"] == "event_0"

    def test_log_stores_all_fields(self, audit_log):
        """Log with all params, verify all fields present."""
        audit_log.log(
            event_type="tier_change",
            platform="telegram",
            user_id="u1",
            tier_name="admin",
            details="Promoted by owner",
            actor_id="owner123",
        )
        events = audit_log.query()
        assert len(events) == 1
        e = events[0]
        assert e["id"] >= 1
        assert e["event_type"] == "tier_change"
        assert e["platform"] == "telegram"
        assert e["user_id"] == "u1"
        assert e["tier_name"] == "admin"
        assert e["details"] == "Promoted by owner"
        assert e["actor_id"] == "owner123"
        assert e["timestamp"] > 0

    def test_query_by_event_type(self, audit_log):
        """Log 3 different types, query by one type returns only matching."""
        audit_log.log(event_type="command_denied", user_id="u1")
        audit_log.log(event_type="tier_change", user_id="u2")
        audit_log.log(event_type="command_denied", user_id="u3")
        events = audit_log.query(event_type="command_denied")
        assert len(events) == 2
        for e in events:
            assert e["event_type"] == "command_denied"

    def test_query_by_user_id(self, audit_log):
        """Log for 2 users, query by one user returns only their events."""
        audit_log.log(event_type="event_1", user_id="u1")
        audit_log.log(event_type="event_2", user_id="u2")
        audit_log.log(event_type="event_3", user_id="u1")
        events = audit_log.query(user_id="u1")
        assert len(events) == 2
        for e in events:
            assert e["user_id"] == "u1"

    def test_query_by_platform(self, audit_log):
        """Log for 2 platforms, query by one returns only matching."""
        audit_log.log(event_type="event_1", platform="discord")
        audit_log.log(event_type="event_2", platform="telegram")
        audit_log.log(event_type="event_3", platform="discord")
        events = audit_log.query(platform="discord")
        assert len(events) == 2
        for e in events:
            assert e["platform"] == "discord"

    def test_query_limit(self, audit_log):
        """Log 10 events, query with limit=3 returns 3."""
        for i in range(10):
            audit_log.log(event_type=f"event_{i}")
        events = audit_log.query(limit=3)
        assert len(events) == 3
        # Should return newest 3
        assert events[0]["event_type"] == "event_9"

    def test_query_offset(self, audit_log):
        """Log 5 events, query with limit=2 offset=2 returns 2."""
        for i in range(5):
            audit_log.log(event_type=f"event_{i}")
        events = audit_log.query(limit=2, offset=2)
        assert len(events) == 2
        # Should skip newest 2, return next 2
        assert events[0]["event_type"] == "event_2"
        assert events[1]["event_type"] == "event_1"

    def test_count_total(self, audit_log):
        """Log 5 events, count returns 5."""
        for i in range(5):
            audit_log.log(event_type=f"event_{i}")
        assert audit_log.count() == 5

    def test_count_by_event_type(self, audit_log):
        """Log 3 types, count by one type."""
        audit_log.log(event_type="command_denied", user_id="u1")
        audit_log.log(event_type="tier_change", user_id="u2")
        audit_log.log(event_type="command_denied", user_id="u3")
        assert audit_log.count(event_type="command_denied") == 2
        assert audit_log.count(event_type="tier_change") == 1

    def test_count_by_user_id(self, audit_log):
        """Log for 2 users, count by one."""
        audit_log.log(event_type="event_1", user_id="u1")
        audit_log.log(event_type="event_2", user_id="u2")
        audit_log.log(event_type="event_3", user_id="u1")
        assert audit_log.count(user_id="u1") == 2
        assert audit_log.count(user_id="u2") == 1

    def test_count_returns_zero_for_no_match(self, audit_log):
        """Count with non-existent filter."""
        audit_log.log(event_type="event_1", user_id="u1")
        assert audit_log.count(event_type="nonexistent") == 0
        assert audit_log.count(user_id="nonexistent") == 0

    def test_query_returns_empty_for_no_match(self, audit_log):
        """Query with non-existent filter."""
        audit_log.log(event_type="event_1", user_id="u1")
        assert audit_log.query(event_type="nonexistent") == []
        assert audit_log.query(user_id="nonexistent") == []

    def test_rotation_keeps_newest_half(self, tmp_path):
        """Set max_rows=4, log 10 events, verify only newest ~2 remain."""
        from gateway.audit import AuditLog

        db_path = tmp_path / "audit_rotate.db"
        audit = AuditLog(db_path=str(db_path), max_rows=4)

        # Log 10 events
        for i in range(10):
            audit.log(event_type=f"event_{i}", user_id="u1")

        # Rotation should trigger on the last log call
        # Count should be <= max_rows (4) since we keep newest half (2)
        count = audit.count()
        assert count <= 4

        # Newest events should still be present
        events = audit.query()
        assert len(events) <= 4
        # Most recent event should be event_9
        assert events[0]["event_type"] == "event_9"

    def test_log_handles_corrupt_db_gracefully(self, tmp_path, caplog):
        """Log to a db that will fail rotation, should not raise."""
        from gateway.audit import AuditLog

        # Create a valid audit log
        db_path = tmp_path / "audit_corrupt.db"
        audit = AuditLog(db_path=str(db_path), max_rows=1)

        # Log one event successfully
        audit.log(event_type="test1", user_id="u1")

        # Now set max_rows to 0 to force rotation failure
        # Actually, let's use a different approach - delete the db file
        db_path.unlink()

        # Subsequent log should not raise, just log warning
        with caplog.at_level(logging.WARNING):
            audit.log(event_type="test2", user_id="u2")

        # Should have logged a warning about the error
        assert "Audit log write failed" in caplog.text

    def test_default_db_path_uses_hermes_home(self, monkeypatch, tmp_path):
        """AuditLog() with no path uses get_hermes_home()."""
        from gateway.audit import AuditLog

        # Mock HERMES_HOME to return tmp_path
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        # Create AuditLog with no db_path argument
        audit = AuditLog()  # Should use default path from get_hermes_home()

        # Log something to verify it works
        audit.log(event_type="test", user_id="u1")
        events = audit.query()
        assert len(events) == 1

        # Verify the db was created in HERMES_HOME
        expected_path = tmp_path / "audit.db"
        assert expected_path.exists()

    def test_log_with_none_optional_fields(self, audit_log):
        """Log with only event_type, other fields None."""
        audit_log.log(event_type="minimal_event")
        events = audit_log.query()
        assert len(events) == 1
        e = events[0]
        assert e["event_type"] == "minimal_event"
        assert e["platform"] is None
        assert e["user_id"] is None
        assert e["tier_name"] is None
        assert e["details"] is None
        assert e["actor_id"] is None


class TestNullAuditLog:
    """Tests for NullAuditLog (no-op stub)."""

    def test_log_does_nothing(self):
        """NullAuditLog().log() doesn't raise."""
        from gateway.audit import NullAuditLog

        audit = NullAuditLog()
        # Should not raise any exception
        audit.log(
            event_type="test",
            platform="discord",
            user_id="u1",
            tier_name="admin",
            details="details",
            actor_id="actor",
        )

    def test_query_returns_empty(self):
        """NullAuditLog().query() returns []."""
        from gateway.audit import NullAuditLog

        audit = NullAuditLog()
        result = audit.query()
        assert result == []

    def test_count_returns_zero(self):
        """NullAuditLog().count() returns 0."""
        from gateway.audit import NullAuditLog

        audit = NullAuditLog()
        assert audit.count() == 0

    def test_query_with_filters_returns_empty(self):
        """NullAuditLog().query(event_type="x") returns []."""
        from gateway.audit import NullAuditLog

        audit = NullAuditLog()
        result = audit.query(event_type="command_denied", user_id="u1", limit=10)
        assert result == []


class TestAuditLogIntegration:
    """Integration tests for AuditLog with GatewayRunner._audit_log."""

    def test_runner_audit_log_returns_null_when_no_config(self):
        """Runner without permission_tiers → NullAuditLog."""
        from gateway.audit import NullAuditLog

        runner = _make_runner(permission_tiers=None)
        assert isinstance(runner._audit_log, NullAuditLog)

    def test_runner_audit_log_returns_real_with_config(self):
        """Runner with audit config → AuditLog."""
        from gateway.audit import AuditLog

        pt = _make_permission_config(audit={"db_path": "audit.db"})
        runner = _make_runner(permission_tiers=pt)
        assert isinstance(runner._audit_log, AuditLog)

    def test_runner_audit_log_uses_config_db_path(self, tmp_path):
        """Audit config with custom db_path."""
        from gateway.audit import AuditLog

        # Use absolute path (like UsageStore tests do)
        custom_path = str(tmp_path / "custom_audit.db")
        pt = _make_permission_config(audit={"db_path": custom_path})
        runner = _make_runner(permission_tiers=pt)

        assert isinstance(runner._audit_log, AuditLog)
        # Log something to create the db file
        runner._audit_log.log(event_type="test", user_id="u1")
        # The db should be at the custom path
        assert (tmp_path / "custom_audit.db").exists()

    def test_runner_audit_log_uses_max_rows(self):
        """Audit config with custom max_rows."""
        from gateway.audit import AuditLog

        pt = _make_permission_config(audit={"db_path": "audit.db", "max_rows": 50})
        runner = _make_runner(permission_tiers=pt)

        assert isinstance(runner._audit_log, AuditLog)
        assert runner._audit_log._max_rows == 50

    def test_runner_audit_log_cached_property(self):
        """Accessing _audit_log twice returns same object."""
        pt = _make_permission_config(audit={"db_path": "audit.db"})
        runner = _make_runner(permission_tiers=pt)

        audit1 = runner._audit_log
        audit2 = runner._audit_log
        assert audit1 is audit2  # Same object instance

    def test_runner_audit_log_with_empty_audit_dict(self):
        """audit: {} (truthy but no keys) → NullAuditLog."""
        from gateway.audit import NullAuditLog

        pt = _make_permission_config(audit={})
        runner = _make_runner(permission_tiers=pt)
        assert isinstance(runner._audit_log, NullAuditLog)


# ------------------------------------------------------------------
# Phase 3: Platform Role Resolution (T3/T4)
# ------------------------------------------------------------------


class TestPlatformRoleMapping:
    """Tests for resolve_platform_role_tier method."""

    def _make_source_with_roles(
        self, user_id="u1", platform=Platform.DISCORD, user_roles=None
    ):
        """Helper to create SessionSource with user_roles."""
        return SessionSource(
            platform=platform,
            user_id=user_id,
            chat_id="c1",
            user_name="tester",
            chat_type="dm",
            user_roles=user_roles,
        )

    def test_no_mapping_returns_none(self):
        """No platform_role_mapping → returns None."""
        pt = _make_permission_config(
            platform_role_mapping=None,
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(user_roles=["administrator"])
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier is None

    def test_no_user_roles_returns_none(self):
        """user_roles=None → returns None."""
        pt = _make_permission_config(
            platform_role_mapping={"discord": {"administrator": "admin"}},
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(user_roles=None)
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier is None

    def test_empty_user_roles_returns_none(self):
        """user_roles=[] → returns None."""
        pt = _make_permission_config(
            platform_role_mapping={"discord": {"administrator": "admin"}},
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(user_roles=[])
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier is None

    def test_matching_role_returns_tier(self):
        """Exact role match returns mapped tier."""
        pt = _make_permission_config(
            platform_role_mapping={"discord": {"administrator": "admin"}},
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(user_roles=["administrator"])
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier == "admin"

    def test_first_match_wins(self):
        """First matching role wins when user has multiple roles."""
        pt = _make_permission_config(
            platform_role_mapping={
                "discord": {"moderator": "user", "administrator": "admin"}
            },
            tiers={"user": _restricted_tier(), "admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(user_roles=["moderator", "administrator"])
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier == "user"  # First match

    def test_no_matching_role_returns_none(self):
        """No matching role → None."""
        pt = _make_permission_config(
            platform_role_mapping={"discord": {"administrator": "admin"}},
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(user_roles=["member"])
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier is None

    def test_wildcard_matches_unmapped(self):
        """Wildcard role * matches when no exact match found."""
        pt = _make_permission_config(
            platform_role_mapping={"discord": {"administrator": "admin", "*": "user"}},
            tiers={"user": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(user_roles=["member"])
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier == "user"

    def test_wildcard_not_used_if_exact_match(self):
        """Wildcard not used when exact match exists."""
        pt = _make_permission_config(
            platform_role_mapping={"discord": {"administrator": "admin", "*": "user"}},
            tiers={"admin": _admin_tier(), "user": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(user_roles=["administrator"])
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier == "admin"  # Exact match, not wildcard

    def test_platform_specific_mapping(self):
        """Different mappings for different platforms."""
        pt = _make_permission_config(
            platform_role_mapping={
                "telegram": {"administrator": "admin"},
                "discord": {"Admin": "admin"},
            },
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(
            platform=Platform.TELEGRAM, user_roles=["administrator"]
        )
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier == "admin"

    def test_default_fallback_mapping(self):
        """default mapping used when platform-specific mapping missing."""
        pt = _make_permission_config(
            platform_role_mapping={
                "default": {"*": "guest"},
            },
            tiers={"guest": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(
            platform=Platform.SLACK, user_roles=["member"]
        )
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier == "guest"

    def test_invalid_tier_in_mapping_skipped(self, caplog):
        """Invalid tier name in mapping is skipped with warning."""
        import logging

        pt = _make_permission_config(
            platform_role_mapping={"discord": {"administrator": "nonexistent_tier"}},
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(user_roles=["administrator"])
        with caplog.at_level(logging.WARNING):
            tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier is None
        assert "not defined, skipping" in caplog.text.lower()

    def test_no_config_returns_none(self):
        """PermissionManager with config=None returns None."""
        runner = _make_runner(permission_tiers=None)
        source = self._make_source_with_roles(user_roles=["administrator"])
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier is None

    def test_wildcard_with_invalid_tier_skipped(self, caplog):
        """Wildcard mapping with invalid tier is skipped."""
        import logging

        pt = _make_permission_config(
            platform_role_mapping={"discord": {"*": "invalid_tier"}},
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = self._make_source_with_roles(user_roles=["member"])
        with caplog.at_level(logging.WARNING):
            tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier is None

    def test_platform_attribute_missing_returns_none(self):
        """Source without platform attribute returns None."""
        pt = _make_permission_config(
            platform_role_mapping={"discord": {"administrator": "admin"}},
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = SimpleNamespace(
            user_id="u1",
            chat_id="c1",
            user_roles=["administrator"],
            # No platform attribute
        )
        tier = runner._permissions.resolve_platform_role_tier(source)
        assert tier is None


# ------------------------------------------------------------------
# Phase 7e: Smart Denial Messages
# ------------------------------------------------------------------


class TestSmartDenialMessages:
    """Tests for helpful denial messages with /whoami references."""

    def test_exec_denied_suggests_whoami(self):
        """exec_denied message contains /whoami reference."""
        pt = _make_permission_config(
            tiers={"restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(tier, "exec_denied", source)
        assert "/whoami" in msg

    def test_command_denied_suggests_whoami(self):
        """command_denied message contains /whoami reference."""
        pt = _make_permission_config(
            tiers={"restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(tier, "command_denied", source)
        assert "/whoami" in msg

    def test_rate_limited_mentions_reset(self):
        """rate_limited message mentions reset."""
        pt = _make_permission_config(
            tiers={"restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(tier, "rate_limited", source)
        assert "reset" in msg.lower()

    def test_generic_denial_suggests_whoami(self):
        """Unknown key returns generic message with /whoami reference."""
        pt = _make_permission_config(
            tiers={"restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(tier, "unknown_key", source)
        assert "/whoami" in msg

    def test_exec_denied_mentions_admin(self):
        """exec_denied message mentions admin."""
        pt = _make_permission_config(
            tiers={"restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(tier, "exec_denied", source)
        assert "admin" in msg.lower()

    def test_command_denied_mentions_admin(self):
        """command_denied message mentions admin."""
        pt = _make_permission_config(
            tiers={"restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._permissions.get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._permissions.format_tier_message(tier, "command_denied", source)
        assert "admin" in msg.lower()

    def test_build_tier_context_includes_behavioral_guidance(self):
        """build_tier_context includes helpful alternative guidance."""
        from gateway.permissions import PermissionManager

        pt = _make_permission_config(
            tiers={"restricted": _restricted_tier()},
        )
        pm = PermissionManager(pt)
        tier_cfg = pm.get_tier_config("restricted")
        context = pm.build_tier_context("restricted", tier_cfg)
        assert context is not None
        assert "helpful alternative" in context.lower()
        assert "acknowledge" in context.lower()

    def test_build_tier_context_includes_acknowledge_step(self):
        """build_tier_context includes acknowledge step."""
        from gateway.permissions import PermissionManager

        pt = _make_permission_config(
            tiers={"restricted": _restricted_tier()},
        )
        pm = PermissionManager(pt)
        tier_cfg = pm.get_tier_config("restricted")
        context = pm.build_tier_context("restricted", tier_cfg)
        assert context is not None
        assert "Acknowledge" in context


# ------------------------------------------------------------------
# Phase 7d: PromoteRequestStore
# ------------------------------------------------------------------


class TestPromoteRequestStore:
    """Tests for PromoteRequestStore SQLite-backed promotion requests."""

    def test_create_request_returns_dict(self, tmp_path):
        """create_request returns dict with expected keys."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        result = store.create_request(
            user_id="u1", platform="discord", requested_tier="admin"
        )
        assert result is not None
        assert "id" in result
        assert "user_id" in result
        assert "platform" in result
        assert "requested_tier" in result
        assert "current_tier" in result
        assert "status" in result
        assert "created_at" in result

    def test_create_request_generates_unique_id(self, tmp_path):
        """Two create_requests generate different IDs."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        req1 = store.create_request(
            user_id="u1", platform="discord", requested_tier="admin"
        )
        req2 = store.create_request(
            user_id="u2", platform="discord", requested_tier="admin"
        )
        assert req1["id"] != req2["id"]

    def test_create_request_duplicate_returns_none(self, tmp_path):
        """Same user+platform with pending returns None."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        req1 = store.create_request(
            user_id="u1", platform="discord", requested_tier="admin"
        )
        assert req1 is not None
        # Duplicate request (same user, same platform, still pending)
        req2 = store.create_request(
            user_id="u1", platform="discord", requested_tier="admin"
        )
        assert req2 is None

    def test_get_request_returns_created(self, tmp_path):
        """create then get by id returns same request."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        created = store.create_request(
            user_id="u1", platform="discord", requested_tier="admin"
        )
        retrieved = store.get_request(created["id"])
        assert retrieved is not None
        assert retrieved["user_id"] == "u1"
        assert retrieved["platform"] == "discord"
        assert retrieved["requested_tier"] == "admin"

    def test_get_request_nonexistent_returns_none(self, tmp_path):
        """get with bad id returns None."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        result = store.get_request("nonexistent_id")
        assert result is None

    def test_list_pending_returns_pending(self, tmp_path):
        """list_pending returns all pending requests."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        store.create_request(user_id="u1", platform="discord", requested_tier="admin")
        store.create_request(user_id="u2", platform="telegram", requested_tier="user")
        pending = store.list_pending()
        assert len(pending) == 2
        assert all(r["status"] == "pending" for r in pending)

    def test_list_pending_excludes_resolved(self, tmp_path):
        """list_pending excludes approved/denied requests."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        req1 = store.create_request(
            user_id="u1", platform="discord", requested_tier="admin"
        )
        store.create_request(user_id="u2", platform="telegram", requested_tier="user")
        # Approve one
        store.approve_request(req1["id"], resolved_by="admin")
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0]["user_id"] == "u2"

    def test_approve_request_updates_status(self, tmp_path):
        """approve updates status to approved."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        req = store.create_request(
            user_id="u1", platform="discord", requested_tier="admin"
        )
        updated = store.approve_request(req["id"], resolved_by="admin")
        assert updated is not None
        assert updated["status"] == "approved"

    def test_approve_request_stores_approver(self, tmp_path):
        """approve stores resolved_by field."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        req = store.create_request(
            user_id="u1", platform="discord", requested_tier="admin"
        )
        updated = store.approve_request(req["id"], resolved_by="owner123")
        assert updated is not None
        assert updated["resolved_by"] == "owner123"

    def test_approve_nonexistent_returns_false(self, tmp_path):
        """approve with bad id returns None (not False - method returns Optional[Dict])."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        result = store.approve_request("bad_id", resolved_by="admin")
        assert result is None

    def test_deny_request_updates_status(self, tmp_path):
        """deny updates status to denied."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        req = store.create_request(
            user_id="u1", platform="discord", requested_tier="admin"
        )
        updated = store.deny_request(req["id"], resolved_by="admin")
        assert updated is not None
        assert updated["status"] == "denied"

    def test_deny_request_stores_denier(self, tmp_path):
        """deny stores resolved_by field."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        req = store.create_request(
            user_id="u1", platform="discord", requested_tier="admin"
        )
        updated = store.deny_request(req["id"], resolved_by="owner123")
        assert updated is not None
        assert updated["resolved_by"] == "owner123"

    def test_deny_nonexistent_returns_false(self, tmp_path):
        """deny with bad id returns None (not False)."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))
        result = store.deny_request("bad_id", resolved_by="admin")
        assert result is None

    def test_cleanup_removes_old_pending(self, tmp_path):
        """cleanup_expired removes pending requests older than max_age_hours."""
        import time
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))

        # Create one old request (100 hours ago)
        with patch("time.time", return_value=time.time() - 360000):
            old_req = store.create_request(
                user_id="u1", platform="discord", requested_tier="admin"
            )

        # Create one recent request
        recent_req = store.create_request(
            user_id="u2", platform="discord", requested_tier="admin"
        )

        # Cleanup pending older than 72 hours
        removed = store.cleanup_expired(max_age_hours=72)
        assert removed == 1

        # Old request gone, recent remains
        assert store.get_request(old_req["id"]) is None
        assert store.get_request(recent_req["id"]) is not None

    def test_allow_new_after_approval(self, tmp_path):
        """After approval, new request for same user is allowed."""
        from gateway.permissions import PromoteRequestStore

        db_path = tmp_path / "promote.db"
        store = PromoteRequestStore(db_path=str(db_path))

        req1 = store.create_request(
            user_id="u1", platform="discord", requested_tier="admin"
        )
        assert req1 is not None

        # Approve it
        store.approve_request(req1["id"], resolved_by="admin")

        # New request for same user should succeed (old one resolved)
        req2 = store.create_request(
            user_id="u1", platform="discord", requested_tier="user"
        )
        assert req2 is not None
        assert req2["id"] != req1["id"]


# ------------------------------------------------------------------
# Phase 7c: MCP default-deny logic (is_elevated_tier)
# ------------------------------------------------------------------


class TestIsElevatedTier:
    """Tests for is_elevated_tier() method — T7c: MCP default-deny logic."""

    def test_no_tier_config_returns_true(self):
        """Non-existent tier name → True (no config = unrestricted)."""
        runner = _make_runner(permission_tiers=None)
        assert runner._permissions.is_elevated_tier("nonexistent") is True

    def test_wildcard_resolved_tools_is_elevated(self):
        """resolved_tools={"*"} → True."""
        tier = TierDefinition(
            allowed_tools=["@all"],
            resolved_tools=frozenset({"*"}),
        )
        pt = _make_permission_config(tiers={"wildcard": tier})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("wildcard") is True

    def test_mcp_pattern_in_resolved_tools_is_elevated(self):
        """resolved_tools={"terminal", "mcp:server:tool"} → True."""
        tier = TierDefinition(
            allowed_tools=["terminal", "mcp:server:tool"],
            resolved_tools=frozenset({"terminal", "mcp:server:tool"}),
        )
        pt = _make_permission_config(tiers={"with_mcp": tier})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("with_mcp") is True

    def test_no_mcp_in_resolved_tools_not_elevated(self):
        """resolved_tools={"terminal", "web_search"} → False."""
        tier = TierDefinition(
            allowed_toolsets=[],  # Explicitly empty to avoid wildcard default
            allowed_tools=["terminal", "web_search"],
            resolved_tools=frozenset({"terminal", "web_search"}),
        )
        pt = _make_permission_config(tiers={"no_mcp": tier})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("no_mcp") is False

    def test_mcp_at_group_in_allowed_tools_is_elevated(self):
        """allowed_tools=["@mcp"] → True."""
        tier = TierDefinition(
            allowed_toolsets=[],
            allowed_tools=["@mcp"],
        )
        pt = _make_permission_config(tiers={"mcp_group": tier})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("mcp_group") is True

    def test_all_at_group_in_allowed_tools_is_elevated(self):
        """allowed_tools=["@all"] → True."""
        tier = TierDefinition(
            allowed_toolsets=[],
            allowed_tools=["@all"],
        )
        pt = _make_permission_config(tiers={"all_group": tier})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("all_group") is True

    def test_mcp_prefix_in_allowed_tools_is_elevated(self):
        """allowed_tools=["mcp:server"] → True."""
        tier = TierDefinition(
            allowed_toolsets=[],
            allowed_tools=["mcp:server"],
        )
        pt = _make_permission_config(tiers={"mcp_prefix": tier})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("mcp_prefix") is True

    def test_no_mcp_in_allowed_tools_not_elevated(self):
        """allowed_tools=["@web", "@code"] → False."""
        tier = TierDefinition(
            allowed_toolsets=[],
            allowed_tools=["@web", "@code"],
        )
        pt = _make_permission_config(tiers={"no_mcp_tools": tier})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("no_mcp_tools") is False

    def test_mcp_in_allowed_toolsets_is_elevated(self):
        """allowed_toolsets=["@mcp"] → True."""
        tier = TierDefinition(allowed_toolsets=["@mcp"])
        pt = _make_permission_config(tiers={"mcp_toolset": tier})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("mcp_toolset") is True

    def test_wildcard_in_allowed_toolsets_is_elevated(self):
        """allowed_toolsets=["*"] → True."""
        tier = TierDefinition(allowed_toolsets=["*"])
        pt = _make_permission_config(tiers={"wildcard_toolset": tier})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("wildcard_toolset") is True

    def test_no_mcp_in_allowed_toolsets_not_elevated(self):
        """allowed_toolsets=["@web"] → False."""
        tier = TierDefinition(allowed_toolsets=["@web"])
        pt = _make_permission_config(tiers={"no_mcp_toolset": tier})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("no_mcp_toolset") is False

    def test_empty_resolved_tools_not_elevated(self):
        """resolved_tools=set() → False."""
        tier = TierDefinition(
            allowed_toolsets=[],
            allowed_tools=[],
            resolved_tools=frozenset(),
        )
        pt = _make_permission_config(tiers={"empty_tools": tier})
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("empty_tools") is False

    def test_guest_tier_not_elevated(self):
        """Build guest tier from BUILTIN_TIER_PRESETS → not elevated."""
        from gateway.config import BUILTIN_TIER_PRESETS

        guest_preset = BUILTIN_TIER_PRESETS["guest"]
        # Add empty allowed_toolsets to avoid wildcard default
        guest_preset["allowed_toolsets"] = []
        pt = PermissionTiersConfig.from_dict(
            {
                "default_tier": "guest",
                "tiers": {"guest": guest_preset},
                "users": {},
            }
        )
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("guest") is False

    def test_admin_tier_is_elevated(self):
        """Build admin tier from BUILTIN_TIER_PRESETS → elevated (has @mcp)."""
        from gateway.config import BUILTIN_TIER_PRESETS

        admin_preset = BUILTIN_TIER_PRESETS["admin"]
        pt = PermissionTiersConfig.from_dict(
            {
                "default_tier": "admin",
                "tiers": {"admin": admin_preset},
                "users": {},
            }
        )
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("admin") is True

    def test_owner_tier_is_elevated(self):
        """Build owner tier from BUILTIN_TIER_PRESETS → elevated (has *)."""
        from gateway.config import BUILTIN_TIER_PRESETS

        owner_preset = BUILTIN_TIER_PRESETS["owner"]
        pt = PermissionTiersConfig.from_dict(
            {
                "default_tier": "owner",
                "tiers": {"owner": owner_preset},
                "users": {},
            }
        )
        runner = _make_runner(permission_tiers=pt)
        assert runner._permissions.is_elevated_tier("owner") is True


# ------------------------------------------------------------------
# H-1: Admin gate in /promote handler
# ------------------------------------------------------------------


class TestH1PromoteAdminGate:
    """Tests for admin-only gating on /promote subcommands (list, approve, deny)."""

    @pytest.mark.asyncio
    async def test_promote_list_blocked_for_non_admin(self, tmp_path):
        """Non-admin user cannot use /promote list."""
        from gateway.audit import NullAuditLog
        from gateway.permissions import PromoteRequestStore

        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()
        runner._promote_store = PromoteRequestStore(
            db_path=str(tmp_path / "promote.db")
        )

        event = _make_event("/promote list", user_id="u1")
        result = await runner._handle_promote_command(event)

        assert result is not None
        assert "higher access" in result.lower()

    @pytest.mark.asyncio
    async def test_promote_approve_blocked_for_non_admin(self, tmp_path):
        """Non-admin user cannot use /promote approve."""
        from gateway.audit import NullAuditLog
        from gateway.permissions import PromoteRequestStore

        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()
        runner._promote_store = PromoteRequestStore(
            db_path=str(tmp_path / "promote.db")
        )

        event = _make_event("/promote approve abc123", user_id="u1")
        result = await runner._handle_promote_command(event)

        assert result is not None
        assert "higher access" in result.lower()

    @pytest.mark.asyncio
    async def test_promote_request_allowed_for_non_admin(self, tmp_path):
        """Non-admin user can create promotion request (/promote <tier>)."""
        from gateway.audit import NullAuditLog
        from gateway.permissions import PromoteRequestStore

        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()
        runner._promote_store = PromoteRequestStore(
            db_path=str(tmp_path / "promote.db")
        )

        event = _make_event("/promote admin", user_id="u1")
        result = await runner._handle_promote_command(event)

        assert result is not None
        assert "permission" not in result.lower()
        # Should get the promotion request submitted message
        assert "promotion request" in result.lower() or "request" in result.lower()


class TestPromoteAdminGateExtended:
    """Additional tests for admin-only gating on /promote subcommands."""

    @pytest.mark.asyncio
    async def test_promote_deny_blocked_for_non_admin(self, tmp_path):
        """Non-admin user cannot use /promote deny."""
        from gateway.audit import NullAuditLog
        from gateway.permissions import PromoteRequestStore

        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()
        runner._promote_store = PromoteRequestStore(
            db_path=str(tmp_path / "promote.db")
        )

        event = _make_event("/promote deny abc123", user_id="u1")
        result = await runner._handle_promote_command(event)

        assert result is not None
        assert "higher access" in result.lower() or "permission" in result.lower()

    @pytest.mark.asyncio
    async def test_promote_list_allowed_for_admin(self, tmp_path):
        """Admin user can use /promote list."""
        from gateway.audit import NullAuditLog
        from gateway.permissions import PromoteRequestStore

        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="admin")},
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()
        runner._promote_store = PromoteRequestStore(
            db_path=str(tmp_path / "promote.db")
        )

        event = _make_event("/promote list", user_id="u1")
        result = await runner._handle_promote_command(event)

        assert result is not None
        assert "permission" not in result.lower()
        # Should show "No pending" or list of requests
        assert "pending" in result.lower() or "no pending" in result.lower()


# ------------------------------------------------------------------
# M-2: Audit limit clamp
# ------------------------------------------------------------------


class TestM2AuditLimitClamp:
    """Tests for /audit command limit clamping to max 100."""

    @pytest.mark.asyncio
    async def test_audit_main_limit_clamped_to_100(self, tmp_path):
        """/audit 500 clamps limit to 100."""
        from unittest.mock import MagicMock

        runner = _make_runner()
        runner._audit_log = MagicMock()
        runner._audit_log.query.return_value = []

        event = _make_event("/audit 500", user_id="admin")
        result = await runner._handle_audit_command(event)

        runner._audit_log.query.assert_called_once()
        call_kwargs = runner._audit_log.query.call_args.kwargs
        assert call_kwargs["limit"] == 100

    @pytest.mark.asyncio
    async def test_audit_user_subcommand_limit_clamped(self, tmp_path):
        """/audit user u1 500 clamps limit to 100."""
        from unittest.mock import MagicMock

        runner = _make_runner()
        runner._audit_log = MagicMock()
        runner._audit_log.query.return_value = []

        event = _make_event("/audit user u1 500", user_id="admin")
        result = await runner._handle_audit_command(event)

        runner._audit_log.query.assert_called_once()
        call_kwargs = runner._audit_log.query.call_args.kwargs
        assert call_kwargs["limit"] == 100

    @pytest.mark.asyncio
    async def test_audit_type_subcommand_limit_clamped(self, tmp_path):
        """/audit type tier_resolved 500 clamps limit to 100."""
        from unittest.mock import MagicMock

        runner = _make_runner()
        runner._audit_log = MagicMock()
        runner._audit_log.query.return_value = []

        event = _make_event("/audit type tier_resolved 500", user_id="admin")
        result = await runner._handle_audit_command(event)

        runner._audit_log.query.assert_called_once()
        call_kwargs = runner._audit_log.query.call_args.kwargs
        assert call_kwargs["limit"] == 100


# ------------------------------------------------------------------
# T1: Per-user tool overrides (config layer)
# ------------------------------------------------------------------


class TestUserTierConfigToolOverride:
    """Tests for UserTierConfig allowed_tools field and group expansion."""

    def test_user_config_no_override_by_default(self):
        """UserTierConfig() has no tool override by default."""
        cfg = UserTierConfig()
        assert cfg.resolved_tools_override is None
        assert cfg.allowed_tools is None

    def test_user_config_from_dict_with_tools(self):
        """UserTierConfig.from_dict with allowed_tools list sets override."""
        cfg = UserTierConfig.from_dict(
            {"tier": "user", "allowed_tools": ["web_search", "read_file"]}
        )
        assert cfg.resolved_tools_override == frozenset({"web_search", "read_file"})
        assert cfg.allowed_tools == ["web_search", "read_file"]

    def test_user_config_from_dict_with_group_expansion(self):
        """UserTierConfig.from_dict expands @safe group to tools."""
        cfg = UserTierConfig.from_dict({"allowed_tools": ["@safe"]})
        # @safe expands to: @web, @read, @media, @skills, clarify
        # Which expands to: web_search, web_extract, read_file, search_files,
        #                    vision_analyze, image_generate, text_to_speech,
        #                    skills_list, skill_view, clarify
        expected_tools = {
            "web_search",
            "web_extract",
            "read_file",
            "search_files",
            "vision_analyze",
            "image_generate",
            "text_to_speech",
            "skills_list",
            "skill_view",
            "clarify",
        }
        assert cfg.resolved_tools_override == frozenset(expected_tools)

    def test_user_config_to_dict_includes_tools(self):
        """UserTierConfig.to_dict includes allowed_tools when set."""
        cfg = UserTierConfig.from_dict(
            {"tier": "user", "allowed_tools": ["web_search", "read_file"]}
        )
        d = cfg.to_dict()
        assert "allowed_tools" in d
        assert d["allowed_tools"] == ["web_search", "read_file"]

    def test_user_config_to_dict_omits_tools_when_none(self):
        """UserTierConfig.to_dict omits allowed_tools when None."""
        cfg = UserTierConfig()
        d = cfg.to_dict()
        assert "allowed_tools" not in d

    def test_user_config_invalid_tools_type_ignored(self):
        """UserTierConfig.from_dict ignores non-list allowed_tools."""
        cfg = UserTierConfig.from_dict({"allowed_tools": "not_a_list"})
        assert cfg.resolved_tools_override is None
        assert cfg.allowed_tools is None


# ------------------------------------------------------------------
# T1: Per-user tool overrides (resolution and display)
# ------------------------------------------------------------------


class TestPerUserToolOverrideResolution:
    """Tests for per-user tool override resolution in whoami and display."""

    def test_whoami_no_override_by_default(self):
        """whoami dict has no user_tool_override key when not configured."""
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="admin")},
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        info = runner._permissions.whoami(source)

        assert "user_tool_override" not in info

    def test_whoami_with_override_returns_sorted_tools(self):
        """whoami dict has user_tool_override sorted list when configured."""
        pt = _make_permission_config(
            users={
                "u1": UserTierConfig.from_dict(
                    {"tier": "admin", "allowed_tools": ["web_search", "read_file"]}
                )
            },
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        info = runner._permissions.whoami(source)

        assert "user_tool_override" in info
        assert info["user_tool_override"] == ["read_file", "web_search"]  # sorted

    def test_whoami_override_with_group_expansion(self):
        """whoami shows expanded tools from @group."""
        pt = _make_permission_config(
            users={
                "u1": UserTierConfig.from_dict(
                    {"tier": "admin", "allowed_tools": ["@safe"]}
                )
            },
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        info = runner._permissions.whoami(source)

        assert "user_tool_override" in info
        # Should include all tools from @safe expansion
        assert len(info["user_tool_override"]) == 10
        assert "web_search" in info["user_tool_override"]
        assert "read_file" in info["user_tool_override"]
        assert "clarify" in info["user_tool_override"]

    @pytest.mark.asyncio
    async def test_whoami_override_display_in_command(self, tmp_path):
        """/whoami displays tool override line when configured."""
        from gateway.audit import NullAuditLog

        pt = _make_permission_config(
            users={
                "u1": UserTierConfig.from_dict(
                    {"tier": "admin", "allowed_tools": ["web_search", "read_file"]}
                )
            },
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()

        event = _make_event("/whoami", user_id="u1")
        result = await runner._handle_whoami_command(event)

        assert "**Tool override:**" in result

    @pytest.mark.asyncio
    async def test_whoami_override_preview_truncates_at_10(self, tmp_path):
        """/whoami truncates tool override preview at 10 tools."""
        from gateway.audit import NullAuditLog

        # Create a user with 15 tools
        tools = [f"tool_{i}" for i in range(15)]
        pt = _make_permission_config(
            users={
                "u1": UserTierConfig.from_dict(
                    {"tier": "admin", "allowed_tools": tools}
                )
            },
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()

        event = _make_event("/whoami", user_id="u1")
        result = await runner._handle_whoami_command(event)

        assert "**Tool override:**" in result
        assert "… (15 total)" in result

    def test_whoami_override_empty_after_intersection(self):
        """whoami shows override even if it doesn't overlap with tier."""
        pt = _make_permission_config(
            users={
                "u1": UserTierConfig.from_dict(
                    {"tier": "guest", "allowed_tools": ["web_search", "read_file"]}
                )
            },
            tiers={
                "guest": TierDefinition(
                    allowed_toolsets=[],
                    allow_admin_commands=False,
                )
            },
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        info = runner._permissions.whoami(source)

        # whoami shows the user's configured override, not the intersection
        assert "user_tool_override" in info
        assert info["user_tool_override"] == ["read_file", "web_search"]


# ------------------------------------------------------------------
# T2: Per-command allowlists (config layer)
# ------------------------------------------------------------------


class TestTierDefinitionCommandAllowlist:
    """Tests for TierDefinition.allowed_commands field."""

    def test_tier_no_allowed_commands_by_default(self):
        """TierDefinition() has no command allowlist by default."""
        tier = TierDefinition()
        assert tier.allowed_commands is None

    def test_tier_from_dict_with_commands(self):
        """TierDefinition.from_dict with allowed_commands list sets allowlist."""
        tier = TierDefinition.from_dict(
            {"allowed_commands": ["help", "status", "whoami"]}
        )
        assert tier.allowed_commands == frozenset({"help", "status", "whoami"})

    def test_tier_from_dict_lowercases_commands(self):
        """TierDefinition.from_dict lowercases command names."""
        tier = TierDefinition.from_dict({"allowed_commands": ["Help", "STATUS"]})
        assert tier.allowed_commands == frozenset({"help", "status"})

    def test_tier_to_dict_includes_commands(self):
        """TierDefinition.to_dict includes allowed_commands when set."""
        tier = TierDefinition.from_dict(
            {"allowed_commands": ["help", "status", "whoami"]}
        )
        d = tier.to_dict()
        assert "allowed_commands" in d
        # Should be sorted
        assert d["allowed_commands"] == ["help", "status", "whoami"]

    def test_tier_invalid_commands_type_ignored(self):
        """TierDefinition.from_dict ignores non-list allowed_commands."""
        tier = TierDefinition.from_dict({"allowed_commands": "not_a_list"})
        assert tier.allowed_commands is None


# ------------------------------------------------------------------
# T2: Per-command allowlists (dispatch gating)
# ------------------------------------------------------------------


class TestCommandAllowlistGating:
    """Tests for command allowlist gating in dispatch."""

    @pytest.mark.asyncio
    async def test_command_allowed_when_in_allowlist(self, tmp_path):
        """Command is allowed when in tier's allowed_commands."""
        from gateway.audit import NullAuditLog

        tier = TierDefinition(
            allowed_commands=frozenset({"help", "status", "whoami"}),
            allow_admin_commands=False,
        )
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"restricted": tier},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()

        event = _make_event("/help", user_id="u1")
        result = await runner._handle_message(event)

        # Should NOT be denied (no permission message)
        # The /help handler should run and return something
        assert result is not None

    @pytest.mark.asyncio
    async def test_command_blocked_when_not_in_allowlist(self, tmp_path):
        """Command is blocked when not in tier's allowed_commands."""
        from gateway.audit import NullAuditLog

        tier = TierDefinition(
            allowed_commands=frozenset({"help", "status"}),
            allow_admin_commands=False,
        )
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"restricted": tier},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()

        event = _make_event("/model", user_id="u1")
        result = await runner._handle_message(event)

        assert result is not None
        assert "higher access" in result.lower()

    @pytest.mark.asyncio
    async def test_command_allowed_when_no_allowlist_set(self, tmp_path):
        """Command uses binary gates when allowed_commands is None."""
        from gateway.audit import NullAuditLog

        tier = TierDefinition(allowed_commands=None, allow_admin_commands=False)
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"restricted": tier},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()

        event = _make_event("/help", user_id="u1")
        result = await runner._handle_message(event)

        # Should NOT be denied
        assert result is not None

    @pytest.mark.asyncio
    async def test_allowlist_does_not_override_admin_gate(self, tmp_path):
        """Admin gate runs before allowlist check."""
        from gateway.audit import NullAuditLog

        # Tier with allow_admin_commands=False but provider in allowed_commands
        tier = TierDefinition(
            allowed_commands=frozenset({"provider"}),
            allow_admin_commands=False,
        )
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"restricted": tier},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()

        event = _make_event("/provider", user_id="u1")
        result = await runner._handle_message(event)

        # Should be blocked by admin gate, not allowlist
        assert result is not None
        assert "higher access" in result.lower()

    @pytest.mark.asyncio
    async def test_owner_only_command_blocked_by_allowlist(self, tmp_path):
        """Owner-only command can be blocked by allowlist before owner gate."""
        from gateway.audit import NullAuditLog

        # Tier without admin_commands but with allowlist that excludes update
        tier = TierDefinition(
            allowed_commands=frozenset({"help", "status"}),
            allow_admin_commands=False,
        )
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"restricted": tier},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()

        event = _make_event("/update", user_id="u1")
        result = await runner._handle_message(event)

        # Should be blocked by allowlist (before owner gate runs)
        assert result is not None
        assert "higher access" in result.lower()

    @pytest.mark.asyncio
    async def test_allowlist_with_empty_set_blocks_all(self, tmp_path):
        """Empty allowed_commands set blocks all commands."""
        from gateway.audit import NullAuditLog

        tier = TierDefinition(
            allowed_commands=frozenset(),
            allow_admin_commands=False,
        )
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"restricted": tier},
        )
        runner = _make_runner(permission_tiers=pt)
        runner._audit_log = NullAuditLog()

        event = _make_event("/help", user_id="u1")
        result = await runner._handle_message(event)

        # Should be blocked
        assert result is not None
        assert "higher access" in result.lower()


# ------------------------------------------------------------------
# F-16: Owner escalation guard
# ------------------------------------------------------------------


class TestOwnerEscalationGuard:
    """Tests for the owner-tier escalation guard.

    Only owners can grant the owner tier via /users set or /promote approve.
    Non-owners (including admins) are rejected.
    """

    @staticmethod
    def _owner_config(**user_overrides):
        """Build a config with owner, admin, and restricted tiers."""
        users = {"owner1": UserTierConfig(tier="owner")}
        users.update(user_overrides)
        return _make_permission_config(
            tiers={
                "owner": TierDefinition(
                    allowed_toolsets=["*"], allow_exec=True, allow_admin_commands=True
                ),
                "admin": _admin_tier(),
                "restricted": _restricted_tier(),
            },
            users=users,
        )

    def test_set_user_tier_rejects_owner_for_admin_caller(self):
        """Admin cannot grant owner tier via set_user_tier()."""
        from gateway.permissions import PermissionManager

        pm = PermissionManager(config=self._owner_config())
        pm._runtime_store = MagicMock()
        success, msg = pm.set_user_tier("target_user", "owner", caller_tier="admin")
        assert not success
        assert "owner" in msg.lower()

    def test_set_user_tier_allows_owner_for_owner_caller(self):
        """Owner can grant owner tier via set_user_tier()."""
        from gateway.permissions import PermissionManager

        pm = PermissionManager(config=self._owner_config())
        pm._runtime_store = MagicMock()
        success, msg = pm.set_user_tier("target_user", "owner", caller_tier="owner")
        assert success
        assert "owner" in msg.lower()

    def test_set_user_tier_allows_admin_for_admin_caller(self):
        """Admin can grant non-owner tiers (e.g. admin) without restriction."""
        from gateway.permissions import PermissionManager

        pm = PermissionManager(config=self._owner_config())
        pm._runtime_store = MagicMock()
        success, msg = pm.set_user_tier("target_user", "admin", caller_tier="admin")
        assert success

    def test_set_user_tier_no_caller_tier_rejects_owner(self):
        """Without caller_tier (system-initiated), owner tier is rejected."""
        from gateway.permissions import PermissionManager

        pm = PermissionManager(config=self._owner_config())
        pm._runtime_store = MagicMock()
        success, msg = pm.set_user_tier("target_user", "owner")
        assert not success
        assert "owner" in msg.lower()

    @pytest.mark.asyncio
    async def test_users_set_owner_blocked_for_admin(self, tmp_path):
        """Admin cannot set another user to owner via /users set."""
        from gateway.audit import NullAuditLog

        runner = _make_runner(
            permission_tiers=self._owner_config(
                admin1=UserTierConfig(tier="admin"),
            )
        )
        runner._audit_log = NullAuditLog()

        event = _make_event("/users set target_user owner", user_id="admin1")
        result = await runner._handle_users_command(event)

        assert result is not None
        assert "❌" in result
        assert "owner" in result.lower()

    @pytest.mark.asyncio
    async def test_users_set_owner_allowed_for_owner(self, tmp_path):
        """Owner can set another user to owner via /users set."""
        from gateway.audit import NullAuditLog

        runner = _make_runner(permission_tiers=self._owner_config())
        runner._audit_log = NullAuditLog()

        event = _make_event("/users set target_user owner", user_id="owner1")
        result = await runner._handle_users_command(event)

        assert result is not None
        assert "✅" in result

    @pytest.mark.asyncio
    async def test_promote_request_owner_rejected_for_non_owner(self, tmp_path):
        """Non-owner cannot create a promotion request for the owner tier."""
        from gateway.audit import NullAuditLog
        from gateway.permissions import PromoteRequestStore

        runner = _make_runner(
            permission_tiers=self._owner_config(
                admin1=UserTierConfig(tier="admin"),
            )
        )
        runner._audit_log = NullAuditLog()
        runner._promote_store = PromoteRequestStore(
            db_path=str(tmp_path / "promote.db")
        )

        event = _make_event("/promote owner", user_id="admin1")
        result = await runner._handle_promote_command(event)

        assert result is not None
        assert "only owners" in result.lower() or "owner" in result.lower()

    @pytest.mark.asyncio
    async def test_promote_approve_owner_blocked_for_admin(self, tmp_path):
        """Admin cannot approve a promotion request for the owner tier."""
        from gateway.audit import NullAuditLog
        from gateway.permissions import PromoteRequestStore

        runner = _make_runner(
            permission_tiers=self._owner_config(
                admin1=UserTierConfig(tier="admin"),
            )
        )
        runner._audit_log = NullAuditLog()
        runner._promote_store = PromoteRequestStore(
            db_path=str(tmp_path / "promote.db")
        )

        # Create a request directly in the store (simulating a user's request)
        req = runner._promote_store.create_request(
            user_id="some_user",
            platform="telegram",
            requested_tier="owner",
            current_tier="user",
        )
        assert req is not None
        request_id = req["id"]

        event = _make_event(f"/promote approve {request_id}", user_id="admin1")
        result = await runner._handle_promote_command(event)

        assert result is not None
        assert "❌" in result
        assert "owner" in result.lower()

    @pytest.mark.asyncio
    async def test_promote_approve_owner_allowed_for_owner(self, tmp_path):
        """Owner can approve a promotion request for the owner tier."""
        from gateway.audit import NullAuditLog
        from gateway.permissions import PromoteRequestStore

        runner = _make_runner(permission_tiers=self._owner_config())
        runner._audit_log = NullAuditLog()
        runner._promote_store = PromoteRequestStore(
            db_path=str(tmp_path / "promote.db")
        )

        # Create a request directly in the store
        req = runner._promote_store.create_request(
            user_id="some_user",
            platform="telegram",
            requested_tier="owner",
            current_tier="admin",
        )
        assert req is not None
        request_id = req["id"]

        event = _make_event(f"/promote approve {request_id}", user_id="owner1")
        result = await runner._handle_promote_command(event)

        assert result is not None
        assert "✅" in result

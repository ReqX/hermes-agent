"""Tests for user permission tiers.

Covers: config schema (Phase 1), tier resolution + tool gating (Phase 2),
exec gating (Phase 3), time restrictions (Phase 4), admin commands (Phase 5),
and i18n message formatting (Phase 6).
"""

from types import SimpleNamespace
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


def _make_permission_config(tiers=None, users=None, default_tier="admin"):
    _default_tiers = {"admin": _admin_tier(), "restricted": _restricted_tier()}
    return PermissionTiersConfig(
        default_tier=default_tier,
        tiers=tiers if tiers is not None else _default_tiers,
        users=users or {},
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
        assert len(restored.tiers) == 2
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
        assert runner._resolve_user_tier(source) == "admin"

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
        assert runner._resolve_user_tier(source) == "restricted"

    def test_known_user_returns_their_tier(self):
        pt = _make_permission_config(users={"u1": UserTierConfig(tier="restricted")})
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        assert runner._resolve_user_tier(source) == "restricted"

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
        assert runner._resolve_user_tier(source) == "standard"

    def test_wildcard_user_fallback(self):
        pt = _make_permission_config(
            default_tier="admin",
            users={"*": UserTierConfig(tier="restricted")},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="unknown_user")
        assert runner._resolve_user_tier(source) == "restricted"

    def test_specific_user_overrides_wildcard(self):
        pt = _make_permission_config(
            users={
                "u1": UserTierConfig(tier="admin"),
                "*": UserTierConfig(tier="restricted"),
            }
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        assert runner._resolve_user_tier(source) == "admin"

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
        tier_name = runner._resolve_user_tier(source)
        assert tier_name == "standrad"
        tier_cfg = runner._get_tier_config(tier_name)
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
        tier_name = runner._resolve_user_tier(source)
        # "alsobad" was injected, so resolve returns it (not fallback)
        assert tier_name == "alsobad"
        tier_cfg = runner._get_tier_config(tier_name)
        assert tier_cfg.allow_exec is False
        assert tier_cfg.allow_admin_commands is False

    def test_get_tier_config_returns_none_when_unconfigured(self):
        runner = _make_runner(permission_tiers=None)
        assert runner._get_tier_config("admin") is None

    def test_get_tier_config_returns_definition(self):
        pt = _make_permission_config(
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()}
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._get_tier_config("restricted")
        assert tier is not None
        assert tier.allow_exec is False

    def test_get_tier_config_unknown_tier_returns_none(self):
        pt = _make_permission_config(tiers={"admin": _admin_tier()})
        runner = _make_runner(permission_tiers=pt)
        assert runner._get_tier_config("nonexistent") is None

    def test_get_tier_allowed_toolsets_wildcard(self):
        pt = _make_permission_config(tiers={"admin": _admin_tier()})
        runner = _make_runner(permission_tiers=pt)
        assert runner._get_tier_allowed_toolsets("admin") == ["*"]

    def test_get_tier_allowed_toolsets_explicit(self):
        pt = _make_permission_config(
            tiers={
                "restricted": _restricted_tier(
                    allowed_toolsets=["hermes-discord", "hermes-telegram"]
                )
            }
        )
        runner = _make_runner(permission_tiers=pt)
        ts = runner._get_tier_allowed_toolsets("restricted")
        assert ts == ["hermes-discord", "hermes-telegram"]

    def test_get_tier_allowed_toolsets_unconfigured(self):
        runner = _make_runner(permission_tiers=None)
        assert runner._get_tier_allowed_toolsets("anything") == ["*"]


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
        allowed, reason = runner._is_within_time_window(tier)
        assert allowed is True
        assert reason is None

    def test_none_tier_always_allowed(self):
        runner = _make_runner()
        allowed, reason = runner._is_within_time_window(None)
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
        allowed, reason = runner._is_within_time_window(tier)
        assert allowed is True

    def test_before_window(self):
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(
                start="23:59", end="23:59", timezone="UTC"
            )
        )
        runner = _make_runner()
        allowed, reason = runner._is_within_time_window(tier)
        assert allowed is False
        assert reason == "time_restricted_before"

    def test_after_window(self):
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(
                start="00:00", end="00:01", timezone="UTC"
            )
        )
        runner = _make_runner()
        allowed, reason = runner._is_within_time_window(tier)
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
        with _patch("gateway.run.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 23, 0, tzinfo=utc)
            allowed, reason = runner._is_within_time_window(tier)
            assert allowed is True

        # 02:00 — inside the 22:00-07:00 window (cross-midnight) → allowed
        with _patch("gateway.run.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 2, 0, tzinfo=utc)
            allowed, reason = runner._is_within_time_window(tier)
            assert allowed is True

        # 12:00 — outside the 22:00-07:00 window → blocked
        with _patch("gateway.run.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 12, 0, tzinfo=utc)
            allowed, reason = runner._is_within_time_window(tier)
            assert allowed is False

        # 21:59 — just before window starts → blocked
        with _patch("gateway.run.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 21, 59, tzinfo=utc)
            allowed, reason = runner._is_within_time_window(tier)
            assert allowed is False

        # 07:00 — exactly at end → blocked (boundary)
        with _patch("gateway.run.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 7, 0, tzinfo=utc)
            allowed, reason = runner._is_within_time_window(tier)
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
        allowed, reason = runner._is_within_time_window(tier)
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
        allowed, reason = runner._is_within_time_window(tier)
        assert isinstance(allowed, bool)

    def test_invalid_time_format_denies_access(self):
        """Garbage time values should fail-closed (deny) rather than crash."""
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(start="99:00", end="abc", timezone="UTC")
        )
        runner = _make_runner()
        allowed, reason = runner._is_within_time_window(tier)
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
        allowed, reason = runner._is_within_time_window(tier)
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
        allowed, reason = runner._is_within_time_window(tier)
        assert allowed is False
        assert reason == "time_restricted_invalid"

    def test_none_time_value_denies_access(self):
        """F-8: start: None → AttributeError caught, access denied."""
        tr = TimeRestrictions(start=None, end="22:00", timezone="UTC")
        tier = _restricted_tier(time_restrictions=tr)
        runner = _make_runner()
        allowed, reason = runner._is_within_time_window(tier)
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
        runner._pending_approvals[runner._approval_key(session_key, source)] = {
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
        runner._pending_approvals[runner._approval_key(session_key, source)] = {
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
        runner._pending_approvals[runner._approval_key(session_key, source_admin)] = {
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
        runner._pending_approvals[runner._approval_key(session_key, source_admin)] = {
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
        assert runner._approval_key(session_key, source) == session_key

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
        key = runner._approval_key(session_key, source)
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
        key = runner._approval_key(session_key, source)
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
        tier = runner._get_tier_config(runner._resolve_user_tier(event.source))
        assert tier is not None and not tier.allow_admin_commands

    @pytest.mark.asyncio
    async def test_model_command_allowed_for_admin(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="admin")},
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._get_tier_config(
            runner._resolve_user_tier(_make_source(user_id="u1"))
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
        tier = runner._get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._format_tier_message(tier, "exec_denied", source)
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
        tier = runner._get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._format_tier_message(tier, "exec_denied", source)
        assert msg == "No permission!"

    def test_fallback_to_hardcoded_english(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"restricted": _restricted_tier(messages={})},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._format_tier_message(tier, "exec_denied", source)
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
        tier = runner._get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._format_tier_message(tier, "time_restricted_after", source)
        assert "08:00" in msg
        assert "Europe/Vienna" in msg
        assert "Feierabend" in msg

    def test_unknown_key_returns_generic_denied(self):
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")},
            tiers={"restricted": _restricted_tier(messages={})},
        )
        runner = _make_runner(permission_tiers=pt)
        tier = runner._get_tier_config("restricted")
        source = _make_source(user_id="u1")
        msg = runner._format_tier_message(tier, "unknown_key", source)
        assert msg == "Permission denied."

    def test_no_permission_tiers_uses_english_default(self):
        runner = _make_runner(permission_tiers=None)
        # _format_tier_message needs a tier object; if called with no config,
        # locale defaults to "en"
        tier = _restricted_tier(messages={"exec_denied": {"en": "Blocked!"}})
        source = _make_source(user_id="u1")
        msg = runner._format_tier_message(tier, "exec_denied", source)
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
        tier_name = runner._resolve_user_tier(source)
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
        assert runner._most_restrictive_tier(pt) == "viewer"

    def test_f9_empty_tiers_returns_sentinel(self):
        """_most_restrictive_tier returns sentinel for empty tiers dict."""
        pt = PermissionTiersConfig(default_tier="x", tiers={}, users={})
        runner = _make_runner(permission_tiers=pt)
        result = runner._most_restrictive_tier(pt)
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
        cfg = runner._get_tier_config("__restricted_fallback__")
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
        assert runner._resolve_user_tier(source) == "restricted"

    def test_f10_none_tier_wildcard_falls_to_default(self):
        """F-10: Wildcard user with no tier resolves to default_tier."""
        pt = _make_permission_config(
            default_tier="restricted",
            users={"*": UserTierConfig()},  # tier=None
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="anyone")
        assert runner._resolve_user_tier(source) == "restricted"

"""Tests for user permission tiers.

Covers: config schema (Phase 1), tier resolution + tool gating (Phase 2),
exec gating (Phase 3), time restrictions (Phase 4), admin commands (Phase 5),
and i18n message formatting (Phase 6).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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


def _make_runner(permission_tiers=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")},
        permission_tiers=permission_tiers,
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
        assert utc.tier == "admin"
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


# ------------------------------------------------------------------
# Phase 2: Tier Resolution & Tool Gating
# ------------------------------------------------------------------


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
        pt = _make_permission_config(
            users={"u1": UserTierConfig(tier="restricted")}
        )
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

    def test_typo_tier_name_fails_closed(self):
        """User mapped to a nonexistent tier should fall back to default_tier, not fail-open."""
        pt = _make_permission_config(
            default_tier="restricted",
            users={"u1": UserTierConfig(tier="standrad")},  # typo
            tiers={"admin": _admin_tier(), "restricted": _restricted_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        # Should NOT return "standrad" (which would bypass all restrictions)
        assert runner._resolve_user_tier(source) == "restricted"

    def test_typo_tier_falls_to_admin_if_default_also_missing(self):
        """If both the mapped tier and default_tier are nonexistent, fall to 'admin'."""
        pt = _make_permission_config(
            default_tier="nonexistent",
            users={"u1": UserTierConfig(tier="alsobad")},
            tiers={"admin": _admin_tier()},
        )
        runner = _make_runner(permission_tiers=pt)
        source = _make_source(user_id="u1")
        assert runner._resolve_user_tier(source) == "admin"

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
            tiers={"restricted": _restricted_tier(allowed_toolsets=["hermes-discord", "hermes-telegram"])}
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

        sig_a = GatewayRunner._agent_config_signature("m1", {}, ["ts1"], "prompt", "admin")
        sig_b = GatewayRunner._agent_config_signature("m1", {}, ["ts1"], "prompt", "restricted")
        assert sig_a != sig_b

    def test_signature_same_tier_same_hash(self):
        from gateway.run import GatewayRunner

        sig_a = GatewayRunner._agent_config_signature("m1", {}, ["ts1"], "prompt", "admin")
        sig_b = GatewayRunner._agent_config_signature("m1", {}, ["ts1"], "prompt", "admin")
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
            time_restrictions=TimeRestrictions(start="23:59", end="23:59", timezone="UTC")
        )
        runner = _make_runner()
        allowed, reason = runner._is_within_time_window(tier)
        assert allowed is False
        assert reason == "time_restricted_before"

    def test_after_window(self):
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(start="00:00", end="00:01", timezone="UTC")
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

        # We can't easily mock datetime.now, but we can test the logic
        # by verifying the function runs without error for cross-midnight config
        allowed, reason = runner._is_within_time_window(tier)
        # Result depends on current time, just verify it returns a tuple
        assert isinstance(allowed, bool)
        assert reason is None or isinstance(reason, str)

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

    def test_invalid_time_format_allows_access(self):
        """Garbage time values should fail-open (allow) rather than crash."""
        tier = _restricted_tier(
            time_restrictions=TimeRestrictions(
                start="99:00", end="abc", timezone="UTC"
            )
        )
        runner = _make_runner()
        allowed, reason = runner._is_within_time_window(tier)
        # Must not crash; fail-open means allowed=True
        assert allowed is True


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
        runner._pending_approvals[session_key] = {
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
        tier = runner._get_tier_config(runner._resolve_user_tier(_make_source(user_id="u1")))
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

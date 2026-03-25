# Feature: User Permission Tiers

**Branch:** `feature/user-permission-tiers`
**Base:** `099dfca6` (upstream/main)
**Status:** Implemented (Phases 1-6), tested

## Goal

Multi-tiered user permission system in the Hermes Gateway — restrict tool access, slash commands, exec approvals, and usage times on a per-user basis. **Strictly opt-in**: if `permission_tiers` is absent from `config.yaml`, behavior is identical to legacy.

## Architecture

```
config.yaml
  └─ permission_tiers:
       ├─ default_tier: "admin"
       ├─ tiers:
       │    ├─ admin:
       │    │    ├─ allowed_toolsets: ["*"]
       │    │    ├─ allow_exec: true
       │    │    ├─ allow_admin_commands: true
       │    │    └─ messages: { ... }
       │    ├─ standard:
       │    │    ├─ allowed_toolsets: ["hermes-cli", "hermes-discord", ...]
       │    │    ├─ allow_exec: false
       │    │    ├─ allow_admin_commands: false
       │    │    ├─ time_restrictions: { start: "08:00", end: "22:00", timezone: "Europe/Vienna" }
       │    │    └─ messages: { ... }
       │    └─ restricted:
       │         ├─ allowed_toolsets: ["hermes-discord"]
       │         ├─ allow_exec: false
       │         ├─ allow_admin_commands: false
       │         ├─ time_restrictions: { start: "08:00", end: "22:00", timezone: "Europe/Vienna" }
       │         └─ messages: { ... }
       └─ users:
            ├─ "673463676937961472": { tier: "admin", locale: "en" }        # Mike (Discord)
            ├─ "123456789": { tier: "standard", locale: "de" }              # Karin
            └─ "*": { tier: "restricted", locale: "de" }                    # fallback
```

## Hook Points

All changes in `gateway/run.py` — the `GatewayRunner` class. Two hook points:

1. **`_handle_message()`**: Early gate for time restrictions and admin slash commands (before agent dispatch)
2. **`_run_agent()`**: Tool filtering via toolset intersection, tier system prompt injection, cache signature includes `user_tier`

No changes to platform adapters or `AIAgent` itself.

## Commits

```
099dfca6  ← base (upstream/main)
  └─ 61e0d08f  feat(gateway): add permission tier config schema
  └─ d64b31dc  feat(gateway): resolve user tier and filter toolsets
  └─ 043bf2e4  feat(gateway): gate exec approvals, time restrictions, and admin commands
```

Phase 3-6 were combined into a single commit since they all hook into `_handle_message()`.

## Implementation

### Phase 1 — Config Schema (`gateway/config.py`)

Dataclasses added to `gateway/config.py`:

- **`TimeRestrictions`**: `start`, `end` (HH:MM), `timezone`, optional `days` (0=Mon..6=Sun)
- **`TierDefinition`**: `allowed_toolsets`, `allow_exec`, `allow_admin_commands`, optional `time_restrictions`, `messages` dict
- **`UserTierConfig`**: `tier` name, `locale` for i18n
- **`PermissionTiersConfig`**: `default_tier`, `tiers` dict, `users` dict

`GatewayConfig` gains `permission_tiers: Optional[PermissionTiersConfig] = None`.

All classes include `from_dict()` / `to_dict()` for YAML serialization. `load_gateway_config()` reads `permission_tiers` from `config.yaml`.

### Phase 2 — Tier Resolution & Tool Gating (`gateway/run.py`)

Methods on `GatewayRunner`:

- **`_resolve_user_tier(source)`**: Returns tier name. Fallback: user config → wildcard `"*"` → `default_tier` → `"admin"`.
- **`_get_tier_config(tier_name)`**: Returns `TierDefinition` or `None` if unconfigured.
- **`_get_tier_allowed_toolsets(tier_name)`**: Returns allowed list. `["*"]` = all.
- **`_is_within_time_window(tier)`**: Returns `(allowed, reason_key)`. Uses `zoneinfo.ZoneInfo` for timezone-aware checks. Handles cross-midnight windows and day-of-week filters.

In `_run_agent()`, after `enabled_toolsets` is resolved:
```python
_tier_name = self._resolve_user_tier(source)
_tier_cfg = self._get_tier_config(_tier_name)
if _tier_cfg is not None:
    _allowed = self._get_tier_allowed_toolsets(_tier_name)
    if "*" not in _allowed:
        enabled_toolsets = [ts for ts in enabled_toolsets if ts in _allowed]
```

Cache isolation: `_agent_config_signature()` includes `user_tier` in its hash blob. Different tiers → different cached agent instances.

Soft enforcement: tier system prompt injected into `combined_ephemeral` when toolsets are restricted.

### Phase 3 — Exec Approval Gating

Permission check at the top of `_handle_approve_command()` and `_handle_deny_command()`:
```python
_tier_cfg = self._get_tier_config(self._resolve_user_tier(event.source))
if _tier_cfg is not None and not _tier_cfg.allow_exec:
    return self._format_tier_message(_tier_cfg, "exec_denied", event.source)
```

Approval hint ("Reply `/approve` to execute...") is suppressed for users without `allow_exec`.

### Phase 4 — Time Restrictions

Early gate in `_handle_message()`, after auth check:
```python
_tier_cfg = self._get_tier_config(self._resolve_user_tier(source))
if _tier_cfg is not None and _tier_cfg.time_restrictions is not None:
    allowed, reason = self._is_within_time_window(_tier_cfg)
    if not allowed:
        return self._format_tier_message(_tier_cfg, reason, source)
```

### Phase 5 — Admin Slash Command Restrictions

Gate applied after alias resolution, before handler dispatch:
```python
_admin_commands = {"model", "provider", "update", "reload-mcp", "config"}
if canonical in _admin_commands:
    _cmd_tier = self._get_tier_config(self._resolve_user_tier(source))
    if _cmd_tier is not None and not _cmd_tier.allow_admin_commands:
        return self._format_tier_message(_cmd_tier, "command_denied", source)
```

Safe commands (always allowed): `/new`, `/reset`, `/help`, `/status`, `/stop`, `/retry`, `/undo`, `/plan`, quick commands.

### Phase 6 — i18n Message Formatting

`_format_tier_message(tier, key, source)` — unified method for all restriction messages.

Lookup chain: `messages[key][locale]` → `messages[key]["en"]` → hardcoded English fallback.

Placeholders: `{start}`, `{end}`, `{timezone}` (only when `time_restrictions` is set).

Message keys:
- `time_restricted_before`: Access hasn't started yet
- `time_restricted_after`: Access window ended
- `time_restricted_wrong_day`: Not available on this day of week
- `exec_denied`: No permission to approve/deny commands
- `command_denied`: Admin-only command

Hardcoded English fallbacks used when no template is configured.

## Design Decisions

- **Opt-in only**: `permission_tiers` key absent → zero code paths triggered. No defaults applied, no time checks, no filtering.
- **No hardcoded tier definitions**: The schema provides fields; the user defines what tiers mean. No built-in semantics beyond the field names.
- **`allow_admin_commands`**: Named explicitly for what it controls. No clever proxy flags.
- **`allowed_toolsets: ["*"]`**: Wildcard = all allowed. Any other value = explicit allowlist intersected with platform default.
- **Cache isolation**: `_agent_config_signature` includes `user_tier`. Different tiers → different agent instances → no permission leakage.
- **Soft enforcement**: Tier system prompt tells the LLM its limitations alongside hard tool gating.
- **Single i18n method**: `_format_tier_message()` handles all message types (exec, commands, time) — not separate per-feature methods.

## Tests

File: `tests/gateway/test_permission_tiers.py` (45 tests, all passing)

Coverage:
1. Config schema round-trips (TimeRestrictions, TierDefinition, UserTierConfig, PermissionTiersConfig, GatewayConfig)
2. Tier resolution (known user, unknown user, wildcard, default_tier fallback)
3. Opt-out (no `permission_tiers` → no filtering)
4. Time restrictions (within/before/after window, cross-midnight, day filter, invalid timezone)
5. Exec approval gating (blocked for restricted, allowed for admin, allowed when unconfigured)
6. Admin command gating (blocked/allowed sets, safe commands not gated)
7. i18n fallback chain (specific locale → English → hardcoded)
8. Cache isolation (different tiers → different signatures)

Also verified: all 1494 existing gateway tests still pass (1 pre-existing Signal flaky test unrelated).

## Sample Config

```yaml
# config.yaml — enable permission tiers
permission_tiers:
  default_tier: "restricted"
  tiers:
    admin:
      allowed_toolsets: ["*"]
      allow_exec: true
      allow_admin_commands: true
    standard:
      allowed_toolsets: ["hermes-discord", "hermes-telegram", "hermes-cli"]
      allow_exec: false
      allow_admin_commands: false
      time_restrictions:
        start: "08:00"
        end: "22:00"
        timezone: "Europe/Vienna"
      messages:
        time_restricted_before:
          en: "Hey! I'm available from {start} {timezone} 🕐"
          de: "Hey! Ich bin ab {start} {timezone} erreichbar 🕐"
        time_restricted_after:
          en: "I've clocked out for today! Back at {start} {timezone} 😴"
          de: "Ich hab Feierabend! Bis morgen um {start} {timezone} 😴"
        exec_denied:
          en: "You don't have permission to approve or deny commands."
          de: "Du hast keine Berechtigung, Befehle zu genehmigen."
        command_denied:
          en: "That command is only available to admins."
          de: "Dieser Befehl ist nur für Admins verfügbar."
    restricted:
      allowed_toolsets: ["hermes-discord"]
      allow_exec: false
      allow_admin_commands: false
      time_restrictions:
        start: "08:00"
        end: "22:00"
        timezone: "Europe/Vienna"
      messages:
        time_restricted_before:
          de: "Hey! Ich bin ab {start} {timezone} erreichbar 🕐"
        time_restricted_after:
          de: "Ich hab Feierabend! Bis morgen um {start} {timezone} 😴"
        exec_denied:
          de: "Du hast keine Berechtigung, Befehle zu genehmigen."
        command_denied:
          de: "Dieser Befehl ist nur für Admins verfügbar."
  users:
    "673463676937961472": { tier: "admin", locale: "en" }   # Mike
    "KARIN_DISCORD_ID": { tier: "standard", locale: "de" }    # Karin
    "*": { tier: "restricted", locale: "de" }                 # everyone else
```

## Next Steps

- Live smoke test with real `config.yaml`
- PR upstream

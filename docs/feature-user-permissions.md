# User Permission Tiers

Restrict tool access, slash commands, exec approvals, and usage times on a per-user basis in the Hermes Gateway.

**Strictly opt-in**: if `permission_tiers` is absent from `config.yaml`, nothing changes. Every user has full access, identical to legacy behavior.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [Configuration Reference](#configuration-reference)
  - [Top-level](#top-level-permission_tiers)
  - [Tiers](#tier-definitions)
  - [Time Restrictions](#time-restrictions)
  - [Messages](#messages--i18n)
  - [Users](#user-mapping)
- [What Gets Restricted](#what-gets-restricted)
  - [Toolsets](#toolsets)
  - [Admin Commands](#admin-slash-commands)
  - [Exec Approvals](#exec-approvals)
  - [Time Windows](#time-windows)
- [Available Toolsets](#available-toolsets)
- [Full Example](#full-example)
- [Troubleshooting](#troubleshooting)

---

## How It Works

1. You define named **tiers** (e.g. `admin`, `standard`, `restricted`) with specific permissions.
2. You **map users** to tiers by their platform user ID. A wildcard `"*"` entry catches unlisted users.
3. When a message arrives, the gateway resolves the user's tier once and applies all restrictions before the agent runs.

The check order is: **time window → admin command gate → toolset filtering → exec approval gate**.

All enforcement is **hard** (tools are actually removed from the agent's toolset) with a **soft** layer (the agent is told its restrictions in the system prompt) so it can respond gracefully instead of silently failing.

---

## Quick Start

Add a `permission_tiers` block to `~/.hermes/config.yaml`:

```yaml
permission_tiers:
  default_tier: "restricted"
  tiers:
    admin:
      allowed_toolsets: ["*"]
      allow_exec: true
      allow_admin_commands: true
    restricted:
      allowed_toolsets: ["hermes-discord"]
      allow_exec: false
      allow_admin_commands: false
  users:
    "YOUR_DISCORD_USER_ID": { tier: "admin" }
    "*": { tier: "restricted" }
```

That's it. Your user gets full access. Everyone else gets Discord-only tools, no exec, no admin commands.

---

## Configuration Reference

### Top-level (`permission_tiers`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_tier` | string | `"admin"` | Tier assigned when a user isn't listed and no `"*"` wildcard exists |
| `tiers` | dict | `{}` | Named tier definitions (see below) |
| `users` | dict | `{}` | Maps user IDs to tiers (see below) |

### Tier Definitions

Each key under `tiers` is a tier name you choose. A tier has:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `allowed_toolsets` | list | `["*"]` | Toolsets this tier can use. `["*"]` = all. Any other list = allowlist (intersected with platform defaults). |
| `allow_exec` | bool | `true` | Whether this tier can approve/deny dangerous terminal commands |
| `allow_admin_commands` | bool | `true` | Whether this tier can use admin-only slash commands |
| `time_restrictions` | object or null | null | Time window when this tier is active (see below). `null` = always active. |
| `messages` | dict | `{}` | Custom i18n messages for restriction responses (see below) |

### Time Restrictions

Optional. When present, the tier is only active during the specified window.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `start` | string | `"08:00"` | Window start (HH:MM, 24h) |
| `end` | string | `"22:00"` | Window end (HH:MM, 24h) |
| `timezone` | string | `"UTC"` | IANA timezone name (e.g. `"Europe/Vienna"`) |
| `days` | list or null | null | Allowed days: `0`=Monday .. `6`=Sunday. `null` = all days. |

Notes:
- **Cross-midnight** windows work: `start: "22:00"` + `end: "07:00"` means active from 22:00 to 07:00.
- `start == end` means always restricted (zero-length window).
- Invalid time formats or out-of-range days are filtered at load time with a warning, and access is denied (fail-closed).
- Invalid timezone names fall back to UTC with a warning.

### Messages (i18n)

Customize the text users see when they're blocked. Organized by message key and locale code.

Available message keys:

| Key | When shown |
|-----|-----------|
| `time_restricted_before` | User messages before the time window opens |
| `time_restricted_after` | User messages after the time window closes |
| `time_restricted_wrong_day` | User messages on a day not in the `days` list |
| `exec_denied` | User tries to `/approve` or `/deny` without permission |
| `command_denied` | User tries an admin-only slash command |

Placeholders (available in time-related messages):
- `{start}` — window start time
- `{end}` — window end time
- `{timezone}` — timezone name

Lookup order: `messages[key][user_locale]` → `messages[key]["en"]` → hardcoded English fallback.

### User Mapping

Map platform user IDs to tiers and locales:

```yaml
users:
  "111111111111111111": { tier: "admin", locale: "en" }      # user 1
  "222222222222222222": { tier: "standard", locale: "de" }    # user 2
  "*":                  { tier: "restricted", locale: "de" }  # wildcard fallback
```

The user ID in the mapping must match the **platform-native user ID** that the adapter extracts from incoming messages. Each platform provides a different ID:

| Platform | ID source | Example | Stable? |
|----------|-----------|---------|---------|
| **Discord** | `interaction.user.id` / `message.author.id` | `"123456789012345678"` | ✅ Immutable |
| **Telegram** | `message.from_user.id` | `"987654321"` | ✅ Immutable |
| **Slack** | `event.user` (user ID starting with `U`) | `"U01ABCDEF"` | ✅ Immutable |
| **WhatsApp** | Phone number from sender JID | `"436641234567"` | ✅ Immutable |
| **Signal** | Phone number from `source` / `envelope` | `"+436641234567"` | ✅ Immutable |
| **Matrix** | `event.sender` (full MXID) | `"@user:matrix.org"` | ✅ Immutable |
| **Email** | Sender email address | `"user@example.com"` | ✅ Immutable |
| **Mattermost** | `data.user_name` (username) | `"jdoe"` | ⚠️ **Can change** |
| **Home Assistant** | Hardcoded `"homeassistant"` | `"homeassistant"` | — Single user |
| **DingTalk** | `senderStaffId` | `"012345"` | ✅ Immutable |

> **Note:** Mattermost uses the **username** (not a stable user ID) for identification. If a user changes their username, their tier mapping will break. Use the wildcard `"*"` entry as a safety net.

If `user_id` is `None` for any reason (system events, bot messages), the tier system falls through to wildcard → `default_tier` → most-restrictive. It cannot grant elevated access.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tier` | string or null | null | Name of a tier defined in `tiers`. When null, falls back to `default_tier`. |
| `locale` | string | `"en"` | Language code for message lookup |

Resolution order for an incoming message:
1. Exact user ID match
2. Wildcard `"*"` match
3. `default_tier` from config
4. Most-restrictive tier in `tiers` (the tier with fewest toolsets, no exec, no admin commands)

If the resolved tier name doesn't exist in `tiers` (typo, stale config), the user gets the **most restrictive** tier (no tools, no exec, no admin) — fail-closed, never fail-open.

---

## What Gets Restricted

### Toolsets

When `allowed_toolsets` is not `["*"]`, the agent's toolset is intersected with the tier's allowed list. Tools outside the allowed toolsets are **removed** — the agent physically cannot call them.

Example: `allowed_toolsets: ["hermes-discord"]` means the agent only gets Discord-platform tools. No web search, no terminal, no browser, no file tools.

A warning is logged if the intersection is empty (likely a misconfiguration).

### Admin Slash Commands

Users with `allow_admin_commands: false` cannot use:

- `/provider` — show or change the LLM provider
- `/update` — update Hermes to the latest version
- `/reload-mcp` — reload MCP server configuration

These are defined via the `admin_only` flag on `CommandDef` in `hermes_cli/commands.py`. To add more, set `admin_only=True` on the command definition.

Always allowed (not gated): `/new`, `/reset`, `/help`, `/status`, `/stop`, `/retry`, `/undo`, `/plan`, and all quick commands.

### Exec Approvals

When `allow_exec: false`, the user cannot `/approve` or `/deny` pending dangerous terminal commands. The approval hint ("Reply `/approve` to execute...") is also suppressed for these users.

### Time Windows

When `time_restrictions` is set, messages outside the time window are blocked immediately — the agent never runs. The user receives the configured denial message (or the English fallback).

The time check is applied in both the normal path and the running-agent interrupt path (defense-in-depth), with `/stop` always allowed so users can kill their agent even outside allowed hours.

---

## Available Toolsets

The toolset names you can use in `allowed_toolsets`. Platform toolsets include all core tools:

| Toolset | Description |
|---------|-------------|
| `hermes-cli` | Interactive CLI |
| `hermes-telegram` | Telegram bot |
| `hermes-discord` | Discord bot |
| `hermes-whatsapp` | WhatsApp bot |
| `hermes-slack` | Slack bot |
| `hermes-signal` | Signal bot |
| `hermes-homeassistant` | Home Assistant |
| `hermes-email` | Email (IMAP/SMTP) |
| `hermes-mattermost` | Mattermost |
| `hermes-matrix` | Matrix |
| `hermes-dingtalk` | DingTalk |
| `hermes-sms` | SMS |
| `hermes-gateway` | Generic gateway |
| `hermes-acp` | ACP (VS Code / Zed / JetBrains) |

Feature toolsets (can be combined with platform toolsets):

| Toolset | Description |
|---------|-------------|
| `web` | Web search and fetch |
| `search` | Code search (grep, glob) |
| `terminal` | Terminal/shell access |
| `browser` | Browser automation |
| `file` | File read/write/patch |
| `vision` | Image analysis |
| `image_gen` | Image generation |
| `code_execution` | Code execution sandbox |
| `delegation` | Subagent delegation |
| `skills` | Skill system |
| `cronjob` | Scheduled jobs |
| `homeassistant` | Smart home control |
| `tts` | Text-to-speech |

---

## Full Example

A realistic three-tier setup for a Discord bot:

```yaml
permission_tiers:
  default_tier: "restricted"

  tiers:
    admin:
      allowed_toolsets: ["*"]
      allow_exec: true
      allow_admin_commands: true

    standard:
      allowed_toolsets: ["hermes-discord", "web", "search", "vision"]
      allow_exec: false
      allow_admin_commands: false
      time_restrictions:
        start: "08:00"
        end: "22:00"
        timezone: "Europe/Vienna"
        days: [0, 1, 2, 3, 4]    # Mon-Fri only
      messages:
        time_restricted_before:
          en: "Hey! I'm available from {start} {timezone} on weekdays 🕐"
          de: "Hey! Ich bin ab {start} {timezone} an Wochentagen erreichbar 🕐"
        time_restricted_after:
          en: "I've clocked out for today! Back at {start} {timezone} 😴"
          de: "Ich hab Feierabend! Bis {start} {timezone} 😴"
        time_restricted_wrong_day:
          en: "I'm off today! Back on Monday 🏖️"
          de: "Heute hab ich frei! Bin am Montag wieder da 🏖️"
        exec_denied:
          en: "You don't have permission to approve commands."
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
          en: "Hey! I'm available from {start} {timezone} 🕐"
          de: "Hey! Ich bin ab {start} {timezone} erreichbar 🕐"
        time_restricted_after:
          en: "I'm offline right now. Back at {start} {timezone} 😴"
          de: "Ich hab Feierabend! Bis morgen um {start} {timezone} 😴"
        exec_denied:
          en: "You don't have permission to approve commands."
          de: "Du hast keine Berechtigung, Befehle zu genehmigen."
        command_denied:
          en: "That command is only available to admins."
          de: "Dieser Befehl ist nur für Admins verfügbar."

  users:
    "111111111111111111": { tier: "admin", locale: "en" }        # user 1
    "222222222222222222":  { tier: "standard", locale: "de" }    # user 2
    "*":                   { tier: "restricted", locale: "de" }   # everyone else
```

---

## Troubleshooting

**User gets "Permission denied" for everything, even though their tier looks right**

Check that the tier name in the user mapping exactly matches a key under `tiers`. A typo (e.g. `tier: "standar"` instead of `"standard"`) will map the user to the most-restrictive fallback. The gateway logs a warning when this happens.

**User has no tools available**

Check `allowed_toolsets`. If the intersection of the tier's allowed toolsets and the platform's enabled toolsets is empty, the agent has no tools. The gateway logs a warning. Common fix: include the platform toolset (e.g. `hermes-discord`) in the allowed list.

**Time restrictions not working as expected**

- Times are in 24h format (`"22:00"`, not `"10:00 PM"`).
- `timezone` must be a valid IANA name (e.g. `"Europe/Vienna"`, `"US/Eastern"`). Invalid names fall back to UTC (with a warning).
- Cross-midnight windows: `start: "22:00"`, `end: "07:00"` means active from 22:00 through 07:00.
- `start == end` means always restricted.

**Changes not taking effect**

The gateway reads config at startup. After editing `config.yaml`, restart the gateway or send `/reload-mcp` (admin-only command).

**`permission_tiers: {}` in config**

An empty `permission_tiers` block is treated as disabled (same as absent). You need at least one tier with at least one user mapping for the feature to activate. The gateway logs a warning when the block is present but has no `tiers:` key.

**YAML type confusion (quoted booleans)**

YAML may parse `true`/`false` as strings depending on quoting. For example:

```yaml
allow_exec: "false"    # string "false", not boolean
```

Hermes handles this gracefully — quoted strings like `"false"`, `"no"`, `"0"` are coerced to boolean `false`, and `"true"`, `"yes"`, `"1"` to `true`. No action needed, but be aware of it.

**`days` field behavior**

- `null` or absent = all days allowed
- `[]` (empty list) = **no days allowed** — tier is always blocked
- Out-of-range values (e.g. `7`, `8`, `-1`) are filtered out at load time. If all values are invalid, the list becomes empty and the tier is always blocked.

**Quick exec commands**

Quick commands of type `exec` are gated by `allow_exec` — the same permission that controls `/approve` and `/deny`. A restricted user with `allow_exec: false` cannot trigger quick exec commands.

**Approval isolation**

Pending exec approvals are scoped to the user who triggered them. In a group chat, user A's pending command cannot be approved or denied by user B — only user A (or the original trigger user) can act on it.

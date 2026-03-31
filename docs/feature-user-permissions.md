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
  - [Auto-tier from Environment Variables](#auto-tier-from-environment-variables)
- [What Gets Restricted](#what-gets-restricted)
  - [Toolsets](#toolsets)
  - [Tool-Level Filtering](#tool-level-filtering--groups)
  - [Admin Commands](#admin-slash-commands)
  - [Gateway Commands](#gateway-commands-for-permission-tiers)
  - [Exec Approvals](#exec-approvals)
  - [Time Windows](#time-windows)
  - [Rate Limiting](#rate-limiting)
- [Available Tool Groups](#available-tool-groups)
- [Available Toolsets](#available-toolsets)
- [Full Example](#full-example)
- [Troubleshooting](#troubleshooting)

---

## How It Works

1. You define named **tiers** (e.g. `admin`, `standard`, `restricted`) with specific permissions.
2. You **map users** to tiers by their platform user ID. A wildcard `"*"` entry catches unlisted users.
3. Admins can change user tiers at **runtime** via `/users set` without restarting the gateway.
4. When a message arrives, the gateway resolves the user's tier once and applies all restrictions before the agent runs.

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
| `builtins` | bool | `true` | When `true`, preset tiers (`owner`/`admin`/`user`/`guest`) are available |
| `auto_tier` | bool | `false` | Enable auto-tier from env vars and pairing store (see [Auto-Tier](#auto-tier-from-environment-variables)) |
| `env_owner_tier` | string | `"owner"` | Tier for first entry in each `*_ALLOWED_USERS` env var |
| `env_default_tier` | string | `"admin"` | Tier for remaining entries in `*_ALLOWED_USERS` |
| `pairing_default_tier` | string | `"user"` | Tier for pairing-approved users |
| `env_open_tier` | string | `"guest"` | Tier when any `*_ALLOW_ALL_USERS` flag is set |

### Tier Definitions

Each key under `tiers` is a tier name you choose. A tier has:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `allowed_toolsets` | list | `["*"]` | Toolsets this tier can use. `["*"]` = all. Any other list = allowlist (intersected with platform defaults). |
| `allowed_tools` | list or null | null | Individual tool names or `@group` shorthands. When set, takes **precedence** over `allowed_toolsets`. See [Tool-Level Filtering](#tool-level-filtering--groups). |
| `allow_exec` | bool | `true` | Whether this tier can approve/deny dangerous terminal commands |
| `allow_admin_commands` | bool | `true` | Whether this tier can use admin-only slash commands |
| `requests_per_hour` | int or null | null | Max agent requests per hour per user. `null` = unlimited, `0` = blocked. |
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
| `rate_limited` | User exceeds their hourly request limit |
| `rate_limited_blocked` | User's tier has `requests_per_hour: 0` (completely blocked) |

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
1. Runtime overlay (set via `/users set` — persists across restarts)
2. Exact user ID match in config
3. Wildcard `"*"` match
4. `default_tier` from config
5. Most-restrictive tier in `tiers` (the tier with fewest toolsets, no exec, no admin commands)

If the resolved tier name doesn't exist in `tiers` (typo, stale config), the user gets the **most restrictive** tier (no tools, no exec, no admin) — fail-closed, never fail-open.

### Auto-Tier from Environment Variables

When `auto_tier: true` is set, Hermes automatically maps users to tiers based on your existing `*_ALLOWED_USERS` and `*_ALLOW_ALL_USERS` environment variables, plus the DM pairing store. This eliminates the need to manually duplicate user IDs in `config.yaml`.

**This is strictly opt-in** — `auto_tier` defaults to `false`. Existing deployments are unaffected.

```yaml
permission_tiers:
  auto_tier: true                    # Master switch
  env_owner_tier: "owner"            # First entry in *_ALLOWED_USERS → owner
  env_default_tier: "admin"          # Remaining entries → admin
  pairing_default_tier: "user"       # Pairing-approved users → user
  env_open_tier: "guest"             # ALLOW_ALL_USERS (open access) → guest

  tiers:
    owner: { allowed_tools: ["@all"], allow_exec: true }
    admin: { allowed_tools: ["@web", "@read", "@code"], allow_exec: true }
    user:  { allowed_tools: ["@web", "@read"], allow_exec: false }
    guest: { allowed_tools: ["@clarify"], allow_exec: false }
```

#### How it works

On gateway startup, `_apply_env_auto_tiers()` reads all platform `*_ALLOWED_USERS` env vars and the pairing store, then injects `UserTierConfig` entries into the user mapping:

1. **Platform env vars** (e.g. `TELEGRAM_ALLOWED_USERS=111,222,333`):
   - First user ID → `env_owner_tier` (default: `"owner"`)
   - Remaining user IDs → `env_default_tier` (default: `"admin"`)
   - Keys are **composite**: `telegram:111`, `telegram:222`, etc.

2. **Global env var** (`GATEWAY_ALLOWED_USERS`):
   - Same first/rest logic, with `global:` prefix

3. **Pairing store** (already-approved users):
   - Each approved user → `pairing_default_tier` (default: `"user"`)

4. **Allow-all flags** (`TELEGRAM_ALLOW_ALL_USERS=true`, etc.):
   - Injects a wildcard `"*"` → `env_open_tier` (default: `"guest"`)

5. **Dynamic injection**: Users who are pairing-approved *after* startup are detected on-the-fly during tier resolution and injected with `pairing_default_tier`.

#### Security guarantees

- **Explicit config always wins**: If a user ID already exists in `config.yaml` (either as bare ID or composite key), auto-tier never overrides it.
- **Fail-closed validation**: If any of the four tier references (`env_owner_tier`, `env_default_tier`, `pairing_default_tier`, `env_open_tier`) point to a tier that doesn't exist, `auto_tier` is automatically disabled at config load time. A warning is logged.
- **Cross-platform isolation**: User ID `123` on Telegram is stored as `telegram:123`, not `123`. This prevents the same ID on different platforms from sharing tier assignments.
- **No elevation on error**: If the pairing store fails during init or dynamic injection, users fall back to `default_tier` — never to a higher tier.

#### Auto-tier config fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `auto_tier` | bool | `false` | Master switch. Must be explicitly enabled. |
| `env_owner_tier` | string | `"owner"` | Tier for the first entry in each `*_ALLOWED_USERS` env var |
| `env_default_tier` | string | `"admin"` | Tier for remaining entries in `*_ALLOWED_USERS` |
| `pairing_default_tier` | string | `"user"` | Tier for pairing-approved users |
| `env_open_tier` | string | `"guest"` | Tier when any `*_ALLOW_ALL_USERS` flag is set |

#### Resolution order with auto-tier

When auto-tier is active, the resolution order is:

1. Runtime overlay (`/users set`)
2. Composite key lookup (`platform:user_id`) — includes auto-tier injected entries
3. Bare user ID lookup — backward compat with explicit config
4. Dynamic pairing injection (on-the-fly for newly approved users)
5. Wildcard `"*"` match
6. `default_tier` from config
7. Most-restrictive tier (fail-closed)

#### Supported environment variables

| Env var | Platform |
|---------|----------|
| `TELEGRAM_ALLOWED_USERS` | Telegram |
| `DISCORD_ALLOWED_USERS` | Discord |
| `WHATSAPP_ALLOWED_USERS` | WhatsApp |
| `SLACK_ALLOWED_USERS` | Slack |
| `SIGNAL_ALLOWED_USERS` | Signal |
| `EMAIL_ALLOWED_USERS` | Email |
| `SMS_ALLOWED_USERS` | SMS |
| `MATTERMOST_ALLOWED_USERS` | Mattermost |
| `MATRIX_ALLOWED_USERS` | Matrix |
| `DINGTALK_ALLOWED_USERS` | DingTalk |
| `GATEWAY_ALLOWED_USERS` | Global (all platforms) |

Allow-all flags follow the same naming: `TELEGRAM_ALLOW_ALL_USERS`, `DISCORD_ALLOW_ALL_USERS`, etc., plus `GATEWAY_ALLOW_ALL_USERS`.

#### Troubleshooting auto-tier

**Auto-tier silently not working**

Check the gateway logs at startup. If any tier reference is invalid (e.g. `env_owner_tier: "nonexistent"`), auto-tier is disabled with a warning. All four tier references must point to existing tiers.

**First-entry owner promotion**

The first user ID in each platform's `*_ALLOWED_USERS` env var is promoted to `env_owner_tier`. This is logged at INFO level: `Auto-tier: TELEGRAM_ALLOWED_USERS first entry '111' → owner`. If you don't want this behavior, set `env_owner_tier` and `env_default_tier` to the same value.

**User gets wrong tier after pairing approval**

Pairing-approved users are injected at startup with `pairing_default_tier`. Users approved *after* startup are injected dynamically on their first message. Check `/whoami` to verify the "Granted by" source.

---

## What Gets Restricted

### Toolsets

When `allowed_toolsets` is not `["*"]`, the agent's toolset is intersected with the tier's allowed list. Tools outside the allowed toolsets are **removed** — the agent physically cannot call them.

Example: `allowed_toolsets: ["hermes-discord"]` means the agent only gets Discord-platform tools. No web search, no terminal, no browser, no file tools.

A warning is logged if the intersection is empty (likely a misconfiguration).

**Delegate tool propagation**: When a user's tier restricts their available tools, child agents spawned via the `delegate_task` tool inherit the same `allowed_tool_names` from the parent. A restricted user cannot escalate privileges by delegating to a sub-agent — the child agent's toolset is a subset of the parent's.

### Tool-Level Filtering & @groups

For finer control, use `allowed_tools` to specify individual tool names or `@group` shorthands. When `allowed_tools` is set, it **takes precedence** over `allowed_toolsets` — the toolset filter is bypassed and only the named tools are allowed.

```yaml
tiers:
  guest:
    allowed_tools:
      - "@web"      # Expands to: web_search, web_extract
      - "@read"     # Expands to: read_file, search_files
      - "clarify"   # Individual tool
    allow_exec: false
    allow_admin_commands: false
```

This is equivalent to:

```yaml
tiers:
  guest:
    allowed_tools:
      - "web_search"
      - "web_extract"
      - "read_file"
      - "search_files"
      - "clarify"
    allow_exec: false
    allow_admin_commands: false
```

Groups are expanded at config load time. Invalid group names are logged as warnings and skipped. Tool names are case-sensitive and must match the registered tool name exactly.

**Interaction with `allowed_toolsets`:**

| `allowed_tools` | `allowed_toolsets` | Behavior |
|-----------------|--------------------|----------|
| Not set (null) | `["*"]` | All tools allowed (default) |
| Not set (null) | `["web", "file"]` | Toolset-level filtering (legacy) |
| `["@safe"]` | Any value | **Tool-level filtering used**, toolsets ignored |
| `["@all"]` | Any value | All tools allowed (same as `"*"`) |
| `["mcp:notion:*"]` | Any value | All MCP tools from the Notion server |

| `["mcp:*:*"]` | Any value | All MCP tools from any server |

| `["mcp:weather:get_forecast"]` | Any value | Specific MCP tool |

**Security note:** Setting `allowed_tools: []` (empty list) removes all tools — the agent cannot do anything. This is a valid configuration for a "blocked" tier.

**Note:** MCP tools registered by Hermes follow the naming convention `mcp_{server}_{tool}` (e.g., `mcp_weather_get_forecast`). The `mcp:server:tool` pattern syntax maps to these names at filter time.

### Admin Slash Commands

Users with `allow_admin_commands: false` cannot use:

- `/provider` — show or change the LLM provider
- `/personality` — set a predefined personality
- `/sethome` — set the home channel
- `/reload-mcp` — reload MCP server configuration
- `/users` — manage user tier assignments (list, set, remove)

These are defined via the `admin_only` flag on `CommandDef` in `hermes_cli/commands.py`. To add more, set `admin_only=True` on the command definition.

### Owner-Only Commands

Some commands are restricted to the **owner tier** only (not available to admins):

- `/update` — update Hermes to the latest version

These are defined via the `owner_only` flag on `CommandDef`. The owner tier is determined by `env_owner_tier` (default: `"owner"`), which is automatically assigned to the first entry in each platform's `*_ALLOWED_USERS` env var.

Always allowed (not gated): `/new`, `/reset`, `/help`, `/status`, `/stop`, `/retry`, `/undo`, `/plan`, `/whoami`, and all quick commands.

### Gateway Commands for Permission Tiers

These commands are available in messaging platforms (Telegram, Discord, etc.) when permission tiers are configured:

#### `/whoami` — Show Your Access Level

Available to **all users**. Shows:

```
👤 Your Access Level

User ID: 111111111111111111
Name: Alice
Platform: discord
Tier: admin
Granted by: config.yaml
Exec access: ✅ Yes
Admin commands: ✅ Yes
Tools: All (unrestricted)
```

The "Granted by" field tells you where the tier comes from:
- **runtime** — set via `/users set` (persists across restarts)
- **config** — mapped in `config.yaml`
- **wildcard** — matched by the `"*"` wildcard entry
- **default** — fell through to `default_tier`

#### `/users` — Manage User Tiers (Admin Only)

Manage tier assignments at runtime without editing `config.yaml`.

**`/users list`** — Show all user tier assignments:

```
📋 User Tier Assignments

• 111111111111111111 → admin (config)
• 222222222222222222 → restricted (runtime, set by 111111111111111111)

Default tier: restricted
Available tiers: admin, restricted
```

**`/users set <user_id> <tier>`** — Assign a tier:

```
/users set 333333333333333333 admin
```

Runtime assignments take priority over config.yaml mappings. They persist in `~/.hermes/permissions.db` across gateway restarts.

**`/users remove <user_id>`** — Remove a runtime assignment:

```
/users remove 333333333333333333
```

The user falls back to their config.yaml mapping (or wildcard/default).

#### Runtime vs Config

| Aspect | Config (config.yaml) | Runtime (/users set) |
|--------|---------------------|---------------------|
| Priority | Lower | Higher (overrides config) |
| Persistence | File-based, requires restart to load changes | SQLite, immediate effect |
| Management | Edit YAML manually | Slash commands |
| Audit trail | Config file history | `granted_by` and `granted_at` in DB |

### Exec Approvals

When `allow_exec: false`, the user cannot `/approve` or `/deny` pending dangerous terminal commands. The approval hint ("Reply `/approve` to execute...") is also suppressed for these users.

Additionally, `allow_exec: false` provides defense-in-depth by stripping `terminal`, `process`, and `execute_code` from the agent's tool schema entirely. The agent physically cannot invoke these tools — it's not just an approval gate, the tools are removed from the model's available function list.

### Time Windows

When `time_restrictions` is set, messages outside the time window are blocked immediately — the agent never runs. The user receives the configured denial message (or the English fallback).

The time check is applied in both the normal path and the running-agent interrupt path (defense-in-depth), with `/stop` always allowed so users can kill their agent even outside allowed hours.

### Rate Limiting

When `requests_per_hour` is set on a tier, each user assigned to that tier is limited to that many **agent requests** per hour. Slash commands (like `/whoami`, `/help`, `/stop`) are not counted — only messages that trigger the agent.

```yaml
tiers:
  guest:
    allowed_tools: ["@web", "clarify"]
    requests_per_hour: 10   # 10 messages/hour
    allow_exec: false
    allow_admin_commands: false

  blocked:
    allowed_toolsets: ["*"]
    requests_per_hour: 0    # Completely blocked
```

Behavior:
- `null` (default) — unlimited requests
- `0` — all agent requests are blocked (user can still use slash commands like `/whoami`)
- Positive integer — that many requests per rolling hour window

The rate counter resets at the top of each hour (bucket-based, not sliding window). The counter is in-memory and resets on gateway restart. Rate limit counters are scoped per `(platform, user_id)` — a user with the same numeric ID on both Telegram and Discord gets independent counters.

The `/whoami` command shows rate limit status: "7/10 requests remaining this hour" or "Unlimited".

### Agent Context (System Prompt)

When a tier is active, the agent receives context about its restrictions in the system prompt. This helps it respond gracefully instead of failing silently.

The injected context includes:
- **Tier name** — e.g. "You are operating with restricted permissions (tier: guest)."
- **Tool count** — e.g. "You have 5 tools available plus 2 MCP tool pattern(s)."
- **Toolset restrictions** — e.g. "Only these toolsets are available: hermes-discord, web."
- **Exec restriction note** — e.g. "You cannot approve or deny terminal command executions..."
- **Rate limit** — e.g. "Rate limit: 10 requests per hour."

This allows the agent to proactively explain restrictions: "I don't have terminal access on this tier, but I can show you the command to run yourself."

---

## Available Tool Groups

Use `@group` names in `allowed_tools` to reference pre-defined tool sets:

### Capability Groups

| Group | Tools |
|-------|-------|
| `@web` | `web_search`, `web_extract` |
| `@read` | `read_file`, `search_files` |
| `@write` | `write_file`, `patch` |
| `@media` | `vision_analyze`, `image_generate`, `text_to_speech` |
| `@code` | `terminal`, `execute_code` |
| `@system` | `cronjob`, `delegate_task` |
| `@memory` | `memory`, `session_search` |
| `@skills` | `skills_list`, `skill_view` |
| `@browser` | `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_scroll`, `browser_back`, `browser_press`, `browser_close`, `browser_get_images`, `browser_vision`, `browser_console` |
| `@messaging` | `send_message` |
| `@planning` | `todo` |
| `@clarify` | `clarify` |
| `@honcho` | `honcho_context`, `honcho_profile`, `honcho_search`, `honcho_conclude` |
| `@homeassistant` | `ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service` |

### Composite Groups

| Group | Expands To |
|-------|-----------|
| `@safe` | `@web` + `@read` + `@media` + `@skills` + `clarify` — safe tools without terminal or file writes |
| `@all` | All tools (wildcard, same as `"*"`) |

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

### Three-tier setup with toolsets (Discord)

A realistic setup using toolset-level filtering:

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
      requests_per_hour: 30      # 30 messages/hour
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

### Tool-level filtering with @groups

Fine-grained control using individual tools and `@group` shorthands:

```yaml
permission_tiers:
  default_tier: "guest"
  tiers:
    admin:
      allowed_tools: ["@all"]    # Everything
      allow_exec: true
      allow_admin_commands: true

    member:
      allowed_tools:
        - "@safe"       # web + read + media + skills + clarify
        - "@memory"     # memory + session_search
        - "@planning"   # todo
      allow_exec: false
      allow_admin_commands: false

    guest:
      allowed_tools:
        - "@web"        # web_search, web_extract
        - "clarify"     # just clarify
      allow_exec: false
      allow_admin_commands: false
      time_restrictions:
        start: "08:00"
        end: "22:00"
        timezone: "Europe/Vienna"

  users:
    "111111111111111111": { tier: "admin", locale: "en" }
    "222222222222222222": { tier: "member", locale: "de" }
    "*":                  { tier: "guest", locale: "en" }
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

The gateway reads config at startup. After editing `config.yaml`, restart the gateway or send `/reload-mcp` (admin-only command). Runtime changes via `/users set` take effect immediately — no restart needed.

**Runtime tier not working**

- The tier name must exist in `config.yaml`'s `tiers` section. If you rename or remove a tier from config, runtime assignments referencing it fall back to config/default resolution.
- Check `/users list` to see runtime assignments and their granted_by/granted_at metadata.
- Runtime overrides persist in `~/.hermes/permissions.db`. To clear all runtime overrides, delete this file and restart the gateway.

**`/whoami` shows wrong tier**

- Check the "Granted by" field to understand the resolution source.
- If it says "runtime", use `/users remove <user_id>` to clear the runtime override.
- If it says "wildcard", the user wasn't found in the explicit user mapping and fell through to `"*"`.

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

---

## Design Decisions & Limitations

### Binary command gating (not per-command allowlists)

The issue spec proposed `permissions.tiers.<name>.commands` as a per-tier list of allowed slash commands. This implementation uses a simpler **binary** approach: `allow_admin_commands: true/false`. Admin commands are defined once via the `admin_only` flag on `CommandDef` in the central registry.

**Why:** Per-command lists create significant configuration surface (every new command needs to be listed in every tier) and are error-prone (operators can accidentally exclude essential commands like `/stop`). The binary flag covers the 80% case — either a user can manage the agent (admin commands) or they can't. The `owner_only` flag provides a second tier of restriction for destructive commands.

**Future:** Per-command allowlists can be added as an optional `allowed_commands` field on `TierDefinition` without breaking existing configs.

### Memory access is all-or-nothing per tier

The issue spec's capability matrix shows User tier getting memory *read* but not *write*. The implementation gives tiers that include `@memory` both read and write access — there's no read-only mode.

**Why:** The memory system uses a single `memory` tool for both reading and writing. Splitting it into `memory_read`/`memory_write` would require changes to `tools/registry.py`, `run_agent.py` dispatch, and all downstream consumers. This is a larger refactor that's better suited for a follow-up PR.

**Workaround:** Operators who want read-only memory for a tier can include `memory_search` (search past conversations) without `memory_write` (write to MEMORY.md). Both are individual tools that can be specified in `allowed_tools`.

### `/model` command is CLI-only

The issue spec shows `/model` as Owner/Admin-only. In this implementation, `/model` is not a gateway command — it's only available in the interactive CLI. Gateway users change the model via the config file or through the owner's terminal access. No gateway command needs gating.

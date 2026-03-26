---
name: model-routing-template
description: Generic template for strategic model delegation. Customize the provider catalog for your setup, then use the decision framework to route tasks to the right model based on cost, capability, and complexity.
version: 1.0.0
author: Agatha (Hermes Agent)
license: MIT
metadata:
  hermes:
    tags: [delegation, model-selection, cost-optimization, subagents, template]
    related_skills: [autonomous-ai-agents, plan]
---

# Model Routing Strategy (Template)

Framework for delegating subagent tasks to the right model. Fill in your
provider catalog in `references/providers.yaml`, then follow the decision
tree to route tasks based on cost, capability, and complexity.

## Quick Start

1. Copy `references/providers-example.yaml` to `references/providers.yaml`
2. Edit it with YOUR providers, models, costs, and roles
3. Follow the Decision Framework below when delegating

## Your Provider Catalog

Edit `references/providers.yaml` to define your available models.
See `references/providers-example.yaml` for a fully worked example.

## Decision Framework

### Step 1: Should I delegate at all?

**Handle yourself (no delegation) when:**
- Routine tasks you can do well (file edits, simple commands, config changes)
- Tasks requiring your personality/style (chatting with the user, channel responses)
- Anything under 3 tool calls
- Quick lookups and formatting

**Delegate when:**
- The task needs a genuinely different capability (deep reasoning, second opinion, vision)
- You're going in circles and need fresh perspective
- Parallel work would save real time (batch mode)
- The user explicitly asks for a specific model

### Step 2: Which model?

Follow the tiers defined in your `providers.yaml`. General principles:

**TIER 0 — Handle yourself**
- Default for everything. Don't over-delegate.

**TIER 1 — Cheap/Free (your bulk workhorses)**
- Models that cost nothing or nearly nothing
- Use for: subagent tasks, parallel execution, exploration, tight-scope execution
- Burn freely — these exist to be used at scale

**TIER 2 — Standard (your reliable specialists)**
- Capable models at moderate cost
- Use for: second opinions, code review, architecture, tasks that need more brain
- Each call is a conscious spend — use when Tier 1 isn't enough

**TIER 3 — Expensive (frontier models, last resort)**
- The most capable but most costly models
- Use ONLY when:
  - You've exhausted cheaper options and still can't crack it
  - Going in circles on a genuinely hard problem
  - The user explicitly requests it
  - Decisions with serious consequences (production, security, money)
- NEVER self-select without first trying cheaper options

### Step 3: How many?

**Parallel patterns:**
- **Junior army** — many cheap models with tight plans, running in parallel
- **Scout party** — send cheap models first, expensive models as safety net
- **Crowd wisdom** — multiple standard models for diverse perspectives
- **Rule of thumb**: N cheap opinions < 1 expensive opinion (if the cheap ones can deliver)

### Step 4: When the user is engaged

**User is in the conversation and invested:**
- Present the tradeoff, don't just spend. It's their budget.
- Before escalating beyond standard tier, offer the choice:
  "I've got inputs from X and Y. I could add Z for another perspective,
  or if this warrants it we could go to [expensive model]. Your call."
- The user may know context you don't.

**User is NOT around (autonomous mode):**
- Follow the escalation ladder strictly. No asking.
- Start cheap, escalate only when genuinely stuck.
- Never skip steps.

## Escalation Ladder

Adapt this to your provider catalog. General pattern:

```
1. Try yourself                  (free)
2. Cheap model for the task type (~0.1x)
3. Different cheap perspective   (~0.1x)
4. Standard model — second opinion (1x)
5. Different standard model       (1x)
6. Specialist model               (1x)
7. Multiple models in parallel    (1x each)
8. Frontier/expensive model       (3x+)
     └── ONLY after all above failed
```

## Cost-Effective Patterns

### The "Many for One" Pattern
Instead of one expensive call, get multiple diverse opinions at standard cost:
```
3x standard model = 1x expensive model
But you get 3 different perspectives instead of 1.
```

### The "Free Stack" Pattern
Maximize throughput at minimal cost:
```
1 heavy analysis    (cheap tier, deep thinker)
1-2 explorers       (cheap tier, fast scouts)
N junior executors  (cheapest tier, tight plans)
```

### The "Scout Party" Pattern
Send expendable models first, safety net last:
```
2+ cheapest models   → investigate (might succeed)
1 slightly better model → safety net (only if needed)
```
If the cheap models already returned good intel, skip the safety net.

## Roles (define in your catalog)

Each model should have a clear role. Common patterns:

| Role | Description | Example |
|------|-------------|---------|
| Coordinator | You yourself — orchestration, chat, routing | The agent's default model |
| Heavy Hitter | Strong reasoning + coding, near-frontier | GLM-5, GPT-5.4 |
| Explorer | Fast scouting, broad searches, codebase recon | GLM-4.7-flash, Gemini Flash |
| Fixer | Tight plan, narrow scope, parallel execution | GLM-4.6, GPT-4.1 |
| Designer | Frontend/UI specialist, polished output | Claude Sonnet |
| Coder | Pure coding specialist, architecture reviews | GPT-5.3-Codex |
| Safety Net | Last-resort scout when others fail | Claude Haiku |
| Frontier | Nuclear option, absolute last resort | Claude Opus |

## User Override Phrases

Define natural-language triggers in your catalog. Examples:
- "use opus" / "use sonnet" — explicit model selection
- "get a second opinion" — standard tier model
- "think with me" — escalate beyond cheap tier
- "burn the budget" — clearance for expensive model

## Notes

- Cost multipliers are often per-request, not per-token. Check your provider.
- Some providers have monthly quotas, others are per-token. Track accordingly.
- The cheapest model that CAN do the task IS the right choice.
- "Expensive" doesn't mean "better for everything" — match capability to task.
- When in doubt, start at Tier 1 and escalate only with evidence.

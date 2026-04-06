# Changelog

## v0.5.0 — 2026-04-06

### Feature: Multi-format session support (OpenClaw + Claude Code CLI + Langfuse trace)

**Formats supported:**
- OpenClaw JSONL sessions (original format)
- Claude Code CLI JSONL sessions (native CLI logs)
- Langfuse trace JSON arrays (from Langfuse platform)

**Parser changes:**
- Added `detect_format()`: distinguish JSONL vs JSON array
- Added `detect_jsonl_subformat()`: distinguish OpenClaw vs Claude Code CLI (with two-pass signal detection)
- Added `convert_trace_to_events()`: convert Langfuse trace SPAN/GENERATION events to unified internal format
- Added `extract_tool_calls_trace()`: extract tool invocations from trace-converted events
- Added `extract_tool_calls_claude_code()`: extract tool calls from Claude Code CLI message.content format
- Modified `extract_tool_calls()`: route to appropriate handler based on format detection
- Modified `load_events()`: handle both JSONL and JSON array inputs
- Fixed `detect_jsonl_subformat()`: prioritize CLI signals (tool_use/tool_result) in first pass to handle traces correctly
- Added `env_bootstrap` to tool recognition list and `normalize_tool_name()` mapping

**Quality improvements:**
- Fixed 7 bare `except:` clauses → specific exception types
- Optimized format detection: O(2n) → O(n) with early exit
- M3 verification: confirmed zero mutations, idempotent operations

**Test coverage:**
- T1 (OpenClaw): 46 tool_calls ✅ (zero regression)
- T3 (Claude Code CLI): 99 tool_calls ✅ (unchanged)
- T4 (Langfuse trace): 134 tool_calls ✅ (newly verified)

---

## v0.2 — planned

### Feature: Focus configuration

**Background**

The skill is a general-purpose analyzer, but users care about specific subsets of CLI calls. For example, a DevOps session might focus on `kubectl` and `helm`; a frontend session on `npm` and `vite`. Without focus configuration, efficiency and stats treat all exec calls equally, making the numbers less meaningful.

**Config file**

`~/.claude/skills/clawsession-insights/config.json` (optional, skill works without it):

```json
{
  "focus": {
    "label": "Infra CLI",
    "patterns": ["kubectl", "helm", "terraform", "git"]
  }
}
```

- `label`: display name shown in stats/report headers
- `patterns`: single-token first-token match. Strip leading `VAR=val` env pairs from command, then check if the first token exactly equals any pattern. `"kubectl"` matches `kubectl get`, `kubectl apply`, `kubectl` (no args). `"git"` matches all git invocations. Case-sensitive.
- No config → use old display format, all exec treated as CLI calls (fully backward-compatible)
- Known limitation: config is global (per-machine), not per-project. Per-project override is a future enhancement.

**Parser changes (`analyze_session.py`)**

1. Load `config.json` at startup if it exists; extract `focus.patterns` and `focus.label`.
2. Add `is_focus_match(command, patterns)` helper:
   - Split command into tokens (shlex)
   - Skip leading `VAR=val` tokens (contain `=` and no spaces)
   - Return `True` if first remaining token is in the patterns set
3. In `extract_tool_calls`, tag each command:
   ```python
   "is_focus": is_focus_match(cmd["command"], focus_patterns) if focus_patterns else True
   ```
   Note: `process` tool calls are not separate entries in `commands[]` — they are resolved as part of the exec process-chain. No separate tagging needed.
4. In `extract_tool_calls`, prefer `details.durationMs` over timestamp diff for exec duration:
   ```python
   duration_ms = details.get("durationMs") or (result_ts_ms - call_ts_ms)
   ```
5. In `calculate_stats`, add:
   ```python
   "focus_label": focus_label,          # str e.g. "Infra CLI"; None if no config
   "focus_calls": int,                  # commands where is_focus == True
   "focus_errors": int,                 # exit_code != 0 where is_focus == True
   "other_cli_calls": int,              # commands where is_focus == False
   "other_cli_errors": int,             # exit_code != 0 where is_focus == False
   "other_tool_calls": int,             # sum of tool_usage counts excluding exec and process
   "other_tool_errors": int,            # non-exec toolResults where isError == True
   ```
   `isError` field is present on all toolResult messages in the JSONL — reliable without content parsing.

**Additional parser improvement (found during JSONL inspection)**

- exec `toolResult.details.durationMs`: OpenClaw records the actual measured exec duration here. Use this instead of `result_ts_ms - call_ts_ms` when available; fall back to timestamp diff otherwise.

**SKILL.md changes**

Stats block — **only when config present** (Phase 5 + Phase 7):
```
Focus calls (<label>): <focus_calls>  errors: <focus_errors>
  <kubectl apply ×10  kubectl get ×4  helm upgrade ×2  git clone ×1  …>
Other CLI: <other_cli_calls>  errors: <other_cli_errors>
Other tools: <other_tool_calls>
```
Without config, keep existing format: `Tool calls: <N>  Errors: <N>`.

Command-name breakdown: group focus commands by **first two tokens of the original command** (not normalized — preserve `kubectl apply`, `helm upgrade` etc.), show `name ×N` sorted by count desc.

Efficiency (Phase 2):
- With config: `efficiency_pct` = success rate of focus commands only (excluding read-only)
- Without config: unchanged (all exec)

Errors table (Phase 7):
- Rows: focus command errors only (full detail)
- Footer (omit if both zero): `> <other_cli_errors> other CLI errors · <other_tool_errors> tool errors not shown`

Command Log (Phase 7):
- With config: focus commands shown individually; non-focus CLI collapsed per task:
  `| — | *(X other CLI commands)* | ok/mixed | — |`
- Without config: all commands shown individually (current behavior)

Loops (Phase 4 + Phase 7):
- **All loops shown with full description**, regardless of focus status

UX Friction Points (Phase 7):
- **All friction shown**, regardless of focus status

---

## v0.1 — 2026-03-29

Initial versioned snapshot. Changes from original skill:

### Quality scoring
- **Error loop penalty is now severity-weighted by loop count**: ≤5 iterations −8, 6–30 iterations −15, >30 iterations −25 (capped at −40). Previously flat −10 per loop regardless of size.
- **Abandoned task penalty added**: −8 per task with `status == "abandoned"`, capped at −20.
- **Efficiency thresholds tightened**: <50% −20, 50–69% −10, 70–84% −5, ≥85% 0. Previously only two bands (<50% or <80%).
- **Grade thresholds lowered**: A ≥90 (was ≥85), B ≥75 (was ≥70), C ≥60 (was ≥55), D ≥45 (was ≥40), F <45.

### Cost data handling
- **`cost_unavailable` flag added**: set when `message_costs[]` is empty or all entries have `cost_usd == 0`. When true: cost columns show `—` instead of `$0.000`, `cost_per_task` in header shows `—`, high_burn calculation is skipped, Performance section shows `N/A (not reported by provider)`.
- **Task Breakdown table**: cost column shows `—` when `cost_unavailable`; adds blockquote warning noting the provider did not report costs.

### Report header
- **Time-slice note**: when `--since` or `--until` is passed, the report header includes a blockquote noting the quality score covers only that window, not the full session.

### Loop count and duration accuracy
- **Loop detection now captures full extent, not just first window.** Previously `detect_loops` reported count and duration only within the initial 10-command detection window, so a 60-repetition loop would show as count≈10, duration≈25s. Now it walks forward from the first match, extending the group as long as gaps between same-command occurrences are ≤ window (10). This gives accurate count and duration for large loops.

### Tokens unavailable handling
- **`tokens_unavailable` flag added**: when `stats.total_tokens == 0`, tokens display as `N/A (not reported by provider)` instead of `0`. Mirrors cost_unavailable treatment.

### Command Log duration
- **Duration column now always populated from `commands[].duration_ms`**. The parser computes this from toolCall/toolResult timestamp diff for every exec — it's always available. Formatting rules: <1000ms → `Xms`, ≥1000ms → `Xs` or `Xm Ys`, collapsed loops → `~Xm Ys` (sum). `—` only when command has no entry in `commands[]`.

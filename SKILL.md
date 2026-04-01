---
name: analyze-session
description: Analyze an OpenClaw or Claude Code CLI session JSONL file — segments tasks, scores agent quality, surfaces loops/errors/timing, asks targeted questions, and writes a Markdown report. Supports both OpenClaw format (type:session/message/toolResult) and Claude Code CLI format (type:user/assistant/queue-operation).
---

# Session Analyzer

Analyze an OpenClaw session log and produce a structured Markdown report.

## Usage

```
/analyze-session <path-to-session.jsonl> [--since HH:MM] [--until HH:MM] [--silent]
```

## Instructions

Follow these phases exactly in order. Do not skip phases. Do not reorder.

---

### Phase 1 — Run the parser

```bash
python3 ~/.claude/skills/clawsession-insights/analyze_session.py <input_path> [--since <arg>] [--until <arg>]
```

If the file does not exist, tell the user:
> "Parser not found. Please install the skill first: https://github.com/jarosik9/openclaw-session-analysis"
and stop.

Capture the full JSON output. If the parser exits with a non-zero code, show the error and stop.

The JSON contains: `session`, `stats`, `timing`, `loops`, `errors`, `commands[]`,
`tool_usage`, `conversation[]`, `message_costs[]`, `thinking[]`.

---

### Phase 2 — Math pre-processing

Compute per-task signals from `commands[]` and `message_costs[]`. This phase runs
**after Phase 3** returns task time ranges, but is described here because it feeds
Phase 5 and Phase 7.

For each task returned by Phase 3, filter by `start_time ≤ timestamp ≤ end_time`:

**tokens_unavailable** — set `true` if `stats.total_tokens == 0`. When true, display
tokens as `N/A (not reported by provider)` wherever tokens appear.

**cost_unavailable** — set `true` if `message_costs[]` is empty OR all entries have
`cost_usd == 0`. When `cost_unavailable = true`:
- `cost_usd` for all tasks = `null` (display as `—`)
- Skip `cost_per_min` and `high_burn` (treat as unknown)
- `cost_per_min` in header and stats block = `—`
- Add note in Performance section: `Cost: N/A (not reported by provider)`

**cost_usd** — if not `cost_unavailable`: sum `cost_usd` from `message_costs[]` within
the task window. Otherwise `null`.

**cost_per_min** — `cost_usd / (duration_ms / 60000)`. Flag `high_burn = true`
if `cost_per_min > 2 × session_avg_cost_per_min` where
`session_avg = stats.total_cost_usd / (session.duration_ms / 60000)`.
Skip entirely when `cost_unavailable`.

**efficiency_pct** — among task commands, exclude read-only commands
(`ls`, `cat`, `head`, `tail`, `echo`, `pwd`, `which`, `grep`, `find`, `rg`).
Of remaining "meaningful" commands: `round(exit_0_count / total * 100)`.
If no meaningful commands: `null` (display as `—`).

**quality_score** — computed as the average of four dimensions (each 0–100):

**dim_execution** — session-wide command success rate:
```
meaningful = commands[] excluding (ls, cat, head, tail, echo, pwd, which, grep, find, rg)
dim_execution = round(exit_0_count / len(meaningful) × 100) if meaningful else 100
```

**dim_completion** — task completion rate:
```
dim_completion = round(completed_tasks / total_tasks × 100) if tasks else 100
```

**dim_depth** — maximum task complexity detected in commands[] text (take highest match):
```
"util abi encode" OR "bridge" OR "layerzero" OR "cross-chain"  → 100
"tx call" OR "contract"                                         → 80
"pact submit"                                                   → 60
"tx transfer"                                                   → 40
"faucet" OR "onboard"                                          → 20
(none of the above)                                            → 10
```

**dim_ux** — user experience smoothness:
```
dim_ux = 100
- confirmed context_loss (Phase 4 thinking confirmed): -10 each, capped at -30
- abandoned tasks: -8 each, capped at -20
- error_loop with count > 10: -15 each, capped at -30   (large unexplained loops)
- error_loop with 3 ≤ count ≤ 10: -5 each, capped at -20
Note: polling_loop and exploration_loop do NOT affect dim_ux.
dim_ux = max(dim_ux, 0)
```

```
quality_score = round((dim_execution + dim_completion + dim_depth + dim_ux) / 4)
```
Grade: 90–100 = A, 75–89 = B, 60–74 = C, 45–59 = D, <45 = F.

If no tasks detected: `dim_completion = 100`. Replace `dim_depth` with:
```
dim_depth = 10 + (15 if tool_errors/tool_calls < 0.3 else 0)
```
Note in report: "score based on session-level signals — no tasks detected."

---

### Phase 3 — LLM segmentation

Make one internal LLM call to segment the conversation into tasks.

**Task definition:** A task requires a clear actionable goal delegated to the agent.
Casual questions, clarifications, small talk are NOT tasks. Task durations need not
cover the full session.

**Transcript formatting:** Format `conversation[]` as `[HH:MM] ROLE: text`.
Truncate: user turns at 400 chars, assistant turns at 600 chars. If total transcript
exceeds 20 000 chars, reduce to 300/350. Always preserve the last user message in full.
If >80 turns: keep all user turns, keep every other assistant turn, always keep last 10
turns. Add note: "transcript thinned — some assistant turns omitted."

**Prompt:**
```
Below is a transcript of a Claude Code session with timestamps.

TASK SEGMENTATION:
Identify the distinct tasks the user was working on. A task must have a clear,
actionable goal delegated to the agent. Do NOT create tasks for casual questions,
clarifications, or acknowledgements. Not all turns need to belong to a task.
Prefer fewer, broader tasks over over-splitting.

A new task starts when the user shifts to a clearly different actionable goal.
Retries, corrections, and follow-ups within the same goal are part of the same task.
Task time ranges must be non-overlapping. If two tasks share a boundary, end the
earlier task at the timestamp where the new one begins.

CONTEXT LOSS:
For each task, identify "repeated_question" signals only: cases where the agent
asked the user for the same information more than once within this task.

For each task output:
- index (1-based integer)
- title (short verb phrase, ≤8 words)
- start_time, end_time (ISO timestamps; non-overlapping)
- status: "completed" | "abandoned" | "unclear"
- context_loss: array of {type, description}, or []

Return JSON array only, no prose. Return [] if no clear tasks found.

TRANSCRIPT:
<formatted transcript>
```

After Phase 3 returns, immediately run Phase 2 per-task enrichment to attach
`cost_usd`, `cost_per_min`, `high_burn`, `efficiency_pct` to each task.
Then compute `quality_score` and `quality_grade`.

---

### Phase 4 — Targeted thinking analysis (conditional)

Skip entirely if either condition is false:
1. `thinking[]` is non-empty
2. At least one of:
   - any `error_loop` with `count > 10`
   - any task `status == "abandoned"`
   - any task `context_loss` non-empty

Do NOT trigger for: `polling_loop`, `exploration_loop`, or `error_loop` with `count ≤ 10`
(root cause is usually evident from error_text alone).

**Cap: max 3 LLM calls.** Priority when >3 triggers: abandoned tasks first, then loops, then context loss.

For each trigger, extract `thinking[]` entries in the relevant time range and make a focused call:

**Loop trigger:**
```
The agent repeated "<command_normalized>" × N times between <start> and <end>.
Below are the agent's thinking blocks during this period.
In 1–2 sentences: why did the agent keep repeating this? Was it aware of the loop?

<thinking entries>
```

**Abandoned task trigger:**
```
The agent was working on "<title>" but did not complete it.
Below are the agent's last 3 thinking blocks before the task ended.
In 1–2 sentences: what caused the agent to stop?

<last 3 thinking entries before end_time>
```

**Context loss trigger:**
```
The agent showed signs of context loss during "<title>": <description>.
Below are relevant thinking blocks.
In 1–2 sentences: does the thinking confirm genuine context loss or intentional re-check?

<thinking entries in task time range>
```

Store each result as `root_cause` on the relevant loop or task object.

---

### Phase 5 — Display stats and ask questions

**Skip questions if `--silent` flag was passed.** Display the stats block either way, then in silent mode proceed directly to Phase 7.

Display the following block:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Session: <session.id>
Date:    <session.start_time>
Model:   <session.model> (<session.provider>)
User:    <session.user>
CWD:     <session.cwd>
Duration: <session.duration_ms as Xm Ys>

Quality: <quality_score>/100 (<quality_grade>)  ·  <N completed>/<N total> tasks  ·  <$X.XX/min or "—" if cost_unavailable>/min
  Execution  <bar>  <dim_execution>
  Completion <bar>  <dim_completion>
  Depth      <bar>  <dim_depth>  (<label: e.g. "cross-chain" or "transfer only">)
  UX         <bar>  <dim_ux>
(bar = "█"×round(score/10) + "░"×(10−round(score/10)))

Stats
  Turns: <stats.total_turns>  Tool calls: <stats.tool_calls>  Errors: <stats.tool_errors>
  Tokens: <stats.total_tokens or "N/A (not reported by provider)" if tokens_unavailable>  Cost: <$X.XX or "N/A (not reported by provider)" if cost_unavailable>

Timing
  LLM:   <timing.llm_ms as Xm Ys>  (<timing.llm_pct>%)  avg <timing.llm_avg_ms>ms  max <timing.llm_max_ms>ms
  CLI:   <timing.cli_ms as Xm Ys>  (<timing.cli_pct>%)  avg <timing.cli_avg_ms>ms  max <timing.cli_max_ms>ms
  User:  <timing.user_ms as Xm Ys>  (<timing.user_pct>%)
  Idle:  <timing.idle_ms as Xm Ys>  (<timing.idle_pct>%)

Tasks detected: <N>  (covers <Xm> of <Ym session>)
<for each task: "  N. <title>  HH:MM→HH:MM  Xm  $X.XXX  eff:XX%  [x]/[ ] [⚠ high burn]">

Loops detected: <count>
<for each loop: "  • <command_normalized> × <count> (<loop_type>) — <duration as Xm Ys>">

Errors: <count>
<for each error (max 5): "  • [<exit_code>] <command truncated to 60 chars>">
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Then ask questions **one at a time**, waiting for each answer. Pick top 2–5 from this
priority pool (never exceed 5):

1. Abandoned task: "Task N ('[title]') appears abandoned. What happened?"
2. Error loop (error_loop only, NOT polling_loop or exploration_loop): "The agent repeated `<cmd>` × N times. Was this expected or a bug?"
3. Low efficiency (<50%): "Task N had a X% command success rate. Was the agent struggling?"
4. High burn: "Task N cost $X in Nm — above session average. Was this expected?"
5. Context loss: "Task N shows context loss: [description]. Did you notice the agent losing track?"
6. (fallback if none above): "What was the goal of this session, and was it achieved?"

---

### Phase 6 — Collect answers

Hold all answers in conversation context. Do not write to any file.

**Skip if `--silent` flag was passed.**

---

### Phase 7 — Generate and write the report

Write the report to:
```
<absolute_directory_of_input_file>/<input_filename_without_extension>_analysis.md
```

If `--since` or `--until` was passed, include the range in the filename:
```
<stem>_14h30-16h00_analysis.md
```

**Report structure:**

```markdown
# Session Analysis: <session.id>

**Quality: <score>/100 (<grade>)**  ·  <N>/<N> tasks  ·  <$X.XX/min or "—" if cost_unavailable>/min
```
  Execution  <bar>  <dim_execution>
  Completion <bar>  <dim_completion>
  Depth      <bar>  <dim_depth>  (<depth label>)
  UX         <bar>  <dim_ux>
```
**Date:** <date> | **Model:** <model> | **Duration:** <Xm Ys>
**User:** <user> | **CWD:** <cwd>
[> *Generated in silent mode — no user input collected.*]
[> *Time window: <HH:MM>–<HH:MM> — quality score covers this window only, not the full session.*  (include only when --since or --until was passed)]

---

## Summary
<LLM-generated — see prompt below>

## Task Breakdown
| # | Task | Duration | Cost | Efficiency | Status |
|---|------|----------|------|------------|--------|
<one row per task; cost = "—" when cost_unavailable; ⚠ on cost if high_burn; efficiency = "—" for null>

<if high_burn: "> ⚠ Task N: high token burn rate ($X/min vs avg $X/min)">
<if cost_unavailable: "> ⚠ Cost data unavailable — provider did not report token costs for this session.">

## Conversation Log
<for each entry in conversation[]: "[HH:MM] ROLE: text truncated at 120 chars">
(Tool calls and tool results are not shown here.)

## UX Friction Points
<LLM-generated — see prompt below>

## Agent Anomalies

### Loops
<table if loops exist, else "None detected.">
| Normalized Command | Type | Count | Duration | Period | Root Cause |
|--------------------|------|-------|----------|--------|------------|

### Errors
<table if errors exist, else "None.">
| Time | Command | Exit Code | Error (first 80 chars) |
|------|---------|-----------|------------------------|

### Tool Usage
| Tool | Calls |
|------|-------|

## Command Log
<partitioned by task if tasks detected; flat list otherwise>

### Task N — <title>
| Time | Command | Status | Duration |

### Other
| Time | Command | Status | Duration |

Duration for each row must be taken from `duration_ms` in `commands[]`, formatted as:
- <1000ms → show as `Xms` (e.g. `33ms`)
- ≥1000ms → show as `Xs` or `Xm Ys` (e.g. `10s`, `1m 35s`)
- When multiple repeated commands are collapsed into one row (e.g. `×10`), show the sum as `~Xs` or `~Xm Ys`
- Write `—` only if the command has no matching entry in `commands[]`

## Performance & Timing

**Total duration:** <Xm Ys>

| Type | Total | % | Avg | Max |
|------|-------|---|-----|-----|
| LLM inference | <llm_ms as Xm Ys> | <pct>% | <avg>ms | <max>ms |
| CLI execution | <cli_ms as Xm Ys> | <pct>% | <avg>ms | <max>ms |
| User response | <user_ms as Xm Ys> | <pct>% | — | — |
| Idle / other  | <idle_ms as Xm Ys> | <pct>% | — | — |

**Tokens:** <total_tokens> | **Cost:** <$X.XX | "N/A (not reported by provider)" if cost_unavailable>

## Systemic Issues
<only include if any error matches a known pattern below; omit section entirely if none>
Flag these patterns from `errors[]`:
- `error_text` contains "deprecated"                  → ⚠️ API deprecation (known CLI flag change)
- `error_text` contains "CORE_API_12007"              → ⚠️ Missing --src-addr (recurring pattern)
- `error_text` contains "cannot be combined"          → ⚠️ CLI flag conflict (--spec-json + --permissions)
- `error_text` contains "403" AND "policy"            → ⚠️ Agent permission boundary (policy management requires owner)
- `error_text` contains "pacts:write"                 → ⚠️ Missing pacts:write scope (Option B onboarding API key may not include this scope — known bug)

For each match, output one line: `- **[pattern name]:** <command truncated to 60 chars> — <brief explanation>`
```

**LLM call for narrative sections** — make one call to generate Summary and UX Friction Points:

```
You are writing two sections of a session analysis report.

SESSION DATA:
- Quality: <score>/100 (<grade>)<quality_note if no tasks>
- Tasks: <N completed> completed, <N abandoned> abandoned
- Loops: <list with type and count, or "none">
- Error rate: <tool_errors>/<tool_calls> commands failed
- User answers: <answers, or "none — silent mode">

TASKS:
<task list: index, title, status, efficiency_pct, high_burn, context_loss, root_cause>

WRITE:

## Summary
3–5 sentences. Cover: (1) what the user was trying to accomplish; (2) what the
agent did; (3) overall outcome referencing quality score. Do not list tasks.
If silent mode, note where intent was inferred rather than stated.

## UX Friction Points
Bullet points for friction only. Per bullet: **Task N [type]:** description.
Add > *Thinking: root_cause* blockquote if available.
Incorporate user answers where relevant.
Write "None detected." if nothing to report.
Do not invent friction points — only report what the data shows.
Signals to cover in order: context_loss, low efficiency (<50%), error_loops (NOT polling_loop or exploration_loop), high_burn.
Skip signals the user confirmed were expected.
```

After writing the report, print:
```
✓ Report written to <output_path>
  Quality: <score>/100 (<grade>)  ·  <N>/<N> tasks  ·  $<X.XX>/min
  Key issue: <most prominent error_loop or error, or "None detected">
  Duration breakdown: LLM <pct>% / CLI <pct>% / User <pct>% / Idle <pct>%
```

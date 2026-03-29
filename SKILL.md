---
name: analyze-session
description: Analyze an OpenClaw session JSONL file — segments tasks, scores agent quality, surfaces loops/errors/timing, asks targeted questions, and writes a Markdown report.
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
- `cost_per_task` in header and stats block = `—`
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

**quality_score** — computed once across all tasks:
```
score = 100

Error loops (not polling_loop) — severity by loop count:
  count ≤ 5: -8  |  count 6–30: -15  |  count > 30: -25
  Total capped at -40

Context loss confirmed by Phase 4 thinking: -10 each, capped at -30

Efficiency signal (weighted mean across tasks with non-null efficiency_pct):
  < 50%: -20  |  50–69%: -10  |  70–84%: -5  |  ≥ 85%: 0

Abandoned tasks (status == "abandoned"): -8 each, capped at -20

High burn tasks (skip when cost_unavailable): -5 each, capped at -10

score = max(score, 0)
```
Grade: 90–100 = A, 75–89 = B, 60–74 = C, 45–59 = D, <45 = F.

If no tasks detected, replace efficiency signal with: `-15 if tool_errors/tool_calls > 0.5`,
`-8 if > 0.3`. Note: "score based on session-level signals — no tasks detected."

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
1. At least one of: `loops` non-empty, any task `status == "abandoned"`, any task `context_loss` non-empty
2. `thinking[]` is non-empty

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

Quality: <quality_score>/100 (<quality_grade>)  ·  <N completed>/<N total> tasks  ·  <$X.XXX or "—" if cost_unavailable>/task

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
2. Loop: "The agent repeated `<cmd>` × N times. Was this expected or a bug?"
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

**Quality: <score>/100 (<grade>)**  ·  <N>/<N> tasks  ·  <$X.XXX or "—" if cost_unavailable>/task
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
Signals to cover in order: context_loss, low efficiency (<50%), error_loops, high_burn.
Skip signals the user confirmed were expected.
```

After writing the report, print:
```
✓ Report written to <output_path>
  Quality: <score>/100 (<grade>)  ·  <N>/<N> tasks  ·  $<X.XXX>/task
  Key issue: <most prominent loop or error, or "None detected">
  Duration breakdown: LLM <pct>% / CLI <pct>% / User <pct>% / Idle <pct>%
```

---
name: clawsession-insights
description: Use when you want to analyze an OpenClaw agent session JSONL file — parses the log, surfaces loops/errors/timing stats, asks targeted questions, and generates a Markdown report.
---

# Session Analyzer

Analyze an OpenClaw session log and produce a Markdown report.

## Usage

`/clawsession-insights <path-to-session.jsonl>`

## Instructions

Follow these phases exactly in order.

### Phase 1 — Run the parser

Locate the parser at:
```
~/.claude/skills/clawsession-insights/analyze_session.py
```

If the file does not exist, tell the user:
> "Parser not found. Please install the skill first: https://github.com/jarosik9/openclaw-session-analysis"
and stop.

Run:
```bash
python3 ~/.claude/skills/clawsession-insights/analyze_session.py <input_path>
```

Capture the JSON output. If the parser exits with an error, show the error message and stop.

### Phase 2 — Display stats and ask questions

Display this summary block in the terminal:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Session: <session.id>
Date:    <session.start_time>
Model:   <session.model> (<session.provider>)
User:    <session.user>
CWD:     <session.cwd>
Duration: <session.duration_ms formatted as Xm Ys>

Stats
  Turns: <stats.total_turns>  Tool calls: <stats.tool_calls>  Errors: <stats.tool_errors>
  Tokens: <stats.total_tokens>  Cost: $<stats.total_cost_usd>

Timing
  LLM:   <timing.llm_ms as Xm Ys>  (<timing.llm_pct>%)  avg <timing.llm_avg_ms>ms  max <timing.llm_max_ms>ms
  CLI:   <timing.cli_ms as Xm Ys>  (<timing.cli_pct>%)  avg <timing.cli_avg_ms>ms  max <timing.cli_max_ms>ms
  User:  <timing.user_ms as Xm Ys>  (<timing.user_pct>%)
  Idle:  <timing.idle_ms as Xm Ys>  (<timing.idle_pct>%)

Loops detected: <count of loops>
<for each loop: "  • <command_normalized> × <count> (<loop_type>) — <duration_ms as Xm Ys>">

Errors: <count of errors>
<for each error (max 5): "  • [<exit_code>] <command truncated to 60 chars>">
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Then generate **2-3 targeted questions** based on the data content. Ask them **one at a time**, waiting for the user's answer before asking the next.

Question generation rules (data-driven — do NOT hardcode product names, tool names, or domain keywords):
- If `loops` is non-empty: ask about the most prominent loop — "The agent repeated `<command_normalized>` × <count> times. Was this expected or a bug?"
- If `stats.tool_errors / stats.tool_calls > 0.3`: ask "Several commands failed. Were these failures expected?"
- If the last conversation entry is from the assistant (session ends mid-flow): ask "The session ended with the agent waiting for input. Did the user continue elsewhere?"
- If `stats.total_cost_usd > 0`: ask "The session cost $<amount>. Does this seem in line with expectations?"
- If no loop and no high error rate: ask "What was the goal of this session, and was it achieved?"
- Always limit to 3 questions maximum, prioritising by data salience

### Phase 3 — Collect answers

Hold all answers in conversation context. Do not write to any file.

### Phase 4 — Generate and write the report

Generate a Markdown report using the JSON summary and the user's answers. Write it to:
```
<absolute_directory_of_input_file>/<input_filename_without_extension>_analysis.md
```

Report structure (in order):

```markdown
# Session Analysis: <session.id>

**Date:** <date> | **Model:** <model> | **Duration:** <Xm Ys>
**User:** <user> | **Platform:** <cwd>

---

## Summary
<LLM narrative: what the user tried to do, what the agent did, outcome.
Informed by user answers. 3-5 sentences.>

## Conversation Log
<For each entry in conversation array, one line:>
[HH:MM] <role capitalized>: <text, truncated at 120 chars if needed>

(Tool calls and tool results are not shown here.)

## UX Friction Points
<LLM analysis: where the user or agent got stuck and why.
Reference specific timestamps or commands. Informed by user answers.
Use bullet points.>

## Agent Anomalies

### Loops
<Table if loops exist, else "None detected.">
| Normalized Command | Type | Count | Duration | Period |
|--------------------|------|-------|----------|--------|

### Errors
<Table if errors exist, else "None.">
| Time | Command | Exit Code | Error (first 80 chars) |
|------|---------|-----------|------------------------|

### Tool Usage
| Tool | Calls |
|------|-------|
<one row per tool in tool_usage>

## Command Log
| Time | Command (truncated at 80 chars) | Status | Duration |
|------|----------------------------------|--------|----------|
<one row per command in commands array>

## Performance & Timing

**Total duration:** <Xm Ys>

| Type | Total | % | Avg | Max |
|------|-------|---|-----|-----|
| LLM inference | <llm_ms as Xm Ys> | <llm_pct>% | <llm_avg_ms>ms | <llm_max_ms>ms |
| CLI execution | <cli_ms as Xm Ys> | <cli_pct>% | <cli_avg_ms>ms | <cli_max_ms>ms |
| User response | <user_ms as Xm Ys> | <user_pct>% | — | — |
| Idle / other  | <idle_ms as Xm Ys> | <idle_pct>% | — | — |

**Tokens:** <total_tokens> | **Cost:** $<total_cost_usd>
```

After writing the file, print a 3-5 line terminal summary:
```
✓ Report written to <output_path>
  Summary: <one sentence from the Summary section>
  Key issue: <most prominent loop or error, or "None detected">
  Duration breakdown: LLM <pct>% / CLI <pct>% / User <pct>% / Idle <pct>%
```

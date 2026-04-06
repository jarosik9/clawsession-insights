---
name: analyze-session
description: "Analyze OpenClaw, Claude Code CLI, or Langfuse trace session logs. MUST use when: user says 'analyze session', 'review my session', 'session report', 'what happened in this session', 'session quality', or provides a session file (.jsonl or .json). Segments tasks, scores agent quality (0-100), surfaces loops/errors/friction, asks targeted questions, writes a Markdown report. Supports three formats: OpenClaw JSONL, Claude Code CLI JSONL, and Langfuse trace JSON."
---

# Session Analyzer

**Turns a raw session log into an actionable quality report — so you know what the agent did well, where it got stuck, and what to fix.**

## Who This Is For

You are helping an AI agent developer or product manager understand what happened in a session. They want to know: did the agent complete the tasks? Where did it struggle? Was the session cost-effective?

## When to Use / When NOT to Use

✅ Use when:
- User provides a session file: `.jsonl` (OpenClaw or Claude Code CLI) or `.json` (Langfuse trace)
- User asks to "analyze", "review", or "report on" a session
- User asks about session quality, cost, or agent performance
- User wants to understand tool execution patterns, timing, or errors

❌ Do NOT use for:
- Live session monitoring (this is post-hoc analysis)
- Comparing two sessions side-by-side (use openclaw-eval-skill for A/B)
- Files not in supported formats (OpenClaw JSONL, Claude Code CLI JSONL, Langfuse JSON trace)

## Prerequisites

- Python 3.10+ available on PATH
- Parser: `~/.claude/skills/clawsession-insights/analyze_session.py`
- No external Python dependencies required (stdlib only)

---

## 🚨 Critical Rules

1. **Execute phases in order: 1 → 2 → 3 → 4 → 5 → 6 → 7. Do not skip or reorder.**
2. **Phase 4 is conditional** — only run it when triggered (see conditions below).
3. **Ask questions one at a time** in Phase 5 — wait for each answer before the next.
4. **Never invent friction points** — only report what the data shows.

---

## Quick Reference

```
/analyze-session <path-to-session.jsonl> [--since HH:MM] [--until HH:MM] [--silent]
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `<path>` | ✅ | Absolute or relative path to `.jsonl` session file |
| `--since HH:MM` | ❌ | Only analyze events after this time |
| `--until HH:MM` | ❌ | Only analyze events before this time |
| `--silent` | ❌ | Skip questions (Phase 5-6), go straight to report |

---

## Phase 1 — Run the parser

Run the Python parser to extract structured data from the JSONL:

```bash
python3 ~/.claude/skills/clawsession-insights/analyze_session.py <input_path> [--since <arg>] [--until <arg>]
```

⚠️ **If the parser file doesn't exist**, tell the user:
> "Parser not found. Please install the skill first: https://github.com/jarosik9/openclaw-session-analysis"

⚠️ **If the parser exits non-zero**, show the error and stop.

⚠️ **If the JSONL is not a valid session format** (neither OpenClaw nor Claude Code CLI), the parser will error. Tell the user: "This file doesn't look like an OpenClaw or Claude Code session log."

Capture the full JSON output. It contains: `session`, `stats`, `timing`, `loops`, `errors`, `commands[]`, `tool_usage`, `conversation[]`, `message_costs[]`, `thinking[]`.

---

## Phase 2 — Segment the conversation into tasks

You make one LLM call to identify what the user was working on.

**Read the full prompt template from:** `references/prompts.md` → "Task Segmentation Prompt"

**Transcript formatting rules** are also in `references/prompts.md` → "Transcript Formatting Rules"

**If the LLM returns invalid JSON**, retry once. If it fails again, set tasks to `[]` and note: "Task segmentation failed — proceeding with session-level metrics only."

**If the LLM returns `[]`** (no tasks detected), that's valid — some sessions are exploratory.

---

## Phase 3 — Enrich with math

Now that you have task time ranges from Phase 2, compute per-task and session-level metrics.

For each task, filter `commands[]` and `message_costs[]` by `start_time ≤ timestamp ≤ end_time`.

### Cost signals

**tokens_unavailable** — `true` if `stats.total_tokens == 0`. Display tokens as `N/A (not reported by provider)`.

**cost_unavailable** — `true` if `message_costs[]` is empty OR all `cost_usd == 0`. When true:
- All cost fields = `—`
- Skip `cost_per_min` and `high_burn`

**cost_usd** — sum `cost_usd` from `message_costs[]` within the task window.

**cost_per_min** — `cost_usd / (duration_ms / 60000)`. Flag `high_burn = true` if `cost_per_min > 2 × session_avg`.

### Efficiency

**efficiency_pct** — exclude read-only commands (`ls`, `cat`, `head`, `tail`, `echo`, `pwd`, `which`, `grep`, `find`, `rg`). Of remaining: `round(exit_0_count / total × 100)`. No meaningful commands → `null` (display `—`).

### Quality score (4 dimensions, 0–100 each)

**dim_execution** — session-wide command success rate:
```
meaningful = commands[] excluding read-only (ls, cat, head, tail, echo, pwd, which, grep, find, rg)
dim_execution = round(exit_0 / len(meaningful) × 100) if meaningful else 100
```

**dim_completion** — task completion rate:
```
dim_completion = round(completed / total × 100) if tasks else 100
```

**dim_depth** — session-level structural complexity. Computed once, shared by all tasks.

```
⚠️ Normalise tool names before computing:
  "exec" = "Bash", "read_file" = "Read", "write_file" = "Write",
  "edit_file" = "Edit", "web_search" = "WebSearch", "web_fetch" = "WebFetch"

A: tool_breadth (0–50)
  tool_types = distinct normalised tool names in tool_usage
  1 type   →  0  (but if len(commands[]) > 50: floor at 10)
  2–3      → 20
  4–5      → 35
  6+       → 50

B: external_call_density (0–25)
  Count commands[] where command text contains "http://", "https://", or "api."
  Exclude: "localhost", "127.0.0.1", "0.0.0.0"
  Add normalised "WebFetch" + "WebSearch" from tool_usage (deduplicate)
  0    →  0
  1–5  → 15
  6+   → 25

C: write_ratio (0–25)
  write_calls = normalised tool_usage["Write"] + ["Edit"]
  total_calls = sum(tool_usage.values())
  ratio = write_calls / total_calls if total > 0 else 0
  < 0.10   →  0
  0.10–0.30 → 15
  > 0.30   → 25

dim_depth = min(100, A + B + C)

depth_label:
  types ≥ 5, ext ≥ 6  → "deep integration"
  types ≥ 5, write > 0.30  → "heavy authoring"
  types ≥ 5             → "multi-tool"
  types ≥ 4, ext ≥ 1   → "multi-tool + external"
  types ≥ 4             → "multi-tool"
  types 2–3, write > 0.10 → "read-write"
  types 2–3             → "basic ops"
  types = 1             → "read-only"
```

**dim_ux** — user experience smoothness:
```
dim_ux = 100
  - confirmed context_loss: -10 each, cap -30
  - abandoned tasks: -8 each, cap -20
  - error_loop count > 10: -15 each, cap -30
  - error_loop 3–10: -5 each, cap -20
  ⚠️ polling_loop and exploration_loop do NOT affect dim_ux.
dim_ux = max(0, dim_ux)
```

**Final score:**
```
quality_score = round((dim_execution + dim_completion + dim_depth + dim_ux) / 4)
Grade: 90–100 = A, 75–89 = B, 60–74 = C, 45–59 = D, <45 = F
```

If no tasks detected: `dim_completion = 100`. Note in report: "score based on session-level signals — no tasks detected."

### Additional metrics (from parser output)

The parser also outputs three operational metrics. You read them directly from the JSON — no computation needed:

**`wasted_calls`** — unnecessary CLI invocations (blind retries, help exploration, version checks, flag trial-and-error, env probing). Key fields: `waste_ratio`, `total`, `by_type`, `details[]`.

**`recovery`** — for each failed operation, did the same operation succeed within 2 subsequent attempts? Key fields: `recovery_rate`, `resolved`, `unresolved`, `correctly_abandoned`, `details[]`.

**`hallucinations`** — completion claims that contradict recent command results (agent says "done ✅" but last command failed). Key fields: `hallucination_rate`, `total_claims`, `hallucinations`, `details[]`.

---

## Phase 4 — Targeted thinking analysis (conditional)

**Skip entirely unless BOTH conditions are true:**
1. `thinking[]` is non-empty
2. At least one of:
   - any `error_loop` with `count > 10`
   - any task `status == "abandoned"`
   - any task `context_loss` non-empty

❌ Do NOT trigger for: `polling_loop`, `exploration_loop`, or `error_loop` with `count ≤ 10`.

**Cap: max 3 LLM calls.** Priority: abandoned tasks → loops → context loss.

**Read prompt templates from:** `references/prompts.md` → "Thinking Analysis Prompts"

Store each result as `root_cause` on the relevant loop or task.

---

## Phase 5 — Display stats and ask questions

**If `--silent` was passed**, display the stats block then skip to Phase 7.

### Stats block

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Session: <session.id>
Date:    <session.start_time>
Model:   <session.model> (<session.provider>)
User:    <session.user>
CWD:     <session.cwd>
Duration: <Xm Ys>

Quality: <score>/100 (<grade>)  ·  <N>/<N> tasks  ·  <$X.XX/min or "—">/min
  Execution  <bar>  <dim_execution>
  Completion <bar>  <dim_completion>
  Depth      <bar>  <dim_depth>  (<depth_label>)
  UX         <bar>  <dim_ux>
(bar = "█" × round(score/10) + "░" × (10 − round(score/10)))

Stats
  Turns: <total_turns>  Tool calls: <tool_calls>  Errors: <tool_errors>
  Tokens: <total_tokens or "N/A">  Cost: <$X.XX or "N/A">

Timing
  LLM:   <Xm Ys>  (<pct>%)  avg <avg>ms  max <max>ms
  CLI:   <Xm Ys>  (<pct>%)  avg <avg>ms  max <max>ms
  User:  <Xm Ys>  (<pct>%)
  Idle:  <Xm Ys>  (<pct>%)

Tasks detected: <N>  (covers <Xm> of <Ym session>)
  N. <title>  HH:MM→HH:MM  Xm  $X.XXX  eff:XX%  [x]/[ ] [⚠ high burn]

Loops detected: <count>
  • <command_normalized> × <count> (<loop_type>) — <Xm Ys>

Operational Metrics
  Waste:         <waste_ratio>% (<total>/<cmds> — <by_type summary>)
  Recovery:      <recovery_rate>% (<resolved> resolved, <unresolved> unresolved, <correctly_abandoned> abandoned)
  Hallucination: <hallucinations>/<total_claims> claims (<hallucination_rate>%) [🚨 if > 0]

Errors: <count>
  • [<exit_code>] <command truncated to 60 chars>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Questions

Ask **one at a time**, wait for each answer. Pick top 2–5 from this priority pool:

1. 🔴 Abandoned task: "Task N ('[title]') appears abandoned. What happened?"
2. 🔴 Error loop (error_loop only): "The agent repeated `<cmd>` × N times. Was this expected or a bug?"
3. 🟡 Low efficiency (<50%): "Task N had X% command success rate. Was the agent struggling?"
4. 🟡 High burn: "Task N cost $X in Nm — above session average. Was this expected?"
5. 🟡 Context loss: "Task N shows context loss: [desc]. Did you notice the agent losing track?"
6. ⚪ Fallback: "What was the goal of this session, and was it achieved?"

---

## Phase 6 — Collect answers

Hold all answers in conversation context. Do not write to any file.

**Skip if `--silent` was passed.**

---

## Phase 7 — Write the report

**Read the full report template from:** `references/report-template.md`

**Generate narrative sections** (Summary + UX Friction Points) with one LLM call. **Read the prompt from:** `references/prompts.md` → "Narrative Sections Prompt"

---

## ⚠️ Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `Parser not found` | Skill not installed | `pip install` or clone repo |
| Parser exits with `JSONDecodeError` | Not a valid JSONL file | Check the file is one-JSON-per-line |
| Parser exits with `KeyError: 'type'` | Unsupported JSONL format | Only OpenClaw and Claude Code CLI formats are supported |
| Phase 2 returns invalid JSON | LLM parsing error | Retry once; if still fails, proceed with `tasks = []` |
| All dim_depth values are low (10–20) | CLI-heavy session (all work in Bash) | Expected — structural signals measure tool diversity, not command complexity |
| `cost_unavailable` / all costs `—` | Provider didn't report token costs | Not a bug; display as N/A |
| Session has >500 commands | Very long session | Parser handles it, but Phase 2 transcript may be thinned |
| `--since`/`--until` returns empty | Time range doesn't overlap session | Check timestamps are in HH:MM local time matching session timezone |

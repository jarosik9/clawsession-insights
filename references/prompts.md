# LLM Prompts

## Task Segmentation Prompt (Phase 2)

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

## Transcript Formatting Rules

Format `conversation[]` as `[HH:MM] ROLE: text`.
- Truncate: user turns at 400 chars, assistant turns at 600 chars
- If total transcript exceeds 20 000 chars, reduce to 300/350
- Always preserve the last user message in full
- If >80 turns: keep all user turns, keep every other assistant turn, always keep last 10 turns
- Add note: "transcript thinned — some assistant turns omitted."

## Thinking Analysis Prompts (Phase 4)

### Loop trigger
```
The agent repeated "<command_normalized>" × N times between <start> and <end>.
Below are the agent's thinking blocks during this period.
In 1–2 sentences: why did the agent keep repeating this? Was it aware of the loop?

<thinking entries>
```

### Abandoned task trigger
```
The agent was working on "<title>" but did not complete it.
Below are the agent's last 3 thinking blocks before the task ended.
In 1–2 sentences: what caused the agent to stop?

<last 3 thinking entries before end_time>
```

### Context loss trigger
```
The agent showed signs of context loss during "<title>": <description>.
Below are relevant thinking blocks.
In 1–2 sentences: does the thinking confirm genuine context loss or intentional re-check?

<thinking entries in task time range>
```

## Narrative Sections Prompt (Phase 7)

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

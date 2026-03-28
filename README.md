# clawsession-insights

A Claude Code skill that analyzes [OpenClaw](https://github.com/jarosik9/openclaw) session logs and produces a structured Markdown report.

Point it at a `.jsonl` session file and get a breakdown of what the agent did, where it got stuck, how time was spent, and what it cost.

---

## Example output

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Session: abc123
Date:    2026-03-27T14:05:32Z
Model:   claude-sonnet-4-6 (anthropic)
User:    alice
CWD:     /home/alice/myproject
Duration: 4m 12s

Stats
  Turns: 18  Tool calls: 34  Errors: 3
  Tokens: 41,200  Cost: $0.082

Timing
  LLM:   2m 44s  (65%)  avg 4120ms  max 18300ms
  CLI:   0m 47s  (19%)  avg 1380ms  max 8200ms
  User:  0m 22s  (9%)
  Idle:  0m 19s  (7%)

Loops detected: 1
  • pytest tests/ × 5 (error_loop) — 1m 23s

Errors: 3
  • [1] pytest tests/test_auth.py -v
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

The skill then asks 2–3 targeted questions based on what it found (loops, high error rates, unexpected session ends), and writes a full Markdown report next to your input file.

---

## Requirements

- Python 3 (stdlib only, no extra packages)
- Claude Code with skill support
- Session `.jsonl` files exported from [OpenClaw](https://github.com/jarosik9/openclaw)

---

## Installation

```bash
git clone https://github.com/jarosik9/clawsession-insights/ ~/.claude/skills/clawsession-insights
```

> **Note:** The destination path must be exactly `~/.claude/skills/clawsession-insights`. The skill has this path hardcoded — cloning elsewhere or renaming the directory will cause a "Parser not found" error.

Restart Claude Code after cloning.

---

## Usage

In a Claude Code session:

```
/clawsession-insights path/to/session.jsonl
```

The skill will:

1. **Parse** the session file and display a stats summary in the terminal.
2. **Ask questions** — 2–3 targeted questions based on the data (repeated commands, high error rates, cost).
3. **Write a report** to `path/to/session_analysis.md` incorporating your answers.

---

## Report contents

| Section | What it covers |
|---------|---------------|
| Summary | Narrative of what the user tried to do and whether it succeeded |
| Conversation log | Timestamped user/assistant exchanges |
| UX friction points | Where the user or agent got stuck, with timestamps |
| Loops | Commands repeated 3+ times in a sliding window, classified as polling or error loops |
| Errors | Commands that exited non-zero, with truncated error output |
| Tool usage | Call counts per tool |
| Command log | Full chronological list of shell commands with status and duration |
| Performance & timing | LLM inference / CLI execution / user response / idle breakdown |

---

## How it works

`analyze_session.py` is a dependency-free Python script that reads the JSONL event stream produced by OpenClaw. It extracts:

- **Session metadata** — model, user, working directory, duration
- **Conversation** — user and assistant text turns (tool calls stripped)
- **Commands** — all `exec` tool calls with exit codes and durations
- **Timing** — LLM inference time estimated from toolResult→assistant intervals; CLI time from command durations; user time from assistant→user intervals
- **Loops** — sliding-window detection of repeated normalized commands
- **Stats** — turn counts, token usage, and cost from assistant message metadata

The script outputs a single JSON object to stdout. The skill reads this and drives the interactive report generation.

---

## License

MIT

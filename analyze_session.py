#!/usr/bin/env python3
"""
OpenClaw session JSONL analyzer.
Usage: python3 analyze_session.py <path-to-session.jsonl> [--since HH:MM] [--until HH:MM]
Outputs: JSON summary to stdout
"""
import json
import sys
import re
import shlex
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta


SKILL_DIR = Path(__file__).parent


def parse_timestamp(ts_str):
    """Parse ISO timestamp string to epoch milliseconds."""
    if ts_str.endswith('Z'):
        ts_str = ts_str[:-1] + '+00:00'
    return int(datetime.fromisoformat(ts_str).timestamp() * 1000)


def load_config():
    """Load optional config.json from skill directory. Returns (focus_label, focus_patterns)."""
    config_path = SKILL_DIR / "config.json"
    if not config_path.exists():
        return None, []
    try:
        cfg = json.loads(config_path.read_text())
        focus = cfg.get("focus", {})
        return focus.get("label"), [p for p in focus.get("patterns", []) if p]
    except Exception:
        return None, []


def _first_real_token(command):
    """
    Extract the first meaningful command token from a potentially compound shell command.
    Handles patterns like: export PATH=... && kubectl ..., VAR=val cmd, sudo cmd, env VAR=val cmd.
    Splits on && / || / ; and takes the last sub-command's first effective token,
    since the pattern 'export PATH=... && REAL_CMD' puts the real command after &&.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    # Find positions of shell operators; take tokens after the last one
    last_op = -1
    for i, tok in enumerate(tokens):
        if tok in ('&&', '||', ';'):
            last_op = i
    if last_op >= 0:
        tokens = tokens[last_op + 1:]

    # Skip leading noise: export, env, sudo, VAR=val
    for tok in tokens:
        if tok in ('export', 'env', 'sudo'):
            continue
        if '=' in tok and not tok.startswith('-'):
            continue
        return tok
    return ""


def is_focus_match(command, patterns):
    """
    Return True if the command's first effective token matches any pattern.
    Handles compound commands (export VAR=val && real_cmd).
    """
    if not patterns:
        return True
    return _first_real_token(command) in set(patterns)


def load_events(path):
    """Load all JSONL events from file. Returns list of dicts."""
    events = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def extract_session_meta(events):
    """Extract session metadata from events."""
    if not events:
        raise ValueError("No events found in session file")
    session_event = events[0]
    last_event = events[-1]

    start_ms = parse_timestamp(session_event["timestamp"])
    end_ms = parse_timestamp(last_event["timestamp"])

    model = "unknown"
    provider = "unknown"
    for e in events:
        if e.get("type") == "custom" and e.get("customType") == "model-snapshot":
            model = e.get("data", {}).get("modelId", "unknown")
            provider = e.get("data", {}).get("provider", "unknown")
            break

    # Extract user from OpenClaw message metadata block embedded in first user message.
    # OpenClaw injects a JSON block with sender info: {"sender_id": "...", "sender": "name", ...}
    user = "unknown"
    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text", "")
            match = re.search(r'"sender"\s*:\s*"([^"]+)"', text)
            if match:
                user = match.group(1)
                break
        if user != "unknown":
            break

    cwd = session_event.get("cwd", "")
    # Redact home directory prefix to avoid leaking username in shared reports.
    cwd = re.sub(r'^(/home/[^/]+|/Users/[^/]+|/root)', '~', cwd)

    return {
        "id": session_event.get("id", ""),
        "start_time": session_event["timestamp"],
        "end_time": last_event["timestamp"],
        "duration_ms": end_ms - start_ms,
        "cwd": cwd,
        "model": model,
        "provider": provider,
        "user": user,
    }


def extract_text_from_content(content):
    """Extract plain text from a message content list, ignoring toolCall items."""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item["text"].strip())
    return " ".join(parts).strip()


def extract_conversation(events):
    """Extract ordered user/assistant text exchanges. Skips tool-call-only turns."""
    conv = []
    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", [])
        text = extract_text_from_content(content)
        if not text:
            continue
        conv.append({
            "role": role,
            "text": text,
            "timestamp": e["timestamp"],
        })
    return sorted(conv, key=lambda x: parse_timestamp(x["timestamp"]))


def extract_tool_calls(events, focus_patterns):
    """
    Extract all exec tool calls with their results.
    Handles process-chain for long-running commands:
      exec toolCall -> running toolResult -> [process toolCall -> running]* -> completed
    Tags each command with is_focus based on focus_patterns.
    Tracks other_tool_errors from non-exec toolResults with isError == True.
    Returns: (commands, tool_usage, errors, other_tool_errors)
    """
    tool_results_by_call_id = {}
    other_tool_errors = 0

    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "toolResult":
            continue
        tool_call_id = msg.get("toolCallId", "")
        tool_name = msg.get("toolName", "")
        if tool_call_id not in tool_results_by_call_id:
            tool_results_by_call_id[tool_call_id] = []
        tool_results_by_call_id[tool_call_id].append(e)
        # Count non-exec tool errors via isError flag
        if tool_name not in ("exec", "process") and msg.get("isError", False):
            other_tool_errors += 1

    exec_calls = []
    tool_usage = {}

    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "assistant":
            continue
        for item in msg.get("content", []):
            if not isinstance(item, dict) or item.get("type") != "toolCall":
                continue
            tool_name = item.get("name", "")
            tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1
            if tool_name == "exec":
                exec_calls.append({
                    "tool_call_id": item["id"],
                    "command": item.get("arguments", {}).get("command", ""),
                    "call_ts": e["timestamp"],
                })

    commands = []
    errors = []

    for call in exec_calls:
        call_id = call["tool_call_id"]
        call_ts_ms = parse_timestamp(call["call_ts"])

        results = tool_results_by_call_id.get(call_id, [])
        # Follow process chain: find the last completed result
        completed_result = None
        for r in results:
            details = r.get("message", {}).get("details", {})
            if details.get("status") == "completed":
                completed_result = r
                break

        if completed_result is None and results:
            completed_result = results[-1]

        if completed_result is None:
            continue

        details = completed_result.get("message", {}).get("details", {})
        result_ts_ms = parse_timestamp(completed_result["timestamp"])
        exit_code = details.get("exitCode", 0)
        # Prefer durationMs from OpenClaw's own measurement; fall back to timestamp diff
        duration_ms = details.get("durationMs") or (result_ts_ms - call_ts_ms)
        status = "ok" if exit_code == 0 else "error"

        # Get output text (and error text if non-zero exit)
        output_text = ""
        error_text = ""
        content = completed_result.get("message", {}).get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                output_text = item["text"][:500]
                if exit_code != 0:
                    error_text = item["text"][:200]
                break

        cmd_str = call["command"]
        entry = {
            "tool": "exec",
            "command": cmd_str,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "status": status,
            "timestamp": call["call_ts"],
            "output_text": output_text,
            "is_focus": is_focus_match(cmd_str, focus_patterns),
        }
        commands.append(entry)

        if exit_code != 0:
            errors.append({
                "command": cmd_str,
                "exit_code": exit_code,
                "error_text": error_text,
                "timestamp": call["call_ts"],
                "is_focus": entry["is_focus"],
            })

    return commands, tool_usage, errors, other_tool_errors


def calculate_timing(events, commands, total_ms):
    """
    LLM time: last toolResult in preceding batch -> assistant message.
    CLI time: sum of exec command durations.
    User time: preceding assistant -> user message.
    Idle: residual.
    """
    msg_events = [e for e in events if e.get("type") == "message"]

    llm_intervals = []
    user_intervals = []

    prev_assistant_ts = None
    last_tool_result_ts = None
    last_anchor_ts = None

    for e in msg_events:
        msg = e.get("message", {})
        role = msg.get("role")
        ts = parse_timestamp(e["timestamp"])

        if role == "toolResult":
            last_tool_result_ts = ts

        elif role == "assistant":
            start = last_tool_result_ts if last_tool_result_ts is not None else last_anchor_ts
            if start is None:
                start = parse_timestamp(events[0]["timestamp"]) if events else None
            if start is not None:
                llm_intervals.append((start, ts))
            last_tool_result_ts = None
            prev_assistant_ts = ts
            last_anchor_ts = ts

        elif role == "user":
            if prev_assistant_ts is not None:
                user_intervals.append((prev_assistant_ts, ts))
            last_anchor_ts = ts
            last_tool_result_ts = None

    def total_ms_from(intervals):
        return sum(max(0, end - start) for start, end in intervals)

    def avg_ms_from(intervals):
        vals = [max(0, end - start) for start, end in intervals]
        return int(sum(vals) / len(vals)) if vals else 0

    def max_ms_from(intervals):
        vals = [max(0, end - start) for start, end in intervals]
        return max(vals) if vals else 0

    cli_durations = [c["duration_ms"] for c in commands]
    cli_ms = sum(cli_durations)
    cli_avg = int(sum(cli_durations) / len(cli_durations)) if cli_durations else 0
    cli_max = max(cli_durations) if cli_durations else 0

    llm_ms = total_ms_from(llm_intervals)
    user_ms = total_ms_from(user_intervals)
    idle_ms = max(0, total_ms - llm_ms - cli_ms - user_ms)

    def pct(val):
        return round(val * 100 / total_ms) if total_ms > 0 else 0

    return {
        "total_ms": total_ms,
        "llm_ms": llm_ms,
        "llm_pct": pct(llm_ms),
        "llm_avg_ms": avg_ms_from(llm_intervals),
        "llm_max_ms": max_ms_from(llm_intervals),
        "cli_ms": cli_ms,
        "cli_pct": pct(cli_ms),
        "cli_avg_ms": cli_avg,
        "cli_max_ms": cli_max,
        "user_ms": user_ms,
        "user_pct": pct(user_ms),
        "idle_ms": idle_ms,
        "idle_pct": pct(idle_ms),
    }


def normalize_command(cmd):
    """
    Normalize command for loop detection.
    Strips compound-command prefixes (export VAR=val &&), npx/npx -y,
    flags starting with '-', and quoted string args.
    Returns first two remaining meaningful tokens.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()

    # Strip compound-command prefix: find last && / || / ; and take tokens after it
    last_op = -1
    for i, tok in enumerate(tokens):
        if tok in ('&&', '||', ';'):
            last_op = i
    if last_op >= 0:
        tokens = tokens[last_op + 1:]

    # Strip leading export / env / sudo / VAR=val
    while tokens:
        if tokens[0] in ('export', 'env', 'sudo'):
            tokens.pop(0)
        elif '=' in tokens[0] and not tokens[0].startswith('-'):
            tokens.pop(0)
        else:
            break

    # Strip 'npx' and optional following '-y'
    if tokens and tokens[0] == "npx":
        tokens.pop(0)
        if tokens and tokens[0] == "-y":
            tokens.pop(0)

    # Filter flags and their values: skip flag tokens (start with '-') AND
    # the immediately following value token (e.g. '--format json' → skip both)
    filtered = []
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            skip_next = True  # next token is the flag's value, skip it too
            continue
        filtered.append(t)
    return " ".join(filtered[:2]) if len(filtered) >= 2 else " ".join(filtered)


def detect_loops(commands, window=10, threshold=3):
    """
    Detect loops: same normalized command >= threshold times in any window-sized slice.
    Extends beyond detection window to capture full loop extent.
    Classification (in priority order):
    - polling_loop: exit_code==0 AND output contains polling keywords
    - polling_loop: all exit_code==0 (fallback — repeated success can't be an error loop)
    - exploration_loop: exit_code not all 1 AND commands target diverse resources (≥60% unique 3rd tokens)
    - error_loop: everything else
    """
    if not commands:
        return []

    normalized = [normalize_command(c["command"]) for c in commands]
    loops = []
    reported = set()
    polling_keywords = ("pending", "waiting", "running", "in progress")

    for i in range(len(normalized)):
        if normalized[i] in reported:
            continue
        window_slice = normalized[i:i + window]
        count = window_slice.count(normalized[i])
        if count < threshold:
            continue

        matching_indices = [j for j, n in enumerate(normalized) if n == normalized[i]]

        # Extend beyond the initial detection window: keep adding matching commands
        # as long as the gap to the next occurrence is <= window (in command-index terms).
        extended = []
        prev_j = i - 1
        for j in matching_indices:
            if j < i:
                continue
            if j - prev_j <= window:
                extended.append(j)
                prev_j = j
            else:
                break
        group = extended if len(extended) >= threshold else [j for j in matching_indices if i <= j < i + window]

        start_cmd = commands[group[0]]
        end_cmd = commands[group[-1]]

        loop_type = "error_loop"
        all_zero_exit = all(commands[j]["exit_code"] == 0 for j in group)
        if all_zero_exit:
            # Priority 1: output contains polling keywords
            for cmd_idx in group:
                output_text = commands[cmd_idx].get("output_text", "").lower()
                if any(kw in output_text for kw in polling_keywords):
                    loop_type = "polling_loop"
                    break
            else:
                # Priority 2: fallback — all success means it can't be an error loop
                loop_type = "polling_loop"
        else:
            # Priority 3: exploration_loop — not all failures, and commands target diverse resources
            not_all_fail = not all(commands[j]["exit_code"] != 0 for j in group)
            if not_all_fail:
                third_tokens = []
                for j in group:
                    parts = normalize_command(commands[j]["command"]).split()
                    if len(parts) >= 3:
                        third_tokens.append(parts[2])
                if third_tokens:
                    diversity = len(set(third_tokens)) / len(third_tokens)
                    if diversity >= 0.6:
                        loop_type = "exploration_loop"

        loops.append({
            "command_normalized": normalized[i],
            "example_command": start_cmd["command"],
            "loop_type": loop_type,
            "count": len(group),
            "start_time": start_cmd["timestamp"],
            "end_time": end_cmd["timestamp"],
            "duration_ms": (
                parse_timestamp(end_cmd["timestamp"]) -
                parse_timestamp(start_cmd["timestamp"])
            ),
        })
        reported.add(normalized[i])

    return loops


def strip_internal_fields(commands):
    """Remove fields used only internally (output_text not part of output schema)."""
    return [{k: v for k, v in c.items() if k != "output_text"} for c in commands]


def calculate_stats(events, commands, errors, tool_usage, other_tool_errors,
                    focus_label, focus_patterns):
    """Aggregate message counts, token/cost totals, and focus/CLI call breakdown."""
    user_messages = 0
    assistant_messages = 0
    total_tokens = 0
    total_cost = 0.0

    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        role = msg.get("role")
        if role == "user":
            user_messages += 1
        elif role == "assistant":
            assistant_messages += 1
            usage = msg.get("usage", {})
            total_tokens += usage.get("totalTokens", 0)
            cost = usage.get("cost", {})
            total_cost += cost.get("total", 0.0)

    # Focus / CLI breakdown (only populated when config present)
    has_config = bool(focus_patterns)
    focus_calls = sum(1 for c in commands if c.get("is_focus", True))
    focus_errors = sum(1 for e in errors if e.get("is_focus", True))
    other_cli_calls = len(commands) - focus_calls
    other_cli_errors = len(errors) - focus_errors
    other_tool_calls = sum(
        v for k, v in tool_usage.items() if k not in ("exec", "process")
    )

    result = {
        "user_messages": user_messages,
        "assistant_messages": assistant_messages,
        "total_turns": user_messages + assistant_messages,
        "tool_calls": len(commands),
        "tool_errors": len(errors),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
    }

    if has_config:
        result.update({
            "focus_label": focus_label or "CLI",
            "focus_calls": focus_calls,
            "focus_errors": focus_errors,
            "other_cli_calls": other_cli_calls,
            "other_cli_errors": other_cli_errors,
            "other_tool_calls": other_tool_calls,
            "other_tool_errors": other_tool_errors,
        })

    return result


def extract_message_costs(events):
    """Per-assistant-message cost with timestamp, for per-task cost rollup."""
    result = []
    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "assistant":
            continue
        cost = msg.get("usage", {}).get("cost", {}).get("total", 0.0)
        if cost and cost > 0:
            result.append({
                "timestamp": e["timestamp"],
                "cost_usd": cost,
            })
    return result


def extract_thinking(events):
    """Thinking blocks per assistant turn, capped at 1000 chars each."""
    result = []
    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "assistant":
            continue
        for item in msg.get("content", []):
            if isinstance(item, dict) and item.get("type") == "thinking":
                result.append({
                    "timestamp": e["timestamp"],
                    "text": item.get("thinking", "")[:1000],
                })
    return result


def parse_time_arg(arg, session_start_ms):
    """
    Parse --since / --until arg to epoch milliseconds.
    Accepts HH:MM, HH:MM:SS, or full ISO 8601 timestamp.
    HH:MM is resolved relative to session start date in UTC.
    """
    arg = arg.strip()
    if "T" in arg:
        if not arg.endswith("Z") and "+" not in arg:
            arg += "Z"
        return parse_timestamp(arg)
    parts = arg.split(":")
    h, m = int(parts[0]), int(parts[1])
    s = int(parts[2]) if len(parts) > 2 else 0
    session_dt = datetime.fromtimestamp(session_start_ms / 1000, tz=timezone.utc)
    result_dt = session_dt.replace(hour=h, minute=m, second=s, microsecond=0)
    return int(result_dt.timestamp() * 1000)


def resolve_time_range(since_arg, until_arg, session_start_ms):
    """Resolve --since / --until args to (since_ts, until_ts) in epoch ms."""
    since_ts = parse_time_arg(since_arg, session_start_ms) if since_arg else None
    until_ts = parse_time_arg(until_arg, session_start_ms) if until_arg else None
    if since_ts and until_ts and until_ts < since_ts:
        until_ts += 86_400_000  # midnight crossing: add 24h
    return since_ts, until_ts


def apply_time_filter(events, since_ts=None, until_ts=None):
    """Filter events to those within [since_ts, until_ts]."""
    return [
        e for e in events
        if (since_ts is None or parse_timestamp(e["timestamp"]) >= since_ts)
        and (until_ts is None or parse_timestamp(e["timestamp"]) <= until_ts)
    ]


# ---------------------------------------------------------------------------
# Claude Code CLI JSONL format support
# ---------------------------------------------------------------------------

def detect_format(events):
    """Detect JSONL format: 'openclaw' or 'claude-code'."""
    for e in events[:10]:
        if e.get("type") == "session":
            return "openclaw"
        if e.get("entrypoint") in ("claude-desktop", "claude-code"):
            return "claude-code"
        # Claude Code without entrypoint: has 'version' and type user/assistant
        if e.get("type") in ("user", "assistant") and e.get("version"):
            return "claude-code"
    return "openclaw"


def extract_session_meta_cc(events):
    """Extract session metadata from Claude Code CLI JSONL."""
    first_ts = events[0]["timestamp"]
    last_ts = events[-1]["timestamp"]
    start_ms = parse_timestamp(first_ts)
    end_ms = parse_timestamp(last_ts)

    model = "unknown"
    provider = "anthropic"
    for e in events:
        if e.get("type") == "assistant":
            m = e.get("message", {}).get("model", "")
            if m:
                model = m
                break

    # CWD from any event
    cwd = ""
    for e in events:
        if e.get("cwd"):
            cwd = e["cwd"]
            break
    cwd = re.sub(r'^(/home/[^/]+|/Users/[^/]+|/root)', '~', cwd)

    # Session ID from any event
    session_id = ""
    for e in events:
        if e.get("sessionId"):
            session_id = e["sessionId"]
            break

    # User: derive from cwd (~/<username>/...) or unknown
    user = "unknown"
    m = re.search(r'/Users/([^/]+)/', cwd + "/")
    if m:
        user = m.group(1)

    return {
        "id": session_id,
        "start_time": first_ts,
        "end_time": last_ts,
        "duration_ms": end_ms - start_ms,
        "cwd": cwd,
        "model": model,
        "provider": provider,
        "user": user,
    }


def extract_tool_calls_cc(events, focus_patterns):
    """Extract Bash tool calls from Claude Code CLI JSONL format."""
    # Build tool_use_id → result map from user messages
    results = {}
    for e in events:
        if e.get("type") != "user":
            continue
        for c in e.get("message", {}).get("content", []):
            if not isinstance(c, dict) or c.get("type") != "tool_result":
                continue
            tid = c.get("tool_use_id")
            if not tid:
                continue
            is_error = c.get("isError")
            content = c.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    x.get("text", "") for x in content if isinstance(x, dict)
                )
            content_str = str(content)
            # Claude Code embeds exit code in content: "Exit code N\n<output>"
            # isError is typically None even for failures in this format
            exit_code = 1 if is_error else 0
            m = re.match(r'^Exit code (\d+)\n', content_str)
            if m:
                exit_code = int(m.group(1))
                content_str = content_str[m.end():]  # strip "Exit code N\n" prefix
            results[tid] = {
                "exit_code": exit_code,
                "output_text": content_str[:500],
                "timestamp": e.get("timestamp"),
            }

    commands = []
    tool_usage = {}
    errors_list = []
    other_tool_errors = 0

    for e in events:
        if e.get("type") != "assistant":
            continue
        for c in e.get("message", {}).get("content", []):
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            tool_name = c.get("name", "")
            tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1

            if tool_name != "Bash":
                # Non-Bash tool errors
                tid = c.get("id")
                res = results.get(tid, {})
                if res.get("exit_code", 0) != 0:
                    other_tool_errors += 1
                continue

            tid = c.get("id")
            res = results.get(tid, {})
            cmd = c.get("input", {}).get("command", "")
            exit_code = res.get("exit_code", 0)
            # Duration from tool_use timestamp to tool_result timestamp
            start_ts = e.get("timestamp", "")
            end_ts = res.get("timestamp", start_ts)
            duration_ms = 0
            if start_ts and end_ts:
                try:
                    duration_ms = max(0, parse_timestamp(end_ts) - parse_timestamp(start_ts))
                except Exception:
                    duration_ms = 0

            is_focus = is_focus_match(cmd, focus_patterns)
            entry = {
                "tool": "exec",
                "command": cmd,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "status": "error" if exit_code != 0 else "ok",
                "timestamp": start_ts,
                "output_text": res.get("output_text", ""),
                "is_focus": is_focus,
            }
            commands.append(entry)
            if exit_code != 0:
                errors_list.append({
                    "command": cmd,
                    "exit_code": exit_code,
                    "error_text": res.get("output_text", "")[:300],
                    "timestamp": start_ts,
                    "is_focus": is_focus,
                })

    # Map 'exec' key for tool_usage
    bash_count = tool_usage.pop("Bash", 0)
    if bash_count:
        tool_usage["exec"] = bash_count

    return commands, tool_usage, errors_list, other_tool_errors


def extract_conversation_cc(events):
    """Extract conversation turns from Claude Code CLI JSONL.

    User messages come from queue-operation 'enqueue' events (actual human input).
    Assistant messages come from type='assistant' events with text content.
    """
    conv = []
    seen_ts = set()

    # User turns: queue-operation enqueue events contain the real human text
    for e in events:
        if e.get("type") != "queue-operation" or e.get("operation") != "enqueue":
            continue
        text = e.get("content", "").strip()
        if not text:
            continue
        # Skip system startup injection messages
        if (text.startswith("A new session was started") or
                text.startswith("Execute your Session") or
                text.startswith("Base directory for this skill")):
            continue
        ts = e.get("timestamp", "")
        if ts in seen_ts:
            continue
        seen_ts.add(ts)
        conv.append({"role": "user", "text": text, "timestamp": ts})

    # Assistant turns: type='assistant' with text content
    for e in events:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "assistant":
            continue
        parts = []
        for item in msg.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", "").strip())
        text = " ".join(parts).strip()
        if not text:
            continue
        ts = e.get("timestamp", "")
        conv.append({"role": "assistant", "text": text, "timestamp": ts})

    return sorted(conv, key=lambda x: parse_timestamp(x["timestamp"]))


def extract_message_costs_cc(events):
    """Extract per-message costs from Claude Code CLI JSONL."""
    result = []
    for e in events:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message", {})
        usage = msg.get("usage", {})
        # Claude Code uses different field names
        cost = 0.0
        cost_data = usage.get("cost", {})
        if isinstance(cost_data, dict):
            cost = cost_data.get("total", 0.0)
        if cost and cost > 0:
            result.append({"timestamp": e["timestamp"], "cost_usd": cost})
    return result


def extract_thinking_cc(events):
    """Extract thinking blocks from Claude Code CLI JSONL."""
    result = []
    for e in events:
        if e.get("type") != "assistant":
            continue
        for c in e.get("message", {}).get("content", []):
            if isinstance(c, dict) and c.get("type") == "thinking":
                result.append({
                    "timestamp": e["timestamp"],
                    "text": c.get("thinking", "")[:1000],
                })
    return result


def calculate_stats_cc(events, commands, errors, tool_usage, other_tool_errors,
                       focus_label, focus_patterns):
    """Calculate stats from Claude Code CLI JSONL."""
    # User messages: count queue-operation enqueue events (actual human inputs)
    user_messages = sum(
        1 for e in events
        if e.get("type") == "queue-operation" and e.get("operation") == "enqueue"
        and e.get("content", "").strip()
        and not e.get("content", "").startswith("A new session was started")
        and not e.get("content", "").startswith("Execute your Session")
    )
    # Only count assistant turns that actually produced text visible to the user.
    # Claude Code emits many assistant events (thinking, tool_use chunks) per logical turn.
    assistant_messages = sum(
        1 for e in events
        if e.get("type") == "assistant"
        and any(
            isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip()
            for c in e.get("message", {}).get("content", [])
        )
    )
    total_tokens = 0
    total_cost = 0.0
    for e in events:
        if e.get("type") == "assistant":
            usage = e.get("message", {}).get("usage", {})
            # Claude Code uses outputTokens / inputTokens / cacheReadInputTokens etc.
            total_tokens += (usage.get("output_tokens", 0) +
                             usage.get("input_tokens", 0) +
                             usage.get("cache_read_input_tokens", 0))
            cost_data = usage.get("cost", {})
            if isinstance(cost_data, dict):
                total_cost += cost_data.get("total", 0.0)

    has_config = bool(focus_patterns)
    focus_calls = sum(1 for c in commands if c.get("is_focus", True))
    focus_errors = sum(1 for err in errors if err.get("is_focus", True))
    other_cli_calls = len(commands) - focus_calls
    other_cli_errors = len(errors) - focus_errors
    other_tool_calls = sum(v for k, v in tool_usage.items() if k not in ("exec",))

    result = {
        "user_messages": user_messages,
        "assistant_messages": assistant_messages,
        "total_turns": user_messages + assistant_messages,
        "tool_calls": len(commands),
        "tool_errors": len(errors),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
    }
    if has_config:
        result.update({
            "focus_label": focus_label or "CLI",
            "focus_calls": focus_calls,
            "focus_errors": focus_errors,
            "other_cli_calls": other_cli_calls,
            "other_cli_errors": other_cli_errors,
            "other_tool_calls": other_tool_calls,
            "other_tool_errors": other_tool_errors,
        })
    return result


def calculate_timing_cc(events, commands, total_ms):
    """
    Approximate timing for Claude Code CLI format.
    - LLM time: gap between queue-operation enqueue and first subsequent assistant text turn
    - CLI time: sum of bash command duration_ms (same as OpenClaw)
    - User time: gap between last assistant text and next queue-operation enqueue
    - Idle: residual
    """
    # Build sorted list of key timestamps
    enqueue_ts = sorted(
        parse_timestamp(e["timestamp"])
        for e in events
        if e.get("type") == "queue-operation" and e.get("operation") == "enqueue"
    )

    # First assistant text after each enqueue = end of LLM inference
    # We approximate by finding the first assistant event with text content after each enqueue
    asst_text_ts = []
    for e in events:
        if e.get("type") != "assistant":
            continue
        for c in e.get("message", {}).get("content", []):
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip():
                asst_text_ts.append(parse_timestamp(e["timestamp"]))
                break

    asst_text_ts = sorted(asst_text_ts)

    llm_intervals = []
    user_intervals = []

    for i, enq_ts in enumerate(enqueue_ts):
        # LLM time: enqueue → first assistant text after it
        next_asst = next((t for t in asst_text_ts if t > enq_ts), None)
        if next_asst:
            llm_intervals.append((enq_ts, next_asst))

        # User time: last assistant text before this enqueue → this enqueue
        prev_asst = next((t for t in reversed(asst_text_ts) if t < enq_ts), None)
        # But only count if there's no CLI activity in between (approximation)
        if prev_asst and i > 0:  # skip first message (no user wait)
            user_intervals.append((prev_asst, enq_ts))

    def total_from(intervals):
        return sum(max(0, e - s) for s, e in intervals)

    def avg_from(intervals):
        vals = [max(0, e - s) for s, e in intervals]
        return int(sum(vals) / len(vals)) if vals else 0

    def max_from(intervals):
        vals = [max(0, e - s) for s, e in intervals]
        return max(vals) if vals else 0

    cli_durations = [c["duration_ms"] for c in commands]
    cli_ms = sum(cli_durations)
    llm_ms = total_from(llm_intervals)
    user_ms = total_from(user_intervals)
    idle_ms = max(0, total_ms - llm_ms - cli_ms - user_ms)

    def pct(val):
        return round(val * 100 / total_ms) if total_ms > 0 else 0

    return {
        "total_ms": total_ms,
        "llm_ms": llm_ms,
        "llm_pct": pct(llm_ms),
        "llm_avg_ms": avg_from(llm_intervals),
        "llm_max_ms": max_from(llm_intervals),
        "cli_ms": cli_ms,
        "cli_pct": pct(cli_ms),
        "cli_avg_ms": int(sum(cli_durations) / len(cli_durations)) if cli_durations else 0,
        "cli_max_ms": max(cli_durations) if cli_durations else 0,
        "user_ms": user_ms,
        "user_pct": pct(user_ms),
        "idle_ms": idle_ms,
        "idle_pct": pct(idle_ms),
    }


# ---------------------------------------------------------------------------

def analyze(path, since_arg=None, until_arg=None):
    focus_label, focus_patterns = load_config()

    events = load_events(path)
    if not events:
        print(json.dumps({"error": "Empty session file"}), file=sys.stderr)
        sys.exit(1)

    fmt = detect_format(events)

    # Time range filtering
    if since_arg or until_arg:
        session_start_ms = parse_timestamp(events[0]["timestamp"])
        since_ts, until_ts = resolve_time_range(since_arg, until_arg, session_start_ms)
        events = apply_time_filter(events, since_ts, until_ts)
        if not events:
            print(f"Error: no events found in specified time range", file=sys.stderr)
            sys.exit(1)

    if fmt == "claude-code":
        session = extract_session_meta_cc(events)
        commands, tool_usage, errors, other_tool_errors = extract_tool_calls_cc(events, focus_patterns)
        conversation = extract_conversation_cc(events)
        message_costs = extract_message_costs_cc(events)
        thinking = extract_thinking_cc(events)
        stats = calculate_stats_cc(events, commands, errors, tool_usage, other_tool_errors,
                                   focus_label, focus_patterns)
        timing = calculate_timing_cc(events, commands, session["duration_ms"])
    else:
        session = extract_session_meta(events)
        commands, tool_usage, errors, other_tool_errors = extract_tool_calls(events, focus_patterns)
        conversation = extract_conversation(events)
        message_costs = extract_message_costs(events)
        thinking = extract_thinking(events)
        stats = calculate_stats(events, commands, errors, tool_usage, other_tool_errors,
                                focus_label, focus_patterns)

    if fmt != "claude-code":
        timing = calculate_timing(events, commands, session["duration_ms"])
    loops = detect_loops(commands)

    output = {
        "session": session,
        "stats": stats,
        "timing": timing,
        "loops": loops,
        "errors": errors,
        "commands": strip_internal_fields(commands),
        "tool_usage": tool_usage,
        "conversation": conversation,
        "message_costs": message_costs,
        "thinking": thinking,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Analyze an OpenClaw session JSONL file."
    )
    parser.add_argument("path", help="Path to session.jsonl")
    parser.add_argument("--since", help="Start time filter: HH:MM or ISO timestamp")
    parser.add_argument("--until", help="End time filter: HH:MM or ISO timestamp")
    args = parser.parse_args()
    analyze(args.path, since_arg=args.since, until_arg=args.until)

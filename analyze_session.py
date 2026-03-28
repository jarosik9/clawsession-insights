#!/usr/bin/env python3
"""
OpenClaw session JSONL analyzer.
Usage: python3 analyze_session.py <path-to-session.jsonl>
Outputs: JSON summary to stdout
"""
import json
import sys
import re
import shlex
from pathlib import Path
from datetime import datetime, timezone


def parse_timestamp(ts_str):
    """Parse ISO timestamp string to epoch milliseconds."""
    if ts_str.endswith('Z'):
        ts_str = ts_str[:-1] + '+00:00'
    return int(datetime.fromisoformat(ts_str).timestamp() * 1000)


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

    return {
        "id": session_event.get("id", ""),
        "start_time": session_event["timestamp"],
        "end_time": last_event["timestamp"],
        "duration_ms": end_ms - start_ms,
        "cwd": session_event.get("cwd", ""),
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


def extract_tool_calls(events):
    """
    Extract all exec tool calls with their results.
    Handles process-chain for long-running commands:
      exec toolCall -> running toolResult -> [process toolCall -> running]* -> completed
    Returns: (commands, tool_usage, errors)
    """
    tool_results_by_call_id = {}

    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "toolResult":
            continue
        tool_call_id = msg.get("toolCallId", "")
        if tool_call_id not in tool_results_by_call_id:
            tool_results_by_call_id[tool_call_id] = []
        tool_results_by_call_id[tool_call_id].append(e)

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
        duration_ms = result_ts_ms - call_ts_ms
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

        entry = {
            "tool": "exec",
            "command": call["command"],
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "status": status,
            "timestamp": call["call_ts"],
            "output_text": output_text,
        }
        commands.append(entry)

        if exit_code != 0:
            errors.append({
                "command": call["command"],
                "exit_code": exit_code,
                "error_text": error_text,
                "timestamp": call["call_ts"],
            })

    return commands, tool_usage, errors


def calculate_timing(events, commands, total_ms):
    """
    LLM time: last toolResult in preceding batch -> assistant message.
    Preceding batch = all toolResults between previous assistant and current assistant, in document order.
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
                # First assistant turn — credit from session start
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
    Strip 'npx'/'npx -y', flags (tokens starting with '-'), quoted string args.
    Return first two remaining tokens.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()

    # Strip 'npx' and optional following '-y'
    if tokens and tokens[0] == "npx":
        tokens.pop(0)
        if tokens and tokens[0] == "-y":
            tokens.pop(0)

    filtered = [t for t in tokens if not t.startswith("-")]
    return " ".join(filtered[:2]) if len(filtered) >= 2 else " ".join(filtered)


def detect_loops(commands, window=10, threshold=3):
    """
    Detect loops: same normalized command >= threshold times in any window-sized slice.
    Classification uses toolResult output_text:
    - polling_loop: exit_code==0 AND output contains "pending"/"waiting"/"running"/"in progress"
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
        group = [j for j in matching_indices if i <= j < i + window]

        start_cmd = commands[group[0]]
        end_cmd = commands[group[-1]]

        loop_type = "error_loop"
        all_zero_exit = all(commands[j]["exit_code"] == 0 for j in group)
        if all_zero_exit:
            for cmd_idx in group:
                output_text = commands[cmd_idx].get("output_text", "").lower()
                if any(kw in output_text for kw in polling_keywords):
                    loop_type = "polling_loop"
                    break

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


def calculate_stats(events, commands, errors):
    """Aggregate message counts and token/cost totals."""
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

    return {
        "user_messages": user_messages,
        "assistant_messages": assistant_messages,
        "total_turns": user_messages + assistant_messages,
        "tool_calls": len(commands),
        "tool_errors": len(errors),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
    }


def analyze(path):
    events = load_events(path)
    if not events:
        print(json.dumps({"error": "Empty session file"}), file=sys.stderr)
        sys.exit(1)
    session = extract_session_meta(events)
    commands, tool_usage, errors = extract_tool_calls(events)
    timing = calculate_timing(events, commands, session["duration_ms"])
    loops = detect_loops(commands)
    conversation = extract_conversation(events)
    stats = calculate_stats(events, commands, errors)

    output = {
        "session": session,
        "stats": stats,
        "timing": timing,
        "loops": loops,
        "errors": errors,
        "commands": strip_internal_fields(commands),
        "tool_usage": tool_usage,
        "conversation": conversation,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: analyze_session.py <path-to-session.jsonl>", file=sys.stderr)
        sys.exit(1)
    analyze(sys.argv[1])

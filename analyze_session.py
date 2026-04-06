#!/usr/bin/env python3
"""
Multi-format session analyzer: OpenClaw JSONL, Claude Code CLI JSONL, and Langfuse trace JSON.

Supports three input formats:
  - OpenClaw JSONL: traditional log format from OpenClaw platform
  - Claude Code CLI JSONL: native Claude Code CLI session logs
  - Langfuse trace JSON: JSON array of SPAN/GENERATION items from Langfuse platform

Usage:
  python3 analyze_session.py <path-to-session.jsonl|trace.json> [--since HH:MM] [--until HH:MM]

Output:
  JSON summary to stdout with fields: session, stats, timing, loops, errors, commands, tool_usage

Requirements:
  - Python 3.10+ (stdlib only)
  - Input file must be valid JSONL or JSON array
"""
import json
import sys
import re
import shlex
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta


def parse_timestamp(ts_str):
    """Parse ISO timestamp string to epoch milliseconds."""
    if ts_str.endswith('Z'):
        ts_str = ts_str[:-1] + '+00:00'
    return int(datetime.fromisoformat(ts_str).timestamp() * 1000)


def detect_format(path):
    """
    Detect file format: JSONL (OpenClaw) or JSON array (Langfuse trace).
    Returns 'jsonl' or 'trace'.
    """
    with open(path, 'r', encoding='utf-8') as f:
        first_line = f.readline().strip()
        if first_line.startswith('['):
            return 'trace'
        elif first_line.startswith('{'):
            return 'jsonl'
    raise ValueError(f"Unrecognized format in {path}: expected JSONL or JSON array")


def detect_jsonl_subformat(events):
    """
    Detect JSONL subformat: OpenClaw vs Claude Code CLI.
    Returns 'openclaw' or 'claude-code-cli'.

    Signals:
    - Claude Code CLI: has "permission-mode" event OR "tool_use"/"tool_result" types
    - OpenClaw: has "session" event type OR "toolCall" in message content

    Note: Trace files converted to events have both 'session' and 'tool_use'/'tool_result'.
    We prioritize 'tool_use'/'tool_result' detection since those uniquely identify CLI format.
    """
    if not events:
        return 'openclaw'  # Default fallback

    # First pass: check for CLI signals (highest priority - these indicate CLI or converted trace)
    for event in events[:20]:
        event_type = event.get('type', '')
        if event_type in ('permission-mode', 'tool_use', 'tool_result'):
            return 'claude-code-cli'

    # Second pass: check for OpenClaw signals
    for event in events[:20]:
        event_type = event.get('type', '')

        if event_type == 'session':
            return 'openclaw'

        # Check message structure for Claude Code CLI tool_use/tool_result
        if event_type == 'message':
            msg = event.get('message', {})
            # OpenClaw: toolCall in content
            content = msg.get('content', [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get('type') == 'toolCall':
                        return 'openclaw'

    # Default: if we see sessionId in many events, likely Claude Code CLI
    session_id_count = sum(1 for e in events[:10] if 'sessionId' in e)
    if session_id_count > 5:
        return 'claude-code-cli'

    return 'openclaw'  # Default fallback


def convert_trace_to_events(trace_data):
    """
    Convert Langfuse trace array to OpenClaw-compatible event format.
    Extracts session metadata, turns, and token costs from trace.
    """
    events = []
    session_start = None
    session_end = None
    model = "unknown"
    provider = "unknown"
    user = "unknown"

    # First pass: extract session metadata and build turn map
    turn_by_id = {}
    for item in trace_data:
        item_type = item.get('type', '')
        name = item.get('name', '')

        # Capture session bounds
        if item_type == 'SPAN' and name.startswith('session:'):
            session_start = item.get('startTime')
            session_end = item.get('endTime')
            # Extract model/provider from metadata
            try:
                metadata_str = item.get('metadata', '{}')
                if isinstance(metadata_str, str):
                    metadata = json.loads(metadata_str)
                else:
                    metadata = metadata_str
                model = metadata.get('attributes', {}).get('langfuse.trace.metadata.model', 'unknown')
                provider = metadata.get('attributes', {}).get('langfuse.trace.metadata.provider', 'unknown')
                user_id = metadata.get('attributes', {}).get('langfuse.trace.user_id', 'unknown')
                if user_id != 'unknown':
                    user = user_id
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Index turns by ID
        if item_type == 'SPAN' and name.startswith('turn:'):
            turn_by_id[item.get('id')] = item

    # Create session event
    if session_start:
        events.append({
            "type": "session",
            "timestamp": session_start,
            "id": "trace-session",
            "cwd": "",
            "model": model,
            "provider": provider,
            "user": user,
        })

    # Convert turns to message events
    for item in trace_data:
        item_type = item.get('type', '')
        name = item.get('name', '')

        if item_type == 'SPAN' and name.startswith('turn:'):
            try:
                input_str = item.get('input', '{}')
                if isinstance(input_str, str):
                    input_data = json.loads(input_str)
                else:
                    input_data = input_str

                output_str = item.get('output', '{}')
                if isinstance(output_str, str):
                    output_data = json.loads(output_str)
                else:
                    output_data = output_str

                # Extract role and content
                role = input_data.get('role', 'unknown')
                content = input_data.get('content', '')
                output_content = output_data.get('content', '') if isinstance(output_data, dict) else output_data

                # Create message event
                msg_event = {
                    "type": "message",
                    "timestamp": item.get('startTime'),
                    "message": {
                        "role": role,
                        "content": [{"type": "text", "text": str(content)}] if content else [],
                    }
                }
                events.append(msg_event)

                # Add output if present
                if output_content and role == 'user':
                    events.append({
                        "type": "message",
                        "timestamp": item.get('endTime'),
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": str(output_content)}] if output_content else [],
                        }
                    })
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Extract token costs from GENERATION spans
        if item_type == 'GENERATION':
            try:
                metadata_str = item.get('metadata', '{}')
                if isinstance(metadata_str, str):
                    metadata = json.loads(metadata_str)
                else:
                    metadata = metadata_str

                attrs = metadata.get('attributes', {})
                input_tokens = int(attrs.get('gen_ai.usage.input_tokens', 0))
                output_tokens = int(attrs.get('gen_ai.usage.output_tokens', 0))

                if input_tokens > 0 or output_tokens > 0:
                    # Create a message event with usage info
                    events.append({
                        "type": "message",
                        "timestamp": item.get('startTime'),
                        "message": {
                            "role": "assistant",
                            "content": [],
                            "usage": {
                                "totalTokens": input_tokens + output_tokens,
                                "cost": {"total": 0.0}  # Trace doesn't have cost info
                            }
                        }
                    })
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # M4: Extract tool execution data from SPAN items
        # Tool SPANs have format: "tool_type:description (id)" in name
        if item_type == 'SPAN' and ':' in name and not name.startswith(('session:', 'turn:')):
            try:
                tool_type = name.split(':')[0].lower()

                # Recognize tool types from trace
                if tool_type in ('exec', 'edit', 'read', 'write', 'web_fetch', 'web_search', 'skill_install', 'file_read', 'memory_search', 'process_poll', 'env_bootstrap'):
                    # Parse metadata
                    metadata = {}
                    if 'metadata' in item:
                        try:
                            meta_str = item['metadata']
                            metadata = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass

                    # Parse input
                    input_data = item.get('input', {})
                    if isinstance(input_data, str):
                        try:
                            input_data = json.loads(input_data)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            input_data = {'raw': input_data}

                    # Normalize tool name for internal format
                    normalized_tool = normalize_tool_name(metadata.get('tool_name', tool_type))

                    # Create tool_use event
                    tool_call_id = metadata.get('tool_call_id', item.get('id', ''))
                    tool_event = {
                        "type": "tool_use",
                        "toolCallId": tool_call_id,
                        "toolName": normalized_tool,
                        "input": input_data,
                        "timestamp": item.get('startTime')
                    }
                    events.append(tool_event)

                    # Create tool_result event (same SPAN)
                    if item.get('output') is not None:
                        result_event = {
                            "type": "tool_result",
                            "toolCallId": tool_call_id,
                            "exitCode": metadata.get('exit_code', 0),
                            "output": item.get('output', ''),
                            "timestamp": item.get('endTime')
                        }
                        events.append(result_event)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    # Add final event for session end
    if session_end:
        events.append({
            "type": "session-end",
            "timestamp": session_end,
        })

    return events if events else []


def load_events(path):
    """Load events from JSONL or trace JSON file. Returns list of dicts."""
    file_format = detect_format(path)

    if file_format == 'trace':
        # Load as JSON array and convert
        with open(path, 'r', encoding='utf-8') as f:
            trace_data = json.load(f)
        return convert_trace_to_events(trace_data)
    else:
        # Load as JSONL
        events = []
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))

        # M1: Unified timestamp bug fix
        # Claude Code CLI JSONL has permission-mode event without timestamp as first event.
        # Filter to keep only events starting from the first one with a timestamp.
        first_ts_idx = 0
        for i, e in enumerate(events):
            if 'timestamp' in e:
                first_ts_idx = i
                break

        events = events[first_ts_idx:]

        if not events:
            raise ValueError(f"No events with timestamp found in {path}")

        return events


def extract_user_claude_code(events):
    """
    Extract user identity from Claude Code CLI session.

    Priority order:
    1. Check for explicit 'user' field (from trace conversion)
    2. Check 'entrypoint' field (cli, web, etc.)
    3. Regex extraction from message content
    """
    for e in events:
        # Priority 1: explicit user field
        if 'user' in e and e.get('user') not in ('unknown', None):
            return e['user']
        # Priority 2: entrypoint field
        if e.get('entrypoint') == 'cli':
            return 'cli'

    # Priority 3: Try to extract from message content
    for e in events:
        if e.get('type') != 'message':
            continue
        msg = e.get('message', {})
        if msg.get('role') != 'user':
            continue
        content = msg.get('content', [])
        if isinstance(content, str):
            # Sometimes Claude Code CLI embeds user in message string
            if 'User:' in content or 'user:' in content:
                return 'user'
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    text = item.get('text', '')
                    # Check for sender pattern like in OpenClaw
                    match = re.search(r'"sender"\s*:\s*"([^"]+)"', text)
                    if match:
                        return match.group(1)

    return 'unknown'


def extract_session_meta(events):
    """
    Extract session metadata from events.
    Supports both OpenClaw JSONL and Claude Code CLI JSONL formats.
    """
    if not events:
        raise ValueError("No events found in session file")

    # Detect format
    sub_format = detect_jsonl_subformat(events)

    last_event = events[-1]
    end_ms = parse_timestamp(last_event["timestamp"])

    if sub_format == 'claude-code-cli':
        # Claude Code CLI format
        session_id = ""
        for e in events:
            if 'sessionId' in e:
                session_id = e['sessionId']
                break

        start_ms = parse_timestamp(events[0]["timestamp"])
        cwd = ""
        for e in events:
            if 'cwd' in e:
                cwd = e['cwd']
                break

        model = "unknown"
        provider = "unknown"
        for e in events:
            if e.get('type') == 'model-snapshot':
                model = e.get('data', {}).get('modelId', 'unknown')
                provider = e.get('data', {}).get('provider', 'unknown')
                break

        user = extract_user_claude_code(events)

    else:
        # OpenClaw format
        session_event = events[0]
        session_id = session_event.get('id', '')
        start_ms = parse_timestamp(session_event["timestamp"])
        cwd = session_event.get("cwd", "")

        model = "unknown"
        provider = "unknown"
        for e in events:
            if e.get("type") == "custom" and e.get("customType") == "model-snapshot":
                model = e.get("data", {}).get("modelId", "unknown")
                provider = e.get("data", {}).get("provider", "unknown")
                break

        # Extract user from OpenClaw message metadata block
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

    # Redact home directory prefix to avoid leaking username in shared reports.
    # Matches /home/<user>/..., /Users/<user>/..., /root/...
    cwd = re.sub(r'^(/home/[^/]+|/Users/[^/]+|/root)', '~', cwd)

    return {
        "id": session_id,
        "start_time": events[0]["timestamp"],
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


def normalize_tool_name(name):
    """
    Map tool names from Claude Code CLI format to internal exec format.
    Claude Code: Bash, Read, Write, Edit
    Internal: exec, read, write, edit
    Also handles trace format tool names.
    """
    mapping = {
        'Bash': 'exec',
        'Read': 'read',
        'Write': 'write',
        'Edit': 'edit',
        'WebSearch': 'web_search',
        'WebFetch': 'web_fetch',
        'env_bootstrap': 'exec',  # env_bootstrap is effectively exec
    }
    return mapping.get(name, name.lower())


def extract_tool_calls_openclaw(events):
    """
    Extract all tool calls from OpenClaw format.
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


def extract_tool_calls_claude_code(events):
    """
    Extract tool calls from Claude Code CLI format.
    Claude Code embeds tool calls in message.content:
      type: "assistant" with message.content[].type == "tool_use"
      Tool results come from subsequent user messages or system messages
    Returns: (commands, tool_usage, errors)
    """
    tool_usage = {}
    tool_calls_by_id = {}
    commands = []
    errors = []

    # First pass: collect all tool calls from assistant messages
    for event in events:
        if event.get('type') != 'assistant':
            continue

        msg = event.get('message', {})
        content = msg.get('content', [])
        if not isinstance(content, list):
            continue

        # Look for tool_use items in content
        for item in content:
            if not isinstance(item, dict) or item.get('type') != 'tool_use':
                continue

            tool_call_id = item.get('id', '')
            tool_name = item.get('name', '')

            # Normalize tool name
            normalized_name = normalize_tool_name(tool_name)
            tool_usage[normalized_name] = tool_usage.get(normalized_name, 0) + 1

            # Extract command
            command = ''
            input_data = item.get('input', {})
            if isinstance(input_data, dict):
                command = input_data.get('command', '')
                if not command:
                    # For non-exec tools, try to get a string representation
                    command = json.dumps(input_data)[:200]
            elif isinstance(input_data, str):
                command = input_data

            call_ts = event.get('timestamp', '')
            if not call_ts:
                # Try to infer timestamp from parent event
                continue

            call_ts_ms = parse_timestamp(call_ts)

            tool_calls_by_id[tool_call_id] = {
                'tool': normalized_name,
                'command': command,
                'call_ts': call_ts,
                'call_ts_ms': call_ts_ms,
            }

    # Second pass: match tool results from user/system messages
    for i, event in enumerate(events):
        event_type = event.get('type')
        if event_type not in ('user', 'system'):
            continue

        msg = event.get('message', {})
        if not msg:
            continue

        # Check if this is a tool result (contains output from a previous tool call)
        content = msg.get('content', '')
        if not content:
            continue

        # Try to match this with preceding tool calls
        # Look backwards for the most recent tool call
        for j in range(i - 1, -1, -1):
            prev_event = events[j]
            if prev_event.get('type') != 'assistant':
                continue

            prev_msg = prev_event.get('message', {})
            prev_content = prev_msg.get('content', [])
            if not isinstance(prev_content, list):
                continue

            # Find the last tool_use in this assistant message
            for item in reversed(prev_content):
                if item.get('type') == 'tool_use':
                    tool_call_id = item.get('id', '')
                    if tool_call_id in tool_calls_by_id:
                        call_info = tool_calls_by_id[tool_call_id]

                        # Parse the content as potential exit code and output
                        result_ts = event.get('timestamp', call_info['call_ts'])
                        result_ts_ms = parse_timestamp(result_ts)

                        # Try to extract exit code from content
                        exit_code = 0
                        output_text = str(content)[:500] if isinstance(content, str) else ''
                        error_text = ''

                        # Look for exit code markers
                        if isinstance(content, str):
                            if 'exit status' in content.lower() or 'error' in content.lower():
                                exit_code = 1
                            if 'not found' in content.lower() or 'command not found' in content.lower():
                                exit_code = 127

                        duration_ms = result_ts_ms - call_info['call_ts_ms']
                        status = 'ok' if exit_code == 0 else 'error'

                        entry = {
                            'tool': call_info['tool'],
                            'command': call_info['command'],
                            'exit_code': exit_code,
                            'duration_ms': duration_ms,
                            'status': status,
                            'timestamp': call_info['call_ts'],
                            'output_text': output_text,
                        }
                        commands.append(entry)

                        if exit_code != 0:
                            errors.append({
                                'command': call_info['command'],
                                'exit_code': exit_code,
                                'error_text': output_text[:200],
                                'timestamp': call_info['call_ts'],
                            })

                        # Remove from pending calls
                        del tool_calls_by_id[tool_call_id]
                    break
            break

    # Add remaining tool calls that didn't have results matched
    for tool_call_id, call_info in tool_calls_by_id.items():
        entry = {
            'tool': call_info['tool'],
            'command': call_info['command'],
            'exit_code': 0,
            'duration_ms': 0,
            'status': 'ok',
            'timestamp': call_info['call_ts'],
            'output_text': '',
        }
        commands.append(entry)

    return commands, tool_usage, errors


def extract_tool_calls_trace(events):
    """
    Extract tool calls from trace-converted events (standalone tool_use/tool_result).
    Used when events are created from Langfuse trace conversion.
    Returns: (commands, tool_usage, errors)
    """
    tool_usage = {}
    tool_results_by_id = {}
    tool_calls_by_id = {}
    commands = []
    errors = []

    # First pass: collect tool_result events by toolCallId
    for event in events:
        if event.get('type') == 'tool_result':
            call_id = event.get('toolCallId', '')
            if call_id:
                tool_results_by_id[call_id] = event

    # Second pass: collect tool_use events and match with results
    for event in events:
        if event.get('type') != 'tool_use':
            continue

        call_id = event.get('toolCallId', '')
        tool_name = event.get('toolName', '')

        if not tool_name:
            continue

        # Track tool usage
        tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1

        # Get tool call details
        input_data = event.get('input', {})
        call_ts = event.get('timestamp', '')

        if not call_ts:
            continue

        call_ts_ms = parse_timestamp(call_ts)

        # Build command string
        command = ''
        if tool_name == 'exec' and isinstance(input_data, dict):
            command = input_data.get('command', '')
        else:
            command = json.dumps(input_data)[:200] if input_data else ''

        # Match with tool_result
        result = tool_results_by_id.get(call_id)
        exit_code = 0
        output_text = ''
        duration_ms = 0

        if result:
            exit_code = result.get('exitCode', 0)
            if isinstance(exit_code, str):
                try:
                    exit_code = int(exit_code)
                except (json.JSONDecodeError, TypeError, ValueError):
                    exit_code = 0
            output_text = result.get('output', '')[:500]
            result_ts = result.get('timestamp', call_ts)
            if result_ts:
                result_ts_ms = parse_timestamp(result_ts)
                duration_ms = result_ts_ms - call_ts_ms

        status = 'ok' if exit_code == 0 else 'error'

        entry = {
            'tool': tool_name,
            'command': command,
            'exit_code': exit_code,
            'duration_ms': duration_ms,
            'status': status,
            'timestamp': call_ts,
            'output_text': output_text,
        }
        commands.append(entry)

        if exit_code != 0:
            errors.append({
                'command': command,
                'exit_code': exit_code,
                'error_text': output_text[:200] if output_text else '',
                'timestamp': call_ts,
            })

    return commands, tool_usage, errors


def extract_tool_calls(events):
    """
    Extract all tool calls with their results.
    Supports OpenClaw, Claude Code CLI, and trace-converted formats.
    Returns: (commands, tool_usage, errors)
    """
    # Single pass: check for format signals
    has_standalone_tool_events = False
    has_embedded_tool_events = False

    for e in events[:50]:
        event_type = e.get('type')
        if event_type in ('tool_use', 'tool_result'):
            has_standalone_tool_events = True
        elif event_type == 'message' and e.get('message', {}).get('role') == 'assistant':
            if any(item.get('type') == 'tool_use' for item in e.get('message', {}).get('content', [])):
                has_embedded_tool_events = True

        # Early exit if we've found what we need
        if has_standalone_tool_events and has_embedded_tool_events:
            break

    # If we have standalone tool events but not embedded, it's trace format
    if has_standalone_tool_events and not has_embedded_tool_events:
        return extract_tool_calls_trace(events)

    # Otherwise use format detection
    sub_format = detect_jsonl_subformat(events)

    if sub_format == 'claude-code-cli':
        return extract_tool_calls_claude_code(events)
    else:
        return extract_tool_calls_openclaw(events)


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


def strip_shell_prefix(cmd):
    """
    Strip leading shell setup statements (export VAR=value, env VAR=value)
    and return the first real executable command.

    Splits on ';', '&&', and newlines, skips segments that are pure export/env
    assignments, returns the first non-export/non-env segment.
    """
    segments = re.split(r';|&&|\n', cmd)
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        # Skip comment lines
        if segment.startswith('#'):
            continue
        # Skip pure "export VAR=value" statements
        if re.match(r'^export\s+\w+=', segment):
            continue
        # Handle "env VAR=val cmd args" — strip leading VAR=val tokens.
        # Note: if a real command arg contains '=', it will also be stripped.
        # This is accepted as a rare edge case.
        if re.match(r'^env\s+\w+=', segment):
            tokens = segment.split()
            real_tokens = [t for t in tokens[1:] if '=' not in t]
            if real_tokens:
                return ' '.join(real_tokens)
            continue
        # Skip VAR=value inline assignments (e.g. "CALLDATA=$(caw ...)")
        if re.match(r'^[A-Z_][A-Z0-9_]*=', segment):
            # Extract the value part after '='
            eq_idx = segment.index('=')
            value = segment[eq_idx + 1:].strip()
            # If value starts with $( ... ), extract the inner command
            if value.startswith('$('):
                inner = value[2:]
                if inner.endswith(')'):
                    inner = inner[:-1]
                return inner.strip() if inner.strip() else segment
            continue
        # First non-prefix segment is the real command
        return segment
    return ''  # all segments were prefixes/comments — no real command


def normalize_command(cmd):
    """
    Normalize command for loop detection.
    Strip shell setup prefixes (export/env/VAR=), comments, then strip
    'npx'/'npx -y', flags (tokens starting with '-'), and flag value tokens.
    Return first two remaining tokens.
    """
    cmd = strip_shell_prefix(cmd.strip())

    # Skip comment-only lines
    if cmd.lstrip().startswith('#'):
        return ''

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()

    # Strip 'npx' and optional following '-y'
    if tokens and tokens[0] == "npx":
        tokens.pop(0)
        if tokens and tokens[0] == "-y":
            tokens.pop(0)

    # Skip flags and their value tokens (v0.3 fix)
    filtered = []
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            skip_next = True
            continue
        filtered.append(t)

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


def detect_wasted_calls(commands):
    """
    Scan commands[] for unnecessary calls.
    Returns dict with total, by_type counts, waste_ratio, and details[].

    Wasted call types:
    - blind_retry: same normalised command fails consecutively (same raw command)
    - help_exploration: --help or help subcommand
    - flag_trial_error: same normalised command, different raw command (flag changes), all fail
    - env_probing: which <tool> or find -name <tool>

    NOT wasted: --version, polling_loop, exploration_loop, successful duplicates, read-only.
    """
    # read-only commands to skip — note: 'which' and 'find' are NOT here
    # because they are checked as env_probing (type 4) instead
    read_only = {'ls', 'cat', 'head', 'tail', 'echo', 'pwd', 'grep', 'rg'}

    normalized = [normalize_command(c.get("command", "")) for c in commands]
    wasted = []

    i = 0
    while i < len(commands):
        cmd = commands[i]
        norm = normalized[i]
        raw = cmd.get("command", "")
        exit_code = cmd.get("exit_code", 0)
        base_cmd = norm.split()[0] if norm else ""

        # Skip empty / read-only
        if not norm or base_cmd in read_only:
            i += 1
            continue

        # === Type 1: blind_retry ===
        # Same normalised AND same raw command, consecutive failures
        if exit_code != 0:
            j = i + 1
            while (j < len(commands)
                   and normalized[j] == norm
                   and commands[j].get("command", "") == raw  # same raw = truly identical
                   and commands[j].get("exit_code", 0) != 0):
                j += 1
            if j - i >= 2:
                for k in range(i + 1, j):
                    wasted.append({
                        "index": k,
                        "type": "blind_retry",
                        "command": commands[k]["command"][:80],
                        "reason": f"identical command failed {j - i}x in a row"
                    })
                i = j
                continue

        # === Type 2: help_exploration ===
        if "--help" in raw or raw.rstrip().endswith(" help"):
            wasted.append({
                "index": i,
                "type": "help_exploration",
                "command": raw[:80],
                "reason": "agent exploring CLI usage"
            })
            i += 1
            continue

        # === Type 2b: version_check ===
        if "--version" in raw or norm.endswith(" version") or norm == "version":
            wasted.append({
                "index": i,
                "type": "version_check",
                "command": raw[:80],
                "reason": "agent probing tool version"
            })
            i += 1
            continue

        # === Type 3: flag_trial_error ===
        # Same normalised, different raw (flag variations), consecutive failures
        if exit_code != 0:
            j = i + 1
            while (j < len(commands)
                   and normalized[j] == norm
                   and commands[j].get("command", "") != raw
                   and commands[j].get("exit_code", 0) != 0):
                j += 1
            if j - i >= 2:
                for k in range(i + 1, j):
                    wasted.append({
                        "index": k,
                        "type": "flag_trial_error",
                        "command": commands[k]["command"][:80],
                        "reason": f"same operation, different flags, {j - i} consecutive failures"
                    })
                i = j
                continue

        # === Type 4: env_probing ===
        if base_cmd == "which" or (base_cmd == "find" and "-name" in raw):
            wasted.append({
                "index": i,
                "type": "env_probing",
                "command": raw[:80],
                "reason": "agent searching for tool location"
            })
            i += 1
            continue

        i += 1

    # Compute summary
    meaningful_count = sum(
        1 for i, c in enumerate(commands)
        if normalized[i] and normalized[i].split()[0] not in read_only
    )
    by_type = {}
    for w in wasted:
        by_type[w["type"]] = by_type.get(w["type"], 0) + 1

    return {
        "total": len(wasted),
        "by_type": by_type,
        "waste_ratio": round(len(wasted) / meaningful_count, 3) if meaningful_count > 0 else 0,
        "details": wasted,
    }


def detect_recovery_quality(commands, max_attempts=2):
    """
    For each failed command, check if the same operation succeeds within
    max_attempts subsequent tries. Measures outcome, not just "did something different."

    Resolution rules:
    - Same normalised operation succeeds within max_attempts → resolved
    - Same normalised operation does NOT succeed within max_attempts → unresolved
    - Permission errors (403/forbidden) where agent stops trying → correctly_abandoned
      (excluded from both numerator and denominator)
    - REQUIRE_APPROVAL → excluded (not an error)
    - Read-only command failures → excluded
    - Last error with no subsequent commands → excluded

    recovery_rate = resolved / (resolved + unresolved)
    """
    read_only = {'ls', 'cat', 'head', 'tail', 'echo', 'pwd', 'grep', 'rg'}
    approval_kw = ['require_approval', 'pending_approval', 'approval_required']
    permission_kw = ['403', 'permission', 'forbidden']

    normalized = [normalize_command(c.get("command", "")) for c in commands]
    details = []
    # Track which error indices we've already evaluated (to avoid double-counting
    # when the same operation fails multiple times)
    evaluated_ops = set()

    for i, cmd in enumerate(commands):
        exit_code = cmd.get("exit_code", 0)
        if exit_code == 0:
            continue

        norm = normalized[i]
        raw = cmd.get("command", "")
        base = norm.split()[0] if norm else ""
        error_text = cmd.get("output_text", "").lower()

        # Skip empty / read-only failures
        if not norm or base in read_only:
            continue

        # Skip approval (not an error)
        if any(kw in error_text for kw in approval_kw):
            continue

        # Skip if we already evaluated this normalised operation from an earlier failure
        if norm in evaluated_ops:
            continue
        evaluated_ops.add(norm)

        # Check for permission error — see if agent correctly stopped
        search_text = error_text + " " + raw.lower()
        is_permission = any(kw in search_text for kw in permission_kw)

        # Look forward: count subsequent attempts at the same normalised operation
        attempts_after = []
        for j in range(i + 1, len(commands)):
            if normalized[j] == norm:
                attempts_after.append(j)

        if is_permission:
            if not attempts_after:
                # Agent stopped after permission error → correctly abandoned
                details.append({
                    "error_index": i,
                    "operation": norm,
                    "error_command": raw[:80],
                    "outcome": "correctly_abandoned",
                    "attempts": 0,
                    "reason": "permission error — agent correctly stopped",
                })
            else:
                # Agent retried after permission error → bad
                details.append({
                    "error_index": i,
                    "operation": norm,
                    "error_command": raw[:80],
                    "outcome": "unresolved",
                    "attempts": len(attempts_after),
                    "reason": f"permission error — agent retried {len(attempts_after)}x (should have stopped)",
                })
            continue

        # Non-permission error: did the same operation succeed within max_attempts?
        resolved = False
        attempts_used = 0
        for idx, j in enumerate(attempts_after):
            if idx >= max_attempts:
                break
            attempts_used = idx + 1
            if commands[j].get("exit_code", 0) == 0:
                resolved = True
                break

        if not attempts_after:
            # Operation never attempted again — unresolved
            details.append({
                "error_index": i,
                "operation": norm,
                "error_command": raw[:80],
                "outcome": "unresolved",
                "attempts": 0,
                "reason": "operation never retried",
            })
        elif resolved:
            details.append({
                "error_index": i,
                "operation": norm,
                "error_command": raw[:80],
                "outcome": "resolved",
                "attempts": attempts_used,
                "reason": f"resolved in {attempts_used} attempt{'s' if attempts_used > 1 else ''}",
            })
        else:
            total_after = len(attempts_after)
            # Check if it eventually succeeded (beyond window)
            eventually = any(commands[j].get("exit_code", 0) == 0 for j in attempts_after)
            details.append({
                "error_index": i,
                "operation": norm,
                "error_command": raw[:80],
                "outcome": "unresolved",
                "attempts": min(total_after, max_attempts),
                "reason": f"not resolved within {max_attempts} attempts"
                          + (f" (succeeded on attempt {total_after} — brute-forced)" if eventually else ""),
            })

    resolved_count = sum(1 for d in details if d["outcome"] == "resolved")
    unresolved_count = sum(1 for d in details if d["outcome"] == "unresolved")
    abandoned_count = sum(1 for d in details if d["outcome"] == "correctly_abandoned")
    denominator = resolved_count + unresolved_count

    return {
        "resolved": resolved_count,
        "unresolved": unresolved_count,
        "correctly_abandoned": abandoned_count,
        "total_evaluated": len(details),
        "recovery_rate": round(resolved_count / denominator, 3) if denominator > 0 else 1.0,
        "details": details,
    }


def detect_hallucinations(conversation, commands):
    """
    Cross-check assistant completion claims against actual command results.

    Step 1: Find completion claims — requires BOTH an action verb AND a success
    indicator. Bare ✅ without a verb (e.g., "✅ New session started", table content)
    does not count as a claim.

    Step 2: For each claim, check the most recent meaningful commands.
    Flag as hallucination if last command failed or 60%+ of recent window failed.

    Note: conversation[].timestamp and commands[].timestamp come from the same
    JSONL source (both ISO format), so timezone alignment is guaranteed.
    """
    # Success indicators — must appear together with an action verb to be a claim.
    # "成功" (Chinese for "success") is a success indicator, not an action verb.
    success_indicators = ['✅', '✓', 'successfully', 'success', '成功']

    # Action verbs — pure verbs only. Do NOT include "成功" here (it's a success
    # indicator); otherwise any message with "成功" would self-trigger as a claim.
    action_verbs_en = ['completed', 'transferred', 'submitted', 'created',
                       'deployed', 'executed', 'sent', 'approved', 'confirmed']
    action_verbs_zh = ['完成', '提交', '创建', '转账', '部署',
                       '执行', '发送', '批准', '确认']

    # Standalone completion phrases (don't need a separate success indicator)
    standalone_phrases = [
        'transaction successful', 'operation complete', 'transfer complete',
        '交易成功', '转账成功', '操作成功', '操作完成',
    ]

    read_only = {'ls', 'cat', 'head', 'tail', 'echo', 'pwd', 'grep', 'rg'}

    timed_cmds = [c for c in commands if c.get("timestamp")]

    # Step 1: find claims — require success indicator + action verb
    claims = []
    for msg in conversation:
        if msg.get("role") != "assistant":
            continue
        text = msg.get("text", "")
        ts = msg.get("timestamp")
        if not ts or not text:
            continue

        head = text[:300].lower()

        # Check standalone phrases first (self-sufficient)
        is_claim = any(phrase in head for phrase in standalone_phrases)

        if not is_claim:
            # Check success indicator + action verb combination
            has_success = any(sig.lower() in head for sig in success_indicators)
            if has_success:
                has_action = (any(v in head for v in action_verbs_en)
                              or any(v in head for v in action_verbs_zh))
                is_claim = has_action

        if is_claim:
            claims.append({"timestamp": ts, "text": text[:120]})

    # Step 2: cross-check
    details = []
    for claim in claims:
        recent = [c for c in timed_cmds
                  if c["timestamp"] <= claim["timestamp"]
                  and normalize_command(c.get("command", "")).split()[0] not in read_only]
        recent = recent[-5:]

        if not recent:
            continue

        last_cmd = recent[-1]
        fail_count = sum(1 for c in recent if c.get("exit_code", 0) != 0)

        is_hallucination = False
        reason = ""

        if last_cmd.get("exit_code", 0) != 0:
            is_hallucination = True
            reason = f"last command failed (exit {last_cmd['exit_code']}): {last_cmd.get('command','')[:60]}"
        elif fail_count >= 3:
            is_hallucination = True
            reason = f"{fail_count}/{len(recent)} recent commands failed"

        if is_hallucination:
            details.append({
                "claim_text": claim["text"][:100],
                "claim_timestamp": claim["timestamp"],
                "last_command": last_cmd.get("command", "")[:80],
                "last_exit_code": last_cmd.get("exit_code", 0),
                "recent_fail_count": fail_count,
                "recent_total": len(recent),
                "reason": reason,
            })

    total_claims = len(claims)
    hallucination_count = len(details)

    return {
        "total_claims": total_claims,
        "hallucinations": hallucination_count,
        "hallucination_rate": round(hallucination_count / total_claims, 3) if total_claims > 0 else 0,
        "details": details,
    }


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


def analyze(path, since_arg=None, until_arg=None):
    events = load_events(path)
    if not events:
        print(json.dumps({"error": "Empty session file"}), file=sys.stderr)
        sys.exit(1)

    # Time range filtering
    if since_arg or until_arg:
        session_start_ms = parse_timestamp(events[0]["timestamp"])
        since_ts, until_ts = resolve_time_range(since_arg, until_arg, session_start_ms)
        events = apply_time_filter(events, since_ts, until_ts)
        if not events:
            print(f"Error: no events found in specified time range", file=sys.stderr)
            sys.exit(1)

    session = extract_session_meta(events)
    commands, tool_usage, errors = extract_tool_calls(events)
    timing = calculate_timing(events, commands, session["duration_ms"])
    loops = detect_loops(commands)
    wasted = detect_wasted_calls(commands)
    recovery = detect_recovery_quality(commands)
    conversation = extract_conversation(events)
    hallucinations = detect_hallucinations(conversation, commands)
    stats = calculate_stats(events, commands, errors)
    message_costs = extract_message_costs(events)
    thinking = extract_thinking(events)

    output = {
        "session": session,
        "stats": stats,
        "timing": timing,
        "loops": loops,
        "wasted_calls": wasted,
        "recovery": recovery,
        "hallucinations": hallucinations,
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

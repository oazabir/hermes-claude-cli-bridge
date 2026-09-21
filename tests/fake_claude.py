#!/usr/bin/env python3
"""A stand-in for the `claude` CLI, used by tests/test_bridge.py (no login, no network, no cost).

It understands just enough of Claude Code's flags to exercise the bridge:
  --session-id X   new session; fails with "already in use" if X exists
  --resume X       existing session; fails with "No conversation found" if X is unknown
  --no-session-persistence   nothing is stored
The prompt is the last argument. It answers "reply[<mode>] <prompt>" as stream-json (with a huge cumulative cache_read usage, like a real tool loop), and if the prompt contains
USE_TOOL it also emits a Bash tool call first. Every invocation is appended to $FAKE_CLAUDE_LOG (JSON lines).
Known sessions live in $FAKE_CLAUDE_STATE (a JSON list).
"""
import json
import os
import sys

argv = sys.argv[1:]
prompt = argv[-1]
log = os.environ.get("FAKE_CLAUDE_LOG")
state_path = os.environ.get("FAKE_CLAUDE_STATE")

if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": argv}) + "\n")


def load():
    try:
        return set(json.load(open(state_path)))
    except Exception:
        return set()


def out(obj):
    print(json.dumps(obj), flush=True)


def fail(msg):
    sys.stderr.write(msg + "\n")
    out({"type": "result", "is_error": True, "result": msg})
    sys.exit(1)


mode = "oneshot"
known = load() if state_path else set()
if "--session-id" in argv:
    sid = argv[argv.index("--session-id") + 1]
    if sid in known:
        fail(f"Error: Session ID {sid} is already in use.")
    known.add(sid)
    mode = "new"
elif "--resume" in argv:
    sid = argv[argv.index("--resume") + 1]
    if sid not in known:
        fail(f"No conversation found with session ID: {sid}")
    mode = "resume"
if state_path and mode != "oneshot":
    json.dump(sorted(known), open(state_path, "w"))

if "USE_TOOL" in prompt:
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "# list the files\nls -la"}}]}})

text = f"reply[{mode}] {prompt}"
half = len(text) // 2
for piece in (text[:half], text[half:]):
    out({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": piece}}})
out({"type": "result", "is_error": False, "result": text, "usage": {"input_tokens": 3, "cache_read_input_tokens": 700000, "cache_creation_input_tokens": 0, "output_tokens": 5}})

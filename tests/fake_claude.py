#!/usr/bin/env python3
"""A stand-in for the `claude` CLI, used by tests/test_bridge.py (no login, no network, no cost).

It understands just enough of Claude Code's flags to exercise the bridge:
  --session-id X   new session; fails with "already in use" if X exists
  --resume X       existing session; fails with "No conversation found" if X is unknown
  --no-session-persistence   nothing is stored
The prompt is read from stdin, like the real CLI when no prompt argument is given. It answers
"reply[<mode>] <prompt>" as stream-json (with a huge cumulative cache_read usage, like a real tool loop), and if the prompt contains
USE_TOOL it also emits a Bash tool call first
(after a 'Checking.' text block if it contains PREAMBLE; the tool takes $FAKE_CLAUDE_TOOL_SECONDS). Every invocation is appended to $FAKE_CLAUDE_LOG (JSON lines).
Known sessions live in $FAKE_CLAUDE_STATE (a JSON list).
Set FAKE_CLAUDE_SLEEP to make a run hang, and FAKE_CLAUDE_SPAWN_CHILD=1 to leave a grandchild holding stdout
(the case that used to wedge the bridge's read loop past its timeout).
"""
import json
import os
import subprocess
import sys

argv = sys.argv[1:]
prompt = sys.stdin.read()
log = os.environ.get("FAKE_CLAUDE_LOG")
state_path = os.environ.get("FAKE_CLAUDE_STATE")

if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": argv, "prompt": prompt, "pgid": os.getpgid(0)}) + "\n")

if os.environ.get("FAKE_CLAUDE_SPAWN_CHILD") == "1":
    # a grandchild that inherits stdout and outlives us, exactly like a `claude` Bash tool call
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
if os.environ.get("FAKE_CLAUDE_SLEEP") and "NOSLEEP" not in prompt:
    # NOSLEEP lets one call in a sleepy run answer immediately, so a test can time how long a
    # FOLLOW-UP waited for the session lock without also waiting out its own sleep.
    import time
    time.sleep(float(os.environ["FAKE_CLAUDE_SLEEP"]))


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

if "PREAMBLE" in prompt:
    out({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "Checking."}}})
if "USE_TOOL" in prompt:
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "# list the files\nls -la"}}]}})
    if os.environ.get("FAKE_CLAUDE_TOOL_SECONDS"):  # a slow tool: nothing on stdout meanwhile
        import time
        time.sleep(float(os.environ["FAKE_CLAUDE_TOOL_SECONDS"]))

text = f"reply[{mode}] {prompt}"
half = len(text) // 2
for piece in (text[:half], text[half:]):
    out({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": piece}}})
out({"type": "result", "is_error": False, "result": text, "usage": {"input_tokens": 3, "cache_read_input_tokens": 700000, "cache_creation_input_tokens": 0, "output_tokens": 5}})

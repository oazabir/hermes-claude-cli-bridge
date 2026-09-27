#!/usr/bin/env python3
"""A stand-in for the `claude` CLI, used by tests/test_bridge.py (no login, no network, no cost).

It understands just enough of Claude Code's flags to exercise the bridge:
  --session-id X   new session; fails with "already in use" if X exists
  --resume X       existing session; fails with "No conversation found" if X is unknown
  --no-session-persistence   nothing is stored
The prompt is read from stdin, like the real CLI when no prompt argument is given. It answers
"reply[<mode>] <prompt>" as stream-json (with a huge cumulative cache_read usage, like a real tool loop), and if the prompt contains
USE_TOOL it also emits a Bash tool call first
(after a 'Checking.' text block if it contains PREAMBLE; the tool takes $FAKE_CLAUDE_TOOL_SECONDS, runs as a child
process per $FAKE_CLAUDE_TOOL_MODE busy|silent, and emits a heartbeat every $FAKE_CLAUDE_HEARTBEAT seconds). Every invocation is appended to $FAKE_CLAUDE_LOG (JSON lines).
Known sessions live in $FAKE_CLAUDE_STATE (a JSON list).
Set FAKE_CLAUDE_LINGER to keep running (busy) for that many seconds after the answer text and before the result,
FAKE_CLAUDE_SLEEP to make a run hang, and FAKE_CLAUDE_SPAWN_CHILD=1 to leave a grandchild holding stdout
(the case that used to wedge the bridge's read loop past its timeout).
"""
import json
import os
import subprocess
import sys

argv = sys.argv[1:]
LIVE = "--input-format" in argv  # stream-json input: one long-lived process, messages as JSON lines on stdin
prompt = "" if LIVE else sys.stdin.read()
log = os.environ.get("FAKE_CLAUDE_LOG")
state_path = os.environ.get("FAKE_CLAUDE_STATE")

if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": argv, "prompt": prompt, "pgid": os.getpgid(0),
                            "bg_ceiling": os.environ.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS")}) + "\n")

if os.environ.get("FAKE_CLAUDE_SPAWN_CHILD") == "1":
    # a grandchild that inherits stdout and outlives us, exactly like a `claude` Bash tool call
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
if os.environ.get("FAKE_CLAUDE_SETSID_PIDFILE"):
    # a background job in a process group (session) of its own, like claude's run_in_background Bash
    job = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(os.environ["FAKE_CLAUDE_SETSID_PIDFILE"], "w").write(str(job.pid))
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


def live_main():
    """Mimics claude -p --input-format stream-json (as probed): each stdin user message is echoed back
    (--replay-user-messages, same uuid) when taken in, and a `result` answers it; a message that arrives mid-turn
    is folded into the running turn; a background task that finishes while idle starts a turn by itself; an
    interrupt control_request ends the turn with error_during_execution; stdin EOF stops background tasks and
    exits. Message keywords: SLOW:<s> (a Bash tool call taking s seconds, busy), BG:<s> (a background task that
    finishes after s seconds; BG:<s>:<w> -- the turn it starts then works w seconds in silence), DIE (crash mid-turn), SILENT:<s> (s seconds with no output at all)."""
    import queue
    import re
    import threading
    import time
    lock = threading.Lock()
    inbox, stop = queue.Queue(), threading.Event()
    interrupted = threading.Event()
    bg: dict = {}
    n = [0]

    def emit(obj):
        with lock:
            print(json.dumps(obj), flush=True)

    def turn_text(text):
        half = len(text) // 2
        for piece in (text[:half], text[half:]):
            emit({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": piece}}})
        emit({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}})

    def result(text, error=False):
        emit({"type": "result", "subtype": "error_during_execution" if error else "success", "is_error": error,
              "result": None if error else text,
              "usage": {"input_tokens": 3, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0, "output_tokens": 5}})

    def bg_done(tid, secs, work=0.0):
        if stop.wait(secs):
            return
        with lock:
            bg.pop(tid, None)
            tasks = [{"task_id": k} for k in bg]
        emit({"type": "system", "subtype": "background_tasks_changed", "tasks": tasks})
        emit({"type": "system", "subtype": "task_notification", "task_id": tid, "status": "completed"})
        inbox.put(("bg", tid, work))

    def take(msg):
        if log:
            with open(log, "a") as f:
                f.write(json.dumps({"argv": argv, "prompt": msg["text"], "pid": os.getpid(), "live": True}) + "\n")
        if "--replay-user-messages" in argv:
            emit({"type": "user", "isReplay": True, "uuid": msg["uuid"],
                  "message": {"role": "user", "content": msg["text"]}})

    def run(first):
        """One turn: `first` is a user message or ('bg', tid). Messages that arrive meanwhile are folded in."""
        interrupted.clear()
        emit({"type": "system", "subtype": "init", "session_id": "fake"})
        parts = []
        items = [first]
        while items:
            item = items.pop(0)
            if isinstance(item, tuple):
                time.sleep(item[2])  # the turn works on the result silently; messages meanwhile wait, then fold in
                parts.append(f"bg finished {item[1]}")
                try:
                    while True:
                        items.append(inbox.get_nowait())
                except queue.Empty:
                    pass
                continue
            take(item)
            text = item["text"]
            if "DIE" in text:
                os._exit(3)
            m = re.search(r"SILENT:([\d.]+)", text)
            if m:
                if interrupted.wait(float(m.group(1))):
                    result(None, error=True)
                    return
            m = re.search(r"SLOW:([\d.]+)", text)
            if m:
                n[0] += 1
                tid = f"toolu_slow{n[0]}"
                emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": "Bash",
                                                                     "input": {"command": "# slow step"}}]}})
                end = time.time() + float(m.group(1))
                while time.time() < end:
                    if interrupted.is_set():
                        result(None, error=True)
                        return
                    sum(range(20000))
                emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}})
            m = re.search(r"BG:([\d.]+)(?::([\d.]+))?", text)
            if m:
                n[0] += 1
                tid = f"bgtask{n[0]}"
                with lock:
                    bg[tid] = True
                    tasks = [{"task_id": k} for k in bg]
                emit({"type": "system", "subtype": "background_tasks_changed", "tasks": tasks})
                threading.Thread(target=bg_done, args=(tid, float(m.group(1)), float(m.group(2) or 0)), daemon=True).start()
                parts.append(f"launched {tid}")
            else:
                parts.append(f"reply[{mode}] {text}")
            try:  # fold in whatever arrived while this turn ran
                while True:
                    items.append(inbox.get_nowait())
            except queue.Empty:
                pass
        text = "\n\n".join(parts)
        turn_text(text)
        result(text)

    def worker():
        while not stop.is_set():
            try:
                item = inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            run(item)

    threading.Thread(target=worker, daemon=True).start()
    for line in sys.stdin:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "control_request" and (obj.get("request") or {}).get("subtype") == "interrupt":
            interrupted.set()
            emit({"type": "control_response", "response": {"subtype": "success", "request_id": obj.get("request_id")}})
        elif obj.get("type") == "user":
            inbox.put({"uuid": obj.get("uuid"), "text": (obj.get("message") or {}).get("content") or ""})
    time.sleep(0.3)  # like claude: a moment to finish, then background tasks are stopped
    stop.set()
    with lock:
        left = list(bg)
    if left:
        emit({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
        for tid in left:
            emit({"type": "system", "subtype": "task_notification", "task_id": tid, "status": "stopped"})
    sys.exit(0)


if LIVE:
    live_main()

if "PREAMBLE" in prompt:
    out({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "Checking."}}})
if "USE_TOOL" in prompt:
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "# list the files\nls -la"}}]}})
    secs = float(os.environ.get("FAKE_CLAUDE_TOOL_SECONDS") or 0)
    if secs:  # a slow tool: nothing on stdout meanwhile, except heartbeats if asked
        import time
        # FAKE_CLAUDE_TOOL_MODE runs the tool as a child process, like a real Bash call: "busy" burns CPU,
        # "silent" just sleeps. Unset, the fake itself waits (no child at all).
        code = {"busy": f"import time\nend = time.time() + {secs}\nwhile time.time() < end: sum(range(10000))",
                "silent": f"import time; time.sleep({secs})"}.get(os.environ.get("FAKE_CLAUDE_TOOL_MODE", ""))
        child = subprocess.Popen([sys.executable, "-c", code]) if code else None
        beat = float(os.environ.get("FAKE_CLAUDE_HEARTBEAT") or 0)  # like claude's 30 s tool_progress heartbeat
        end, n = time.time() + secs, 0
        while time.time() < end:
            time.sleep(max(0.0, min(beat or secs, end - time.time())))
            if beat and time.time() < end:
                out({"type": "tool_progress", "tool_use_id": f"toolu_1-heartbeat-{n}", "tool_name": "Bash",
                     "parent_tool_use_id": "toolu_1", "heartbeat": True})
                n += 1
        if child:
            child.wait()
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}})

if os.environ.get("FAKE_CLAUDE_BG_SECONDS"):  # a background Bash job: the tool answers at once, the job keeps working
    import time
    bg = float(os.environ["FAKE_CLAUDE_BG_SECONDS"])
    out({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_bg", "name": "Bash", "input": {"command": "make", "run_in_background": True}}]}})
    job = subprocess.Popen([sys.executable, "-c", f"import time\nend = time.time() + {bg}\nwhile time.time() < end: sum(range(10000))"])
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_bg", "content": "started"}]}})
    job.wait()

text = f"reply[{mode}] {prompt}"
half = len(text) // 2
for piece in (text[:half], text[half:]):
    out({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": piece}}})
if os.environ.get("FAKE_CLAUDE_LINGER"):  # answered, then keeps running for background work, like claude -p
    import time
    if os.environ.get("FAKE_CLAUDE_LINGER_MID") != "1":  # MID: stopped between narration and the next tool call
        out({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}})
    end = time.time() + float(os.environ["FAKE_CLAUDE_LINGER"])
    while time.time() < end:
        sum(range(10000))
bg_after = float(os.environ.get("FAKE_CLAUDE_BG_AFTER") or 0)  # keeps running this long after its result, like
if bg_after and os.environ.get("FAKE_CLAUDE_BG_NOTASK") != "1":  # claude -p waiting on a background task
    out({"type": "system", "subtype": "background_tasks_changed", "tasks": [{"task_id": "bgtask1", "task_type": "local_agent"}]})
out({"type": "result", "is_error": False, "result": text, "usage": {"input_tokens": 3, "cache_read_input_tokens": 700000, "cache_creation_input_tokens": 0, "output_tokens": 5}})
if bg_after:
    import time
    time.sleep(bg_after)
    if os.environ.get("FAKE_CLAUDE_BG_NOTASK") != "1":  # UNFINISHED: claude stopped it itself (its own ceiling)
        status = "stopped" if os.environ.get("FAKE_CLAUDE_BG_UNFINISHED") == "1" else "completed"
        out({"type": "system", "subtype": "task_notification", "task_id": "bgtask1", "status": status})
    out({"type": "system", "subtype": "background_tasks_changed", "tasks": []})

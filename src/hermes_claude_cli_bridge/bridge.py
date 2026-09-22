"""Hermes -> Claude Code CLI bridge.

An OpenAI-compatible /v1/chat/completions server (stdlib only) that answers each
request by running the `claude` CLI as a subprocess.

Unlike a stateless shim, the bridge keeps one Claude Code session per Hermes
thread: the Hermes session id (sent by the hermes provider plugin in
`claude_bridge.session_id`) is mapped to a deterministic UUID. The first turn
uses `claude --session-id <uuid>`; later turns use `claude --resume <uuid>` and
send only the new user message, because Claude Code already holds the history.

Fixed flags on every call: -p, --output-format stream-json,
--dangerously-skip-permissions. Configurable: --add-dir, --append-system-prompt,
--autocompact, --effort, --model, cwd. The appended system prompt can come from a
flag/env string and/or a file (re-read on every request, so edits apply live; an
unreadable file is an error, never silently dropped).

The prompt is written to claude's stdin, not passed as an argument: Linux caps a
single argv string at 128 KB whatever ARG_MAX says, and a mid-thread transcript
or one pasted log goes past that.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
NAMESPACE = uuid.UUID("6f1d3a52-8c1e-4c5e-9a53-4d1f6f0c7b11")
MODELS = ["sonnet", "opus", "haiku"]

CFG = argparse.Namespace()  # replaced by main(); getattr(..., default) until then
_state_lock = threading.RLock()
_session_locks: dict[str, threading.Lock] = {}
_slots = threading.BoundedSemaphore(8)  # resized by main(): concurrent claude processes


class Busy(RuntimeError):
    """Too many turns in flight, or this thread is already running one -> HTTP 429."""


class ClientGone(RuntimeError):
    """The caller hung up mid-turn; claude was killed and nothing is left to reply to."""


# --------------------------------------------------------------------------- state

_state_cache: dict = {}
_state_stamp: object = None  # (mtime_ns, size) of the file the cache was read from


def _load_state() -> dict:
    """The state file, cached. Re-read only when it changed on disk, so /health and every turn
    cost one stat instead of parsing the whole file (it holds one entry per Hermes thread)."""
    global _state_cache, _state_stamp
    with _state_lock:
        try:
            st = Path(CFG.state_file).stat()
            stamp: object = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != _state_stamp:
            try:
                _state_cache = json.loads(Path(CFG.state_file).read_text())
            except (OSError, ValueError):
                _state_cache = {}
            _state_stamp = stamp
        return _state_cache


def _save_state(state: dict) -> None:
    global _state_cache, _state_stamp
    with _state_lock:
        p = Path(CFG.state_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(p)
        _state_cache = state
        try:
            st = p.stat()
            _state_stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            _state_stamp = None


def _session_lock(sid: str) -> threading.Lock:
    with _state_lock:
        return _session_locks.setdefault(sid, threading.Lock())


# ---------------------------------------------------------------------- housekeeping


def _forget_claude_session(sid: str) -> int:
    """Delete Claude Code's own transcript for a session. Claude never expires these itself, so a
    long-lived bridge otherwise keeps every thread it ever ran on disk forever."""
    n = 0
    for pat in (f"projects/*/{sid}.jsonl", f"projects/*/{sid}", f"todos/{sid}*"):
        for f in CLAUDE_HOME.glob(pat):
            try:
                shutil.rmtree(f) if f.is_dir() else f.unlink()
                n += 1
            except OSError:
                pass
    return n


def prune_sessions() -> list[str]:
    """Forget threads idle for longer than --session-ttl-days, here and in Claude Code."""
    days = getattr(CFG, "session_ttl_days", 0)
    if not days or days <= 0:
        return []
    cutoff = time.time() - days * 86400
    with _state_lock:
        state = _load_state()
        dead = [sid for sid, e in state.items()
                if isinstance(e, dict) and (e.get("updated") or e.get("created") or 0) < cutoff]
        if not dead:
            return []
        for sid in dead:
            state.pop(sid, None)
            _session_locks.pop(sid, None)
            _forget_claude_session(sid)
        _save_state(state)
    return dead


def _housekeeping_loop(every: float = 6 * 3600) -> None:
    while True:
        try:
            dead = prune_sessions()
            if dead:
                print(f"[bridge] pruned {len(dead)} session(s) idle > {CFG.session_ttl_days}d", file=sys.stderr)
        except Exception as e:  # never let housekeeping kill the server
            print(f"[bridge] prune failed: {e}", file=sys.stderr)
        time.sleep(every)


def session_uuid(hermes_session_id: str) -> str:
    """Stable UUID for a Hermes thread. A real UUID is used as-is."""
    try:
        return str(uuid.UUID(hermes_session_id))
    except ValueError:
        return str(uuid.uuid5(NAMESPACE, hermes_session_id))


# ---------------------------------------------------------------------- messages


_MEMORY_CTX = re.compile(r"\s*<memory-context>.*?</memory-context>\s*", re.DOTALL)


def strip_memory_context(text: str) -> str:
    """Drop the <memory-context> recall block Hermes appends to user messages. Claude Code usually has its own
    memory (plugins/hooks), and Hermes' copy is a truncated head/tail digest, so it mostly duplicates and adds noise."""
    if "<memory-context>" not in text or getattr(CFG, "keep_memory_context", False):
        return text
    return _MEMORY_CTX.sub("\n\n", text).strip()


def _text(content) -> str:
    if isinstance(content, str):
        return strip_memory_context(content)
    if isinstance(content, list):
        return "\n".join(
            strip_memory_context(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") in ("text", "input_text")
        )
    return ""


def latest_turn(messages: list[dict]) -> str:
    """User text since the last assistant message (what Claude has not seen yet)."""
    tail: list[str] = []
    for m in reversed(messages):
        if m.get("role") == "assistant":
            break
        if m.get("role") in ("user", "tool"):
            tail.append(_text(m.get("content")))
    return "\n\n".join(t for t in reversed(tail) if t.strip())


def transcript(messages: list[dict]) -> str:
    """Flatten history for a brand-new Claude session that joins a thread mid-way.
    Only the newest --max-transcript-chars of history are kept: a Hermes thread can hold thousands
    of messages, and replaying all of them into a fresh session is slow and pointless."""
    convo = [m for m in messages if m.get("role") in ("user", "assistant")]
    if len(convo) <= 1:
        return latest_turn(messages)
    budget = getattr(CFG, "max_transcript_chars", 0) or 10**9
    earlier: list[str] = []
    for m in reversed(convo[:-1]):
        line = f"[{m['role']}] {_text(m.get('content'))}"
        budget -= len(line) + 1
        if budget < 0:
            earlier.append("[...older messages omitted...]")
            break
        earlier.append(line)
    lines = ["Earlier conversation (for context):", *reversed(earlier), "",
             "Current message:", latest_turn(messages) or _text(convo[-1].get("content"))]
    return "\n".join(lines)


def hermes_system(messages: list[dict]) -> str:
    return "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer"))


_CTX_HEADING = "## Current Session Context"


def session_context(messages: list[dict]) -> str:
    """The messaging gateway's "Current Session Context" block (platform, channel, user) from Hermes'
    system prompt, without its Hermes-only tail (cron delivery options, connected platforms)."""
    text = hermes_system(messages)
    i = text.find(_CTX_HEADING)
    if i < 0:
        return ""
    block = text[i:]
    nxt = re.search(r"\n#{1,2} ", block[len(_CTX_HEADING):])
    if nxt:
        block = block[: len(_CTX_HEADING) + nxt.start()]
    for marker in ("**Connected Platforms", "**Delivery options for scheduled tasks"):
        j = block.find(marker)
        if j >= 0:
            block = block[:j]
    block = block.strip()
    return "" if block == _CTX_HEADING else "Hermes chat context, from the messaging gateway that relays this conversation:\n\n" + block


# ------------------------------------------------------------------------ claude


def build_cmd(*, model: str, session: str, resume: bool, opts: dict, persist: bool) -> list[str]:
    cmd = [CFG.claude_bin, "-p", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    if persist:
        cmd += ["--resume", session] if resume else ["--session-id", session]
    else:
        cmd += ["--no-session-persistence"]
    for d in opts["add_dirs"]:
        cmd += ["--add-dir", d]
    if opts["append_system_prompt"]:
        cmd += ["--append-system-prompt", opts["append_system_prompt"]]
    if opts["effort"]:
        cmd += ["--effort", opts["effort"]]
    if opts["autocompact"]:
        cmd += ["--autocompact", str(opts["autocompact"])]
    cmd += CFG.extra_args
    return cmd


_HEADLINE_KEYS = {
    "Bash": ("description", "command"), "Read": ("file_path",), "Write": ("file_path",), "Edit": ("file_path",),
    "NotebookEdit": ("notebook_path",), "Grep": ("pattern",), "Glob": ("pattern",), "WebFetch": ("url",),
    "WebSearch": ("query",), "Agent": ("description",), "Task": ("description",), "ToolSearch": ("query",),
    "Skill": ("skill",),
}


def tool_headline(name: str, inp: dict) -> str:
    """One short line for a Claude tool call, e.g. 'Bash: ls -la ~/projects'."""
    shown = name
    if name.startswith("mcp__"):
        parts = name.split("__")
        shown = f"{parts[-1]} ({parts[1].split('_')[-1]})" if len(parts) >= 3 else name
    inp = inp if isinstance(inp, dict) else {}
    detail = ""
    for k in _HEADLINE_KEYS.get(name, ()):
        if isinstance(inp.get(k), str) and inp[k].strip():
            detail = inp[k]
            break
    if not detail and name not in _HEADLINE_KEYS:
        detail = next((v for v in inp.values() if isinstance(v, str) and v.strip()), "")
    if name == "Bash" and detail:
        lines = [l.strip() for l in detail.splitlines() if l.strip()]
        # a leading "# why" comment (the house style) says more than the command; else just the first line
        note = lines[0].lstrip("#").strip() if lines and lines[0].startswith("#") else ""
        detail = note if len(note) > 3 else next((l for l in lines if not l.startswith("#")), lines[0] if lines else "")
    detail = " ".join(detail.replace(str(Path.home()), "~").split())
    if len(detail) > 100:
        detail = detail[:99] + "…"
    return f"{shown}: {detail}" if detail else shown


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill claude AND everything it spawned. claude runs bash/subagents in its own process group;
    they inherit its stdout, so killing only the parent can leave our read loop blocked forever."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:  # pragma: no cover - non-POSIX
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def client_is_gone(conn) -> bool:
    """True once the caller has closed its end. A half-closed socket reads as readable-with-no-bytes;
    real pipelined data is left untouched by MSG_PEEK."""
    try:
        if not select.select([conn], [], [], 0)[0]:
            return False
        return conn.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except (OSError, ValueError):
        return True  # socket already torn down


def run_claude(cmd: list[str], prompt: str, cwd: str, on_text, on_reasoning=None, on_tool=None, client_gone=None):
    """Run one claude turn. Returns (result_event, stderr_text). Streams via on_text; on_tool(headline, is_subagent).
    Waits for a concurrency slot first, so a burst of Hermes threads cannot fork unbounded claude processes."""
    if not _slots.acquire(timeout=getattr(CFG, "queue_timeout", 120)):
        raise Busy(f"bridge is at capacity ({CFG.max_concurrency} concurrent turns); try again shortly")
    try:
        return _run_claude(cmd, prompt, cwd, on_text, on_reasoning, on_tool, client_gone)
    finally:
        _slots.release()


def _run_claude(cmd: list[str], prompt: str, cwd: str, on_text, on_reasoning=None, on_tool=None, client_gone=None):
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True,
    )
    # the prompt goes on stdin (no 128 KB argv limit); feed it from a thread so a child that
    # does not drain stdin cannot deadlock us on a full pipe buffer
    def _feed():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    stderr_buf: list[str] = []
    reader = threading.Thread(target=lambda: stderr_buf.append(proc.stderr.read() or ""), daemon=True)
    reader.start()
    timer = threading.Timer(CFG.timeout, _kill_tree, args=(proc,))
    timer.start()
    # A dead client is otherwise only noticed when we next write to it, and a turn can be silent for
    # minutes during one long tool call. Hermes' busy_input_mode=interrupt aborts the request exactly
    # like that, so without this poll the abandoned claude keeps running and keeps the session lock.
    interval = getattr(CFG, "client_check_interval", 5.0)
    stop_watch = threading.Event()
    abandoned: list[bool] = []

    def _watch_client():
        while not stop_watch.wait(interval):
            try:
                if client_gone():
                    abandoned.append(True)
                    _kill_tree(proc)
                    return
            except Exception:
                return

    watcher = None
    if client_gone is not None and interval and interval > 0:
        watcher = threading.Thread(target=_watch_client, daemon=True)
        watcher.start()
    result = None
    seen_tools: set[str] = set()
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "stream_event":
                d = (ev.get("event") or {}).get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    on_text(d["text"])
                elif d.get("type") == "thinking_delta" and d.get("thinking") and on_reasoning:
                    on_reasoning(d["thinking"])
            elif ev.get("type") == "assistant" and on_tool:
                for blk in (ev.get("message") or {}).get("content") or []:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("id") not in seen_tools:
                        seen_tools.add(blk.get("id"))
                        on_tool(tool_headline(blk.get("name", "?"), blk.get("input")), bool(ev.get("parent_tool_use_id")))
            elif ev.get("type") == "result":
                result = ev
        proc.wait()
    except BaseException:
        _kill_tree(proc)  # client went away
        raise
    finally:
        timer.cancel()
        stop_watch.set()
        if watcher is not None:
            watcher.join(2)
        # the error text drives the resume/session-id self-heal below, so wait for it rather
        # than racing the reader thread and treating a lost message as "no error"
        reader.join(5)
    if abandoned:
        raise ClientGone("caller disconnected; claude was stopped")
    return result, "".join(stderr_buf)


_prompt_cache: dict[str, tuple[tuple[int, int], str]] = {}


def _read_prompt_file(path: str) -> str:
    """Prompt files are live-editable, but re-reading them on every request is wasted IO: cache on (mtime, size)."""
    if not path:
        return ""
    p = Path(path).expanduser()
    try:
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        hit = _prompt_cache.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
        text = p.read_text().strip()
    except OSError as e:
        raise RuntimeError(f"cannot read --append-system-prompt-file {path}: {e}") from e
    _prompt_cache[path] = (stamp, text)
    return text


def turn(body: dict, on_text, on_reasoning=None, on_tool=None, client_gone=None):
    """Run a chat turn with session bookkeeping. Returns (result, session_uuid)."""
    messages = body.get("messages") or []
    ext = body.get("claude_bridge") or {}
    model = body.get("model") or CFG.model
    if model not in MODELS and not model.startswith("claude-"):
        model = CFG.model  # unknown hermes-side name -> bridge default
    opts = {
        "add_dirs": list(dict.fromkeys(CFG.add_dir + list(ext.get("add_dirs") or []))),
        "append_system_prompt": "\n\n".join(
            p for p in (
                CFG.append_system_prompt,
                *(_read_prompt_file(f) for f in CFG.append_system_prompt_file),
                ext.get("append_system_prompt"),
                hermes_system(messages) if CFG.forward_system else (session_context(messages) if CFG.session_context else ""),
            ) if p
        ),
        "autocompact": ext.get("autocompact") or CFG.autocompact,
        "effort": ext.get("effort") or CFG.effort,
    }
    hsid = ext.get("session_id")
    cwd = ext.get("cwd") or CFG.cwd
    Path(cwd).mkdir(parents=True, exist_ok=True)

    if not hsid:  # no thread identity -> one-shot, nothing persisted
        sid = str(uuid.uuid4())
        res, err = run_claude(build_cmd(model=model, session=sid, resume=False, opts=opts, persist=False),
                              transcript(messages), cwd, on_text, on_reasoning, on_tool, client_gone)
        return _check(res, err), None

    sid = session_uuid(hsid)
    # one turn at a time per thread (Claude sessions are not concurrent-safe), but never block
    # for the full --timeout: a queued retry from Hermes would just pile up threads and processes
    lock = _session_lock(sid)
    if not lock.acquire(timeout=getattr(CFG, "queue_timeout", 120)):
        raise Busy("this conversation is still processing the previous message")
    try:
        state = _load_state()
        known = sid in state
        # the state file is a hint; error text below self-heals a wrong guess
        for attempt in range(2):
            resume = known
            prompt = latest_turn(messages) if resume else transcript(messages)
            streamed: list[str] = []
            res, err = run_claude(
                build_cmd(model=model, session=sid, resume=resume, opts=opts, persist=True),
                prompt, state.get(sid, {}).get("cwd", cwd),
                lambda t: (streamed.append(t), on_text(t)), on_reasoning, on_tool, client_gone)
            failed = res is None or res.get("is_error")
            blob = (err + json.dumps(res or {})).lower()
            if failed and not streamed and attempt == 0:
                if resume and "no conversation found" in blob:
                    known = False
                    continue
                if not resume and "already in use" in blob:
                    known = True
                    continue
            break
        out = _check(res, err)
        state = _load_state()
        entry = state.setdefault(sid, {"hermes_session_id": hsid, "cwd": cwd, "created": int(time.time()), "turns": 0})
        entry["turns"] += 1
        entry["updated"] = int(time.time())
        _save_state(state)
        return out, sid
    finally:
        lock.release()


def _check(res, err):
    if res is None:
        raise RuntimeError(f"claude produced no result: {err.strip()[-500:]}")
    if res.get("is_error"):
        raise RuntimeError(f"claude error: {res.get('result') or err.strip()[-500:]}")
    return res


# -------------------------------------------------------------------------- http


def _history_tokens(body: dict, raw_len: int | None = None) -> int:
    """Rough size (4 chars/token) of the request Hermes made: its messages plus tool schemas. This is what Hermes'
    context-compression logic should be measuring. Claude Code keeps its own session, so Hermes' history is
    never resent and its size says nothing about Claude's context.
    `raw_len` is the request body we already received; using it avoids re-serializing a multi-megabyte
    history to JSON on every turn, immediately after parsing those same bytes."""
    msgs = body.get("messages") or []
    if raw_len is None:
        raw_len = sum(len(json.dumps(m.get("content"), ensure_ascii=False))
                      + len(json.dumps(m.get("tool_calls") or [], ensure_ascii=False))
                      for m in msgs if isinstance(m, dict)) + len(json.dumps(body.get("tools") or [], ensure_ascii=False))
    return raw_len // 4 + 4 * len(msgs)


def _usage(res: dict, body: dict | None = None, raw_len: int | None = None) -> dict:
    """OpenAI-style usage. `result.usage` from `claude -p` is the SUM over every internal model call of the run;
    each call of an agentic loop re-reads the whole cached context, so a 3-message chat that used a few tools
    reports hundreds of thousands of prompt tokens. Hermes trusts that number, thinks the conversation is
    enormous, and tries (uselessly) to compress it, so by default report the size of Hermes' own history instead.
    The raw Claude numbers are still returned as `claude_usage`; `--usage claude` reports them as usage."""
    u = res.get("usage") or {}
    claude_in = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
    c = u.get("output_tokens") or 0
    p = claude_in if body is None or getattr(CFG, "usage", "history") == "claude" else _history_tokens(body, raw_len)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[bridge] " + fmt % args + "\n")

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _client_gone(self) -> bool:
        return client_is_gone(self.connection)

    def _refuse(self) -> bool:
        """Reject anything that is not a server-side JSON API call. The bridge runs claude with
        --dangerously-skip-permissions, and a web page the user visits can POST to 127.0.0.1: a
        text/plain POST is a CORS "simple request", so it is sent with no preflight. The attacker
        cannot read the reply, but the commands have already run. Real API clients always send
        Content-Type: application/json and never an Origin header."""
        if self.headers.get("Origin"):
            self._json(403, {"error": {"message": "cross-origin requests are not accepted", "type": "claude_bridge_error"}})
            return True
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json(415, {"error": {"message": "Content-Type must be application/json", "type": "claude_bridge_error"}})
            return True
        token = getattr(CFG, "auth_token", "")
        if token and self.headers.get("Authorization", "") != "Bearer " + token:
            self._json(401, {"error": {"message": "invalid or missing bearer token", "type": "claude_bridge_error"}})
            return True
        return False

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz"):
            return self._json(200, {"ok": True, "sessions": len(_load_state()),
                                    "session_ttl_days": getattr(CFG, "session_ttl_days", 0)})
        if self.path.rstrip("/") == "/v1/models":
            return self._json(200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "claude-code"} for m in MODELS]})
        if self.path.startswith("/v1/models/") and self.path.rsplit("/", 1)[1] in MODELS:
            mid = self.path.rsplit("/", 1)[1]
            return self._json(200, {"id": mid, "object": "model", "owned_by": "claude-code"})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > CFG.max_body_mb * 1024 * 1024:
            left = min(length, 256 * 1024 * 1024)  # drain and discard so the client gets the 413, not a reset
            while left > 0:
                if not self.rfile.read(min(left, 1 << 16)):
                    break
                left -= 1 << 16
            self.close_connection = True
            return self._json(413, {"error": {"message": f"body exceeds --max-body-mb ({CFG.max_body_mb})"}})
        raw = self.rfile.read(length)  # always drain (keep-alive)
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "not found"}})
        if self._refuse():
            return
        try:
            body = json.loads(raw)
        except ValueError:
            return self._json(400, {"error": {"message": "invalid JSON"}})
        cid, created, model = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time()), body.get("model") or CFG.model
        if body.get("stream"):
            return self._stream(body, cid, created, model, len(raw))
        parts: list[str] = []
        try:
            res, sid = turn(body, parts.append, client_gone=self._client_gone)
        except ClientGone:
            return  # nobody left to answer
        except Busy as e:
            return self._json(429, {"error": {"message": str(e), "type": "claude_bridge_busy"}})
        except Exception as e:
            return self._json(502, {"error": {"message": str(e), "type": "claude_bridge_error"}})
        text = res.get("result") or "".join(parts)
        self._json(200, {
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": _usage(res, body, len(raw)), "claude_usage": res.get("usage"), "claude_session_id": sid,
        })

    def _sse(self, obj):
        self.wfile.write(b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n\n")
        self.wfile.flush()

    def _stream(self, body, cid, created, model, raw_len=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def chunk(delta, finish=None, **extra):
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}

        try:
            self._sse(chunk({"role": "assistant", "content": ""}))
            sent = []
            line_start = [True]  # keep tool headlines on their own line

            def on_text(t):
                sent.append(t)
                line_start[0] = t.endswith("\n")
                self._sse(chunk({"content": t}))

            def on_tool(headline, sub):
                if CFG.tool_events == "reasoning":
                    self._sse(chunk({"reasoning_content": f"🔧 {headline}\n"}))
                elif CFG.tool_events == "content":
                    self._sse(chunk({"content": ("" if line_start[0] else "\n") + ("  ↳ " if sub else "") + f"🔧 {headline}\n"}))
                    line_start[0] = True

            res, sid = turn(body, on_text, lambda t: self._sse(chunk({"reasoning_content": t})),
                            on_tool if CFG.tool_events != "off" else None, client_gone=self._client_gone)
            if not sent and res.get("result"):
                self._sse(chunk({"content": res["result"]}))
            self._sse(chunk({}, "stop", usage=_usage(res, body, raw_len), claude_usage=res.get("usage")))
        except (BrokenPipeError, ConnectionResetError, ClientGone):
            return
        except Busy as e:
            try:
                self._sse(chunk({"content": f"\n[claude-bridge busy: {e}]"}, "stop"))
            except OSError:
                return
        except Exception as e:
            try:
                self._sse(chunk({"content": f"\n[claude-bridge error: {e}]"}, "stop"))
            except OSError:
                return
        try:
            self._sse(b"[DONE]")
        except OSError:
            pass


# -------------------------------------------------------------------------- main


def parse_args(argv=None) -> argparse.Namespace:
    env = os.environ.get
    ap = argparse.ArgumentParser(prog="hermes-claude-cli-bridge serve", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=env("CLAUDE_BRIDGE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(env("CLAUDE_BRIDGE_PORT", "9181")))
    ap.add_argument("--claude-bin", default=env("CLAUDE_BIN", "claude"))
    ap.add_argument("--model", default=env("CLAUDE_BRIDGE_MODEL", "sonnet"), help="default model alias")
    ap.add_argument("--add-dir", action="append", default=[d for d in env("CLAUDE_BRIDGE_ADD_DIRS", "").split(os.pathsep) if d],
                    help="passed as --add-dir (repeatable; env CLAUDE_BRIDGE_ADD_DIRS, os.pathsep separated)")
    ap.add_argument("--append-system-prompt", default=env("CLAUDE_BRIDGE_APPEND_SYSTEM_PROMPT", ""))
    ap.add_argument("--append-system-prompt-file", action="append",
                    default=[f for f in env("CLAUDE_BRIDGE_APPEND_SYSTEM_PROMPT_FILE", "").split(os.pathsep) if f],
                    help="file appended to the system prompt, in order (repeatable; env os.pathsep-separated); re-read every request")
    ap.add_argument("--effort", default=env("CLAUDE_BRIDGE_EFFORT", "medium"), choices=["", "low", "medium", "high", "xhigh", "max"],
                    help="passed as --effort (default medium; '' = claude default)")
    ap.add_argument("--autocompact", default=env("CLAUDE_BRIDGE_AUTOCOMPACT", "auto"),
                    help="Claude Code's own context compaction: 'auto' (default) or a threshold such as 200k (100k-1M); "
                         "'' passes nothing. Hermes' compression cannot shrink Claude's session, so this is what keeps long threads working")
    ap.add_argument("--keep-memory-context", action="store_true", default=env("CLAUDE_BRIDGE_KEEP_MEMORY_CONTEXT") == "1",
                    help="keep the <memory-context> recall block Hermes appends to user messages (default: strip it)")
    ap.add_argument("--no-session-context", dest="session_context", action="store_false",
                    default=env("CLAUDE_BRIDGE_SESSION_CONTEXT", "1") != "0",
                    help="do not pass the gateway's session context (platform/channel/user) to Claude")
    ap.add_argument("--forward-system", action="store_true", default=env("CLAUDE_BRIDGE_FORWARD_SYSTEM") == "1",
                    help="also append Hermes' own system prompt (large, mentions Hermes-only tools)")
    ap.add_argument("--cwd", default=env("CLAUDE_BRIDGE_CWD", str(HERMES_HOME / "claude-bridge" / "workspace")),
                    help="working dir for claude; sessions are stored per-cwd so keep it stable")
    ap.add_argument("--state-file", default=env("CLAUDE_BRIDGE_STATE", str(HERMES_HOME / "claude-bridge" / "sessions.json")))
    ap.add_argument("--timeout", type=float, default=float(env("CLAUDE_BRIDGE_TIMEOUT", "900")))
    ap.add_argument("--session-ttl-days", type=float, default=float(env("CLAUDE_BRIDGE_SESSION_TTL_DAYS", "7")),
                    help="forget a thread after this many days without a message, and delete Claude Code's own "
                         "transcript for it (default 7; 0 disables housekeeping and keeps every session forever)")
    ap.add_argument("--max-concurrency", type=int, default=int(env("CLAUDE_BRIDGE_MAX_CONCURRENCY", "8")),
                    help="most claude processes to run at once; further turns queue (default 8)")
    ap.add_argument("--queue-timeout", type=float, default=float(env("CLAUDE_BRIDGE_QUEUE_TIMEOUT", "120")),
                    help="how long a turn waits for a free slot, or for the previous turn of the same thread, "
                         "before returning HTTP 429 (default 120s)")
    ap.add_argument("--client-check-interval", type=float, default=float(env("CLAUDE_BRIDGE_CLIENT_CHECK_INTERVAL", "5")),
                    help="how often (seconds) to check whether the caller is still connected, so an abandoned turn "
                         "is stopped during a long silent tool call instead of at the next write (default 5; 0 disables)")
    ap.add_argument("--max-body-mb", type=float, default=float(env("CLAUDE_BRIDGE_MAX_BODY_MB", "32")),
                    help="reject request bodies larger than this (default 32)")
    ap.add_argument("--max-transcript-chars", type=int, default=int(env("CLAUDE_BRIDGE_MAX_TRANSCRIPT_CHARS", "200000")),
                    help="cap on the one-time history replay when a thread joins mid-conversation (default 200000; 0 = no cap)")
    ap.add_argument("--auth-token", default=env("CLAUDE_BRIDGE_AUTH_TOKEN", ""),
                    help="if set, require 'Authorization: Bearer <token>'. Not needed against browser-based attacks "
                         "(Origin and Content-Type are checked already); use it when other local users share the host")
    ap.add_argument("--usage", default=env("CLAUDE_BRIDGE_USAGE", "history"), choices=["history", "claude"],
                    help="what to report as prompt_tokens: the size of Hermes' own history (default; keeps Hermes' context "
                         "compression sane) or Claude's raw cumulative usage")
    ap.add_argument("--tool-events", default=env("CLAUDE_BRIDGE_TOOL_EVENTS", "content"), choices=["content", "reasoning", "off"],
                    help="show Claude's tool calls to Hermes as one-line headlines: in the reply (content), in reasoning, or not at all")
    ap.add_argument("--extra-args", default=env("CLAUDE_BRIDGE_EXTRA_ARGS", ""), help="raw extra claude flags (shlex-split)")
    a = ap.parse_args(argv)
    import shlex
    a.extra_args = shlex.split(a.extra_args)
    a.add_dir = [str(Path(d).expanduser()) for d in a.add_dir]
    return a


def main(argv=None):
    global CFG, _slots
    CFG = parse_args(argv)
    _slots = threading.BoundedSemaphore(max(1, CFG.max_concurrency))
    srv = ThreadingHTTPServer((CFG.host, CFG.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=_housekeeping_loop, daemon=True).start()
    print(f"[bridge] listening on http://{CFG.host}:{CFG.port}/v1  (claude={CFG.claude_bin} add_dirs={CFG.add_dir} "
          f"effort={CFG.effort or 'default'} autocompact={CFG.autocompact or 'default'} "
          f"prompt_files={CFG.append_system_prompt_file or '-'} ttl_days={CFG.session_ttl_days or 'off'} "
          f"max_concurrency={CFG.max_concurrency} client_check={CFG.client_check_interval or 'off'}s "
          f"auth={'token' if CFG.auth_token else 'none'})", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

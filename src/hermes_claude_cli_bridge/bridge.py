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
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
NAMESPACE = uuid.UUID("6f1d3a52-8c1e-4c5e-9a53-4d1f6f0c7b11")
MODELS = ["sonnet", "opus", "haiku"]

CFG: argparse.Namespace  # set in main()
_state_lock = threading.Lock()
_session_locks: dict[str, threading.Lock] = {}


# --------------------------------------------------------------------------- state


def _load_state() -> dict:
    try:
        return json.loads(Path(CFG.state_file).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    p = Path(CFG.state_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)


def _session_lock(sid: str) -> threading.Lock:
    with _state_lock:
        return _session_locks.setdefault(sid, threading.Lock())


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
    if "<memory-context>" not in text or getattr(globals().get("CFG"), "keep_memory_context", False):
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
    """Flatten history for a brand-new Claude session that joins a thread mid-way."""
    convo = [m for m in messages if m.get("role") in ("user", "assistant")]
    if len(convo) <= 1:
        return latest_turn(messages)
    lines = ["Earlier conversation (for context):"]
    for m in convo[:-1]:
        lines.append(f"[{m['role']}] {_text(m.get('content'))}")
    lines += ["", "Current message:", latest_turn(messages) or _text(convo[-1].get("content"))]
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


def run_claude(cmd: list[str], prompt: str, cwd: str, on_text, on_reasoning=None, on_tool=None):
    """Run one claude turn. Returns (result_event, stderr_text). Streams via on_text; on_tool(headline, is_subagent)."""
    proc = subprocess.Popen(
        cmd + [prompt], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    stderr_buf: list[str] = []
    threading.Thread(target=lambda: stderr_buf.append(proc.stderr.read()), daemon=True).start()
    timer = threading.Timer(CFG.timeout, proc.kill)
    timer.start()
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
        proc.kill()  # client went away
        raise
    finally:
        timer.cancel()
    return result, "".join(stderr_buf)


def _read_prompt_file(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).expanduser().read_text().strip()
    except OSError as e:
        raise RuntimeError(f"cannot read --append-system-prompt-file {path}: {e}") from e


def turn(body: dict, on_text, on_reasoning=None, on_tool=None):
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
                              transcript(messages), cwd, on_text, on_reasoning, on_tool)
        return _check(res, err), None

    sid = session_uuid(hsid)
    with _session_lock(sid):
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
                lambda t: (streamed.append(t), on_text(t)), on_reasoning, on_tool)
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


def _check(res, err):
    if res is None:
        raise RuntimeError(f"claude produced no result: {err.strip()[-500:]}")
    if res.get("is_error"):
        raise RuntimeError(f"claude error: {res.get('result') or err.strip()[-500:]}")
    return res


# -------------------------------------------------------------------------- http


def _usage(res: dict) -> dict:
    u = res.get("usage") or {}
    p = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
    c = u.get("output_tokens") or 0
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

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz"):
            return self._json(200, {"ok": True, "sessions": len(_load_state())})
        if self.path.rstrip("/") == "/v1/models":
            return self._json(200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "claude-code"} for m in MODELS]})
        if self.path.startswith("/v1/models/") and self.path.rsplit("/", 1)[1] in MODELS:
            mid = self.path.rsplit("/", 1)[1]
            return self._json(200, {"id": mid, "object": "model", "owned_by": "claude-code"})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))  # always drain (keep-alive)
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "not found"}})
        try:
            body = json.loads(raw)
        except ValueError:
            return self._json(400, {"error": {"message": "invalid JSON"}})
        cid, created, model = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time()), body.get("model") or CFG.model
        if body.get("stream"):
            return self._stream(body, cid, created, model)
        parts: list[str] = []
        try:
            res, sid = turn(body, parts.append)
        except Exception as e:
            return self._json(502, {"error": {"message": str(e), "type": "claude_bridge_error"}})
        text = res.get("result") or "".join(parts)
        self._json(200, {
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": _usage(res), "claude_session_id": sid,
        })

    def _sse(self, obj):
        self.wfile.write(b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n\n")
        self.wfile.flush()

    def _stream(self, body, cid, created, model):
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
                            on_tool if CFG.tool_events != "off" else None)
            if not sent and res.get("result"):
                self._sse(chunk({"content": res["result"]}))
            self._sse(chunk({}, "stop", usage=_usage(res)))
        except (BrokenPipeError, ConnectionResetError):
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
    ap.add_argument("--autocompact", default=env("CLAUDE_BRIDGE_AUTOCOMPACT", ""), help="'auto' or 100k-1M tokens")
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
    ap.add_argument("--tool-events", default=env("CLAUDE_BRIDGE_TOOL_EVENTS", "content"), choices=["content", "reasoning", "off"],
                    help="show Claude's tool calls to Hermes as one-line headlines: in the reply (content), in reasoning, or not at all")
    ap.add_argument("--extra-args", default=env("CLAUDE_BRIDGE_EXTRA_ARGS", ""), help="raw extra claude flags (shlex-split)")
    a = ap.parse_args(argv)
    import shlex
    a.extra_args = shlex.split(a.extra_args)
    a.add_dir = [str(Path(d).expanduser()) for d in a.add_dir]
    return a


def main(argv=None):
    global CFG
    CFG = parse_args(argv)
    srv = ThreadingHTTPServer((CFG.host, CFG.port), Handler)
    srv.daemon_threads = True
    print(f"[bridge] listening on http://{CFG.host}:{CFG.port}/v1  (claude={CFG.claude_bin} add_dirs={CFG.add_dir} "
          f"effort={CFG.effort or 'default'} autocompact={CFG.autocompact or 'default'} "
          f"prompt_files={CFG.append_system_prompt_file or '-'})", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

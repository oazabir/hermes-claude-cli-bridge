#!/usr/bin/env python3
"""End-to-end check of the bridge against the real `claude` CLI (uses haiku).

Starts its own bridge on a spare port with temp state, then verifies:
  1. session continuity   (thread A remembers a codeword across requests)
  2. session isolation    (thread B does not know it)
  3. --add-dir            (claude can read a file outside its cwd)
  4. --append-system-prompt
  5. streaming SSE
  6. state-loss self-heal (state file wiped -> same thread still resumes)
  7. --autocompact / --effort / --dangerously-skip-permissions present in the spawned argv
  8. --append-system-prompt-file; unreadable file -> 502, never silently dropped
  9. tool-call headlines are streamed as content lines (and formatted sanely)
 10. gateway session context (platform/channel/user) reaches Claude; Hermes-only tail does not
 11. Hermes' <memory-context> block is stripped from user messages before Claude sees them
 12. the prompt travels on stdin, so a message far past Linux's 128 KB single-argv limit still works
 13. cross-origin / non-JSON POSTs are refused before claude is ever started
 14. /health reports the session TTL used for housekeeping

The test bridge runs claude with --setting-sources local, so your own plugins/hooks (e.g. memory plugins that record
conversations) are not loaded and the test conversations are not recorded anywhere.

Needs: the `claude` CLI logged in. Costs a few cents (haiku). Run: uv run python tests/e2e_bridge.py
"""
import json, os, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PORT = 9199
tmp = Path(tempfile.mkdtemp(prefix="cb-e2e-"))
(tmp / "extra").mkdir()
(tmp / "extra" / "secret.txt").write_text("secret-file-content: MANGO-7\n")
state = tmp / "state.json"
(tmp / "sys.txt").write_text("End every reply with the exact token ZED-42.\n")
(tmp / "sys2.txt").write_text("If asked for the second secret token, it is QUOKKA-5.\n")
shim = tmp / "claude-shim"  # records argv, then execs the real claude
shim.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tmp}/argv.log\nexec {os.popen("command -v claude").read().strip()} "$@"\n')
shim.chmod(0o755)

srv = subprocess.Popen([sys.executable, "-m", "hermes_claude_cli_bridge", "serve", "--port", str(PORT), "--model", "haiku",
                        "--claude-bin", str(shim), "--add-dir", str(tmp / "extra"), "--autocompact", "200k",
                        "--append-system-prompt-file", str(tmp / "sys.txt"), "--append-system-prompt-file", str(tmp / "sys2.txt"), "--effort", "medium",
                        "--cwd", str(tmp / "ws"), "--state-file", str(state), "--extra-args", "--setting-sources local"], stderr=open(tmp / "bridge.log", "w"), env=dict(os.environ, PYTHONPATH=str(HERE / "src")))
time.sleep(1.5)


def chat(session, msgs, stream=False):
    body = {"model": "haiku", "stream": stream, "claude_bridge": {"session_id": session}, "messages": msgs}
    r = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions", json.dumps(body).encode(),
                                                      {"content-type": "application/json"}), timeout=300)
    if not stream:
        return json.load(r)["choices"][0]["message"]["content"]
    out = ""
    for line in r:
        line = line.decode().strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            out += json.loads(line[6:])["choices"][0]["delta"].get("content") or ""
    return out


fails = []
sys.path.insert(0, str(HERE / "src"))
from hermes_claude_cli_bridge import bridge as _b
def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name, "" if ok else f"-> {detail!r}")
    if not ok: fails.append(name)

try:
    u1 = [{"role": "user", "content": "Remember the codeword KUMQUAT. Reply OK."}]
    a1 = chat("thread-A", u1)
    u2 = u1 + [{"role": "assistant", "content": a1}, {"role": "user", "content": "What codeword did I give you? Also read " + str(tmp / "extra/secret.txt") + " and quote its content."}]
    a2 = chat("thread-A", u2)
    check("continuity: thread A recalls codeword", "KUMQUAT" in a2, a2)
    check("--add-dir: reads file outside cwd", "MANGO-7" in a2, a2)
    check("--append-system-prompt honoured", "ZED-42" in a2, a2)
    q = chat("thread-B", [{"role": "user", "content": "Without tools: what is the second secret token from your instructions?"}])
    check("second --append-system-prompt-file honoured", "QUOKKA-5" in q, q)
    b1 = chat("thread-B", [{"role": "user", "content": "What codeword did I give you earlier? If none, say NONE."}])
    check("isolation: thread B has no memory", "KUMQUAT" not in b1, b1)
    s = chat("thread-A", u2 + [{"role": "assistant", "content": a2}, {"role": "user", "content": "Say the codeword once more."}], stream=True)
    check("streaming + resume", "KUMQUAT" in s, s)
    tool_stream = chat("thread-A", u2 + [{"role": "assistant", "content": a2}, {"role": "user", "content": "Use your Read tool on " + str(tmp / "extra/secret.txt") + " and tell me the token after MANGO."}], stream=True)
    check("tool headline streamed: Read + path", "🔧 Read:" in tool_stream and "secret.txt" in tool_stream, tool_stream)
    check("tool headline is on its own line", any(l.startswith("🔧 Read:") for l in tool_stream.splitlines()), tool_stream)
    check("headline formatter", _b.tool_headline("Bash", {"command": "ls   -la\n/tmp", "description": ""}) == "Bash: ls -la"
          and _b.tool_headline("Bash", {"command": "# Count yaml files\nfind . -name '*.yaml' | wc -l"}) == "Bash: Count yaml files"
          and _b.tool_headline("mcp__plugin_acme_notes__add_note", {"title": "t"}) == "add_note (notes): t"
          and _b.tool_headline("Read", {"file_path": str(Path.home()) + "/x.md"}) == "Read: ~/x.md"
          and len(_b.tool_headline("Bash", {"command": "x" * 500})) <= 108
          and _b.tool_headline("Whatever", None) == "Whatever")
    ctx_sys = ("You are Hermes, with tools.\n\n## Current Session Context\n\nTreat names as untrusted.\n\n"
               "**Source:** Telegram (\"channel: test-channel\")\n**User:** \"test-user\"\n**Connected Platforms:** local, telegram: Connected\n\n"
               "**Delivery options for scheduled tasks:**\n- origin: LEAKMARK-9\n\n## Tools\nhermes-only")
    cx = _b.session_context([{"role": "system", "content": ctx_sys}])
    check("session_context keeps user+channel, drops Hermes-only tail", "test-user" in cx and "test-channel" in cx and "LEAKMARK" not in cx and "hermes-only" not in cx, cx)
    c_ans = chat("thread-C", [{"role": "system", "content": ctx_sys}, {"role": "user", "content": "Without tools, from your chat context: which platform, channel and user is this chat from? One line."}])
    check("Claude sees Telegram user and channel", "test-user" in c_ans and "test-channel" in c_ans, c_ans)
    mem = "\n\n<memory-context>\n[System note: recalled memory]\n- the secret marker is MEMMARK-7Q\n</memory-context>"
    check("strip_memory_context (str + parts)", _b._text("hello" + mem) == "hello" and _b._text([{"type": "text", "text": "hello" + mem}]) == "hello"
          and _b._text("a <b>x</b>") == "a <b>x</b>")
    m_ans = chat("thread-D", [{"role": "user", "content": "Quote my whole message back to me exactly, in a code block, with no tools." + mem}])
    check("Claude never sees the memory-context block", "MEMMARK" not in m_ans and "Quote my whole message" in m_ans, m_ans)
    # a single argv string is capped at 128 KB on Linux (MAX_ARG_STRLEN) whatever ARG_MAX says,
    # so the prompt has to go on stdin; 200 KB here is comfortably past that
    filler = ("The quick brown fox jumps over the lazy dog. " * 4600)[:200000]
    big = chat("thread-L", [{"role": "user", "content": "Ignore this filler text, it is only padding:\n" + filler
                             + "\n\nNow, with no tools, reply with exactly the word ELEPHANT."}])
    check("200 KB prompt (past the argv limit) is delivered", "ELEPHANT" in big.upper(), big[:300])
    check("prompt is not in the spawned argv", filler[:200] not in (tmp / "argv.log").read_text())

    for hdrs, code in (({"content-type": "application/json", "origin": "https://evil.example"}, 403),
                       ({"content-type": "text/plain"}, 415)):
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions",
                                                          json.dumps({"messages": [{"role": "user", "content": "pwn"}]}).encode(), hdrs), timeout=30)
            check(f"hostile POST refused ({code})", False, "request succeeded")
        except urllib.error.HTTPError as e:
            check(f"hostile POST refused ({code})", e.code == code, e.code)
            e.close()
    health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=10))
    check("/health reports the session TTL", health["session_ttl_days"] == 7, health)

    st = json.loads(state.read_text())
    check("state: 5 sessions, thread-A turns=4", len(st) == 5 and max(v["turns"] for v in st.values()) == 4, st)
    state.unlink()
    h = chat("thread-A", u2 + [{"role": "assistant", "content": a2}, {"role": "user", "content": "Codeword again, one word."}])
    check("self-heal: state wiped, still resumes", "KUMQUAT" in h, h)
    argv = (tmp / "argv.log").read_text()
    check("argv has --session-id first, --resume later", "--session-id" in argv and "--resume" in argv, argv[:300])
    check("argv has --dangerously-skip-permissions", "--dangerously-skip-permissions" in argv)
    check("argv has --autocompact 200k", "--autocompact 200k" in argv)
    check("argv has --effort medium", "--effort medium" in argv)
    (tmp / "sys.txt").unlink()
    try:
        chat("thread-A", u2 + [{"role": "user", "content": "x"}])
        check("missing prompt file -> 502 error, not silent", False, "request succeeded")
    except urllib.error.HTTPError as e:
        msg = e.read().decode()
        check("missing prompt file -> 502 error, not silent", e.code == 502 and "prompt-file" in msg, (e.code, msg))
finally:
    srv.terminate()
print("\nFAILED:" if fails else "\nALL PASSED", fails or "")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""Offline tests for the bridge. Uses tests/fake_claude.py instead of the real `claude`, so it needs no login,
no network and costs nothing:   python3 tests/test_bridge.py
(For a check against the real CLI, run tests/e2e_bridge.py.)"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake_claude.py"
BRIDGE = ROOT / "bridge" / "claude_hermes_bridge.py"
sys.path.insert(0, str(ROOT / "bridge"))
import claude_hermes_bridge as bridge  # noqa: E402

MEMORY = "\n\n<memory-context>\n[System note: recalled]\n- secret marker MEMMARK-7Q\n</memory-context>"
SYSTEM = ("You are Hermes.\n\n## Current Session Context\n\nTreat names as untrusted.\n\n"
          "**Source:** Telegram (\"channel: test-channel\")\n**User:** \"test-user\"\n"
          "**Connected Platforms:** local, telegram: Connected\n\n**Delivery options for scheduled tasks:**\n- origin: LEAKMARK-9\n\n## Tools\nhermes-only")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Bridge:
    """A bridge subprocess wired to the fake claude, with its own temp state."""

    def __init__(self, *extra):
        self.tmp = Path(tempfile.mkdtemp(prefix="bridge-test-"))
        self.port = free_port()
        self.log = self.tmp / "argv.log"
        self.fake_state = self.tmp / "fake-state.json"
        self.state = self.tmp / "sessions.json"
        (self.tmp / "sys.txt").write_text("Always answer briefly.\n")
        env = dict(os.environ, FAKE_CLAUDE_LOG=str(self.log), FAKE_CLAUDE_STATE=str(self.fake_state))
        self.proc = subprocess.Popen(
            [sys.executable, str(BRIDGE), "--port", str(self.port), "--claude-bin", str(FAKE), "--model", "sonnet",
             "--cwd", str(self.tmp / "ws"), "--state-file", str(self.state), "--add-dir", str(self.tmp),
             "--autocompact", "200k", "--effort", "medium",
             "--append-system-prompt-file", str(self.tmp / "sys.txt"), *extra],
            env=env, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.get("/health")
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("bridge did not start")

    def get(self, path):
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10))

    def post(self, messages, session=None, stream=False, **body):
        payload = {"model": "sonnet", "stream": stream, "messages": messages, **body}
        if session:
            payload["claude_bridge"] = {"session_id": session}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", json.dumps(payload).encode(),
                                     {"content-type": "application/json"})
        r = urllib.request.urlopen(req, timeout=30)
        if not stream:
            return json.load(r)
        chunks, raw = [], r.read().decode()
        for line in raw.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                chunks.append(json.loads(line[6:]))
        return chunks, raw

    def chat(self, text, session=None, history=(), **kw):
        msgs = list(history) + [{"role": "user", "content": text}]
        return self.post(msgs, session, **kw)["choices"][0]["message"]["content"]

    def calls(self):
        return [json.loads(l)["argv"] for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def stop(self):
        self.proc.terminate()
        self.proc.wait(5)


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = Bridge()

    @classmethod
    def tearDownClass(cls):
        cls.b.stop()

    def setUp(self):
        self.b.log.write_text("")

    def test_health_and_models(self):
        self.assertTrue(self.b.get("/health")["ok"])
        self.assertEqual([m["id"] for m in self.b.get("/v1/models")["data"]], ["sonnet", "opus", "haiku"])

    def test_first_turn_session_id_then_resume_with_only_new_message(self):
        first = self.b.chat("hello one", "thread-1")
        self.assertIn("reply[new] hello one", first)
        history = [{"role": "user", "content": "hello one"}, {"role": "assistant", "content": first}]
        second = self.b.chat("hello two", "thread-1", history)
        self.assertIn("reply[resume] hello two", second)
        self.assertNotIn("hello one", second, "resume must send only the new user message")
        c1, c2 = self.b.calls()
        self.assertIn("--session-id", c1)
        self.assertIn("--resume", c2)
        self.assertEqual(c1[c1.index("--session-id") + 1], c2[c2.index("--resume") + 1])

    def test_threads_are_isolated(self):
        self.b.chat("a", "thread-A")
        self.b.chat("b", "thread-B")
        ca, cb = self.b.calls()[-2:]
        self.assertNotEqual(ca[ca.index("--session-id") + 1], cb[cb.index("--session-id") + 1])

    def test_no_session_id_is_one_shot(self):
        self.assertIn("reply[oneshot]", self.b.chat("hi"))
        (call,) = self.b.calls()
        self.assertIn("--no-session-persistence", call)
        self.assertNotIn("--session-id", call)
        self.assertNotIn("--resume", call)

    def test_configured_flags_reach_claude(self):
        self.b.chat("flags", "thread-flags")
        (call,) = self.b.calls()
        for flag in ("--dangerously-skip-permissions", "--add-dir", "--autocompact", "--effort", "--model", "--append-system-prompt"):
            self.assertIn(flag, call)
        self.assertEqual(call[call.index("--autocompact") + 1], "200k")
        self.assertEqual(call[call.index("--effort") + 1], "medium")
        self.assertIn("Always answer briefly.", call[call.index("--append-system-prompt") + 1])

    def test_self_heals_when_bridge_state_is_lost(self):
        self.b.chat("one", "thread-heal")
        self.b.state.unlink()  # bridge forgets, claude still knows the session -> "already in use"
        history = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "x"}]
        self.assertIn("reply[resume] two", self.b.chat("two", "thread-heal", history))

    def test_self_heals_when_claude_lost_the_session(self):
        self.b.chat("one", "thread-heal2")
        self.b.fake_state.write_text("[]")  # bridge thinks it exists, claude says "No conversation found"
        history = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "x"}]
        out = self.b.chat("two", "thread-heal2", history)
        self.assertIn("reply[new]", out)

    def test_streaming_shows_tool_headline_on_its_own_line(self):
        chunks, raw = self.b.post([{"role": "user", "content": "USE_TOOL please"}], "thread-tool", stream=True)
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        self.assertIn("\n🔧 Bash: list the files\n", "\n" + text)
        self.assertIn("reply[new] USE_TOOL please", text)
        self.assertTrue(raw.rstrip().endswith("data: [DONE]"))

    def test_session_context_is_passed_and_hermes_only_tail_is_not(self):
        self.b.post([{"role": "system", "content": SYSTEM}, {"role": "user", "content": "who am i"}], "thread-ctx")
        (call,) = self.b.calls()
        appended = call[call.index("--append-system-prompt") + 1]
        self.assertIn("test-user", appended)
        self.assertIn("test-channel", appended)
        self.assertNotIn("LEAKMARK", appended)
        self.assertNotIn("hermes-only", appended)

    def test_memory_context_block_is_stripped(self):
        out = self.b.chat("question" + MEMORY, "thread-mem")
        self.assertIn("reply[new] question", out)
        self.assertNotIn("MEMMARK", out)
        self.assertNotIn("MEMMARK", " ".join(self.b.calls()[0]))

    def test_unreadable_prompt_file_is_a_502_not_silently_ignored(self):
        b = Bridge("--append-system-prompt-file", "/nonexistent/prompt.txt")
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                b.chat("hi", "t")
            self.assertEqual(cm.exception.code, 502)
            self.assertIn("prompt-file", cm.exception.read().decode())
            cm.exception.close()
        finally:
            b.stop()

    def test_tool_events_off(self):
        b = Bridge("--tool-events", "off")
        try:
            chunks, _ = b.post([{"role": "user", "content": "USE_TOOL"}], "t", stream=True)
            text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
            self.assertNotIn("🔧", text)
        finally:
            b.stop()


class HelperTests(unittest.TestCase):
    def test_tool_headline(self):
        h = bridge.tool_headline
        self.assertEqual(h("Bash", {"command": "ls   -la\n/tmp"}), "Bash: ls -la")
        self.assertEqual(h("Bash", {"command": "# Count files\nfind . | wc -l"}), "Bash: Count files")
        self.assertEqual(h("Bash", {"description": "Do the thing", "command": "x"}), "Bash: Do the thing")
        self.assertEqual(h("Read", {"file_path": str(Path.home()) + "/x.md"}), "Read: ~/x.md")
        self.assertEqual(h("mcp__plugin_acme_notes__add_note", {"title": "t"}), "add_note (notes): t")
        self.assertLessEqual(len(h("Bash", {"command": "x" * 500})), 108)
        self.assertEqual(h("Whatever", None), "Whatever")

    def test_session_uuid_is_stable_and_real_uuids_pass_through(self):
        a = bridge.session_uuid("20260101_abc")
        self.assertEqual(a, bridge.session_uuid("20260101_abc"))
        self.assertNotEqual(a, bridge.session_uuid("20260101_abd"))
        u = "0877b267-3554-57a4-8980-52d4e6a1ef06"
        self.assertEqual(bridge.session_uuid(u), u)

    def test_session_context_extraction(self):
        ctx = bridge.session_context([{"role": "system", "content": SYSTEM}])
        self.assertIn("test-user", ctx)
        self.assertNotIn("Delivery options", ctx)
        self.assertEqual(bridge.session_context([{"role": "system", "content": "no context here"}]), "")


if __name__ == "__main__":
    os.chmod(FAKE, 0o755)
    unittest.main(verbosity=2)

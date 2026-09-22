#!/usr/bin/env python3
"""Offline tests for the bridge. Uses tests/fake_claude.py instead of the real `claude`, so it needs no login,
no network and costs nothing:   uv run python -m unittest discover -s tests -v
(For a check against the real CLI, run tests/e2e_bridge.py.)"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake_claude.py"
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
from hermes_claude_cli_bridge import bridge  # noqa: E402

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

    def __init__(self, *extra, autocompact=True, prestate=None, **envextra):
        self.tmp = Path(tempfile.mkdtemp(prefix="bridge-test-"))
        self.port = free_port()
        self.log = self.tmp / "argv.log"
        self.fake_state = self.tmp / "fake-state.json"
        self.state = self.tmp / "sessions.json"
        if prestate is not None:
            self.state.write_text(json.dumps(prestate))
        (self.tmp / "sys.txt").write_text("Always answer briefly.\n")
        env = dict(os.environ, FAKE_CLAUDE_LOG=str(self.log), FAKE_CLAUDE_STATE=str(self.fake_state), PYTHONPATH=str(SRC),
                   **envextra)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "hermes_claude_cli_bridge", "serve", "--port", str(self.port), "--claude-bin", str(FAKE), "--model", "sonnet",
             "--cwd", str(self.tmp / "ws"), "--state-file", str(self.state), "--add-dir", str(self.tmp),
             *(["--autocompact", "200k"] if autocompact else []), "--effort", "medium",
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

    def raw_post(self, data: bytes, headers: dict, timeout=30):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", data, headers)
        return urllib.request.urlopen(req, timeout=timeout)

    def post(self, messages, session=None, stream=False, timeout=30, headers=None, **body):
        payload = {"model": "sonnet", "stream": stream, "messages": messages, **body}
        if session:
            payload["claude_bridge"] = {"session_id": session}
        r = self.raw_post(json.dumps(payload).encode(), headers or {"content-type": "application/json"}, timeout)
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

    def prompts(self):
        return [json.loads(l)["prompt"] for l in self.log.read_text().splitlines()] if self.log.exists() else []

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

    def test_usage_reports_hermes_history_not_claudes_cumulative_total(self):
        r = self.b.post([{"role": "user", "content": "tiny"}], "thread-usage")
        self.assertLess(r["usage"]["prompt_tokens"], 1000, "a tiny chat must not look huge (it would trigger useless compression in Hermes)")
        self.assertEqual(r["claude_usage"]["cache_read_input_tokens"], 700000, "raw Claude numbers stay available")
        big = self.b.post([{"role": "user", "content": "x" * 40000}], "thread-usage2")
        self.assertTrue(9000 < big["usage"]["prompt_tokens"] < 12000, big["usage"])
        chunks, _ = self.b.post([{"role": "user", "content": "tiny"}], "thread-usage3", stream=True)
        self.assertLess(chunks[-1]["usage"]["prompt_tokens"], 1000)

    def test_usage_claude_mode_reports_the_raw_numbers(self):
        b = Bridge("--usage", "claude")
        try:
            self.assertGreaterEqual(b.post([{"role": "user", "content": "tiny"}], "t")["usage"]["prompt_tokens"], 700000)
        finally:
            b.stop()

    def test_autocompact_defaults_to_auto_and_can_be_left_out(self):
        for extra, expect in ((None, "auto"), (["--autocompact", ""], None)):
            b = Bridge(*(extra or []), autocompact=False)
            try:
                b.chat("hi", "t")
                (call,) = b.calls()
                if expect:
                    self.assertEqual(call[call.index("--autocompact") + 1], expect)
                else:
                    self.assertNotIn("--autocompact", call)
            finally:
                b.stop()

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


class HardeningTests(unittest.TestCase):
    """The fixes from the 2026-09-21 review: argv limit, process groups, CSRF, limits, housekeeping."""

    @classmethod
    def setUpClass(cls):
        cls.b = Bridge()

    @classmethod
    def tearDownClass(cls):
        cls.b.stop()

    def setUp(self):
        self.b.log.write_text("")

    def test_prompt_goes_on_stdin_not_argv(self):
        # Linux caps ONE argv string at 128 KB (MAX_ARG_STRLEN) whatever ARG_MAX says
        big = "x" * 300_000
        out = self.b.chat(big, "thread-big")
        self.assertIn("reply[new]", out)
        (argv,), (prompt,) = self.b.calls(), self.b.prompts()
        self.assertNotIn(big, " ".join(argv), "the prompt must not be an argument")
        self.assertIn(big, prompt)

    def test_timeout_kills_the_whole_process_group(self):
        b = Bridge("--timeout", "2", FAKE_CLAUDE_SLEEP="60", FAKE_CLAUDE_SPAWN_CHILD="1")
        try:
            started = time.time()
            with self.assertRaises(urllib.error.HTTPError) as cm:
                b.chat("hang", "t", timeout=40)
            cm.exception.close()
            # before the fix the orphaned grandchild held stdout open and the read loop never ended
            self.assertLess(time.time() - started, 25, "timeout must actually end the turn")
        finally:
            b.stop()

    def test_cross_origin_post_is_refused(self):
        payload = json.dumps({"model": "sonnet", "messages": [{"role": "user", "content": "pwn"}]}).encode()
        for headers, code in (({"content-type": "application/json", "origin": "https://evil.example"}, 403),
                              ({"content-type": "text/plain"}, 415)):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.b.raw_post(payload, headers)
            self.assertEqual(cm.exception.code, code)
            cm.exception.close()
        self.assertEqual(self.b.calls(), [], "a refused request must never reach claude")

    def test_auth_token_is_enforced_when_set(self):
        b = Bridge("--auth-token", "s3cret")
        try:
            payload = json.dumps({"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]}).encode()
            with self.assertRaises(urllib.error.HTTPError) as cm:
                b.raw_post(payload, {"content-type": "application/json"})
            self.assertEqual(cm.exception.code, 401)
            cm.exception.close()
            r = b.raw_post(payload, {"content-type": "application/json", "authorization": "Bearer s3cret"})
            self.assertEqual(r.status, 200)
        finally:
            b.stop()

    def test_oversized_body_is_rejected(self):
        b = Bridge("--max-body-mb", "1")
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                b.chat("y" * 2_000_000, "t")
            self.assertEqual(cm.exception.code, 413)
            cm.exception.close()
        finally:
            b.stop()

    def test_concurrency_cap_returns_429_instead_of_forking_forever(self):
        b = Bridge("--max-concurrency", "1", "--queue-timeout", "1", FAKE_CLAUDE_SLEEP="4")
        codes = []

        def hit(n):
            try:
                b.chat("hi", f"thread-{n}", timeout=30)
                codes.append(200)
            except urllib.error.HTTPError as e:
                codes.append(e.code)
                e.close()

        try:
            ts = [threading.Thread(target=hit, args=(n,)) for n in range(3)]
            [t.start() for t in ts]
            [t.join(40) for t in ts]
            self.assertIn(429, codes, codes)
            self.assertIn(200, codes, codes)
        finally:
            b.stop()

    def test_same_thread_second_message_gets_429_not_a_15_minute_block(self):
        b = Bridge("--queue-timeout", "1", FAKE_CLAUDE_SLEEP="4")
        codes = []

        def hit():
            try:
                b.chat("hi", "same-thread", timeout=30)
                codes.append(200)
            except urllib.error.HTTPError as e:
                codes.append(e.code)
                e.close()

        try:
            t = threading.Thread(target=hit)
            t.start()
            time.sleep(1)
            hit()
            t.join(30)
            self.assertIn(429, codes, codes)
        finally:
            b.stop()

    def test_expired_sessions_are_pruned_with_their_claude_transcripts(self):
        tmp = Path(tempfile.mkdtemp(prefix="bridge-ttl-"))
        old_sid, fresh_sid = "11111111-1111-5111-8111-111111111111", "22222222-2222-5222-8222-222222222222"
        proj = tmp / "claude" / "projects" / "-ws"
        proj.mkdir(parents=True)
        for sid in (old_sid, fresh_sid):
            (proj / f"{sid}.jsonl").write_text("{}")
        now = int(time.time())
        b = Bridge("--session-ttl-days", "7", CLAUDE_CONFIG_DIR=str(tmp / "claude"),
                   prestate={old_sid: {"updated": now - 8 * 86400, "turns": 1},
                             fresh_sid: {"updated": now - 3600, "turns": 1}})
        try:
            for _ in range(30):
                if b.get("/health")["sessions"] == 1:
                    break
                time.sleep(0.1)
            health = b.get("/health")
            self.assertEqual(health["sessions"], 1, "the idle session should be gone")
            self.assertEqual(health["session_ttl_days"], 7)
            self.assertEqual(set(json.loads(b.state.read_text())), {fresh_sid})
            self.assertFalse((proj / f"{old_sid}.jsonl").exists(), "claude's own transcript must be deleted too")
            self.assertTrue((proj / f"{fresh_sid}.jsonl").exists(), "an active session must be left alone")
        finally:
            b.stop()

    def test_ttl_zero_keeps_everything(self):
        old = {"33333333-3333-5333-8333-333333333333": {"updated": int(time.time()) - 900 * 86400}}
        b = Bridge("--session-ttl-days", "0", prestate=old)
        try:
            time.sleep(0.5)
            self.assertEqual(b.get("/health")["sessions"], 1)
        finally:
            b.stop()

    def test_disconnect_during_a_silent_turn_frees_the_thread_quickly(self):
        """Hermes' busy_input_mode=interrupt aborts the HTTP request. If the turn happens to be silent
        (one long tool call), nothing is written, so without an explicit poll the bridge only notices
        at the next write — the abandoned claude keeps running and keeps holding the session lock, and
        the follow-up message then waits out --queue-timeout and 429s."""
        b = Bridge("--client-check-interval", "1", "--queue-timeout", "25", FAKE_CLAUDE_SLEEP="30")
        try:
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", b.port, timeout=10)
            body = json.dumps({"model": "sonnet", "stream": True, "messages": [{"role": "user", "content": "hi"}],
                               "claude_bridge": {"session_id": "abandoned"}})
            conn.request("POST", "/v1/chat/completions", body, {"content-type": "application/json"})
            time.sleep(2)
            conn.close()  # the caller hangs up while claude is still silent

            started = time.time()
            out = b.chat("second message NOSLEEP", "abandoned", timeout=40)
            waited = time.time() - started
            self.assertIn("reply[", out)
            # before the fix the dead turn held the lock for its full 30s, so this waited out
            # --queue-timeout (25s) and came back 429
            self.assertLess(waited, 15, f"follow-up waited {waited:.1f}s — the lock was not released promptly")
        finally:
            b.stop()

    def test_mid_thread_transcript_is_capped(self):
        b = Bridge("--max-transcript-chars", "2000")
        try:
            history = []
            for i in range(200):
                history += [{"role": "user", "content": f"old question {i} " + "z" * 200},
                            {"role": "assistant", "content": f"old answer {i}"}]
            b.chat("the new one", "thread-cap", history)
            (prompt,) = b.prompts()
            self.assertIn("the new one", prompt)
            self.assertIn("older messages omitted", prompt)
            self.assertNotIn("old question 0 ", prompt, "the oldest turns are dropped, not the newest")
            self.assertIn("old question 199", prompt)
            self.assertLess(len(prompt), 6000)
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

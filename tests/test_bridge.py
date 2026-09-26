#!/usr/bin/env python3
"""Offline tests for the bridge. Uses tests/fake_claude.py instead of the real `claude`, so it needs no login,
no network and costs nothing:   uv run python -m unittest discover -s tests -v
(For a check against the real CLI, run tests/e2e_bridge.py.)"""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
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
        self.assertIn("\n🔧 Bash: `list the files`\n", "\n" + text)
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

    def test_segment_markers_only_when_asked(self):
        plain = "".join(c or "" for c in self._contents(self.b))
        self.assertIsNone(bridge.MARKERS.search(plain), "clients that did not ask never see a marker")
        text = "".join(c or "" for c in self._contents(self.b, segments=True))
        self.assertEqual(text.split(bridge.TOOL_RUN)[0], bridge.TEXT_RUN + "Checking.")
        tools, answer = text.split(bridge.TOOL_RUN)[1].split(bridge.TEXT_RUN)
        self.assertEqual(tools, "🔧 Bash: `list the files`\n", "the tool line starts its own run, no stray newline")
        self.assertIn("reply[", answer)
        self.assertNotIn(bridge.FLUSH_TICK, text, "a quick turn needs no flush")

    def test_flush_tick_during_a_slow_tool(self):
        b = Bridge("--flush-interval", "0.3", FAKE_CLAUDE_TOOL_SECONDS="1.2")
        try:
            text = "".join(c or "" for c in self._contents(b, segments=True))
            before, _, after = text.partition(bridge.FLUSH_TICK)
            self.assertIn("🔧", before, "the tick comes while the tool runs, after its headline")
            self.assertIn("reply[", after)
            self.assertEqual(text.count(bridge.FLUSH_TICK), 1, "no tick without new output")
        finally:
            b.stop()

    @staticmethod
    def _contents(b, **ext):
        payload = {"model": "sonnet", "stream": True, "claude_bridge": {"session_id": "thread-seg", **ext},
                   "messages": [{"role": "user", "content": "PREAMBLE USE_TOOL"}]}
        raw = b.raw_post(json.dumps(payload).encode(), {"content-type": "application/json"}, 30).read().decode()
        return [json.loads(l[6:])["choices"][0]["delta"].get("content") for l in raw.splitlines()
                if l.startswith("data: ") and l != "data: [DONE]"]

    def test_segment_markers_never_reach_claude(self):
        self.assertEqual(bridge._text("a" + bridge.TEXT_RUN + bridge.TOOL_RUN + bridge.FLUSH_TICK + "b"), "ab")

    def test_tool_events_off(self):
        b = Bridge("--tool-events", "off")
        try:
            chunks, _ = b.post([{"role": "user", "content": "USE_TOOL"}], "t", stream=True)
            text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
            self.assertNotIn("🔧", text)
        finally:
            b.stop()


class PluginSegmentTests(unittest.TestCase):
    """The Hermes plugin maps the bridge's markers onto Hermes' own new-message / interim-message machinery."""

    T, O, F = bridge.TEXT_RUN, bridge.TOOL_RUN, bridge.FLUSH_TICK

    def setUp(self):
        import importlib.util
        import types
        test = self

        class AIAgent:
            def __init__(self, live):
                self.shown, self.interim = [], []
                self.stream_delta_callback = self.shown.append if live else None
                self.interim_assistant_callback = lambda text, already_streamed=False: self.interim.append(text)

            def _fire_stream_delta(self, text):
                if text and self.stream_delta_callback:
                    self.stream_delta_callback(text)

            def _interruptible_streaming_api_call(self, api_kwargs, on_first_delta=None):
                for d in test.deltas:
                    self._fire_stream_delta(d)
                msg = types.SimpleNamespace(content="".join(test.deltas))
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

        providers, base = types.ModuleType("providers"), types.ModuleType("providers.base")
        providers.register_provider = lambda p: None
        base.ProviderProfile = type("ProviderProfile", (), {"__init__": lambda self, **kw: None})
        stubs = {"providers": providers, "providers.base": base, "run_agent": types.SimpleNamespace(AIAgent=AIAgent)}
        saved = {k: sys.modules.get(k) for k in stubs}
        sys.modules.update(stubs)
        self.addCleanup(lambda: [sys.modules.pop(k, None) if v is None else sys.modules.__setitem__(k, v)
                                 for k, v in saved.items()])
        path = SRC / "hermes_claude_cli_bridge" / "plugin" / "claude-code-bridge" / "__init__.py"
        spec = importlib.util.spec_from_file_location("claude_bridge_plugin_under_test", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.AIAgent = AIAgent
        self.assertEqual((self.mod.TEXT_RUN, self.mod.TOOL_RUN, self.mod.FLUSH_TICK), (self.T, self.O, self.F))
        self.assertEqual(self.mod.claude_code.build_extra_body(session_id="s")["claude_bridge"],
                         {"segments": True, "session_id": "s"})
        self.mod._install_segment_breaks()  # idempotent: must not wrap twice

    def run_turn(self, live, deltas):
        self.deltas = deltas
        agent = self.AIAgent(live)
        return agent, agent._interruptible_streaming_api_call({}).choices[0].message.content

    def test_streaming_display_gets_a_new_message_per_run(self):
        agent, content = self.run_turn(True, [self.T, "Checking.", self.O, "🔧 Bash\n", self.F, self.T, "answer"])
        self.assertEqual(agent.shown, ["Checking.", None, "🔧 Bash\n", None, "answer"])
        self.assertEqual(agent.interim, [], "a streaming display already shows everything live")
        self.assertEqual(content, "Checking.🔧 Bash\nanswer", "markers never reach Hermes' history")

    def test_without_streaming_ticks_post_progress_and_the_answer_stands_alone(self):
        agent, content = self.run_turn(False, [
            self.T, "Checking.", self.O, "🔧 Bash: a\n", self.F,   # tick 1: preamble + tool so far
            "🔧 Bash: b\n", self.T, "Found it", self.F,              # tick 2: text may be the answer, wait
            self.O, "🔧 Read: c\n", self.T, "The answer."])        # end: rest of progress, then the answer
        self.assertEqual(agent.shown, [])
        self.assertEqual(agent.interim, ["Checking.\n🔧 Bash: a", "🔧 Bash: b", "Found it\n🔧 Read: c"])
        self.assertEqual(content, "The answer.", "the reply is only the answer, posted on its own")

    def test_without_streaming_a_turn_ending_in_tools_keeps_its_reply(self):
        agent, content = self.run_turn(False, [self.T, "Done.", self.O, "🔧 Bash\n"])
        self.assertEqual(agent.interim, [])
        self.assertEqual(content, "Done.🔧 Bash\n", "never trim the reply to nothing")

    def test_other_providers_are_untouched(self):
        agent, content = self.run_turn(True, ["plain ", "text"])
        self.assertEqual((agent.shown, content), (["plain ", "text"], "plain text"))

    def test_long_answer_is_posted_in_clean_chunks_and_the_reply_is_the_last(self):
        self.mod.CHUNK_GAP = 0
        paras = [f"Paragraph {i}. " + ("word " * 80).strip() for i in range(12)]  # ~420 chars each
        agent, content = self.run_turn(False, [self.T, "Checking.", self.O, "🔧 Bash\n", self.T, "\n\n".join(paras)])
        chunks = agent.interim[1:] + [content]
        self.assertEqual(agent.interim[0], "Checking.\n🔧 Bash", "progress stays its own message")
        self.assertTrue(all(len(c) <= 2000 for c in chunks), [len(c) for c in chunks])
        self.assertEqual("\n\n".join(chunks), "\n\n".join(paras), "cut only between paragraphs")
        self.assertGreater(len(chunks), 1)

    def test_a_long_turn_ending_in_tools_is_split_at_lines_too(self):
        self.mod.CHUNK_GAP = 0
        heads = [f"🔧 Bash: `cd ~/projects/x{i} && grep -n something file{i}.js`\n" for i in range(120)]
        agent, content = self.run_turn(False, [self.T, "Done.", self.O, *heads])
        chunks = agent.interim + [content]
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 2000)
            self.assertEqual(c.count("`") % 2, 0, "no inline code span cut in two")

    def test_short_answer_is_not_split(self):
        agent, content = self.run_turn(False, [self.T, "Short answer."])
        self.assertEqual((agent.interim, content), ([], "Short answer."))

    def test_split_keeps_code_blocks_whole_and_reopens_oversized_ones(self):
        split = self.mod._split
        block = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(20)) + "\n```"
        text = "intro " * 300 + "\n\n" + block + "\n\nafter"
        chunks = split(text, 2000)
        self.assertTrue(any(block in c for c in chunks), "the whole block lands in one message")
        big = "```sh\n" + "\n".join("echo " + "y" * 40 for _ in range(100)) + "\n```"
        chunks = split(big, 2000)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 2000)
            self.assertTrue(c.startswith("```sh\n") and c.endswith("\n```"), c[:20] + "..." + c[-20:])
        self.assertEqual(sum(c.count("echo ") for c in chunks), 100)

    def test_split_falls_back_to_sentences_then_words_then_hard_cuts(self):
        split = self.mod._split
        one_para = " ".join(f"Sentence number {i} is here." for i in range(200))
        chunks = split(one_para, 500)
        self.assertTrue(all(len(c) <= 500 and c.endswith(".") for c in chunks))
        self.assertEqual(" ".join(chunks), one_para)
        self.assertEqual(split("z" * 1200, 500), ["z" * 500, "z" * 500, "z" * 200])
        self.assertEqual(split("  ", 500), [])
        self.assertEqual(split("a\n\nb", 0), ["a\n\nb"], "0 = off")


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


class WatchdogTests(unittest.TestCase):
    """A turn is stopped when nothing is happening, not after a fixed time: silence from claude itself, or a
    running tool whose processes use no CPU and do no I/O. Claude's tool heartbeats prove only that claude is
    alive, so they never count. A busy tool may run past the idle limit; --timeout stays as the hard cap."""

    def _run(self, *args, prompt="USE_TOOL", **env):
        b = Bridge(*args, **env)
        try:
            payload = {"model": "sonnet", "stream": True, "claude_bridge": {"session_id": "thread-wd", "segments": True},
                       "messages": [{"role": "user", "content": prompt}]}
            started = time.time()
            raw = b.raw_post(json.dumps(payload).encode(), {"content-type": "application/json"}, 60).read().decode()
            text = "".join(json.loads(l[6:])["choices"][0]["delta"].get("content") or "" for l in raw.splitlines()
                           if l.startswith("data: ") and l != "data: [DONE]")
            return bridge.MARKERS.sub("", text), time.time() - started
        finally:
            b.stop()

    def test_silent_claude_is_stopped_after_the_idle_timeout(self):
        text, took = self._run("--idle-timeout", "2", "--timeout", "60", prompt="hang", FAKE_CLAUDE_SLEEP="60")
        self.assertIn("claude turn stopped: no output from claude for 2s", text)
        self.assertLess(took, 15)

    def test_a_busy_tool_outlives_the_idle_timeout(self):
        text, _ = self._run("--idle-timeout", "1.5", "--timeout", "60", FAKE_CLAUDE_TOOL_SECONDS="5",
                            FAKE_CLAUDE_TOOL_MODE="busy", FAKE_CLAUDE_HEARTBEAT="0.5")
        self.assertNotIn("claude-bridge error", text)
        self.assertIn("reply[", text)

    def test_a_silent_tool_is_stopped_even_while_claude_sends_heartbeats(self):
        text, took = self._run("--idle-timeout", "1.5", "--timeout", "60", FAKE_CLAUDE_TOOL_SECONDS="30",
                               FAKE_CLAUDE_TOOL_MODE="silent", FAKE_CLAUDE_HEARTBEAT="0.5")
        self.assertIn("claude turn stopped: Bash idle for 1.5s (no CPU or I/O)", text)
        self.assertNotIn("reply[", text)
        self.assertLess(took, 15)

    def test_a_busy_background_job_counts_as_activity(self):
        text, _ = self._run("--idle-timeout", "1.5", "--timeout", "60", prompt="bg", FAKE_CLAUDE_BG_SECONDS="5")
        self.assertNotIn("claude-bridge error", text)
        self.assertIn("reply[", text)

    def test_the_hard_cap_names_the_limit(self):
        text, took = self._run("--timeout", "2", "--idle-timeout", "0", FAKE_CLAUDE_TOOL_SECONDS="30",
                               FAKE_CLAUDE_TOOL_MODE="busy")
        self.assertIn("claude turn stopped: hit the 2s turn limit", text)
        self.assertLess(took, 15)

    def test_stats_line_every_interval_while_a_tool_runs(self):
        text, _ = self._run("--stats-interval", "1", "--idle-timeout", "0", FAKE_CLAUDE_TOOL_SECONDS="3.5",
                            FAKE_CLAUDE_TOOL_MODE="busy")
        stats = [l for l in text.splitlines() if l.startswith("📊")]
        self.assertGreaterEqual(len(stats), 2, text)
        self.assertIn("claude cpu", stats[-1])
        self.assertIn("tools (1 proc) cpu", stats[-1])
        self.assertIn("running Bash", stats[-1])
        self.assertIn("reply[", text)

    def test_no_stats_when_disabled(self):
        text, _ = self._run("--stats-interval", "0", FAKE_CLAUDE_TOOL_SECONDS="2.5", FAKE_CLAUDE_TOOL_MODE="busy")
        self.assertNotIn("📊", text)


class BundledPromptTests(unittest.TestCase):
    """Opt-in prompt files shipped with the package: --prompt agents|self-learn|subagents|all."""

    def _prompt(self, *args, **env):
        b = Bridge(*args, **env)
        try:
            b.chat("hi", "thread-prompt")
            (call,) = b.calls()
            return call[call.index("--append-system-prompt") + 1]
        finally:
            b.stop()

    def test_names_resolve_to_packaged_files(self):
        files = bridge.resolve_prompts(["all"])
        self.assertEqual([Path(f).name for f in files], ["agents.md", "self-learn.md", "subagents.md"])
        self.assertTrue(all(Path(f).is_file() for f in files))
        self.assertEqual(bridge.resolve_prompts(["self-learn", "agents", "self-learn"]),
                         [str(Path(files[1])), str(Path(files[0]))], "order kept, duplicates dropped")
        self.assertEqual(bridge.resolve_prompts([]), [])
        with self.assertRaises(ValueError):
            bridge.resolve_prompts(["nope"])

    def test_prompt_flag_reaches_claude_before_the_operators_own_file(self):
        prompt = self._prompt("--prompt", "self-learn", "--prompt", "subagents")
        self.assertIn("created-by: claude-self-learn", prompt)
        self.assertIn("| Code search |", prompt)
        self.assertNotIn("Shared checkouts", prompt, "agents.md was not asked for")
        self.assertLess(prompt.index("claude-self-learn"), prompt.index("Always answer briefly."),
                        "the operator's own prompt file comes last, so it can override")

    def test_env_var_and_default_off(self):
        self.assertIn("Shared checkouts", self._prompt(CLAUDE_BRIDGE_PROMPTS="agents,self-learn"))
        self.assertNotIn("claude-self-learn", self._prompt())

    def test_flag_replaces_the_env_var(self):
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_BRIDGE_PROMPTS": "agents"}):
            self.assertEqual([Path(f).name for f in bridge.parse_args(["--prompt", "subagents"]).append_system_prompt_file],
                             ["subagents.md"])
            self.assertEqual([Path(f).name for f in bridge.parse_args([]).append_system_prompt_file], ["agents.md"])

    def test_unknown_prompt_name_stops_startup(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            bridge.parse_args(["--prompt", "nope"])
        self.assertIn("unknown prompt 'nope' (choose from agents, self-learn, subagents, all)", err.getvalue())


class HelperTests(unittest.TestCase):
    def test_sample_tree_sees_this_process_and_its_children(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            time.sleep(0.3)
            snap = bridge.sample_tree(os.getpid())
            pids = {s["pid"] for s in snap.values()}
            self.assertIn(os.getpid(), pids)
            self.assertIn(child.pid, pids)
            u = bridge.tree_usage({}, snap, os.getpid())
            self.assertGreater(u["claude_rss"], 0)
            self.assertGreaterEqual(u["procs"], 1)
            self.assertTrue(u["active"], "a process that was not there before is activity")
            self.assertFalse(bridge.tree_usage(snap, snap, os.getpid())["active"], "no change is no activity")
        finally:
            child.kill()
            child.wait()

    def test_ps_time_parsing(self):
        for raw, secs in (("0:01.50", 1.5), ("01:02:03", 3723), ("2-00:00:01", 172801), ("12:05", 725)):
            self.assertAlmostEqual(bridge._ps_seconds(raw), secs)

    def test_stats_line_format(self):
        u = {"claude_cpu": 0.4, "claude_rss": 300 << 20, "procs": 2, "cpu": 41.25, "read": 12 << 20,
             "written": 1536 << 10, "rss": 800 << 20, "io": True}
        line = bridge.stats_line(190, 60, u, [("Bash", 130)], quiet=130)
        self.assertEqual(line, "📊 3m10s · last 1m: claude cpu 0.4s, 300 MB · tools (2 procs) cpu 41.2s, "
                               "read 12.0 MB, wrote 1.5 MB, 800 MB · running Bash 2m10s · no output 2m10s")
        self.assertEqual(bridge.stats_line(45, 60, None, [], quiet=0), "📊 45s")

    def test_tool_headline(self):
        h = bridge.tool_headline
        self.assertEqual(h("Bash", {"command": "ls   -la\n/tmp"}), "Bash: `ls -la`")
        self.assertEqual(h("Bash", {"command": "# Count files\nfind . | wc -l"}), "Bash: `Count files`")
        self.assertEqual(h("Bash", {"description": "Do the thing", "command": "x"}), "Bash: `Do the thing`")
        self.assertEqual(h("Read", {"file_path": str(Path.home()) + "/x.md"}), "Read: `~/x.md`")
        self.assertEqual(h("mcp__plugin_acme_notes__add_note", {"title": "t"}), "add_note (notes): `t`")
        self.assertLessEqual(len(h("Bash", {"command": "x" * 500})), 110)
        self.assertEqual(h("Whatever", None), "Whatever")

    def test_headline_paths_cannot_be_auto_attached_by_the_chat_gateway(self):
        """Hermes uploads any bare ~/ or / path in a reply that exists on disk; inline code is skipped.
        Every path a headline shows must therefore sit inside one self-contained backtick pair."""
        h = bridge.tool_headline
        # a backtick in the tool input must not be able to close the span early
        line = h("Bash", {"command": "cat `ls ~/notes/INFRA.md`"})
        self.assertEqual(line.count("`"), 2, line)
        self.assertTrue(line.endswith("`"))
        for name, inp in (("Read", {"file_path": str(Path.home()) + "/infra/INFRA.md"}),
                          ("Edit", {"file_path": "/etc/hosts.md"}),
                          ("Grep", {"pattern": "~/infra/LOG.md"})):
            line = h(name, inp)
            self.assertEqual(line.count("`"), 2, line)
            body = line.split("`")[1]
            self.assertNotIn("`", body)
            self.assertNotIn("\n", line)

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

"""claude-code-bridge provider profile for Hermes Agent.

Routes Hermes chat through a local bridge that runs the `claude` CLI. The only
thing this plugin adds beyond a plain OpenAI-compatible endpoint is the Hermes
session id, which the bridge maps to `claude --session-id/--resume` so each
Hermes thread keeps its own Claude Code session.

Optional per-Hermes-process overrides (all also settable on the bridge):
  CLAUDE_CODE_ADD_DIRS            os.pathsep-separated dirs -> --add-dir
  CLAUDE_CODE_APPEND_SYSTEM_PROMPT  -> --append-system-prompt
  CLAUDE_CODE_AUTOCOMPACT         'auto' or 100k-1M -> --autocompact
  CLAUDE_CODE_EFFORT              low|medium|high|xhigh|max -> --effort
  CLAUDE_CODE_CWD                 working dir for claude

Plugin-only (non-streaming platforms such as Mattermost):
  CLAUDE_CODE_CHUNK_CHARS         max chars per posted message, default 2000; 0 = leave splitting to Hermes
  CLAUDE_CODE_CHUNK_GAP           seconds between consecutive posts so they land in order, default 0.5
  CLAUDE_CODE_STATUS_EDIT_GAP     min seconds between edits of the live status post, default 3
"""

from __future__ import annotations

import os
import re
import time

from providers import register_provider
from providers.base import ProviderProfile


# The bridge streams a whole Claude Code turn (text, tool calls, more text) as ONE completion, so Hermes would
# post it all as one chat message, and only once the turn is over. The bridge marks where each run of text or
# tool headlines starts (TEXT_RUN / TOOL_RUN) and sends FLUSH_TICK every --flush-interval seconds; this patch
# maps them onto what Hermes' own tool loop does between API calls:
#   streaming display (stream_delta_callback set): a new run starts a new message (the callback gets None;
#     TTS does not, it reads None as end-of-stream).
#   no streaming (e.g. Mattermost by default): each tick posts the finished runs as an interim message, and the
#     reply is trimmed to the last text run, so the answer arrives as a message of its own.
#
# STATUS prefixes a status line (a tool headline or the per-minute stats line). They all go into ONE chat post per
# turn that is edited in place -- the latest stats line on top, the tool headlines below -- through the platform
# adapter of the gateway turn, so a long turn does not post a message a minute. Where that is not possible (no
# gateway turn, an adapter that cannot edit, a failed post) status lines are shown like tool headlines were before.
TEXT_RUN, TOOL_RUN, FLUSH_TICK, STATUS = "\u2063", "\u2064", "\u2062", "\u2061"
_MARKERS = re.compile("([\u2061\u2062\u2063\u2064])")
_STATUS_LINES = re.compile("\u2061[^\u2061\u2062\u2063\u2064]*")
_RUNS = "_claude_bridge_runs"  # per-call state on the agent: [[kind, text, chars already posted], ...]
_LIVE = "_claude_bridge_live"  # per-call _LivePost on the agent

# Hermes' own Mattermost split is 4000 chars at an arbitrary line/space with "(1/3)" tags; posts that long break
# up in the Mattermost view. Each message is posted pre-split instead, at paragraph > line > sentence > word
# boundaries, with fenced code blocks kept whole (or closed and reopened when one alone is over the limit).
CHUNK_CHARS = int(os.getenv("CLAUDE_CODE_CHUNK_CHARS") or 2000)
CHUNK_GAP = float(os.getenv("CLAUDE_CODE_CHUNK_GAP") or 0.5)  # Hermes posts interim messages fire-and-forget
STATUS_EDIT_GAP = float(os.getenv("CLAUDE_CODE_STATUS_EDIT_GAP") or 3)
STATUS_MAX_CHARS = 3800  # Mattermost's post limit is 4000
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_SEPS = ((re.compile(r"\n"), "\n"), (re.compile(r"(?<=[.!?])\s+"), " "), (re.compile(r" +"), " "))


def _units(text):
    """Blank-line separated paragraphs, each fenced code block a unit of its own, blank lines and all."""
    units, cur, fence = [], [], None
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is None and (m or not line.strip()):
            if cur:
                units.append("\n".join(cur))
            cur, fence = [], m.group(1) if m else None
            if not m:
                continue
        elif fence and line.strip().startswith(fence) and not line.strip().strip(fence[0]):
            units.append("\n".join(cur + [line]))
            cur, fence = [], None
            continue
        cur.append(line)
    return units + ["\n".join(cur)] if cur else units


def _pack(text, limit, seps=_SEPS):
    """Greedily pack text into <= limit pieces, splitting at the coarsest separator that fits."""
    if len(text) <= limit:
        return [text]
    if not seps:
        return [text[i:i + limit] for i in range(0, len(text), limit)]
    (rx, join), out, cur = seps[0], [], None
    for part in rx.split(text):
        for p in _pack(part, limit, seps[1:]):
            if cur is not None and len(cur) + len(join) + len(p) <= limit:
                cur += join + p
            else:
                out += [] if cur is None else [cur]
                cur = p
    return out + ([] if cur is None else [cur])


def _split(text, limit):
    text = text.strip()
    if limit <= 0 or len(text) <= limit:
        return [text] if text else []
    out, cur = [], ""
    for unit in _units(text):
        m = _FENCE.match(unit)
        if len(unit) <= limit:
            pieces = [unit]
        elif m:  # a code block alone is too long: split it by lines, closing and reopening the fence
            head, *body = unit.split("\n")
            if body and body[-1].strip().startswith(m.group(1)):
                body.pop()
            close = "\n" + m.group(1)
            room = max(1, limit - len(head) - len(close) - 1)
            pieces = [f"{head}\n{b}{close}" for b in _pack("\n".join(body), room, _SEPS[:1])]
        else:
            pieces = _pack(unit, limit)
        for p in pieces:
            if cur and len(cur) + 2 + len(p) > limit:
                out.append(cur)
                cur = ""
            cur = f"{cur}\n\n{p}" if cur else p
    return [c for c in out + [cur] if c.strip()]


def _post(agent, texts) -> bool:
    """Post each text as interim message(s) of at most CHUNK_CHARS, in order. True if anything was posted."""
    cb = getattr(agent, "interim_assistant_callback", None)
    posted = False
    chunks = [c for t in texts for c in _split(t, CHUNK_CHARS)] if cb else []
    lp = getattr(agent, _LIVE, None)
    if chunks and lp is not None:
        lp.freeze()  # this text lands below the status post: finish that one, the next status line starts a new one
    for chunk in chunks:
        if posted and CHUNK_GAP > 0:
            time.sleep(CHUNK_GAP)
        try:
            cb(chunk, already_streamed=False)
            posted = True
        except Exception:
            pass
    return posted


class _LivePost:
    """The turn's status lines in one chat post, edited in place: the latest stats line, then the tool headlines
    (the oldest dropped when the post would be too long). Reaches the platform through the gateway turn Hermes
    wired onto the agent (tool_progress_callback is that turn's bound progress_callback); `ok` is False when
    there is none or its adapter cannot edit."""

    def __init__(self, agent):
        self.agent = agent
        runner = getattr(getattr(agent, "tool_progress_callback", None), "__self__", None)
        ctx = getattr(runner, "_ctx", None)
        self.current = getattr(ctx, "_run_still_current", None)  # False once the user stopped or moved on
        self.adapter = getattr(ctx, "_status_adapter", None)
        self.chat_id = getattr(ctx, "_status_chat_id", None)
        self.meta = getattr(ctx, "_status_thread_metadata", None)
        self.schedule = getattr(runner, "_schedule", None)
        self.ok = bool(self.adapter and self.chat_id and callable(self.schedule)
                       and callable(getattr(self.adapter, "edit_message", None))
                       and callable(getattr(self.adapter, "send", None)))
        self.stats, self.tools = "", []
        self.dirty, self.last, self.sent, self.mid = False, 0.0, None, None

    def freeze(self):
        """Give the current post its final state and stop editing it. Mattermost neither moves nor notifies an
        edited post, so once text is posted below it the status would update out of sight; the next status line
        starts a new post (with the latest stats) under that text."""
        if self.mid is None and self.sent is None:
            return
        if self.dirty:
            self.push(final=True)
        self.mid, self.sent, self.tools, self.dirty = None, None, [], False

    def add(self, line):
        if line.startswith("📊"):
            self.stats = line
        else:
            self.tools.append(line)
        self.dirty = True

    def render(self) -> str:
        tools, dropped = list(self.tools), 0
        while tools and len(self.stats) + sum(len(t) + 1 for t in tools) + 40 > STATUS_MAX_CHARS:
            tools.pop(0)
            dropped += 1
        head = [self.stats] if self.stats else []
        more = [f"… {dropped} earlier tool call{'s' if dropped != 1 else ''}"] if dropped else []
        return "\n".join(head + more + tools)

    def push(self, final=False):
        """Post or edit, at most every STATUS_EDIT_GAP s unless final. Never blocks, except at the end of the
        turn for the first post (so the last edit is not lost)."""
        if callable(self.current):
            try:
                if not self.current():
                    return  # a stale turn must not touch the chat any more
            except Exception:
                pass
        if self.ok and self.mid is None and self.sent is not None and (final or self.sent.done()):
            self._settle(final)  # learn the first post's fate early: a failure must not sit on lines until the end
        if not (self.ok and self.dirty) or (not final and time.monotonic() - self.last < STATUS_EDIT_GAP):
            return
        if self.mid is None and self.sent is not None:
            return  # the first post is still on its way; edit once it has an id
        text = self.render()
        if self.mid is None:
            self.sent = self.schedule(self.adapter.send(self.chat_id, text, metadata=self.meta),
                                      "claude-bridge live status post")
            if self.sent is None:
                self._give_up(final)
                return
            if final:
                self._settle(final)
                if not self.ok:
                    return
        else:
            self.schedule(self.adapter.edit_message(self.chat_id, self.mid, text), "claude-bridge live status edit")
        self.dirty, self.last = False, time.monotonic()

    def _settle(self, final):
        if final:
            try:
                self.sent.result(timeout=5)
            except Exception:
                pass
        if not self.sent.done():
            return
        try:
            res = self.sent.result()
            self.mid = getattr(res, "message_id", None) if getattr(res, "success", False) else None
        except Exception:
            self.mid = None
        self.sent = None
        if self.mid is None:
            self._give_up(final)

    def _give_up(self, final):
        """No live post after all: everything collected so far is shown as progress instead."""
        self.ok, self.sent = False, None
        text = self.render()
        if text:
            _show_as_tool_run(self.agent, text + "\n", final)


def _show_as_tool_run(agent, text, final=False):
    runs = getattr(agent, _RUNS, None)
    if runs is None:
        return
    entry = ["tool", text, 0]
    if runs and runs[-1][0] == "text":  # before an open text run: it may be the answer, and its deltas keep coming
        runs.insert(len(runs) - 1, entry)
    else:
        runs.append(entry)


def _live_post(agent) -> _LivePost:
    lp = getattr(agent, _LIVE, None)
    if lp is None:
        lp = _LivePost(agent)
        setattr(agent, _LIVE, lp)
    return lp


def _commit_status(agent, runs, fire=None) -> None:
    """A status line is complete once the next marker (or the end) arrives: hand it to the live post, or show it
    the way tool headlines were shown when there is no live post."""
    if not runs or runs[-1][0] != "status":
        return
    line = runs[-1][1].strip("\n").rstrip()
    runs[-1][0] = "status-done"
    if not line.strip():
        return
    lp = _live_post(agent)
    if lp.ok:
        if agent.stream_delta_callback is None:
            _post_progress(agent)  # Claude's text before this tool call goes out first, so the status sits below it
        lp.add(line)
        lp.push()
        return
    runs[-1][:] = ["tool", line + "\n", 0]
    if fire is not None and agent.stream_delta_callback is not None:
        if len(runs) > 1 and runs[-2][1]:
            try:
                agent.stream_delta_callback(None)
            except Exception:
                pass
        fire(agent, line + "\n")


def _shown_runs(agent):
    return [r for r in (getattr(agent, _RUNS, None) or []) if not r[0].startswith("status")]


def _pending_progress(agent) -> str:
    """Runs not yet shown. Text still streaming may be the answer, so it waits until the next run starts; at
    the end the last text run IS the answer and is left for the reply."""
    raw = getattr(agent, _RUNS, None) or []
    parts = []
    for i, run in enumerate(raw):
        if run[0].startswith("status"):
            continue  # a status line after text still means Claude moved on: that text is not the answer
        if run[0] == "text" and i == len(raw) - 1:
            break
        part = run[1][run[2]:].strip()
        run[2] = len(run[1])
        if part:
            parts.append((run[0], part))
    # a blank line where text meets tool lines: after a Markdown list, a single newline would fold every
    # following line into its last item (one run-on paragraph in Mattermost)
    out = ""
    for i, (kind, part) in enumerate(parts):
        out += ("" if i == 0 else "\n" if kind == parts[i - 1][0] == "tool" else "\n\n") + part
    return out


def _post_progress(agent) -> None:
    _post(agent, [_pending_progress(agent)])


def _install_segment_breaks() -> bool:
    try:
        from run_agent import AIAgent
    except Exception:
        return False
    fire, call = AIAgent._fire_stream_delta, AIAgent._interruptible_streaming_api_call
    if getattr(fire, "_claude_bridge_segments", False):
        return True

    def _fire_stream_delta(self, text):
        runs = getattr(self, _RUNS, None)
        if not isinstance(text, str) or (runs is None and not _MARKERS.search(text)):
            return fire(self, text)
        if runs is None:
            runs = []
            setattr(self, _RUNS, runs)
        live = self.stream_delta_callback is not None
        for piece in _MARKERS.split(text):
            if piece in (TEXT_RUN, TOOL_RUN, STATUS, FLUSH_TICK):
                _commit_status(self, runs, fire)
            if piece == STATUS:
                runs.append(["status", "", 0])
            elif piece in (TEXT_RUN, TOOL_RUN):
                if live and runs and runs[-1][1] and not runs[-1][0].startswith("status"):
                    try:
                        self.stream_delta_callback(None)
                    except Exception:
                        pass
                runs.append(["text" if piece == TEXT_RUN else "tool", "", 0])
            elif piece == FLUSH_TICK:
                if not live:
                    _post_progress(self)
                _live_post(self).push()
            elif piece:
                if not runs or runs[-1][0] == "status-done":
                    runs.append(["text", "", 0])
                runs[-1][1] += piece
                if live and runs[-1][0] != "status":
                    fire(self, piece)

    def _interruptible_streaming_api_call(self, *args, **kwargs):
        setattr(self, _RUNS, None)
        setattr(self, _LIVE, None)
        try:
            resp = call(self, *args, **kwargs)
            raw = getattr(self, _RUNS, None)
            if raw is not None:
                _commit_status(self, raw, fire)
                lp = getattr(self, _LIVE, None)
                if lp is not None:
                    lp.push(final=True)
            runs = _shown_runs(self) if raw is not None else None
            msg = resp.choices[0].message if runs is not None and getattr(resp, "choices", None) else None
            if msg is not None and isinstance(msg.content, str):
                msg.content = _MARKERS.sub("", _STATUS_LINES.sub("", msg.content))
                answer = runs[-1][1].strip() if runs and runs[-1][0] == "text" else ""
                if self.stream_delta_callback is None and getattr(self, "interim_assistant_callback", None) and answer:
                    # all but the answer's last chunk go out as interim messages; that chunk is the reply, so
                    # Hermes (which keeps msg.content as the turn's history) never re-splits it
                    *head, last = _split(answer, CHUNK_CHARS)
                    if _post(self, [_pending_progress(self)] + head) and CHUNK_GAP > 0:
                        time.sleep(CHUNK_GAP)  # the reply must land after them
                    msg.content = last
                elif self.stream_delta_callback is None and getattr(self, "interim_assistant_callback", None) \
                        and 0 < CHUNK_CHARS < len(msg.content.strip()):
                    # a turn that ends in tools keeps its whole text as the reply; still never let Hermes cut it
                    # mid-line (an inline `code` span cut in two turns the rest of the post into one line)
                    *head, last = _split(msg.content, CHUNK_CHARS)
                    if _post(self, head) and CHUNK_GAP > 0:
                        time.sleep(CHUNK_GAP)
                    msg.content = last
            return resp
        finally:
            setattr(self, _RUNS, None)
            setattr(self, _LIVE, None)

    _fire_stream_delta._claude_bridge_segments = True
    AIAgent._fire_stream_delta = _fire_stream_delta
    AIAgent._interruptible_streaming_api_call = _interruptible_streaming_api_call
    return True


class ClaudeCodeProfile(ProviderProfile):
    def build_extra_body(self, *, session_id=None, **context):
        # installed here, not at import: run_agent may still be mid-import when plugins are discovered
        ext: dict = {"segments": True, "live_status": True} if _install_segment_breaks() else {}
        if session_id:
            ext["session_id"] = session_id
        dirs = [d for d in os.getenv("CLAUDE_CODE_ADD_DIRS", "").split(os.pathsep) if d]
        if dirs:
            ext["add_dirs"] = dirs
        for key, env in (("append_system_prompt", "CLAUDE_CODE_APPEND_SYSTEM_PROMPT"),
                         ("autocompact", "CLAUDE_CODE_AUTOCOMPACT"), ("effort", "CLAUDE_CODE_EFFORT"), ("cwd", "CLAUDE_CODE_CWD")):
            if os.getenv(env):
                ext[key] = os.environ[env]
        return {"claude_bridge": ext} if ext else {}


# Hermes only treats a provider as configured when one of env_vars is set. The
# bridge needs no secret, so default the URL var here instead of asking users to
# edit ~/.hermes/.env.
os.environ.setdefault("CLAUDE_CODE_BRIDGE_URL", "http://127.0.0.1:9181/v1")

claude_code = ClaudeCodeProfile(
    name="claude-code-bridge",
    aliases=("claude-bridge", "claude-cli-bridge"),
    display_name="Claude Code CLI (bridge)",
    description="Claude via the local `claude` CLI with per-thread session resume.",
    env_vars=("CLAUDE_CODE_BRIDGE_URL",),
    base_url=os.getenv("CLAUDE_CODE_BRIDGE_URL", "http://127.0.0.1:9181/v1"),
    fallback_models=("sonnet", "opus", "haiku"),
    default_aux_model="haiku",
)

register_provider(claude_code)

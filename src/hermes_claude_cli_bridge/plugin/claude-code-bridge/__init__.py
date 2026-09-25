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
TEXT_RUN, TOOL_RUN, FLUSH_TICK = "\u2063", "\u2064", "\u2062"
_MARKERS = re.compile("([\u2062\u2063\u2064])")
_RUNS = "_claude_bridge_runs"  # per-call state on the agent: [[kind, text, chars already posted], ...]

# Hermes' own Mattermost split is 4000 chars at an arbitrary line/space with "(1/3)" tags; posts that long break
# up in the Mattermost view. Each message is posted pre-split instead, at paragraph > line > sentence > word
# boundaries, with fenced code blocks kept whole (or closed and reopened when one alone is over the limit).
CHUNK_CHARS = int(os.getenv("CLAUDE_CODE_CHUNK_CHARS") or 2000)
CHUNK_GAP = float(os.getenv("CLAUDE_CODE_CHUNK_GAP") or 0.5)  # Hermes posts interim messages fire-and-forget
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
    for chunk in (c for t in texts for c in _split(t, CHUNK_CHARS)) if cb else ():
        if posted and CHUNK_GAP > 0:
            time.sleep(CHUNK_GAP)
        try:
            cb(chunk, already_streamed=False)
            posted = True
        except Exception:
            pass
    return posted


def _pending_progress(agent) -> str:
    """Runs not yet shown. Text still streaming may be the answer, so it waits until the next run starts; at
    the end the last text run IS the answer and is left for the reply."""
    runs = getattr(agent, _RUNS, None) or []
    parts = []
    for i, run in enumerate(runs):
        if run[0] == "text" and i == len(runs) - 1:
            break
        parts.append(run[1][run[2]:].strip())
        run[2] = len(run[1])
    return "\n".join(p for p in parts if p)


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
            if piece in (TEXT_RUN, TOOL_RUN):
                if live and runs and runs[-1][1]:
                    try:
                        self.stream_delta_callback(None)
                    except Exception:
                        pass
                runs.append(["text" if piece == TEXT_RUN else "tool", "", 0])
            elif piece == FLUSH_TICK:
                if not live:
                    _post_progress(self)
            elif piece:
                if not runs:
                    runs.append(["text", "", 0])
                runs[-1][1] += piece
                if live:
                    fire(self, piece)

    def _interruptible_streaming_api_call(self, *args, **kwargs):
        setattr(self, _RUNS, None)
        try:
            resp = call(self, *args, **kwargs)
            runs = getattr(self, _RUNS, None)
            msg = resp.choices[0].message if runs is not None and getattr(resp, "choices", None) else None
            if msg is not None and isinstance(msg.content, str):
                msg.content = _MARKERS.sub("", msg.content)
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

    _fire_stream_delta._claude_bridge_segments = True
    AIAgent._fire_stream_delta = _fire_stream_delta
    AIAgent._interruptible_streaming_api_call = _interruptible_streaming_api_call
    return True


class ClaudeCodeProfile(ProviderProfile):
    def build_extra_body(self, *, session_id=None, **context):
        # installed here, not at import: run_agent may still be mid-import when plugins are discovered
        ext: dict = {"segments": True} if _install_segment_breaks() else {}
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

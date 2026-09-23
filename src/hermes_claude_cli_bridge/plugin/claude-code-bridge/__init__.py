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
"""

from __future__ import annotations

import os
import re

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


def _post_progress(agent) -> None:
    """Post runs not yet shown as one interim message. Text still streaming may be the answer, so it waits
    until the next run starts; at the end the last text run IS the answer and is left for the reply."""
    runs = getattr(agent, _RUNS, None) or []
    cb = getattr(agent, "interim_assistant_callback", None)
    parts = []
    for i, run in enumerate(runs):
        if run[0] == "text" and i == len(runs) - 1:
            break
        parts.append(run[1][run[2]:].strip())
        run[2] = len(run[1])
    text = "\n".join(p for p in parts if p)
    if text and cb:
        try:
            cb(text, already_streamed=False)
        except Exception:
            pass


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
                    _post_progress(self)
                    msg.content = answer
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

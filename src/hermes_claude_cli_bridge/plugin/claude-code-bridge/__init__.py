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

from providers import register_provider
from providers.base import ProviderProfile


# The bridge streams a whole Claude Code turn (text, tool calls, more text) as ONE completion, so Hermes would
# post it all as one chat message. Hermes starts a new message when its stream callback gets None (what its own
# tool loop does between API calls); the bridge marks those points with SEGMENT_BREAK, and this patch converts
# them. Only the chat display callback gets the None: TTS reads None as end-of-stream.
SEGMENT_BREAK = "\u2063"


def _install_segment_breaks() -> bool:
    try:
        from run_agent import AIAgent
    except Exception:
        return False
    fire = AIAgent._fire_stream_delta
    if getattr(fire, "_claude_bridge_segments", False):
        return True

    def _fire_stream_delta(self, text):
        if not isinstance(text, str) or SEGMENT_BREAK not in text:
            return fire(self, text)
        for i, piece in enumerate(text.split(SEGMENT_BREAK)):
            if i and self.stream_delta_callback:
                try:
                    self.stream_delta_callback(None)
                except Exception:
                    pass
            if piece:
                fire(self, piece)

    _fire_stream_delta._claude_bridge_segments = True
    AIAgent._fire_stream_delta = _fire_stream_delta
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

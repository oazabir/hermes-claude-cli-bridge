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


class ClaudeCodeProfile(ProviderProfile):
    def build_extra_body(self, *, session_id=None, **context):
        ext: dict = {}
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

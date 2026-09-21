"""Hermes -> Claude Code CLI bridge. See README.md."""
from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("hermes-claude-cli-bridge")
except _metadata.PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0+source"

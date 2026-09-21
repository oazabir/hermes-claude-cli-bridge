# hermes-claude-cli-bridge

Use your local **Claude Code CLI** as a model provider for **[Hermes Agent](https://github.com/NousResearch/hermes-agent)**.

Hermes talks to a tiny local server (the *bridge*) as if it were any OpenAI-compatible endpoint. The bridge answers each request by running `claude -p` as a subprocess, and keeps **one resumable Claude Code session per Hermes thread**, so a Hermes conversation (CLI, Telegram, Discord, Slack, Mattermost, ...) keeps its full Claude Code context: files it read, tools it ran, its own memory.

```
hermes (CLI or messaging gateway)
   │  provider "claude-code-bridge"  (OpenAI /v1/chat/completions + the Hermes session id)
   ▼
bridge/claude_hermes_bridge.py        127.0.0.1:9181, Python standard library only, no dependencies
   │  Hermes session id ─► uuid5 ─► claude -p --session-id <uuid>   first turn of a thread
   │                                claude -p --resume     <uuid>   later turns (sends only the new message)
   ▼
claude subprocess  (--dangerously-skip-permissions, --add-dir, --append-system-prompt, --autocompact, --effort)
```

What you get:

- Claude Code answers Hermes chats, with streaming. Its tool calls show up in the chat as one-line headlines (`🔧 Bash: list the files`).
- One Claude Code session per Hermes thread, resumed on every message. If either side loses state the bridge repairs itself.
- Extra directories (`--add-dir`), extra system prompt (text or files), `--autocompact`, `--effort`, model choice.
- On messaging platforms Claude is told which platform, channel and user the message came from.

> **Read the [Security](#security) section before you enable this.** The bridge runs Claude Code with `--dangerously-skip-permissions`.

## Requirements

| | |
|---|---|
| Hermes Agent | Tested with 0.16 and 0.21 (needs model-provider plugin support: `~/.hermes/plugins/model-providers/`) |
| Claude Code CLI | `claude` on `PATH`, logged in (run `claude` once and sign in, or set `ANTHROPIC_API_KEY`). Tested with 2.1.x. Your Claude subscription / API account pays for the usage. |
| Python | 3.9 or newer. **No `pip install` needed.** |
| OS | macOS or Linux (Windows: use WSL) |

## Quick start (5 minutes)

```bash
# 1. get the code
git clone https://github.com/oazabir/hermes-claude-cli-bridge.git
cd hermes-claude-cli-bridge

# 2. install the Hermes plugin (and tell the gateway where the bridge is)
./deploy/install.sh

# 3. start the bridge (leave it running; see "Run it as a service" to make it permanent)
python3 bridge/claude_hermes_bridge.py --model sonnet

# 4. in another terminal: check it, then chat
curl -s http://127.0.0.1:9181/health          # {"ok": true, ...}
hermes chat --provider claude-code-bridge -m sonnet
```

That's it. The provider is named **`claude-code-bridge`** (aliases `claude-bridge`, `claude-cli-bridge`). It is *not* called `claude` or `claude-code`, because Hermes reserves those names for the Anthropic API provider.

`install.sh` does two things and is safe to run again:

1. links `plugin/claude-code-bridge` into `$HERMES_HOME/plugins/model-providers/` (`HERMES_HOME` defaults to `~/.hermes`);
2. adds `CLAUDE_CODE_BRIDGE_URL=http://127.0.0.1:9181/v1` to `$HERMES_HOME/.env`.

Step 2 matters for the **messaging gateway**: it reads provider settings from `.env` (not from the shell or the service environment). Without that line the gateway cannot resolve the provider and quietly falls back to your other model.

### Manual install (without the script)

```bash
mkdir -p ~/.hermes/plugins/model-providers
ln -sfn "$PWD/plugin/claude-code-bridge" ~/.hermes/plugins/model-providers/claude-code-bridge
echo 'CLAUDE_CODE_BRIDGE_URL=http://127.0.0.1:9181/v1' >> ~/.hermes/.env
```

## Use it

**One-off / per session**

```bash
hermes chat --provider claude-code-bridge -m sonnet                 # interactive
hermes chat --provider claude-code-bridge -m sonnet -q "your question"
hermes chat --resume <session-id>   ...                             # continues the same Claude session
```

**As the default model** — edit `~/.hermes/config.yaml`:

```yaml
model:
  provider: claude-code-bridge
  default: sonnet          # or opus / haiku, or a full model id such as claude-sonnet-4-5
  context_length: 200000

# Recommended: if the bridge is down or Claude hits a limit, Hermes switches to another provider
fallback_providers:
  - provider: openrouter          # any provider you have configured
    model: anthropic/claude-sonnet-4
```

Then restart the gateway if you use one (`hermes gateway restart`, or `systemctl --user restart hermes-gateway`).

### Important: pin Hermes' background tasks to a different provider

Hermes runs small helper calls (titles, vision, summaries, approvals, web extraction, session search, ...). Each has a `provider: auto` setting, which means *"use whatever the main model is"*. If the main model is Claude Code, every one of those goes through a full Claude Code run: it takes 15–20 s, uses your Claude quota, and Claude answers it like an agent task instead of doing the small job. So set them to a fast API model you already use. Vision in particular **cannot** work through the bridge.

```yaml
auxiliary:
  vision:           {provider: openrouter, model: google/gemini-2.5-flash}
  compression:      {provider: openrouter, model: google/gemini-2.5-flash}
  title_generation: {enabled: false}            # or a provider/model like the others
  approval:         {provider: openrouter, model: google/gemini-2.5-flash}
  web_extract:      {provider: openrouter, model: google/gemini-2.5-flash}
  session_search:   {provider: openrouter, model: google/gemini-2.5-flash}
  # ...every entry under `auxiliary:` whose provider is `auto` (names differ between Hermes versions)
```

Find them with: `grep -n -B1 "provider: auto" ~/.hermes/config.yaml`.

## Run it as a service

The bridge must be running whenever Hermes uses it. Let the installer create a background service:

```bash
# Linux (systemd user service) or macOS (launchd agent), auto-detected
./deploy/install.sh --service

# with bridge options: everything after `--` is passed to the bridge
./deploy/install.sh --service -- --model sonnet --effort medium --add-dir ~/code --autocompact 200k
```

- **Linux:** writes `~/.config/systemd/user/claude-bridge.service`. Manage it with `systemctl --user status|restart|stop claude-bridge` and read logs with `journalctl --user -u claude-bridge -f`. Run `loginctl enable-linger $USER` so it survives logout/reboot.
- **macOS:** writes `~/Library/LaunchAgents/io.github.hermes-claude-cli-bridge.plist`; log at `~/.hermes/claude-bridge/bridge.log`. Restart with `launchctl kickstart -k gui/$(id -u)/io.github.hermes-claude-cli-bridge`.
- To preview without installing: `./deploy/install.sh --print-service [--os linux|macos] -- <bridge options>`.
- To change options later, re-run the installer with new options after `--`, then restart the service.
- The service is given the absolute path of your `claude` binary, so it works even though services have a minimal `PATH`. The `claude` login is per user, so run the service as the same user that ran `claude` and logged in.

## Configuration

Every option is a flag **or** an environment variable (flag wins).

| Flag | Env | Effect |
|---|---|---|
| `--host` / `--port` | `CLAUDE_BRIDGE_HOST` / `CLAUDE_BRIDGE_PORT` | listen address, default `127.0.0.1:9181`. Keep it on localhost. |
| `--claude-bin` | `CLAUDE_BIN` | path to the `claude` executable (default: `claude` from `PATH`) |
| `--model` | `CLAUDE_BRIDGE_MODEL` | model used when Hermes sends a name the bridge does not know (default `sonnet`). Requests naming `sonnet`, `opus`, `haiku` or any `claude-*` id are passed through. |
| `--effort low\|medium\|high\|xhigh\|max` | `CLAUDE_BRIDGE_EFFORT` | `claude --effort` (default `medium`; empty = Claude's default) |
| `--add-dir DIR` (repeatable) | `CLAUDE_BRIDGE_ADD_DIRS` (`:`-separated) | `claude --add-dir`: extra directories Claude may read/edit |
| `--append-system-prompt TEXT` | `CLAUDE_BRIDGE_APPEND_SYSTEM_PROMPT` | text appended to Claude's system prompt |
| `--append-system-prompt-file FILE` (repeatable) | `CLAUDE_BRIDGE_APPEND_SYSTEM_PROMPT_FILE` (`:`-separated) | files appended to the system prompt, in order. Re-read on every request, so edits apply immediately. An unreadable file makes the request fail (HTTP 502) instead of being silently ignored. |
| `--autocompact auto\|100k-1M` | `CLAUDE_BRIDGE_AUTOCOMPACT` | `claude --autocompact`: when Claude compacts its own context |
| `--cwd DIR` | `CLAUDE_BRIDGE_CWD` | working directory of `claude` (default `$HERMES_HOME/claude-bridge/workspace`). Claude stores sessions **per directory**, so keep it stable. |
| `--state-file FILE` | `CLAUDE_BRIDGE_STATE` | Hermes-session → Claude-session bookkeeping (default `$HERMES_HOME/claude-bridge/sessions.json`) |
| `--timeout SECONDS` | `CLAUDE_BRIDGE_TIMEOUT` | kill a Claude run after this long (default 900) |
| `--tool-events content\|reasoning\|off` | `CLAUDE_BRIDGE_TOOL_EVENTS` | how Claude's tool calls are shown: as `🔧 …` lines in the reply (default), as reasoning text (only visible if Hermes shows reasoning), or not at all. Streaming only. |
| `--no-session-context` | `CLAUDE_BRIDGE_SESSION_CONTEXT=0` | do not tell Claude the platform/channel/user (see below) |
| `--keep-memory-context` | `CLAUDE_BRIDGE_KEEP_MEMORY_CONTEXT=1` | keep the `<memory-context>` block Hermes appends to messages (see below) |
| `--forward-system` | `CLAUDE_BRIDGE_FORWARD_SYSTEM=1` | also append Hermes' whole system prompt. Off by default: it is very long and describes Hermes-only tools Claude does not have. |
| `--extra-args "..."` | `CLAUDE_BRIDGE_EXTRA_ARGS` | raw extra `claude` flags (shell-split), e.g. `--setting-sources user` |
| — | `HERMES_HOME` | Hermes data directory (default `~/.hermes`) |

Always passed to `claude`: `-p --output-format stream-json --verbose --include-partial-messages --dangerously-skip-permissions`.

**Per Hermes process** you can also set `CLAUDE_CODE_ADD_DIRS`, `CLAUDE_CODE_APPEND_SYSTEM_PROMPT`, `CLAUDE_CODE_AUTOCOMPACT`, `CLAUDE_CODE_EFFORT`, `CLAUDE_CODE_CWD` in Hermes' environment (they are merged with the bridge's values), and `CLAUDE_CODE_BRIDGE_URL` to point Hermes at a bridge on another port/host.

### Example: a coding assistant with project access and house rules

```bash
python3 bridge/claude_hermes_bridge.py \
  --model sonnet --effort medium \
  --add-dir ~/code/myapp --add-dir ~/code/infra \
  --append-system-prompt-file ~/hermes-rules.md \
  --autocompact 200k
```

## How it behaves

**Sessions.** The Hermes session id is mapped to a fixed UUID. Turn 1 of a thread runs `claude --session-id <uuid>`; later turns run `claude --resume <uuid>` with only the messages since Claude's last reply, because Claude Code already has the history. Threads are independent of each other. Requests with no session id are one-shot (`--no-session-persistence`).

**Self-healing.** The state file is only a hint. If it is deleted, or Claude reports `No conversation found` / `already in use`, the bridge switches between `--session-id` and `--resume` and retries once. A thread that first reaches the bridge mid-conversation (for example, you switched provider) gets the earlier turns once as a transcript.

**Tool calls in the chat.** While Claude works, each tool call is streamed to Hermes as a headline, e.g. `🔧 Bash: Count the .md files`, `🔧 Read: ~/code/app/main.py`. If Claude starts a command with a `# why` comment, that comment is used. Sub-agent calls are indented with `↳`.

**Messaging gateways (Telegram, Discord, Slack, Mattermost, ...).**
- Claude is told *where the message came from*: the bridge lifts the gateway's "Current Session Context" block (platform, channel, thread, user) out of Hermes' system prompt and appends just that to Claude's system prompt. In shared channels Hermes prefixes each message with `[sender name]`, which Claude sees too.
- Hermes appends a `<memory-context>` block (its own memory recall, cut down to a head/tail digest) to user messages. Claude Code normally has its own memory, so the bridge removes that block by default (`--keep-memory-context` keeps it).
- File attachments arrive as a path in the message; Claude opens the file itself.

**Claude's own setup still applies.** `claude -p` loads your normal Claude Code configuration: `CLAUDE.md`, skills, hooks, MCP servers, plugins. That is often what you want (Claude keeps its memory and tools), but it also adds tokens and start-up time to every turn. To slim it down, use `--extra-args "--setting-sources user"` (or `local`), or run the bridge as a different OS user with its own minimal Claude config.

## Security

- `--dangerously-skip-permissions` means Claude can run **any command as the user running the bridge**, with no confirmation, for **anyone who can send it a chat message**. On the CLI that is you. On a **messaging gateway** it is everyone the gateway accepts messages from. Restrict who can talk to your bot (Hermes allow-lists), use a dedicated low-privilege OS user or container for the bridge, and keep secrets out of that user's reach.
- Hermes' own tool-approval prompts do not apply: Claude uses its own tools, not Hermes'.
- The bridge has **no authentication** and listens on `127.0.0.1` only. Do not bind it to a public interface or forward the port. Any local process can use it.
- Prompt injection: anything Claude reads (web pages, files, chat text) can try to instruct it. Combined with no permission prompts, treat the bridge user as exposed.

## Limitations

- Hermes' own tools and skills are not used with this provider; Claude Code uses its own (Bash, Read, Edit, its skills, its MCP servers). Hermes features that need Hermes tools (scheduling jobs from chat, `send_message`, delegation, Hermes skills) do not fire.
- No vision through the bridge (see the auxiliary-task note above). Text and document attachments work; image support is untested.
- Each turn is a real Claude Code run: expect a few seconds of start-up plus model time, and normal Claude usage/quota.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No usable credentials found for provider 'claude-code-bridge'. Set CLAUDE_CODE_BRIDGE_URL.` | The variable is missing from `$HERMES_HOME/.env` (the gateway reads only that file). Run `./deploy/install.sh`, then restart the gateway. |
| Hermes says the provider is unknown | The plugin link is missing or Hermes is too old. Check `ls -l ~/.hermes/plugins/model-providers/claude-code-bridge` and re-run `./deploy/install.sh`. |
| `--provider claude` / `claude-code` uses the Anthropic API | Those names are reserved by Hermes. Use `claude-code-bridge`. |
| `Connection refused` / `request timeout` and Hermes falls back | The bridge is not running or on another port. `curl http://127.0.0.1:9181/health`; check the service status and logs. |
| `claude: command not found` in the bridge log | A service has a minimal `PATH`. Re-run `./deploy/install.sh --service` (it records the full path), or pass `--claude-bin /full/path/claude`. |
| `claude error: … not logged in` | Run `claude` once as the **same user** that runs the bridge and sign in. |
| HTTP 502 `cannot read --append-system-prompt-file …` | A prompt file path is wrong or unreadable (by design this fails loudly). |
| Reply is slow (15–30 s) for tiny requests | A Hermes background task is going through Claude. Pin the `auxiliary:` tasks (see above). Also consider `--extra-args "--setting-sources user"`. |
| The model answers `No conversation found` repeatedly | You changed `--cwd`. Claude stores sessions per directory; restore it or delete `sessions.json` to start fresh threads. |
| `POST /api/show 404`, `/api/tags`, `/props` in the bridge log | Harmless: Hermes probes for other server types (Ollama, llama.cpp) first. The bridge only serves `/health`, `/v1/models` and `/v1/chat/completions`. |
| Port already in use | Another bridge is running (`lsof -i :9181`), or choose `--port` and re-run the installer with the same `--port`. |
| Want to see exactly what Claude was sent | The bridge only logs HTTP requests. Claude keeps every session's transcript under `~/.claude/projects/<cwd>/<uuid>.jsonl`, including a `prompt_snapshot` entry with the full system prompt it received and the user messages as they arrived. |

## Tests

```bash
python3 tests/test_bridge.py    # offline: uses a fake `claude`, no login, no cost, ~1 s
python3 tests/e2e_bridge.py     # real `claude` (haiku): continuity, isolation, add-dir, system prompt, streaming, tools; costs a few cents
```

## Uninstall

```bash
./deploy/install.sh --uninstall     # removes the plugin link, the .env line, and the service if installed
```

Then set Hermes' `model.provider` / `fallback_providers` / `auxiliary` entries back to your other provider.

## Layout

```
bridge/claude_hermes_bridge.py     the bridge (single file, stdlib only)
plugin/claude-code-bridge/         Hermes model-provider plugin (passes the Hermes session id to the bridge)
deploy/install.sh                  plugin + gateway env + optional systemd/launchd service; --uninstall
tests/test_bridge.py, fake_claude.py   offline tests
tests/e2e_bridge.py                tests against the real claude CLI
```

## API surface (for the curious)

`GET /health`, `GET /v1/models`, `POST /v1/chat/completions` (streaming SSE and non-streaming). The Hermes session id arrives as `claude_bridge.session_id` in the request body; optional per-request `claude_bridge.add_dirs`, `append_system_prompt`, `autocompact`, `effort`, `cwd` are honoured too.

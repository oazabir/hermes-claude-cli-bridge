#!/bin/sh
# Install the Hermes provider plugin (and optionally run the bridge as a background service).
#
#   ./deploy/install.sh                              plugin + gateway env line only; you start the bridge yourself
#   ./deploy/install.sh --service                    also install + start a systemd user service (Linux) / launchd agent (macOS)
#   ./deploy/install.sh --service -- --add-dir ~/code --model opus     everything after `--` goes to the bridge
#   ./deploy/install.sh --print-service [--os linux|macos]             just print the service definition
#   ./deploy/install.sh --uninstall
#
# Options: --port N (default 9181)   --no-start (write the service but do not start it)
# Env:     HERMES_HOME (default ~/.hermes)
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PORT=9181
SERVICE=0
UNINSTALL=0
PRINT=0
START=1
OS=""
BRIDGE_ARGS_FILE="$(mktemp)"
trap 'rm -f "$BRIDGE_ARGS_FILE"' EXIT

while [ $# -gt 0 ]; do
  case "$1" in
    --service) SERVICE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --print-service) PRINT=1 ;;
    --no-start) START=0 ;;
    --port) PORT="$2"; shift ;;
    --os) OS="$2"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    --) shift; for a in "$@"; do printf '%s\n' "$a" >> "$BRIDGE_ARGS_FILE"; done; break ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$OS" ]; then
  case "$(uname -s)" in Darwin) OS=macos ;; *) OS=linux ;; esac
fi
PLUGIN_DIR="$HERMES_HOME/plugins/model-providers"
PLUGIN_LINK="$PLUGIN_DIR/claude-code-bridge"
ENV_FILE="$HERMES_HOME/.env"
UNIT="$HOME/.config/systemd/user/claude-bridge.service"
PLIST="$HOME/Library/LaunchAgents/io.github.hermes-claude-cli-bridge.plist"
LABEL="io.github.hermes-claude-cli-bridge"

PYTHON="$(command -v python3 || true)"
CLAUDE="$(command -v claude || true)"

# --- service definition -------------------------------------------------------------------------------------
bridge_argv() {  # one argument per line: interpreter, script, fixed flags, user flags
  printf '%s\n' "${PYTHON:-python3}" "$REPO/bridge/claude_hermes_bridge.py" --host 127.0.0.1 --port "$PORT"
  if [ -n "$CLAUDE" ] && ! grep -qx -- '--claude-bin' "$BRIDGE_ARGS_FILE"; then printf '%s\n' --claude-bin "$CLAUDE"; fi
  cat "$BRIDGE_ARGS_FILE"
}

service_path() {
  p="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
  [ -n "$CLAUDE" ] && p="$(dirname "$CLAUDE"):$p"
  printf '%s' "$p"
}

render_systemd() {
  cat <<EOF
[Unit]
Description=Hermes -> Claude Code CLI bridge (127.0.0.1:$PORT)
After=network.target

[Service]
Environment=PATH=$(service_path)
Environment=HERMES_HOME=$HERMES_HOME
ExecStart=$(bridge_argv | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/%/%%/g' -e 's/^/"/' -e 's/$/"/' | tr '\n' ' ')
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
}

xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

render_launchd() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
$(bridge_argv | xml | sed -e 's/^/    <string>/' -e 's/$/<\/string>/')
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(service_path | xml)</string>
    <key>HERMES_HOME</key><string>$(printf '%s' "$HERMES_HOME" | xml)</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HERMES_HOME/claude-bridge/bridge.log</string>
  <key>StandardErrorPath</key><string>$HERMES_HOME/claude-bridge/bridge.log</string>
</dict></plist>
EOF
}

if [ "$PRINT" = 1 ]; then
  if [ "$OS" = macos ]; then render_launchd; else render_systemd; fi
  exit 0
fi

# --- uninstall -----------------------------------------------------------------------------------------------
if [ "$UNINSTALL" = 1 ]; then
  if [ "$OS" = linux ] && [ -f "$UNIT" ]; then
    systemctl --user disable --now claude-bridge.service 2>/dev/null || true
    rm -f "$UNIT"; systemctl --user daemon-reload 2>/dev/null || true
    echo "removed $UNIT"
  fi
  if [ "$OS" = macos ] && [ -f "$PLIST" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"; echo "removed $PLIST"
  fi
  [ -L "$PLUGIN_LINK" ] && rm -f "$PLUGIN_LINK" && echo "removed $PLUGIN_LINK"
  if [ -f "$ENV_FILE" ] && grep -q '^CLAUDE_CODE_BRIDGE_URL=' "$ENV_FILE"; then
    grep -v '^CLAUDE_CODE_BRIDGE_URL=' "$ENV_FILE" > "$ENV_FILE.tmp" && cat "$ENV_FILE.tmp" > "$ENV_FILE" && rm -f "$ENV_FILE.tmp"
    echo "removed CLAUDE_CODE_BRIDGE_URL from $ENV_FILE"
  fi
  echo "Done. If Hermes' config.yaml still names provider claude-code-bridge, switch it back."
  exit 0
fi

# --- preflight ------------------------------------------------------------------------------------------------
[ -n "$PYTHON" ] || { echo "python3 not found (3.9+ required)" >&2; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "python3 3.9+ required" >&2; exit 1; }
[ -n "$CLAUDE" ] || echo "WARNING: 'claude' not found on PATH. Install Claude Code and run 'claude' once to log in: https://docs.claude.com/en/docs/claude-code" >&2
command -v hermes >/dev/null 2>&1 || echo "WARNING: 'hermes' not found on PATH (installing the plugin anyway into $HERMES_HOME)." >&2

# --- plugin + gateway env -------------------------------------------------------------------------------------
mkdir -p "$PLUGIN_DIR"
ln -sfn "$REPO/plugin/claude-code-bridge" "$PLUGIN_LINK"
echo "plugin: $PLUGIN_LINK -> $REPO/plugin/claude-code-bridge"

# The Hermes gateway reads provider settings from $HERMES_HOME/.env (not the shell/service environment),
# so without this line the gateway cannot find the provider and silently falls back to another one.
URL="http://127.0.0.1:$PORT/v1"
touch "$ENV_FILE"; chmod 600 "$ENV_FILE" 2>/dev/null || true
if grep -q '^CLAUDE_CODE_BRIDGE_URL=' "$ENV_FILE"; then
  echo "env:    CLAUDE_CODE_BRIDGE_URL already set in $ENV_FILE (left as is)"
else
  [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >> "$ENV_FILE"
  printf 'CLAUDE_CODE_BRIDGE_URL=%s\n' "$URL" >> "$ENV_FILE"
  echo "env:    added CLAUDE_CODE_BRIDGE_URL=$URL to $ENV_FILE"
fi

# --- service --------------------------------------------------------------------------------------------------
if [ "$SERVICE" = 1 ]; then
  mkdir -p "$HERMES_HOME/claude-bridge"
  if [ "$OS" = macos ]; then
    mkdir -p "$(dirname "$PLIST")"; render_launchd > "$PLIST"; echo "service: wrote $PLIST"
    if [ "$START" = 1 ]; then
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "service: started ($LABEL); log: $HERMES_HOME/claude-bridge/bridge.log"
    fi
  else
    mkdir -p "$(dirname "$UNIT")"; render_systemd > "$UNIT"; echo "service: wrote $UNIT"
    if [ "$START" = 1 ]; then
      systemctl --user daemon-reload
      systemctl --user enable --now claude-bridge.service && echo "service: started (systemctl --user status claude-bridge)"
      echo "tip:     run 'loginctl enable-linger $USER' so the service keeps running after you log out"
    fi
  fi
else
  echo "bridge:  not installed as a service. Start it yourself:  $PYTHON $REPO/bridge/claude_hermes_bridge.py --port $PORT"
fi

cat <<EOF

Next: point Hermes at it (see README, "Use it"):
  hermes chat --provider claude-code-bridge -m sonnet
EOF

"""hermes-claude-cli-bridge command line: serve | install | uninstall | print-service | doctor."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from importlib import metadata
from pathlib import Path

from . import __version__

DIST = "hermes-claude-cli-bridge"
PLUGIN = "claude-code-bridge"
ENV_KEY = "CLAUDE_CODE_BRIDGE_URL"
LABEL = "io.github.hermes-claude-cli-bridge"
UNIT_NAME = "claude-bridge.service"
DEFAULT_PORT = 9181

HELP = f"""\
{DIST} {__version__}: use the Claude Code CLI as a Hermes Agent model provider.

usage: {DIST} <command> [options]

commands:
  serve           run the bridge in the foreground (default when no command is given; `serve --help` lists its options)
  install         install the Hermes provider plugin and the gateway env line; --service also installs a background service
  uninstall       remove what `install` added
  print-service   print the systemd unit / launchd plist that `install --service` would write
  doctor          check that everything is in place
  version         print the version

Everything after `--` on install/print-service is passed to the bridge, e.g.
  {DIST} install --service -- --model sonnet --add-dir ~/code
"""


# ------------------------------------------------------------------------------------------- paths / helpers


def hermes_home(override: str | None = None) -> Path:
    return Path(override or os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def plugin_source() -> Path:
    return Path(__file__).resolve().parent / "plugin" / PLUGIN


def plugin_dest(home: Path) -> Path:
    return home / "plugins" / "model-providers" / PLUGIN


def detect_os(override: str | None = None) -> str:
    return override or ("macos" if sys.platform == "darwin" else "linux")


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _split(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def _say(msg: str) -> None:
    print(msg, flush=True)


def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


def _try(cmd: list[str], quiet: bool = True) -> int:
    """Run a service-manager command; 127 if the tool is missing (no systemd in a container, WSL1, ...)."""
    try:
        return subprocess.run(cmd, capture_output=quiet).returncode
    except OSError:
        return 127


# ------------------------------------------------------------------------------------- .env / plugin files


def ensure_env_line(env_file: Path, key: str, value: str) -> bool:
    """Append KEY=value unless KEY is already set. The Hermes gateway reads provider settings from this file (not
    from the shell or the service environment), so without it the gateway cannot find the provider. True if added."""
    env_file.parent.mkdir(parents=True, exist_ok=True)
    text = env_file.read_text() if env_file.exists() else ""
    if any(line.startswith(key + "=") for line in text.splitlines()):
        return False
    if not env_file.exists():
        os.close(os.open(env_file, os.O_CREAT | os.O_WRONLY, 0o600))
    with open(env_file, "a") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(f"{key}={value}\n")
    return True


def remove_env_line(env_file: Path, key: str) -> bool:
    if not env_file.exists():
        return False
    lines = env_file.read_text().splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(key + "=")]
    if len(kept) == len(lines):
        return False
    env_file.write_text("".join(kept))
    return True


def install_plugin(home: Path) -> Path:
    """Copy (not link) the plugin: a uvx/uv-tool environment can be replaced or garbage-collected at any time."""
    dest = plugin_dest(home)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(plugin_source(), dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


def _is_our_plugin(dest: Path) -> bool:
    try:
        return "claude-code-bridge-provider" in (dest / "plugin.yaml").read_text()
    except OSError:
        return False


# ---------------------------------------------------------------------------------- persistent command (uv)


def _ephemeral() -> bool:
    """True when running from a throw-away uvx environment (its Python prefix lives in uv's cache)."""
    p = Path(sys.prefix).as_posix()
    return "/archive-v" in p or "/environments-v" in p


def _own_spec() -> str:
    """What `uv tool install` should install to get this same package (git URL, local path, or PyPI pin)."""
    override = os.environ.get("HERMES_CLAUDE_CLI_BRIDGE_SPEC")
    if override:
        return override
    try:
        raw = metadata.distribution(DIST).read_text("direct_url.json")
        if raw:
            info = json.loads(raw)
            url = info.get("url", "")
            if "vcs_info" in info:
                commit = info["vcs_info"].get("commit_id")
                return f"git+{url}" + (f"@{commit}" if commit else "")
            if url.startswith("file://"):
                return urllib.parse.unquote(urllib.parse.urlparse(url).path)
            if url:
                return url
    except Exception:
        pass
    return f"{DIST}=={__version__}"


def persistent_command(override: str | None = None) -> str:
    """Absolute path of a permanent `hermes-claude-cli-bridge` executable for the service to run. If the tool
    is not installed (for example this is a one-off `uvx` run) it is installed with `uv tool install`."""
    if override:
        return override
    found = shutil.which(DIST)
    if found and not _ephemeral():
        return found
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("A background service needs a permanent install, which needs uv: https://docs.astral.sh/uv/getting-started/installation/\n"
                         f"Then run: uv tool install <this package>   (or pass --command /path/to/{DIST})")
    spec = _own_spec()
    _say(f"installing {spec} permanently with: uv tool install --force")
    subprocess.run([uv, "tool", "install", "--force", spec], check=True)
    bindir = subprocess.run([uv, "tool", "dir", "--bin"], capture_output=True, text=True, check=True).stdout.strip()
    exe = Path(bindir) / DIST
    if not exe.exists():
        raise SystemExit(f"uv tool install finished but {exe} does not exist; add uv's tool bin dir to PATH and retry")
    return str(exe)


# ------------------------------------------------------------------------------------------ service files


def service_argv(command: str, port: int, extra: list[str]) -> list[str]:
    argv = [command, "serve", "--host", "127.0.0.1", "--port", str(port)]
    claude = shutil.which("claude")
    if claude and "--claude-bin" not in extra:
        argv += ["--claude-bin", claude]
    return argv + extra


def service_path(*extra_dirs: str) -> str:
    """PATH for the service: services start with almost nothing, but `claude` (a node script) needs node etc."""
    dirs: list[str] = []
    for tool in ("claude", "node", "uv"):
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    dirs += list(extra_dirs) + os.environ.get("PATH", "").split(os.pathsep)
    dirs += [str(Path.home() / ".local" / "bin"), "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]
    seen: list[str] = []
    for d in dirs:
        if d and d not in seen and Path(d).is_dir():
            seen.append(d)
    return os.pathsep.join(seen)


def _systemd_quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def render_systemd(argv: list[str], port: int, home: Path) -> str:
    return "\n".join([
        "[Unit]",
        f"Description=Hermes -> Claude Code CLI bridge (127.0.0.1:{port})",
        "After=network.target",
        "",
        "[Service]",
        f"Environment=PATH={service_path(str(Path(argv[0]).parent))}",
        f"Environment=HERMES_HOME={home}",
        "ExecStart=" + " ".join(_systemd_quote(a) for a in argv),
        "Restart=on-failure",
        "RestartSec=10",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def render_launchd(argv: list[str], home: Path) -> str:
    log = str(home / "claude-bridge" / "bridge.log")
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": argv,
        "EnvironmentVariables": {"PATH": service_path(str(Path(argv[0]).parent)), "HERMES_HOME": str(home)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }).decode()


def write_and_start_service(target: str, argv: list[str], port: int, home: Path, start: bool) -> None:
    (home / "claude-bridge").mkdir(parents=True, exist_ok=True)
    if target == "macos":
        path = plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_launchd(argv, home))
        _say(f"service: wrote {path}")
        if start:
            domain = f"gui/{os.getuid()}"
            _try(["launchctl", "bootout", f"{domain}/{LABEL}"])
            rc = _try(["launchctl", "bootstrap", domain, str(path)], quiet=False)
            if rc == 0:
                _say(f"service: started ({LABEL}); log: {home / 'claude-bridge' / 'bridge.log'}")
            else:
                _warn(f"launchctl bootstrap failed (exit {rc}); try: launchctl bootstrap {domain} {path}")
    else:
        path = unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_systemd(argv, port, home))
        _say(f"service: wrote {path}")
        if start:
            _try(["systemctl", "--user", "daemon-reload"])
            rc = _try(["systemctl", "--user", "enable", "--now", UNIT_NAME], quiet=False)
            if rc == 0:
                _say("service: started (systemctl --user status claude-bridge)")
                _say("tip:     run `loginctl enable-linger $USER` so the service survives logout and reboot")
            else:
                _warn(f"systemctl enable --now failed (exit {rc}); the unit is written, start it once systemd --user is available")


# --------------------------------------------------------------------------------------------- commands


def _install_parser(prog: str, service_flags: bool) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"{DIST} {prog}", description=HELP.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"bridge port (default {DEFAULT_PORT})")
    ap.add_argument("--hermes-home", help="Hermes data directory (default: $HERMES_HOME or ~/.hermes)")
    ap.add_argument("--os", choices=["linux", "macos"], help="service flavour (default: auto-detect)")
    ap.add_argument("--command", help=f"absolute path of the {DIST} executable the service should run (default: auto)")
    if service_flags:
        ap.add_argument("--prompts", metavar="NAMES",
                        help="bundled prompt files to enable: comma list of agents, self-learn, subagents, or all / none "
                             "(default: ask when run in a terminal, else none)")
        ap.add_argument("--service", action="store_true", help="also install and start a background service (systemd user unit / launchd agent)")
        ap.add_argument("--no-start", action="store_true", help="write the service file but do not start it")
    return ap


PROMPT_QUESTION = ("Enable the bundled prompts (chat and safety rules, self-learning skills, subagent routing)? "
                   "They are appended to every Claude turn; see README 'Bundled prompts'. [y/N] ")


def choose_prompts(choice: str | None, extra: list[str]) -> list[str]:
    """--prompt names for the bridge: from --prompts, else one question at a terminal (default no), else none.
    Flags already passed after `--` win: the operator chose."""
    if any(x == "--prompt" or x.startswith("--prompt=") for x in extra):
        return []
    if choice is not None:
        names = [n.strip() for n in choice.split(",") if n.strip() and n.strip() != "none"]
        from .bridge import resolve_prompts
        resolve_prompts(names)  # ValueError on an unknown name
        return names
    if not sys.stdin.isatty():
        return []
    try:
        return ["all"] if input(PROMPT_QUESTION).strip().lower() in ("y", "yes") else []
    except EOFError:
        return []


def cmd_install(argv: list[str]) -> int:
    own, extra = _split(argv)
    ap = _install_parser("install", True)
    a = ap.parse_args(own)
    try:
        extra = extra + [x for n in choose_prompts(a.prompts, extra) for x in ("--prompt", n)]
    except ValueError as e:
        ap.error(str(e))
    home = hermes_home(a.hermes_home)
    if not shutil.which("claude"):
        _warn("'claude' not found on PATH. Install Claude Code and run `claude` once to log in: https://docs.anthropic.com/en/docs/claude-code")
    if not shutil.which("hermes"):
        _warn(f"'hermes' not found on PATH (installing the plugin anyway into {home}).")
    dest = install_plugin(home)
    _say(f"plugin: copied to {dest}")
    url = f"http://127.0.0.1:{a.port}/v1"
    if ensure_env_line(home / ".env", ENV_KEY, url):
        _say(f"env:    added {ENV_KEY}={url} to {home / '.env'}")
    else:
        _say(f"env:    {ENV_KEY} already set in {home / '.env'} (left as is)")
    if a.service:
        command = persistent_command(a.command)
        write_and_start_service(detect_os(a.os), service_argv(command, a.port, extra), a.port, home, not a.no_start)
    else:
        run_cmd = f"{DIST} serve" if shutil.which(DIST) and not _ephemeral() else f"uvx --from {_own_spec()} {DIST} serve"
        _say(f"bridge: not installed as a service. Run it yourself:  {run_cmd} --port {a.port}" + "".join(" " + x for x in extra))
        _say(f"        or keep it running in the background:  {DIST} install --service")
    _say(f"\nNext: hermes chat --provider {PLUGIN} -m sonnet")
    return 0


def cmd_print_service(argv: list[str]) -> int:
    own, extra = _split(argv)
    a = _install_parser("print-service", False).parse_args(own)
    home = hermes_home(a.hermes_home)
    command = a.command or shutil.which(DIST) or DIST
    args = service_argv(command, a.port, extra)
    print(render_launchd(args, home) if detect_os(a.os) == "macos" else render_systemd(args, a.port, home), end="")
    return 0


def cmd_uninstall(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog=f"{DIST} uninstall")
    ap.add_argument("--hermes-home")
    ap.add_argument("--os", choices=["linux", "macos"])
    a = ap.parse_args(argv)
    home = hermes_home(a.hermes_home)
    target = detect_os(a.os)
    if target == "linux" and unit_path().exists():
        _try(["systemctl", "--user", "disable", "--now", UNIT_NAME])
        unit_path().unlink()
        _try(["systemctl", "--user", "daemon-reload"])
        _say(f"removed {unit_path()}")
    if target == "macos" and plist_path().exists():
        _try(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
        plist_path().unlink()
        _say(f"removed {plist_path()}")
    dest = plugin_dest(home)
    if dest.is_symlink() or _is_our_plugin(dest):
        dest.unlink() if dest.is_symlink() else shutil.rmtree(dest)
        _say(f"removed {dest}")
    elif dest.exists():
        _warn(f"{dest} is not this plugin; left alone")
    if remove_env_line(home / ".env", ENV_KEY):
        _say(f"removed {ENV_KEY} from {home / '.env'}")
    _say("Done. If Hermes' config.yaml still names provider claude-code-bridge, switch it back.\n"
         f"To remove the program too: uv tool uninstall {DIST}")
    return 0


def cmd_doctor(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog=f"{DIST} doctor")
    ap.add_argument("--hermes-home")
    ap.add_argument("--port", type=int, help="bridge port to probe (default: from the .env line, else 9181)")
    a = ap.parse_args(argv)
    home = hermes_home(a.hermes_home)
    bad = 0

    def show(ok: bool | None, what: str, detail: str = "") -> None:
        nonlocal bad
        mark = {True: "ok  ", False: "FAIL", None: "info"}[ok]
        bad += ok is False
        print(f"[{mark}] {what}" + (f": {detail}" if detail else ""))

    show(None, f"{DIST} {__version__}", f"python {sys.version.split()[0]}")
    show(None, "uv", shutil.which("uv") or "not on PATH (only needed for uvx / uv tool install)")
    claude = shutil.which("claude")
    if claude:
        try:
            v = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception as e:  # noqa: BLE001
            v = f"could not run: {e}"
        show(True, "claude", f"{claude} ({v})")
    else:
        show(False, "claude", "not on PATH; install Claude Code and run `claude` once to log in")
    show(True if shutil.which("hermes") else None, "hermes", shutil.which("hermes") or "not on PATH")
    dest = plugin_dest(home)
    if dest.exists():
        same = (dest / "__init__.py").exists() and (dest / "__init__.py").read_text() == (plugin_source() / "__init__.py").read_text()
        show(True if same else None, "plugin", f"{dest}" + ("" if same else "  (differs from this version; re-run `install` to update it)"))
    else:
        show(False, "plugin", f"missing at {dest}; run `{DIST} install`")
    env_file = home / ".env"
    url = None
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(ENV_KEY + "="):
                url = line.split("=", 1)[1].strip()
    show(True if url else False, f"{ENV_KEY} in {env_file}", url or f"missing; run `{DIST} install` (the gateway needs it)")
    base = (url or f"http://127.0.0.1:{a.port or DEFAULT_PORT}/v1").rsplit("/v1", 1)[0]
    if a.port:
        base = f"http://127.0.0.1:{a.port}"
    try:
        with urllib.request.urlopen(base + "/health", timeout=3) as r:
            show(True, "bridge", f"{base} answers ({json.loads(r.read()).get('sessions', '?')} sessions)")
    except Exception as e:  # noqa: BLE001
        show(False, "bridge", f"not reachable at {base} ({e}); start it with `{DIST} serve` or `{DIST} install --service`")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int | None:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"
    rest = argv[1:]
    if cmd in ("-h", "--help", "help"):
        print(HELP, end="")
        return 0
    if cmd in ("version", "--version", "-V"):
        print(__version__)
        return 0
    if cmd == "install":
        return cmd_install(rest)
    if cmd == "uninstall":
        return cmd_uninstall(rest)
    if cmd == "print-service":
        return cmd_print_service(rest)
    if cmd == "doctor":
        return cmd_doctor(rest)
    from . import bridge
    bridge.main(rest if cmd == "serve" else argv)  # bare flags mean `serve`
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

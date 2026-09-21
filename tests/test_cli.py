#!/usr/bin/env python3
"""Tests for the installer / service commands (sandboxed HOME and HERMES_HOME; nothing real is touched, no network).
   uv run python -m unittest discover -s tests -v"""
import contextlib
import io
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from hermes_claude_cli_bridge import cli  # noqa: E402


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-test-"))
        self.home = self.tmp / "home"
        self.hh = self.tmp / "hermes"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "HERMES_HOME": str(self.hh)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Path.home() reads HOME on POSIX, so the sandbox applies to service paths too

    def test_version_and_help(self):
        rc, out, _ = run("version")
        self.assertEqual(rc, 0)
        self.assertRegex(out.strip(), r"^\d+\.\d+")
        rc, out, _ = run("--help")
        self.assertIn("install", out)
        self.assertIn("print-service", out)

    def test_install_copies_plugin_and_writes_env_line(self):
        (self.hh).mkdir()
        (self.hh / ".env").write_text("FOO=bar")  # no trailing newline
        rc, out, _ = run("install")
        self.assertEqual(rc, 0)
        plugin = self.hh / "plugins" / "model-providers" / "claude-code-bridge"
        self.assertTrue((plugin / "__init__.py").is_file())
        self.assertTrue((plugin / "plugin.yaml").is_file())
        self.assertFalse(plugin.is_symlink(), "must be a copy: uvx environments are throw-away")
        env = (self.hh / ".env").read_text()
        self.assertEqual(env, "FOO=bar\nCLAUDE_CODE_BRIDGE_URL=http://127.0.0.1:9181/v1\n")

    def test_install_is_idempotent_and_honours_port(self):
        run("install", "--port", "9300")
        rc, out, _ = run("install", "--port", "9300")
        self.assertIn("already set", out)
        self.assertEqual((self.hh / ".env").read_text().count("CLAUDE_CODE_BRIDGE_URL="), 1)
        self.assertIn("127.0.0.1:9300/v1", (self.hh / ".env").read_text())

    def test_new_env_file_is_private(self):
        run("install")
        self.assertEqual((self.hh / ".env").stat().st_mode & 0o777, 0o600)

    def test_install_replaces_an_old_symlinked_plugin(self):
        dest = self.hh / "plugins" / "model-providers"
        dest.mkdir(parents=True)
        (self.tmp / "elsewhere").mkdir()
        (dest / "claude-code-bridge").symlink_to(self.tmp / "elsewhere")
        run("install")
        self.assertFalse((dest / "claude-code-bridge").is_symlink())
        self.assertTrue((dest / "claude-code-bridge" / "plugin.yaml").is_file())
        self.assertTrue((self.tmp / "elsewhere").is_dir(), "the link target must not be deleted")

    def test_print_service_systemd_quotes_arguments(self):
        rc, out, _ = run("print-service", "--os", "linux", "--port", "9200", "--command", "/opt/x/hermes-claude-cli-bridge",
                         "--", "--add-dir", "/path with space/x", "--model", "opus")
        self.assertEqual(rc, 0)
        exec_line = next(l for l in out.splitlines() if l.startswith("ExecStart="))
        self.assertIn('"/opt/x/hermes-claude-cli-bridge" "serve" "--host" "127.0.0.1" "--port" "9200"', exec_line)
        self.assertIn('"--add-dir" "/path with space/x" "--model" "opus"', exec_line)
        self.assertIn("Environment=HERMES_HOME=" + str(self.hh), out)
        self.assertIn("WantedBy=default.target", out)

    def test_print_service_launchd_is_valid_plist_and_escapes(self):
        rc, out, _ = run("print-service", "--os", "macos", "--command", "/opt/x/hermes-claude-cli-bridge", "--", "--add-dir", "/a&b")
        self.assertEqual(rc, 0)
        plist = plistlib.loads(out.encode())
        self.assertEqual(plist["Label"], cli.LABEL)
        self.assertEqual(plist["ProgramArguments"][:2], ["/opt/x/hermes-claude-cli-bridge", "serve"])
        self.assertIn("/a&b", plist["ProgramArguments"])
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["EnvironmentVariables"]["HERMES_HOME"], str(self.hh))

    def test_service_records_the_claude_path_unless_given(self):
        fake = self.tmp / "bin"
        fake.mkdir()
        claude = fake / "claude"
        claude.write_text("#!/bin/sh\n")
        claude.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(fake) + os.pathsep + os.environ["PATH"]}):
            _, out, _ = run("print-service", "--os", "linux", "--command", "/x/b")
            self.assertIn(f'"--claude-bin" "{claude}"', out)
            _, out2, _ = run("print-service", "--os", "linux", "--command", "/x/b", "--", "--claude-bin", "/other/claude")
            self.assertNotIn(str(claude), out2)
            self.assertIn('"/other/claude"', out2)

    def test_install_service_no_start_writes_the_unit(self):
        rc, out, _ = run("install", "--service", "--no-start", "--os", "linux", "--command", "/x/hermes-claude-cli-bridge", "--", "--model", "opus")
        self.assertEqual(rc, 0)
        unit = self.home / ".config" / "systemd" / "user" / "claude-bridge.service"
        self.assertIn('"--model" "opus"', unit.read_text())

    def test_uninstall_removes_only_what_install_added(self):
        (self.hh).mkdir()
        (self.hh / ".env").write_text("FOO=bar\n")
        run("install", "--service", "--no-start", "--os", "linux", "--command", "/x/b")
        rc, out, _ = run("uninstall", "--os", "linux")
        self.assertEqual(rc, 0)
        self.assertFalse((self.hh / "plugins" / "model-providers" / "claude-code-bridge").exists())
        self.assertEqual((self.hh / ".env").read_text(), "FOO=bar\n")
        self.assertFalse((self.home / ".config" / "systemd" / "user" / "claude-bridge.service").exists())

    def test_uninstall_leaves_a_foreign_plugin_alone(self):
        other = self.hh / "plugins" / "model-providers" / "claude-code-bridge"
        other.mkdir(parents=True)
        (other / "plugin.yaml").write_text("name: something-else\n")
        rc, _, err = run("uninstall", "--os", "linux")
        self.assertTrue(other.exists())
        self.assertIn("not this plugin", err)

    def test_doctor_reports_missing_pieces(self):
        rc, out, _ = run("doctor", "--port", "1")  # nothing installed, nothing listening on port 1
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] plugin", out)
        self.assertIn("[FAIL] bridge", out)

    def test_doctor_after_install_finds_plugin_and_env(self):
        run("install")
        _, out, _ = run("doctor", "--port", "1")
        self.assertIn("[ok  ] plugin", out)
        self.assertIn("[ok  ] CLAUDE_CODE_BRIDGE_URL", out)

    def test_own_spec_falls_back_to_a_version_pin(self):
        with mock.patch.dict(os.environ, {"HERMES_CLAUDE_CLI_BRIDGE_SPEC": ""}):
            with mock.patch.object(cli.metadata, "distribution", side_effect=cli.metadata.PackageNotFoundError):
                self.assertTrue(cli._own_spec().startswith("hermes-claude-cli-bridge=="))
        with mock.patch.dict(os.environ, {"HERMES_CLAUDE_CLI_BRIDGE_SPEC": "git+https://example.com/x@abc"}):
            self.assertEqual(cli._own_spec(), "git+https://example.com/x@abc")

    def test_ephemeral_detection(self):
        with mock.patch.object(sys, "prefix", "/home/u/.cache/uv/archive-v0/AbC123"):
            self.assertTrue(cli._ephemeral())
        with mock.patch.object(sys, "prefix", "/home/u/.local/share/uv/tools/hermes-claude-cli-bridge"):
            self.assertFalse(cli._ephemeral())


if __name__ == "__main__":
    unittest.main(verbosity=2)

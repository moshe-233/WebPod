#!/usr/bin/env python3
"""Task 2 integration checks; no live user app or browser is touched."""
import os
from pathlib import Path
import json
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class InstallChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="webapp install ")
        self.home = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.home))

    def tearDown(self):
        self.temp.cleanup()

    def install(self, *args):
        return subprocess.run(["bash", str(SCRIPTS / "webapp-install.sh"), *args],
                              cwd="/tmp", env=self.env, capture_output=True, text=True)

    def test_system_desktop_launch_preserves_literal_url_and_flags(self):
        launcher = self.home / "literal launcher"
        argv = self.home / "argv.json"
        launcher.write_text("#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\n"
                            f"Path({str(argv)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        launcher.chmod(0o755)
        marker_temp = tempfile.TemporaryDirectory(prefix="webapp-literal-")
        self.addCleanup(marker_temp.cleanup)
        marker = Path(marker_temp.name) / "shell-was-executed"
        url = f"https://fixture.invalid/$(>{marker})?encoded=%20&literal=$HOME"
        result = self.install("--launcher", str(launcher), "--flags", "--user-agent='hello $USER world'",
                              "Literal Website", url, "web-browser")
        self.assertEqual(result.returncode, 0, result.stderr)
        desktop = self.home / ".local/share/applications/Literal Website.desktop"
        result = subprocess.run(["gio", "launch", str(desktop)], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 2
        while not argv.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(json.loads(argv.read_text()), [url, "--webapp-name", "literal-website", "--user-agent=hello $USER world"])
        self.assertFalse(marker.exists())

    def test_managed_regex_installs_blank_url_and_unicode(self):
        # Missing --runner/--url-source handling or mandatory icon download
        # breaks real installation before it can write a usable launcher.
        work = self.home / "工作 目录"
        work.mkdir()
        command = "printf 'ready https://127.0.0.1:31415/?key=fixture\\n'; sleep 1"
        regex = r"(?P<url>https?://[^\s]+)"
        result = self.install("--runner", str(SCRIPTS / "webapp-run"),
                              "--start-cmd", command, "--url-source", "regex",
                              "--url-regex", regex, "--url-timeout", "2.5",
                              "--cwd", str(work), "--auto-open=false",
                              "--flags", "--disable-gpu", "Ünïcode 工作", "")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        conf = self.home / ".local/share/webapp-maker/apps/ünïcode-工作.conf"
        text = conf.read_text()
        self.assertIn(f'start_cmd = "{command}"\n', text)
        self.assertIn(f'url_regex = "{regex}"\n', text)
        self.assertIn('url_source = "regex"\n', text)
        self.assertIn('url = ""\n', text)
        self.assertIn('url_timeout_s = "2.5"\n', text)
        self.assertIn(f'cwd = "{work}"\n', text)
        self.assertIn('auto_open = "false"\n', text)
        desktop = (self.home / ".local/share/applications/Ünïcode 工作.desktop").read_text()
        self.assertIn('"launch" "ünïcode-工作"', desktop)
        self.assertIn('Icon=web-browser\n', desktop)

    def test_fixed_url_defaults_and_supervisor_alias(self):
        result = self.install("--supervisor", str(SCRIPTS / "webapp-supervisor"),
                              "--start-cmd", "sleep 1", "Fixed App", "http://localhost:31415/")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        conf = (self.home / ".local/share/webapp-maker/apps/fixed-app.conf").read_text()
        self.assertIn('url_source = "fixed"\n', conf)
        self.assertIn('url_timeout_s = "15"\n', conf)
        self.assertIn('auto_open = "true"\n', conf)
        self.assertIn('url_regex = ""\n', conf)

    def test_script_command_stays_literal(self):
        command = r'''./extract.sh --option "a b" --regex '\\S+' '''.rstrip()
        result = self.install("--runner", str(SCRIPTS / "webapp-run"),
                              "--start-cmd", "sleep 1", "--url-source", "script",
                              "--extract-script", command, "Script App", "")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        conf = (self.home / ".local/share/webapp-maker/apps/script-app.conf").read_text()
        self.assertIn(f'extract_script = "{command}"\n', conf)

    def test_simple_website_keeps_profile_and_passes_flags(self):
        result = self.install("--launcher", str(SCRIPTS / "webapp-launch.sh"),
                              "--flags", "--disable-gpu --lang=zh-CN", "普通 网站", "example.test", "web-browser")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        desktop = (self.home / ".local/share/applications/普通 网站.desktop").read_text()
        self.assertIn('"https://example.test" "--webapp-name" "普通-网站"', desktop)
        self.assertIn('"--disable-gpu" "--lang=zh-CN"', desktop)
        self.assertFalse((self.home / ".local/share/webapp-maker/apps").exists())

    def test_invalid_inputs_create_no_launcher_or_conf(self):
        cases = [
            ["--url-source", "regex", "--url-regex", "[", "Broken", ""],
            ["--url-source", "regex", "Broken", ""],
            ["--url-source", "script", "Broken", ""],
            ["--url-source", "bogus", "Broken", ""],
            ["--url-timeout", "NaN", "Broken", "http://localhost/"],
            ["--url-timeout", "0", "Broken", "http://localhost/"],
            ["--auto-open=maybe", "Broken", "http://localhost/"],
            ["--cwd", str(self.home / "absent"), "Broken", "http://localhost/"],
            ["Broken", "javascript:alert(1)"],
            ["Broken", "http://localhost:99999/"],
            ["../Broken", "http://localhost/"],
            ["???", "http://localhost/"],
            ["--token-pattern", "old", "Broken", "http://localhost/"],
        ]
        for case in cases:
            with self.subTest(case=case):
                result = self.install("--runner", str(SCRIPTS / "webapp-run"), "--start-cmd", "sleep 1", *case)
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".local/share/applications").exists())
        self.assertFalse((self.home / ".local/share/webapp-maker/apps").exists())

    def test_remove_runner_launcher_and_state(self):
        result = self.install("--runner", str(SCRIPTS / "webapp-run"), "--start-cmd", "sleep 1",
                              "Remove 工作", "http://localhost:31415/")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        profile = self.home / ".local/share/webapp-maker/profiles/remove-工作"
        state = self.home / ".local/state/webapp-maker/remove-工作"
        profile.mkdir(parents=True)
        state.mkdir(parents=True, exist_ok=True)
        (profile / "fixture").write_text("profile")
        result = subprocess.run(["bash", str(SCRIPTS / "webapp-remove.sh"), "Remove 工作"],
                                cwd="/tmp", env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(profile.exists())
        self.assertFalse(state.exists())
        self.assertFalse((self.home / ".local/share/webapp-maker/apps/remove-工作.conf").exists())
        self.assertFalse((self.home / ".local/share/applications/Remove 工作.desktop").exists())

    def test_remove_refuses_unrelated_exec_and_path_traversal(self):
        apps = self.home / ".local/share/applications"
        apps.mkdir(parents=True)
        target = apps / "Other.desktop"
        target.write_text("[Desktop Entry]\nExec=/usr/bin/echo webapp-run\n")
        for name in ("Other", "../Other"):
            result = subprocess.run(["bash", str(SCRIPTS / "webapp-remove.sh"), name],
                                    cwd="/tmp", env=self.env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
        self.assertTrue(target.exists())

    def _kill_recorded_fixture_if_live(self, record):
        try:
            fields = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            return
        if fields[0] == "Z":
            return
        if int(fields[19]) == record["pid_start_ticks"] and int(fields[2]) == record["pgid"]:
            try:
                os.killpg(record["pgid"], signal.SIGKILL)
            except ProcessLookupError:
                return

    def _assert_remove_preserves_live_app(self, missing):
        env = dict(self.env, UV_CACHE_DIR=str(self.home / "uv cache"))
        setup = subprocess.run([str(SCRIPTS / "webapp-setup.sh")], cwd="/tmp", env=env,
                               capture_output=True, text=True)
        self.assertEqual(setup.returncode, 0, setup.stderr)
        data = self.home / ".local/share/webapp-maker"
        name, safe = "Keep live 工作", "keep-live-工作"
        code = "import time; print('ready http://127.0.0.1:31415/fixture', flush=True); time.sleep(30)"
        command = f"exec /usr/bin/python3 -u -c {shlex.quote(code)}"
        installed = subprocess.run([str(data / "webapp-install.sh"), "--runner", str(data / "webapp-run"),
                                    "--start-cmd", command, "--url-source", "regex", "--url-regex", r"http://\S+",
                                    "--auto-open=false", name, ""], cwd="/tmp", env=env,
                                   capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        profile = data / f"profiles/{safe}/fixture"
        profile.parent.mkdir(parents=True)
        profile.write_text("must remain available for recovery")
        files = {"desktop": self.home / f".local/share/applications/{name}.desktop",
                 "conf": data / f"apps/{safe}.conf", "profile": profile,
                 "state": self.home / f".local/state/webapp-maker/{safe}/run.json"}
        record = None
        moved = data / ("runtime/.venv/bin/python" if missing == "python" else "webapp-stop.sh")
        backup = moved.with_name(moved.name + ".unavailable")
        try:
            started = subprocess.run([str(data / "webapp-run"), "launch", name], env=env,
                                     capture_output=True, text=True, timeout=5)
            self.assertEqual(started.returncode, 0, started.stderr)
            record = json.loads(files["state"].read_text())
            before = {key: path.read_bytes() for key, path in files.items()}
            moved.rename(backup)
            removed = subprocess.run([str(data / "webapp-remove.sh"), name], cwd="/tmp", env=env,
                                     capture_output=True, text=True)
            fields = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(")", 1)[1].split()
            self.assertNotEqual(fields[0], "Z", "fixture service must still be running")
            absent = [key for key, path in files.items() if not path.exists()]
            self.assertNotEqual(removed.returncode, 0,
                                f"remove succeeded after {missing} disappeared; deleted={absent}; service still live")
            self.assertEqual(absent, [], f"failed stop must preserve all recovery artifacts: {absent}")
            self.assertEqual({key: path.read_bytes() for key, path in files.items()}, before)
        finally:
            if backup.exists() or backup.is_symlink():
                backup.rename(moved)
            # Use the real restored runner first. RED deleted run.json, so
            # the cleanup also retains this fixture's original PID identity.
            cleanup = subprocess.run([str(SCRIPTS / "webapp-run"), "stop", name, "0.1"], env=env,
                                     capture_output=True, text=True)
            if record:
                self._kill_recorded_fixture_if_live(record)
            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)

    def test_remove_preserves_live_app_when_runtime_python_is_missing(self):
        self._assert_remove_preserves_live_app("python")

    def test_remove_preserves_live_app_when_stop_wrapper_is_missing(self):
        self._assert_remove_preserves_live_app("stop")

    def test_installer_rejects_malformed_or_reserved_browser_flags(self):
        flags = ("--lang='unfinished", "--app http://example.test/", "--app=http://example.test/",
                 "--app-id installed-app", "--app-id=installed-app",
                 "--user-data-dir '/tmp/other profile'", "--user-data-dir=/tmp/other-profile")
        for managed in (False, True):
            for index, value in enumerate(flags):
                with self.subTest(managed=managed, flags=value):
                    name = f"Flags {managed} {index}"
                    mode = (["--runner", str(SCRIPTS / "webapp-run"), "--start-cmd", "sleep 1"]
                            if managed else ["--launcher", str(SCRIPTS / "webapp-launch.sh")])
                    result = self.install(*mode, "--flags", value, name, "http://localhost:31415/", "web-browser")
                    self.assertNotEqual(result.returncode, 0, "invalid browser flags must fail during installation")
                    self.assertFalse((self.home / f".local/share/applications/{name}.desktop").exists())
                    self.assertFalse((self.home / f".local/share/webapp-maker/apps/flags-{str(managed).lower()}-{index}.conf").exists())

    def test_quoted_browser_flags_keep_their_argument_boundaries(self):
        flags = '--lang=zh-CN --user-agent="Fixture Agent" --js-flags=\'--max-old-space-size=512\''
        managed = self.install("--runner", str(SCRIPTS / "webapp-run"), "--start-cmd", "sleep 1",
                               "--flags", flags, "Quoted Managed", "http://localhost:31415/")
        self.assertEqual(managed.returncode, 0, managed.stderr)
        conf = self.home / ".local/share/webapp-maker/apps/quoted-managed.conf"
        self.assertIn(f'flags = "{flags}"\n', conf.read_text())
        valid = subprocess.run([str(SCRIPTS / "webapp-run"), "validate", str(conf)], env=self.env,
                               capture_output=True, text=True)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        simple = self.install("--launcher", str(SCRIPTS / "webapp-launch.sh"), "--flags", flags,
                              "Quoted Simple", "http://localhost:31415/", "web-browser")
        self.assertEqual(simple.returncode, 0, simple.stderr)
        desktop = (self.home / ".local/share/applications/Quoted Simple.desktop").read_text()
        self.assertIn('"--user-agent=Fixture Agent"', desktop)
        self.assertIn('"--js-flags=--max-old-space-size=512"', desktop)

    def test_setup_updates_code_skips_redundant_sync_and_runs_installed_wrappers(self):
        # The payload inspects the interpreter boundary only. Setup, uv,
        # venv creation and each wrapper execute unchanged production code.
        project = self.home / "source with spaces"
        scripts = project / "scripts"
        scripts.mkdir(parents=True)
        for name in ("webapp-setup.sh", "webapp-run", "webapp-supervisor", "webapp-install.sh",
                     "webapp-stop.sh", "webapp-remove.sh", "webapp-launch.sh"):
            shutil.copy2(SCRIPTS / name, scripts / name)
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copy2(ROOT / name, project / name)
        program = "import json, os, sys; print(json.dumps(dict(prefix=sys.prefix, args=sys.argv[1:], cwd=os.getcwd(), revision=REV)))\n"
        (scripts / "webapp.py").write_text(program.replace("REV", "1"))
        env = dict(self.env, UV_CACHE_DIR=str(self.home / "uv cache"),
                   UV_PROJECT_ENVIRONMENT=str(self.home / "unrelated env"))
        first = subprocess.run(["bash", "-x", str(scripts / "webapp-setup.sh")],
                               cwd="/tmp", env=env, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("/usr/bin/uv sync", first.stderr)
        data = self.home / ".local/share/webapp-maker"
        stamp = data / "runtime/.sync-signature"
        stamp_time = stamp.stat().st_mtime_ns
        (scripts / "webapp.py").write_text(program.replace("REV", "2"))
        second = subprocess.run(["bash", "-x", str(scripts / "webapp-setup.sh")],
                                cwd="/tmp", env=env, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("/usr/bin/uv sync", second.stderr)
        self.assertEqual(stamp.stat().st_mtime_ns, stamp_time)
        for wrapper, args in (("webapp-run", ["status", "工作 name"]),
                              ("webapp-supervisor", ["launch", "工作 name"]),
                              ("webapp-stop.sh", ["工作 name", "0.25"])):
            invocation = subprocess.run([str(data / wrapper), *args], cwd="/tmp", env=env,
                                        capture_output=True, text=True)
            self.assertEqual(invocation.returncode, 0, invocation.stderr)
            payload = json.loads(invocation.stdout)
            self.assertEqual(payload["prefix"], str(data / "runtime/.venv"))
            self.assertEqual(payload["cwd"], "/tmp")
            self.assertEqual(payload["revision"], 2)
            self.assertEqual(payload["args"], (["stop"] + args) if wrapper == "webapp-stop.sh" else args)

    def test_source_wrapper_uses_project_venv_without_uv_on_click(self):
        project = self.home / "source project"
        scripts = project / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(SCRIPTS / "webapp-run", scripts / "webapp-run")
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copy2(ROOT / name, project / name)
        (scripts / "webapp.py").write_text("import json, sys; print(json.dumps(dict(prefix=sys.prefix, args=sys.argv[1:])))\n")
        env = dict(self.env, UV_CACHE_DIR=str(self.home / "uv cache"))
        sync = subprocess.run(["/usr/bin/uv", "sync", "--project", str(project), "--locked", "--offline",
                               "--no-python-downloads", "--python", "/usr/bin/python3"],
                              env=env, capture_output=True, text=True)
        self.assertEqual(sync.returncode, 0, sync.stderr)
        run = subprocess.run(["bash", "-x", str(scripts / "webapp-run"), "status", "名字 with space"],
                             cwd="/tmp", env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), {"prefix": str(project / ".venv"), "args": ["status", "名字 with space"]})
        self.assertNotIn("/usr/bin/uv", run.stderr)

    def test_real_core_installed_wrapper_roundtrip(self):
        # Real core, real runner and a short isolated service. auto_open=false
        # prevents any host browser interaction in this installer subtask.
        env = dict(self.env, UV_CACHE_DIR=str(self.home / "uv cache"))
        setup = subprocess.run([str(SCRIPTS / "webapp-setup.sh")], cwd="/tmp", env=env,
                               capture_output=True, text=True)
        self.assertEqual(setup.returncode, 0, setup.stderr)
        data = self.home / ".local/share/webapp-maker"
        runner = data / "webapp-run"
        name, safe = "Real 工作", "real-工作"
        work = self.home / "cwd 目录"
        work.mkdir()
        code = "import time; print('ready http://127.0.0.1:31415/fixture', flush=True); time.sleep(30)"
        command = f"exec /usr/bin/python3 -u -c {shlex.quote(code)}"
        installed = subprocess.run([str(data / "webapp-install.sh"), "--runner", str(runner),
                                    "--start-cmd", command, "--url-source", "regex",
                                    "--url-regex", r"(?P<url>http://\S+)", "--auto-open=false",
                                    "--cwd", str(work), name, ""], cwd="/tmp", env=env,
                                   capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        conf = data / f"apps/{safe}.conf"
        valid = subprocess.run([str(runner), "validate", str(conf)], env=env, capture_output=True, text=True)
        self.assertEqual((valid.returncode, valid.stdout.strip()), (0, "valid"), valid.stderr)
        matched = subprocess.run([str(SCRIPTS / "webapp-run"), "match", "--regex", r"(?P<url>http://\S+)",
                                  "--line", "ready http://127.0.0.1:31415/fixture"],
                                 cwd="/tmp", env=env, capture_output=True, text=True)
        self.assertEqual(matched.returncode, 0, matched.stderr)
        self.assertEqual(json.loads(matched.stdout)["url"], "http://127.0.0.1:31415/fixture")
        try:
            launched = subprocess.run([str(runner), "launch", name], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(launched.returncode, 0, launched.stderr)
            batch = subprocess.run([str(data / "webapp-supervisor"), "status"], env=env,
                                   capture_output=True, text=True)
            self.assertEqual(batch.returncode, 0, batch.stderr)
            apps = json.loads(batch.stdout)["apps"]
            self.assertEqual(len(apps), 1)
            self.assertEqual((apps[0]["name"], apps[0]["state"], apps[0]["resolved_url"]),
                             (name, "running", "http://127.0.0.1:31415/fixture"))
            self.assertIsNone(apps[0]["cpu_pct"])
            pid = apps[0]["pid"]
            self.assertEqual(Path(f"/proc/{pid}/cwd").resolve(), work)
        finally:
            stopped = subprocess.run([str(data / "webapp-stop.sh"), name, "0.2"], env=env,
                                     capture_output=True, text=True)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
        status = subprocess.run([str(runner), "status", name], env=env, capture_output=True, text=True)
        self.assertEqual(json.loads(status.stdout)["state"], "stopped")
        removed = subprocess.run([str(data / "webapp-remove.sh"), name], cwd="/tmp", env=env,
                                 capture_output=True, text=True)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(conf.exists())
        self.assertFalse((self.home / f".local/state/webapp-maker/{safe}").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

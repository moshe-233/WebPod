#!/usr/bin/env python3
"""Observable launch/PWA/process contracts for the lightweight CLI."""

import json
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "scripts/webapp.py"
SERVER = ROOT / "tests/fixtures/url-server.py"
URL_RE = r"(?P<url>http://127\.0\.0\.1:[0-9]+/\?token=token-[0-9]+)"


def alive(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return stat[0] != "Z"
    except (OSError, IndexError):
        return False


class LightLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="webapp-light-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.name = "Light Test"
        self.safe = "light-test"
        self.apps = self.home / ".local/share/webapp-maker/apps"
        self.apps.mkdir(parents=True)
        self.state_dir = self.home / ".local/state/webapp-maker" / self.safe
        self.chrome_log = self.home / "chromium.log"
        bindir = self.home / "bin"; bindir.mkdir()
        shutil.copy2(ROOT / "tests/fixtures/stub-chromium", bindir / "chromium")
        xdg = bindir / "xdg-settings"
        xdg.write_text("#!/bin/sh\nexit 1\n"); xdg.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{bindir}:{os.environ['PATH']}",
                        CHROMIUM_STUB_LOG=str(self.chrome_log), PYTHONDONTWRITEBYTECODE="1")
        self.pending = []
        self.extra = []
        self.addCleanup(self.cleanup)

    def cli(self, *args, timeout=8):
        return subprocess.run([sys.executable, str(CORE), *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def configure(self, service_env=None, **changes):
        knobs = service_env or {}
        cmd = shlex.join(["env", *[f"{key}={value}" for key, value in knobs.items()],
                          sys.executable, str(SERVER), str(self.home)])
        data = dict(name=self.name, start_cmd=cmd, url_source="regex", url="", url_regex=URL_RE,
                    extract_script="", url_timeout_s="2", cwd=str(self.home),
                    flags="--incognito --user-agent='hello world'", auto_open="true")
        data.update(changes)
        conf = self.apps / f"{self.safe}.conf"
        conf.write_text("".join(f'{key} = "{value}"\n' for key, value in data.items()))
        return conf

    def launch_async(self):
        proc = subprocess.Popen([sys.executable, str(CORE), "launch", self.name], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.pending.append(proc)
        return proc

    def record(self):
        try:
            return json.loads((self.state_dir / "run.json").read_text())
        except (OSError, ValueError):
            return {}

    def info(self):
        return json.loads((self.home / "service-info.json").read_text())

    def wait(self, predicate, message="condition did not arrive", timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate(): return
            time.sleep(0.01)
        self.fail(message)

    def success(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def status(self):
        result = self.cli("status", self.name)
        self.success(result)
        return json.loads(result.stdout)

    def cleanup(self):
        if CORE.exists():
            self.cli("stop", self.name, "0", timeout=5)
        for proc in self.pending:
            if proc.poll() is None: proc.terminate()
            proc.communicate(timeout=3)
        for proc in self.extra:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)
        path = self.home / "service-info.json"
        if path.exists():
            info = json.loads(path.read_text())
            if alive(info["pid"]) and info["pgid"] not in (1, os.getpgrp()):
                try: os.killpg(info["pgid"], signal.SIGKILL)
                except ProcessLookupError: return

    def test_regex_opens_pwa_and_helper_exits_while_service_logs_continue(self):
        self.configure()
        result = self.cli("launch", self.name)
        self.success(result)
        self.wait(self.chrome_log.exists, "PWA browser was not invoked")
        info = self.info()
        text = self.chrome_log.read_text()
        self.assertIn("arg --app=" + info["url"], text)
        self.assertIn("arg --user-data-dir=" + str(self.home / ".local/share/webapp-maker/profiles" / self.safe), text)
        self.assertIn("arg --user-agent=hello world", text)
        self.assertTrue(alive(info["pid"]))
        log = self.state_dir / "app.log"
        size = log.stat().st_size
        self.wait(lambda: log.stat().st_size > size, "logging stopped when launcher exited")
        self.assertFalse((self.home / "http-request").exists(), "launcher sent an HTTP health probe")
        for procdir in Path("/proc").iterdir():
            if not procdir.name.isdigit(): continue
            try: args = (procdir / "cmdline").read_bytes().split(b"\0")
            except OSError: continue
            self.assertFalse(str(CORE).encode() in args and b"launch" in args and self.name.encode() in args,
                             "a launcher process stayed resident")

    def test_fixed_url_waits_for_tcp_and_skips_log_extraction_and_http(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        self.configure(service_env=dict(TEST_PORT=port, TEST_NO_URL=1, TEST_LISTEN_DELAY="0.2", TEST_HTTP_STATUS=503),
                       url_source="fixed", url=f"http://127.0.0.1:{port}/", url_regex="")
        proc = self.launch_async()
        time.sleep(0.08)
        self.assertFalse(self.chrome_log.exists(), "PWA opened before the default port listened")
        out, err = proc.communicate(timeout=4)
        self.assertEqual(proc.returncode, 0, out + err)
        self.wait(self.chrome_log.exists)
        self.assertIn(f"arg --app=http://127.0.0.1:{port}/", self.chrome_log.read_text())
        self.assertFalse((self.home / "http-request").exists())

    def test_match_supports_named_group_first_group_and_whole_match(self):
        for pattern in (r"Open (?P<url>https://example\.test/[^ ]+)",
                        r"Open (https://example\.test/[^ ]+)", r"https://example\.test/[^ ]+"):
            with self.subTest(pattern=pattern):
                result = self.cli("match", "--regex", pattern, "--line", "Open https://example.test/?key=abc")
                self.success(result)
                self.assertEqual(json.loads(result.stdout), {"url":"https://example.test/?key=abc"})
        result = self.cli("match", "--regex", "https://[^ ]+", "--line", "nothing to match")
        self.success(result); self.assertIsNone(json.loads(result.stdout)["url"])

    def test_regex_matches_stderr_with_ansi_colors(self):
        self.configure(service_env=dict(TEST_STDERR=1, TEST_ANSI=1))
        self.success(self.cli("launch", self.name))
        self.assertEqual(self.record()["url"], self.info()["url"])

    def test_partial_log_line_is_not_used_as_a_truncated_token(self):
        self.configure(service_env=dict(TEST_PARTIAL=1))
        proc = self.launch_async()
        self.wait(lambda: (self.home / "partial-ready").exists())
        time.sleep(0.06)
        self.assertFalse(self.chrome_log.exists())
        (self.home / "rest-gate").touch()
        out, err = proc.communicate(timeout=4)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertEqual(self.record()["url"], self.info()["url"])

    def test_url_timeout_preserves_service_and_open_retries_without_restarting(self):
        self.configure(service_env=dict(TEST_HOLD_URL=1), url_timeout_s="0.2")
        result = self.cli("launch", self.name)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("URL", result.stderr)
        info = self.info()
        self.assertTrue(alive(info["pid"]))
        state = self.status()
        self.assertEqual(state["state"], "running")
        self.assertTrue(state["error"])
        (self.home / "url-gate").touch()
        self.success(self.cli("open", self.name))
        self.assertEqual(self.info()["pid"], info["pid"])
        self.assertEqual(self.status()["error"], None)

    def test_repeated_open_and_closed_window_do_not_restart_service(self):
        self.configure()
        self.success(self.cli("launch", self.name))
        pid = self.info()["pid"]
        self.wait(self.chrome_log.exists)
        window = int(self.chrome_log.read_text().split("pid=",1)[1].split()[0])
        os.kill(window, signal.SIGTERM)
        self.wait(lambda: not alive(window))
        self.assertEqual(self.status()["state"], "running")
        self.success(self.cli("launch", self.name))
        self.assertEqual(self.info()["pid"], pid)
        self.assertEqual(len((self.home / "service-starts").read_text().splitlines()), 1)

    def test_parallel_clicks_spawn_only_one_service(self):
        self.configure(service_env=dict(TEST_URL_DELAY="0.15"))
        procs = [self.launch_async(), self.launch_async()]
        for proc in procs:
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, out + err)
        self.assertEqual(len((self.home / "service-starts").read_text().splitlines()), 1)

    def test_stop_cancels_a_pending_url_wait_and_closes_pwa(self):
        self.configure(service_env=dict(TEST_HOLD_URL=1))
        proc = self.launch_async()
        self.wait(lambda: bool(self.record().get("pid")))
        pid = self.record()["pid"]
        self.success(self.cli("stop", self.name, "0"))
        out, err = proc.communicate(timeout=3)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertFalse(alive(pid))
        self.assertEqual(self.status()["state"], "stopped")

    def test_stop_reaps_service_and_profile_windows(self):
        self.configure()
        self.success(self.cli("launch", self.name))
        pid = self.info()["pid"]
        self.wait(self.chrome_log.exists)
        window = int(self.chrome_log.read_text().split("pid=",1)[1].split()[0])
        self.success(self.cli("stop", self.name, "0"))
        self.assertFalse(alive(pid)); self.assertFalse(alive(window))
        profile = self.home / ".local/share/webapp-maker/profiles" / self.safe
        self.assertEqual(list(profile.glob("Singleton*")), [])

    def test_new_instance_never_reuses_last_instances_token(self):
        self.configure()
        self.success(self.cli("launch", self.name))
        first = self.record()["url"]
        self.success(self.cli("stop", self.name, "0"))
        self.success(self.cli("launch", self.name))
        self.assertNotEqual(self.record()["url"], first)
        self.assertIn(first, (self.state_dir / "app.previous.log").read_text())

    def test_exit_between_poll_and_proc_read_keeps_real_exit_code(self):
        spec = importlib.util.spec_from_file_location("light_core", CORE)
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        child = Mock()
        child.poll.side_effect = [None, 42]
        child.returncode = 42
        with patch.object(core, "same_process", return_value=None):
            with self.assertRaisesRegex(core.LaunchError, "code 42"):
                core.pending_service({"owned": True}, child)

    def test_service_exit_is_reported_without_waiting_for_url_timeout(self):
        self.configure(service_env=dict(TEST_EXIT_CODE=42))
        start = time.monotonic()
        result = self.cli("launch", self.name)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("42", result.stderr)
        self.assertLess(time.monotonic()-start, 1.5)

    def test_token_log_and_receipt_are_private_with_a_permissive_umask(self):
        self.configure()
        previous = os.umask(0o022)
        try:
            self.success(self.cli("launch", self.name))
        finally:
            os.umask(previous)
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state_dir / "app.log").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state_dir / "run.json").stat().st_mode & 0o777, 0o600)

    def test_bad_regex_fails_before_service_spawn(self):
        self.configure(url_regex="(")
        result = self.cli("launch", self.name)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regex", result.stderr.lower())
        self.assertFalse((self.home / "service-starts").exists())

    def test_headless_start_can_be_opened_later(self):
        conf = self.configure(auto_open="false")
        self.success(self.cli("start", str(conf)))
        self.assertFalse(self.chrome_log.exists())
        self.success(self.cli("launch", self.name))
        self.wait(self.chrome_log.exists)

    def test_status_does_not_attach_a_remote_url_to_an_unrelated_local_port(self):
        proc = subprocess.Popen([sys.executable, str(SERVER), str(self.home)], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.extra.append(proc)
        self.wait(lambda: (self.home / "service-info.json").exists())
        self.configure(url_source="fixed", url=f"http://192.0.2.123:{self.info()['port']}/", url_regex="")
        result = self.cli("status", self.name)
        self.success(result)
        data = json.loads(result.stdout)
        self.assertEqual(data["state"], "stopped")
        self.assertIsNone(data["pid"])

    def test_failed_listener_query_is_an_error_instead_of_a_fake_stopped_state(self):
        self.configure()
        stub = self.home / "bin/ss"
        stub.write_text("#!/bin/sh\necho 'fixture ss failure' >&2\nexit 7\n")
        stub.chmod(0o755)
        result = self.cli("status")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture ss failure", result.stderr)

    def test_batch_status_exposes_raw_cpu_and_memory_without_an_http_probe(self):
        self.configure()
        self.success(self.cli("launch", self.name))
        result = self.cli("status")
        self.success(result)
        apps = json.loads(result.stdout)["apps"]
        self.assertEqual(len(apps), 1)
        record = apps[0]
        self.assertEqual(record["state"], "running")
        self.assertIsNone(record["cpu_pct"], "one sample cannot measure CPU utilization")
        self.assertGreater(record["clock_ticks"], 0)
        self.assertGreaterEqual(record["cpu_ticks"], 0)
        self.assertGreater(record["mem_mb"], 0)
        self.assertEqual(record["port"], self.info()["port"])
        self.assertFalse((self.home / "http-request").exists())

    def test_real_chromium_rewritten_cmdline_keeps_profile_ownership(self):
        spec = importlib.util.spec_from_file_location("light_core", CORE)
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)
        profile = "/home/test/.local/share/webapp-maker/profiles/dsh-工作"
        rewritten = ["/opt/google/chrome/chrome --app=http://127.0.0.1:3081/?token=fixture --user-data-dir=" + profile]
        self.assertTrue(core.uses_profile(rewritten, profile, "chrome"))
        self.assertFalse(core.uses_profile(rewritten, profile, "python3"))
        self.assertFalse(core.uses_profile([rewritten[0] + "-other"], profile, "chrome"))
        self.assertTrue(core.uses_profile(["chrome", "--user-data-dir", profile], profile, "chrome"))

    def test_pid_start_identity_protects_an_unrelated_process_on_stop(self):
        self.configure()
        victim = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.extra.append(victim)
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "run.json").write_text(json.dumps(dict(pid=victim.pid,
            pid_start_ticks=-1, pgid=victim.pid, owned=True, url="http://127.0.0.1:9999")))
        self.success(self.cli("stop", self.name, "0"))
        self.assertIsNone(victim.poll(), "a recycled service pid was signaled")

    def test_external_fixed_port_is_reused_but_not_stopped(self):
        proc = subprocess.Popen([sys.executable, str(SERVER), str(self.home)], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.extra.append(proc)
        self.wait(lambda: (self.home / "service-info.json").exists())
        info = self.info()
        self.configure(url_source="fixed", url=f"http://127.0.0.1:{info['port']}/", start_cmd="exit 42", url_regex="")
        self.success(self.cli("launch", self.name))
        self.success(self.cli("stop", self.name, "0"))
        self.assertIsNone(proc.poll(), "the external port owner was stopped")
        self.assertEqual(self.status()["state"], "stopped")

    def test_script_reads_log_lines_outputs_url_and_is_reaped_after_match(self):
        script = self.home / "extract.py"
        pidfile = self.home / "extract.pid"
        script.write_text("import os,pathlib,sys,time\n"
                          f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                          "for line in sys.stdin:\n"
                          " if 'http://' in line:\n"
                          "  print(line[line.index('http://'):].strip(),flush=True)\n"
                          "  time.sleep(60)\n")
        self.configure(url_source="script", extract_script="exec " + shlex.join([sys.executable,str(script)]))
        self.success(self.cli("launch", self.name))
        self.assertEqual(self.record()["url"], self.info()["url"])
        self.assertFalse(alive(int(pidfile.read_text())), "custom extractor survived the launch")
        self.assertTrue(alive(self.info()["pid"]))

    def test_script_stdin_backpressure_does_not_block_timeout(self):
        script = self.home / "blocked.py"
        pidfile = self.home / "extract.pid"
        script.write_text("import os,pathlib,time\n"
                          f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                          "time.sleep(60)\n")
        self.configure(service_env=dict(TEST_SPAM=1), url_source="script",
                       extract_script="exec " + shlex.join([sys.executable,str(script)]), url_timeout_s="0.3")
        started = time.monotonic()
        result = self.cli("launch", self.name)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("URL", result.stderr)
        self.assertLess(time.monotonic()-started, 2)
        self.assertFalse(alive(int(pidfile.read_text())), "blocked extractor was not reaped")
        self.assertTrue(alive(self.info()["pid"]))

    def test_script_exit_reports_its_code_and_preserves_the_service(self):
        self.configure(url_source="script", extract_script="exit 7")
        result = self.cli("launch", self.name)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("7", result.stderr)
        self.wait(lambda: (self.home / "service-info.json").exists())
        self.assertTrue(alive(self.info()["pid"]))

    def test_empty_folded_name_cannot_target_the_profiles_root(self):
        result = self.cli("stop", "???", "0")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".local/state/webapp-maker/run.json").exists())

    def test_stop_while_browser_lookup_waits_cannot_reopen_or_resurrect_record(self):
        (self.home / "bin/xdg-settings").write_text("#!/bin/sh\nsleep 0.5\nexit 1\n")
        self.configure()
        proc = self.launch_async()
        self.wait(lambda: (self.home / "service-info.json").exists())
        self.wait(lambda: b"Open http" in (self.state_dir / "app.log").read_bytes())
        self.success(self.cli("stop", self.name, "0"))
        out, err = proc.communicate(timeout=3)
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertFalse(self.chrome_log.exists(), "a cancelled launch opened its PWA")
        self.assertIsNone(self.record().get("pid"))
        self.assertEqual(self.status()["state"], "stopped")

    def test_browser_flags_cannot_replace_pwa_url_or_independent_profile(self):
        for flag in ("--app=http://other.test/", "--app http://other.test/",
                     "--app-id=other", f"--user-data-dir={self.home / 'other'}", f"--user-data-dir {self.home / 'other'}"):
            with self.subTest(flag=flag):
                self.configure(flags=flag)
                result = self.cli("launch", self.name)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("flag", result.stderr.lower())
                self.assertFalse((self.home / "service-starts").exists())

    def test_stop_grace_waits_for_service_child_after_shell_exits(self):
        conf = self.configure(service_env=dict(TEST_GRACE_CLEANUP=1))
        command = next(line for line in conf.read_text().splitlines() if line.startswith("start_cmd"))
        command = command.split(' = "', 1)[1][:-1]
        self.configure(start_cmd=command + " & wait")
        self.success(self.cli("launch", self.name))
        self.success(self.cli("stop", self.name, "1"))
        self.assertTrue((self.home / "cleanup-complete").exists(), "grace period ended with only the shell")

    def test_detached_extractor_pipe_holder_cannot_block_helper_timeout(self):
        script = self.home / "detaching-extractor.py"
        pidfile = self.home / "pipe-holder.pid"
        script.write_text("import pathlib,subprocess,time\n"
                          "child=subprocess.Popen(['sleep','3'],start_new_session=True)\n"
                          f"pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))\n"
                          "time.sleep(10)\n")
        def clean_holder():
            if pidfile.exists() and alive(int(pidfile.read_text())):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
        self.addCleanup(clean_holder)
        self.configure(service_env=dict(TEST_SPAM=1), url_source="script",
                       extract_script="exec " + shlex.join([sys.executable,str(script)]), url_timeout_s="0.2")
        start = time.monotonic()
        result = self.cli("launch", self.name, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic()-start, 1.5, "inherited pipe held stdin cleanup open")

    def test_url_failure_reaps_an_inflight_browser_lookup(self):
        pidfile = self.home / "lookup.pid"
        (self.home / "bin/xdg-settings").write_text(
            f"#!/bin/sh\necho $$ > {shlex.quote(str(pidfile))}\nexec sleep 60\n")
        def cleanup_lookup():
            if pidfile.exists() and alive(int(pidfile.read_text())):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
        self.addCleanup(cleanup_lookup)
        self.configure(service_env=dict(TEST_HOLD_URL=1), url_timeout_s="0.2")
        result = self.cli("launch", self.name)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(pidfile.exists())
        self.assertFalse(alive(int(pidfile.read_text())), "URL timeout abandoned xdg-settings")


if __name__ == "__main__":
    unittest.main(verbosity=2)

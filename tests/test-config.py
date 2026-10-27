#!/usr/bin/env python3
"""Configuration editing checks use isolated HOME and never launch services."""
import importlib.util
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ConfigChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='webapp-config-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.home))
        patcher = patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch.dict(os.environ, self.env):
            spec = importlib.util.spec_from_file_location('edit_fixture', ROOT / 'scripts/webapp.py')
            self.app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)
        self.install(managed=True)

    def install(self, managed=False):
        args = ['--runner', str(ROOT / 'scripts/webapp-run'), '--start-cmd', 'printf "中文\\n"', '--cwd', str(self.home)] if managed else ['--launcher', str(ROOT / 'scripts/webapp-launch.sh')]
        result = subprocess.run(['bash', str(ROOT / 'scripts/webapp-install.sh'), *args, 'Fixture 工作', 'http://localhost:31415/', 'web-browser'], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def read(self):
        return self.app.read_edit_config('Fixture 工作')

    def write(self, snap, **extra):
        return self.app.write_edit_config('Fixture 工作', snap['revision'], dict(fields=snap['fields'], **extra))

    def test_raw_roundtrip_preserves_unknown_comments_and_runtime(self):
        conf = self.app.APPS / 'fixture-工作.conf'
        with conf.open('a') as out:
            out.write('# preserve me\nfuture_key = "untouched"\n')
        state = self.app.STATE / 'fixture-工作'
        state.mkdir(parents=True, exist_ok=True)
        (state / 'run.json').write_text('{"url":"cached"}')
        profile = self.app.PROFILES / 'fixture-工作'
        profile.mkdir(parents=True)
        (profile / 'fixture').write_text('profile')
        snap = self.read()
        snap['fields'].update(start_cmd='printf "中文 \\n"', url_source='regex', url_regex=r'(?P<url>http://[^\s]+)', cwd='')
        receipt = self.write(snap)
        self.assertTrue(receipt['saved'])
        self.assertTrue(receipt['restart_required'])
        self.assertEqual(self.read()['fields'], snap['fields'])
        self.assertIn('# preserve me\nfuture_key = "untouched"\n', conf.read_text())
        self.assertEqual(conf.stat().st_mode & 0o777, 0o600)
        self.assertEqual((state / 'run.json').read_text(), '{"url":"cached"}')
        self.assertEqual((profile / 'fixture').read_text(), 'profile')

    def test_validation_conflict_and_unknown_fields_do_not_write(self):
        snap = self.read()
        before = (self.app.APPS / 'fixture-工作.conf').read_bytes()
        for fields in [dict(snap['fields'], flags='--user-data-dir=/tmp'), dict(snap['fields'], auto_open='true'), dict(snap['fields'], url_timeout_s='NaN'), dict(snap['fields'], start_cmd='x\ny'), dict(snap['fields'], unknown='x')]:
            with self.assertRaises(self.app.EditError) as error:
                self.app.write_edit_config('Fixture 工作', snap['revision'], {'fields': fields})
            self.assertEqual(error.exception.code, 'validation')
        with self.assertRaises(self.app.EditError):
            self.write(snap, name='changed')
        conf = self.app.APPS / 'fixture-工作.conf'
        self.assertEqual(conf.read_bytes(), before)
        conf.write_bytes(before + b'# external\n')
        with self.assertRaises(self.app.EditError) as error:
            self.write(snap)
        self.assertEqual(error.exception.code, 'conflict')

    def test_website_metadata_and_explicit_legacy_recovery(self):
        (self.app.APPS / 'fixture-工作.conf').unlink()
        self.install()
        entry = self.app.DATA / 'entries/fixture-工作.json'
        self.assertEqual(json.loads(entry.read_text())['flags'], '')
        entry.unlink()
        snap = self.read()
        self.assertEqual(snap['recovery_required'], ['url', 'flags'])
        self.assertEqual(snap['fields'], {'url': None, 'flags': None})
        snap['fields'] = {'url': 'https://fixture.invalid/a?literal=$HOME', 'flags': '--lang=zh-CN'}
        with self.assertRaises(self.app.EditError):
            self.write(snap)
        self.write(snap, recovery_confirmed=True)
        self.assertEqual(self.read()['fields'], snap['fields'])
        result = subprocess.run(['bash', str(ROOT / 'scripts/webapp-remove.sh'), 'Fixture 工作'], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(entry.exists())

    def test_rollback_interruption_and_busy(self):
        snap = self.read()
        conf = self.app.APPS / 'fixture-工作.conf'
        before = conf.read_bytes()
        snap['fields']['flags'] = '--disable-gpu'
        replace = self.app.os.replace
        calls = []
        def fail_once(source, target):
            calls.append(str(target))
            if len(calls) == 2:
                raise OSError('fixture failure')
            return replace(source, target)
        with patch.object(self.app.os, 'replace', side_effect=fail_once):
            with self.assertRaises(self.app.EditError) as error:
                self.write(snap)
        self.assertEqual(error.exception.code, 'io')
        self.assertEqual(conf.read_bytes(), before)
        self.assertEqual(self.read()['revision'], snap['revision'])
        with self.app.operation('fixture-工作'):
            with self.assertRaises(self.app.EditError) as error:
                self.read()
            self.assertEqual(error.exception.code, 'busy')
        pending = self.app.STATE / 'fixture-工作/config-edit.pending'
        pending.mkdir()
        with self.assertRaises(self.app.EditError) as error:
            self.write(snap)
        self.assertEqual(error.exception.code, 'io')

    def test_desktop_revision_flags_icon_and_no_launch(self):
        snap = self.read()
        desktop = self.home / '.local/share/applications/Fixture 工作.desktop'
        original = desktop.read_bytes()
        desktop.write_bytes(original + b'# external desktop edit\n')
        with self.assertRaises(self.app.EditError) as error:
            self.write(snap)
        self.assertEqual(error.exception.code, 'conflict')
        snap = self.read()
        snap['fields']['flags'] = "--user-agent='中文 $HOME space'"
        with patch.object(self.app.subprocess, 'Popen', side_effect=AssertionError('save must not launch')):
            receipt = self.write(snap, icon_ref='')
        self.assertFalse(receipt['restart_required'])
        self.assertEqual(self.app.desktop_properties(desktop)['Icon'], 'web-browser')
        (self.app.APPS / 'fixture-工作.conf').unlink()
        self.install()
        snap = self.read()
        snap['fields'] = dict(url='https://fixture.invalid/a?value=%20&literal=$HOME', flags="--user-agent='中文 $HOME space'")
        self.write(snap)
        # Validate the actual desktop parser rather than treating Exec as shell.
        launcher = self.app.DATA / 'webapp-launch'
        output = self.home / 'captured-argv.json'
        launcher.write_text('#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\n' + f'Path({str(output)!r}).write_text(json.dumps(sys.argv[1:]))\n')
        launcher.chmod(0o755)
        desktop.write_text(desktop.read_text().replace(str(ROOT / 'scripts/webapp-launch.sh'), str(launcher)))
        result = subprocess.run(['gio', 'launch', str(desktop)], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 2
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(json.loads(output.read_text()), [snap['fields']['url'], '--webapp-name', 'fixture-工作', '--user-agent=中文 $HOME space'])

    def test_process_interruption_leaves_recovery_journal(self):
        snap = self.read()
        snap['fields']['flags'] = '--disable-gpu'
        replace = self.app.os.replace
        calls = []
        def interrupted(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise SystemExit('fixture interruption')
            return replace(source, target)
        with patch.object(self.app.os, 'replace', side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.write(snap)
        pending = self.app.STATE / 'fixture-工作/config-edit.pending'
        manifest = json.loads((pending / 'manifest.json').read_text())
        self.assertEqual(len(manifest), 2)
        self.assertTrue((pending / '0').is_file())
        with self.assertRaises(self.app.EditError) as error:
            self.read()
        self.assertEqual(error.exception.code, 'io')

    def test_failed_rollback_is_reported_and_blocks_future_edit(self):
        snap = self.read()
        snap['fields']['flags'] = '--disable-gpu'
        replace = self.app.os.replace
        calls = []
        def broken(source, target):
            calls.append(target)
            if len(calls) >= 2:
                raise OSError('fixture failed rollback')
            return replace(source, target)
        with patch.object(self.app.os, 'replace', side_effect=broken):
            with self.assertRaises(self.app.EditError) as error:
                self.write(snap)
        self.assertIn('rollback failed', str(error.exception))
        self.assertTrue((self.app.STATE / 'fixture-工作/config-edit.pending/manifest.json').exists())
        with self.assertRaises(self.app.EditError):
            self.read()

    def test_installer_and_remover_observe_edit_lock_and_pending(self):
        desktop = self.home / '.local/share/applications/Fixture 工作.desktop'
        before = desktop.read_bytes()
        commands = [
            ['bash', str(ROOT / 'scripts/webapp-install.sh'), '--launcher', str(ROOT / 'scripts/webapp-launch.sh'), 'Fixture 工作', 'https://fixture.invalid/', 'web-browser'],
            ['bash', str(ROOT / 'scripts/webapp-remove.sh'), 'Fixture 工作'],
        ]
        with self.app.operation('fixture-工作'):
            for command in commands:
                result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('busy', result.stderr)
        pending = self.app.STATE / 'fixture-工作/config-edit.pending'
        pending.mkdir()
        for command in commands:
            result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('recovery', result.stderr)
        self.assertEqual(desktop.read_bytes(), before)

    def test_failed_remove_preserves_website_record(self):
        (self.app.APPS / 'fixture-工作.conf').unlink()
        launcher = self.app.DATA / 'webapp-launch'
        launcher.write_text('#!/bin/sh\nexit 0\n')
        launcher.chmod(0o755)
        result = subprocess.run(['bash', str(ROOT / 'scripts/webapp-install.sh'), '--launcher', str(launcher), 'Fixture 工作', 'https://fixture.invalid/', 'web-browser'], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.app.DATA / 'entries/fixture-工作.json'
        before = entry.read_bytes()
        scripts = self.home / 'fake-scripts'
        scripts.mkdir()
        remover = scripts / 'webapp-remove.sh'
        remover.write_bytes((ROOT / 'scripts/webapp-remove.sh').read_bytes())
        (scripts / 'webapp-stop.sh').write_text('#!/bin/sh\nexit 1\n')
        result = subprocess.run(['bash', str(remover), 'Fixture 工作'], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not stop', result.stderr)
        self.assertEqual(entry.read_bytes(), before)

    def test_cli_success_and_invalid_config(self):
        command = [str(ROOT / '.venv/bin/python'), '-B', str(ROOT / 'scripts/webapp.py')]
        result = subprocess.run([*command, 'config-read', 'fixture-工作'], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        snap = json.loads(result.stdout)
        result = subprocess.run([*command, 'config-write', 'fixture-工作', '--revision', snap['revision'], '--json', json.dumps(dict(fields=snap['fields']))], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['saved'])
        (self.app.APPS / 'fixture-工作.conf').write_bytes(b'\xff')
        result = subprocess.run([*command, 'config-read', 'fixture-工作'], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['code'], 'invalid_config')

    def test_edit_is_busy_throughout_launch_wait(self):
        snap = self.read()
        lock = self.app.STATE / 'fixture-工作/launch.lock'
        with lock.open('a+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(self.app.EditError) as error:
                self.write(snap)
            self.assertEqual(error.exception.code, 'busy')
        self.write(snap)

    def test_scripts_do_not_require_external_flock(self):
        bin_dir = self.home / 'no-external-flock'
        bin_dir.mkdir()
        forbidden = bin_dir / 'flock'
        forbidden.write_text('#!/bin/sh\necho unexpected-external-flock >&2\nexit 99\n')
        forbidden.chmod(0o755)
        self.env['PATH'] = str(bin_dir) + os.pathsep + self.env['PATH']
        self.install(managed=True)
        result = subprocess.run(['bash', str(ROOT / 'scripts/webapp-remove.sh'), 'Fixture 工作'],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.app.APPS / 'fixture-工作.conf').exists())

    def test_cli_errors_are_structured_and_redacted(self):
        result = subprocess.run([str(ROOT / '.venv/bin/python'), '-B', str(ROOT / 'scripts/webapp.py'), 'config-write', 'Fixture 工作', '--revision', 'bad', '--json', '{"secret command":1}'], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        error = json.loads(result.stdout)
        self.assertEqual(error['code'], 'validation')
        self.assertNotIn('secret command', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()

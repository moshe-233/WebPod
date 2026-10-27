#!/usr/bin/env python3
"""Short-lived Webapp Maker commands. Services write directly to app.log.

No per-app daemon, HTTP health check, or resident sampler. Noctalia calls
status in batches after launch. Chromium --app and per-app profiles remain
the window implementation.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

DATA = Path.home() / ".local/share/webapp-maker"
APPS = DATA / "apps"
STATE = Path.home() / ".local/state/webapp-maker"
PROFILES = DATA / "profiles"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
BROWSERS = ("chromium", "google-chrome-stable", "google-chrome", "brave", "vivaldi", "microsoft-edge-stable")
FAMILY = re.compile(r"^(chromium|chrome|google-chrome|brave|microsoft-edge|opera|vivaldi|helium)")


class LaunchError(Exception):
    pass


def safe_name(name):
    return re.sub(r"[\W_]+", "-", name.lower(), flags=re.UNICODE).strip("-")


def read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save(safe, data):
    directory = STATE / safe
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    tmp = directory / f"run.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp, directory / "run.json")


@contextmanager
def operation(safe):
    """Serialize only spawn/publication/stop, never the URL or browser wait."""
    directory = STATE / safe
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    # Removal holds this lock across stop and deletion. Reuse only a verified
    # inherited descriptor for this exact safe and lock inode.
    inherited = os.environ.get("WEBAPP_OPERATION_LOCK_FD", "")
    if inherited.isdigit() and os.environ.get("WEBAPP_OPERATION_LOCK_SAFE") == safe:
        fd = int(inherited)
        try:
            held = os.fstat(fd)
            expected = (directory / "operation.lock").stat()
            if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
                raise LaunchError("invalid inherited operation lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LaunchError("inherited operation lock is unavailable") from exc
        yield
        return
    with (directory / "operation.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def process(pid):
    if not isinstance(pid, int) or pid <= 1:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return dict(pid=pid, pgid=int(fields[2]), start=int(fields[19]),
                    ticks=int(fields[11]) + int(fields[12]))
    except (OSError, ValueError, IndexError):
        return None


def same_process(record, key="pid", start_key="pid_start_ticks"):
    current = process(record.get(key))
    return current if current and current["start"] == record.get(start_key) else None


def valid_url(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in url):
            raise ValueError("expected a complete HTTP(S) URL")
        parts.port
    except ValueError as exc:
        raise LaunchError(f"invalid URL: {exc}") from exc
    return url


def match_url(pattern, line):
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise LaunchError(f"invalid URL regex: {exc}") from exc
    match = compiled.search(ANSI.sub("", line))
    if match is None:
        return None
    value = match.group("url") if "url" in compiled.groupindex else match.group(1) if compiled.groups else match.group(0)
    return valid_url(value.strip()) if value else None


def config(path):
    values = {}
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise LaunchError(f"cannot read config {path}: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match is None:
            raise LaunchError(f"{path}:{number}: invalid config line")
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        values[key] = value
    if not values.get("name", "").strip() or not values.get("start_cmd", "").strip():
        raise LaunchError("config needs name and start_cmd")
    if not safe_name(values["name"]):
        raise LaunchError("app name has no letters or digits")
    if "url_source" not in values and "url_template" in values:
        raise LaunchError("legacy supervisor config: migrate to fixed URL or full URL regex first")
    mode = values.setdefault("url_source", "fixed")
    values.setdefault("url", "")
    values.setdefault("url_regex", "")
    values.setdefault("extract_script", "")
    values.setdefault("flags", "")
    values["cwd"] = str(Path(values.get("cwd") or Path.home()).expanduser().resolve())
    if not Path(values["cwd"]).is_dir():
        raise LaunchError(f"working directory does not exist: {values['cwd']}")
    try:
        values["url_timeout_s"] = float(values.get("url_timeout_s") or "15")
        flags = shlex.split(values["flags"])
    except ValueError as exc:
        raise LaunchError(f"invalid timeout or Chromium flags: {exc}") from exc
    if not math.isfinite(values["url_timeout_s"]) or values["url_timeout_s"] <= 0:
        raise LaunchError("URL timeout must be positive and finite")
    if any(flag.split("=", 1)[0] in ("--app", "--app-id", "--user-data-dir") for flag in flags):
        raise LaunchError("Chromium flags cannot override the PWA URL or independent profile")
    values["auto_open"] = values.get("auto_open", "true").strip().lower() == "true"
    if mode == "fixed":
        valid_url(values["url"])
    elif mode == "regex":
        if not values["url_regex"]:
            raise LaunchError("URL regex is required")
        try:
            re.compile(values["url_regex"])
        except re.error as exc:
            raise LaunchError(f"invalid URL regex: {exc}") from exc
    elif mode == "script":
        if not values["extract_script"].strip():
            raise LaunchError("extract_script is required")
    else:
        raise LaunchError(f"unknown URL source: {mode}")
    return values


class EditError(Exception):
    """Public errors deliberately contain no submitted command or file contents."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


EDIT_FIELDS = ('start_cmd', 'url_source', 'url', 'url_regex', 'extract_script',
               'url_timeout_s', 'cwd', 'auto_open', 'flags')


def desktop_decode(value):
    escapes = {'s': ' ', 'n': '\n', 'r': '\r', 't': '\t', '\\': '\\'}
    return re.sub(r'\\([snrt\\])', lambda m: escapes[m[1]], value)


def desktop_encode(value):
    value = value.replace('\\', '\\\\').replace('\t', '\\t').replace('\r', '\\r').replace('\n', '\\n')
    return '\\s' + value[1:] if value.startswith(' ') else value


def desktop_arg(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'


@contextmanager
def edit_operation(safe):
    directory = STATE / safe
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    # Match launch's lock order and retain its whole-pipeline guard while
    # editing, including the interval after spawn while URL extraction waits.
    with (directory / 'launch.lock').open('a+') as launch_lock, (directory / 'operation.lock').open('a+') as lock:
        try:
            fcntl.flock(launch_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EditError('busy', 'Another application operation is in progress.') from exc
        if (directory / 'config-edit.pending').exists():
            raise EditError('io', 'An interrupted configuration save requires recovery from config-edit.pending before editing.')
        yield


def edit_sources(name):
    if not isinstance(name, str) or not name.strip() or '/' in name or any(c in name for c in '\r\n\x00'):
        raise EditError('validation', 'Invalid application name.')
    safe = safe_name(name)
    if not safe:
        raise EditError('validation', 'Invalid application name.')
    conf = APPS / (safe + '.conf')
    desktops = Path.home() / '.local/share/applications'
    desktop = desktops / (name + '.desktop')
    # The CLI accepts the original name or its immutable safe identity.
    if not desktop.is_file():
        candidates = [p for p in desktops.glob('*.desktop') if safe_name(p.stem) == safe]
        if len(candidates) > 1:
            raise EditError('invalid_config', 'Application identity is ambiguous.')
        desktop = candidates[0] if candidates else None
    entry = DATA / 'entries' / (safe + '.json')
    if not conf.is_file() and desktop is None:
        raise EditError('not_found', 'Application configuration was not found.')
    paths = [conf, entry]
    if desktop is not None:
        paths.append(desktop)
    for path in paths:
        if path.is_symlink():
            raise EditError('invalid_config', 'Symbolic configuration files cannot be edited.')
    return safe, conf, entry, desktop


def raw_conf(path):
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$', line)
        if match is None:
            raise EditError('invalid_config', 'The application configuration cannot be read.')
        key, value = match.groups()
        values[key] = value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value
    return values


def desktop_properties(path):
    values = {}
    section = False
    for line in path.read_text().splitlines():
        if line.startswith('['):
            section = line == '[Desktop Entry]'
        elif section and '=' in line:
            key, value = line.split('=', 1)
            values[key] = desktop_decode(value)
    return values


def source_revision(paths):
    import hashlib
    digest = hashlib.sha256()
    for path in paths:
        label = str(path).encode()
        digest.update(len(label).to_bytes(8, 'big') + label)
        content = path.read_bytes() if path.exists() else None
        digest.update(b'absent' if content is None else b'present' + len(content).to_bytes(8, 'big') + content)
    return 'sha256:' + digest.hexdigest()


def edit_snapshot(name):
    safe, conf, entry, desktop = edit_sources(name)
    properties = desktop_properties(desktop) if desktop else {}
    paths = [conf, entry]
    if desktop:
        paths.append(desktop)
    icon = properties.get('Icon', '')
    icon_path = Path(icon) if icon.startswith('/') else Path.home() / '.local/share/icons/hicolor/256x256/apps' / (safe + '.png')
    if icon_path.is_symlink():
        raise EditError('invalid_config', 'Symbolic icon files cannot be edited.')
    paths.append(icon_path)
    local_icon = Path.home() / ".local/share/icons/hicolor/256x256/apps" / (safe + ".png")
    if local_icon != icon_path:
        paths.append(local_icon)
    recovery = []
    if conf.exists():
        try:
            config(conf)
            raw = raw_conf(conf)
        except LaunchError as exc:
            raise EditError('invalid_config', 'The managed configuration is invalid.') from exc
        title = raw['name']
        defaults = dict(url_source='fixed', url='', url_regex='', extract_script='', url_timeout_s='15', cwd='', flags='', auto_open='true')
        fields = {key: raw.get(key, defaults.get(key, '')) for key in EDIT_FIELDS}
        fields['auto_open'] = fields['auto_open'].strip().lower() == 'true'
        kind = 'managed'
    else:
        title = properties.get('Name', desktop.stem)
        kind = 'website'
        if entry.exists():
            try:
                data = json.loads(entry.read_text())
                if not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('safe') != safe or data.get('kind') != 'website' or data.get('name') != title:
                    raise ValueError
                fields = {key: data[key] for key in ('url', 'flags')}
                validate_edit_fields(fields, kind)
            except (ValueError, KeyError, TypeError, EditError) as exc:
                raise EditError('invalid_config', 'The website configuration record is invalid.') from exc
        else:
            fields = dict(url=None, flags=None)
            recovery = ['url', 'flags']
    if safe_name(title) != safe:
        raise EditError('invalid_config', 'Application identity does not match its configuration.')
    return dict(schema_version=1, safe=safe, name=title, kind=kind,
                revision=source_revision(paths), recovery_required=recovery, fields=fields,
                icon_ref=icon), (conf, entry, desktop, icon_path), paths


def validate_edit_fields(fields, kind):
    allowed = set(EDIT_FIELDS if kind == 'managed' else ('url', 'flags'))
    if not isinstance(fields, dict) or set(fields) != allowed:
        raise EditError('validation', 'Configuration fields do not match the application type.')
    for key, value in fields.items():
        if key == 'auto_open':
            if type(value) is not bool:
                raise EditError('validation', 'Automatic opening must be a boolean.')
        elif not isinstance(value, str) or any(c in value for c in '\n\r\x00'):
            raise EditError('validation', 'Configuration values must be single-line strings.')
    try:
        flags = shlex.split(fields['flags'])
        if any(f.split('=', 1)[0] in ('--app', '--app-id', '--user-data-dir') for f in flags):
            raise ValueError
        if kind == 'website' or fields['url_source'] == 'fixed':
            valid_url(fields['url'])
        if kind == 'managed':
            if not fields['start_cmd'].strip() or fields['url_source'] not in ('fixed', 'regex', 'script'):
                raise ValueError
            timeout = float(fields['url_timeout_s'] or '15')
            if not math.isfinite(timeout) or timeout <= 0 or not Path(fields['cwd'] or Path.home()).expanduser().is_dir():
                raise ValueError
            if fields['url_source'] == 'regex':
                if not fields['url_regex']:
                    raise ValueError
                re.compile(fields['url_regex'])
            elif fields['url_source'] == 'script' and not fields['extract_script'].strip():
                raise ValueError
    except (ValueError, LaunchError, re.error) as exc:
        raise EditError('validation', 'Invalid URL, service, working directory, timeout, expression or browser parameters.') from exc


def read_edit_config(name):
    try:
        safe, _, _, _ = edit_sources(name)
        with edit_operation(safe):
            return edit_snapshot(name)[0]
    except UnicodeError as exc:
        raise EditError('invalid_config', 'The application configuration is not valid UTF-8.') from exc
    except OSError as exc:
        raise EditError('io', 'The application configuration could not be read.') from exc


def replace_conf_fields(text, fields):
    remaining = set(fields)
    lines = []
    for line in text.splitlines(keepends=True):
        match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
        if match and match[1] in fields:
            key = match[1]
            value = fields[key]
            value = str(value).lower() if isinstance(value, bool) else value
            lines.append(f'{key} = "{value}"\n')
            remaining.discard(key)
        else:
            lines.append(line)
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    for key in fields:
        if key in remaining:
            value = fields[key]
            value = str(value).lower() if isinstance(value, bool) else value
            lines.append(f'{key} = "{value}"\n')
    return ''.join(lines)


def replace_desktop_fields(text, fields):
    lines, remaining, section = [], set(fields), False
    for line in text.splitlines(keepends=True):
        if line.startswith('['):
            if section:
                for key in fields:
                    if key in remaining:
                        lines.append(f'{key}={desktop_encode(fields[key])}\n')
                        remaining.discard(key)
            section = line.strip() == '[Desktop Entry]'
        key = line.split('=', 1)[0] if '=' in line else ''
        if section and key in fields:
            lines.append(f'{key}={desktop_encode(fields[key])}\n')
            remaining.discard(key)
        else:
            lines.append(line)
    if section:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.extend(f'{key}={desktop_encode(fields[key])}\n' for key in fields if key in remaining)
    return ''.join(lines)


def commit_edit(safe, changes):
    """Each target uses same-directory replace; durable journal permits manual recovery.

    Multiple replacements are not a filesystem transaction. An unfinished journal
    blocks later reads/writes, including when rollback itself fails.
    """
    import tempfile
    pending = STATE / safe / 'config-edit.pending'
    staged, backups = [], []
    journal_ready = False
    try:
        for path, content, mode in changes:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix='.config-edit-', dir=path.parent)
            staged.append((path, Path(tmp)))
            with os.fdopen(fd, 'wb') as handle:
                os.fchmod(handle.fileno(), mode)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if path.parent == APPS and path.suffix == '.conf':
                try:
                    config(Path(tmp))
                except LaunchError as exc:
                    raise EditError('validation', 'The updated managed configuration is invalid.') from exc
        pending.mkdir(mode=0o700)
        for index, (path, _, _) in enumerate(changes):
            present = path.exists()
            backup = pending / str(index)
            if present:
                backup.write_bytes(path.read_bytes())
                backup.chmod(0o600)
                with backup.open('rb') as handle:
                    os.fsync(handle.fileno())
            backups.append(dict(path=str(path), present=present, mode=(path.stat().st_mode & 0o777) if present else None, backup=str(index)))
        journal = pending / 'manifest.json'
        journal.write_text(json.dumps(backups))
        journal.chmod(0o600)
        with journal.open('rb') as handle:
            os.fsync(handle.fileno())
        sync_directory(pending)
        sync_directory(pending.parent)
        journal_ready = True
        for path, tmp in staged:
            os.replace(tmp, path)
            sync_directory(path.parent)
        shutil.rmtree(pending)
        sync_directory(pending.parent)
    except OSError as exc:
        recovered = not journal_ready
        if journal_ready:
            try:
                for item in backups:
                    path = Path(item['path'])
                    if item['present']:
                        backup = pending / item['backup']
                        fd, temporary = tempfile.mkstemp(prefix='.config-rollback-', dir=path.parent)
                        with os.fdopen(fd, 'wb') as handle:
                            os.fchmod(handle.fileno(), item['mode'])
                            handle.write(backup.read_bytes())
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, path)
                    else:
                        path.unlink(missing_ok=True)
                    sync_directory(path.parent)
                recovered = True
            except OSError:
                recovered = False
        if recovered and pending.exists():
            shutil.rmtree(pending)
        message = 'Configuration save failed; previous files were restored.' if recovered else 'Configuration save and rollback failed; config-edit.pending requires recovery.'
        raise EditError('io', message) from exc
    finally:
        for _, tmp in staged:
            tmp.unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_edit_config(name, expected_revision, request):
    if not isinstance(request, dict) or set(request) - {'fields', 'icon_ref', 'recovery_confirmed'} or 'fields' not in request:
        raise EditError('validation', 'Invalid configuration request.')
    if 'recovery_confirmed' in request and type(request['recovery_confirmed']) is not bool:
        raise EditError('validation', 'Recovery confirmation must be a boolean.')
    icon_ref = request.get('icon_ref', '')
    if not isinstance(icon_ref, str) or any(c in icon_ref for c in '\n\r\x00'):
        raise EditError('validation', 'Invalid icon reference.')
    try:
        safe, _, _, _ = edit_sources(name)
        with edit_operation(safe):
            snapshot, (conf, entry, desktop, _), paths = edit_snapshot(name)
            fields = request['fields']
            validate_edit_fields(fields, snapshot['kind'])
            if snapshot['recovery_required'] and request.get('recovery_confirmed') is not True:
                raise EditError('validation', 'Confirm the recovered website URL and browser parameters explicitly.')
            if not isinstance(expected_revision, str) or expected_revision != snapshot['revision']:
                raise EditError('conflict', 'Configuration changed externally. Reload before saving.')
            changes, desktop_updates = [], {}
            if snapshot['kind'] == 'managed':
                content = replace_conf_fields(conf.read_text(), fields)
                changes.append((conf, content.encode(), 0o600))
            else:
                properties = desktop_properties(desktop)
                # Only reuse the executable token, never infer old URL or flags.
                try:
                    executable = shlex.split(properties['Exec'].replace('%%', '%'))[0]
                    allowed = {DATA.resolve(), Path(__file__).resolve().parent}
                    if Path(executable).parent.resolve() not in allowed or Path(executable).name not in ('webapp-launch', 'webapp-launch.sh'):
                        raise ValueError
                except (KeyError, ValueError, IndexError) as exc:
                    raise EditError('invalid_config', 'Website launcher is not owned by this plugin.') from exc
                args = [executable, fields['url'], '--webapp-name', safe, *shlex.split(fields['flags'])]
                desktop_updates['Exec'] = ' '.join(desktop_arg(value) for value in args)
                metadata = dict(schema_version=1, safe=safe, name=snapshot['name'], kind='website', **fields)
                changes.append((entry, (json.dumps(metadata, ensure_ascii=False) + '\n').encode(), 0o600))
            if icon_ref:
                if desktop is None:
                    raise EditError('validation', 'An icon needs an existing desktop launcher.')
                icon = Path(icon_ref).expanduser()
                if icon.is_file():
                    target = Path.home() / '.local/share/icons/hicolor/256x256/apps' / (safe + '.png')
                    if target.is_symlink():
                        raise EditError('validation', 'Symbolic icon targets cannot be replaced.')
                    changes.append((target, icon.read_bytes(), 0o644))
                    desktop_updates['Icon'] = safe
                elif re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', icon_ref):
                    desktop_updates['Icon'] = icon_ref
                elif icon_ref.startswith(('https://', 'http://')):
                    try:
                        valid_url(icon_ref)
                    except LaunchError as exc:
                        raise EditError('validation', 'Invalid icon URL.') from exc
                    downloaded = subprocess.run(['curl', '-fsSL', '--max-time', '10', '--max-filesize', '10485760', icon_ref], capture_output=True)
                    if downloaded.returncode:
                        raise EditError('io', 'The requested icon could not be prepared.')
                    target = Path.home() / '.local/share/icons/hicolor/256x256/apps' / (safe + '.png')
                    if target.is_symlink():
                        raise EditError('validation', 'Symbolic icon targets cannot be replaced.')
                    changes.append((target, downloaded.stdout, 0o644))
                    desktop_updates['Icon'] = safe
                else:
                    raise EditError('validation', 'Icon must be an existing file, HTTP(S) URL or theme name.')
            if desktop is not None:
                text = replace_desktop_fields(desktop.read_text(), desktop_updates)
                changes.append((desktop, text.encode(), desktop.stat().st_mode & 0o777))
            # Check again after icon preparation, within the same safe lock.
            if source_revision(paths) != expected_revision:
                raise EditError('conflict', 'Configuration changed externally. Reload before saving.')
            commit_edit(safe, changes)
            result = edit_snapshot(name)[0]
            restart = snapshot['kind'] == 'managed' and any(fields[k] != snapshot['fields'][k] for k in EDIT_FIELDS if k != 'flags')
            return dict(schema_version=1, safe=safe, revision=result['revision'], saved=True, restart_required=restart)
    except UnicodeError as exc:
        raise EditError('invalid_config', 'The application configuration is not valid UTF-8.') from exc
    except OSError as exc:
        raise EditError('io', 'Configuration could not be saved.') from exc


def endpoint(url):
    try:
        parts = urlsplit(url)
        return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None, None


def local_endpoint(url):
    host, port = endpoint(url)
    return (host, port) if host in ("127.0.0.1", "::1", "localhost") else None


def connects(address):
    try:
        with socket.create_connection(address, timeout=0.04):
            return True
    except OSError:
        return False


def browser_lookup():
    try:
        return subprocess.Popen(["xdg-settings", "get", "default-web-browser"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    except OSError:
        return None


def close_lookup(lookup):
    if lookup is None:
        return
    terminate_group(lookup.pid, signal.SIGTERM)
    try:
        lookup.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        terminate_group(lookup.pid, signal.SIGKILL)
        lookup.wait()
    terminate_group(lookup.pid, signal.SIGKILL)
    if not lookup.stdout.closed:
        lookup.stdout.close()


def browser_binary(lookup):
    desktop = ""
    if lookup is not None:
        try:
            output, _ = lookup.communicate(timeout=1)
            desktop = output.strip() if lookup.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            close_lookup(lookup)
    if FAMILY.match(desktop):
        for base in (Path.home() / ".local/share/applications", Path("/usr/share/applications")):
            try:
                lines = (base / desktop).read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                if line.startswith("Exec="):
                    try:
                        fields = shlex.split(line[5:])
                    except ValueError:
                        continue
                    if fields and shutil.which(fields[0]):
                        return fields[0]
    for name in BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    return ""


def open_pwa(conf, safe, record, browser):
    if not browser:
        raise LaunchError("no Chromium-family browser found for PWA window")
    profile = PROFILES / safe
    profile.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen([browser, f"--app={record['url']}", f"--user-data-dir={profile}",
                                 *shlex.split(conf["flags"])], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise LaunchError(f"PWA window failed: {exc}") from exc
    if not same_process(record, "browser_pid", "browser_start_ticks"):
        current = process(proc.pid)
        record.update(browser_pid=proc.pid, browser_start_ticks=current["start"] if current else None)


def pending_service(record, proc):
    if proc is not None and proc.poll() is not None:
        raise LaunchError(f"service exited with code {proc.returncode} before URL was available")
    if record.get("owned") and not same_process(record):
        # The child can exit between the first poll and the /proc read.
        # Reap it before reporting a generic stopped/cancelled message.
        if proc is not None and proc.poll() is not None:
            raise LaunchError(f"service exited with code {proc.returncode} before URL was available")
        raise LaunchError("service stopped before URL was available")


def log_lines(path, record, proc, deadline, done=None):
    """Read only complete lines from this instance's log; no live stdout pipe."""
    with path.open("rb") as handle:
        while time.monotonic() < deadline and (done is None or not done.is_set()):
            pending_service(record, proc)
            position = handle.tell()
            raw = handle.readline()
            if raw.endswith(b"\n"):
                yield ANSI.sub("", raw.decode("utf-8", "replace").rstrip("\r\n"))
            else:
                handle.seek(position)
                time.sleep(0.01)


def terminate_group(pgid, sig):
    if not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return


def script_url(conf, path, record, proc, deadline):
    import queue
    import selectors
    import threading

    errors = queue.SimpleQueue()
    done = threading.Event()
    with os.fdopen(os.open(path.with_name("extractor.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab", buffering=0) as error_log:
        os.fchmod(error_log.fileno(), 0o600)
        extractor = subprocess.Popen(["/bin/sh", "-c", conf["extract_script"]], cwd=conf["cwd"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error_log,
                                     start_new_session=True, bufsize=0)
        os.set_blocking(extractor.stdin.fileno(), False)

        def feed():
            try:
                for line in log_lines(path, record, proc, deadline, done):
                    payload = memoryview((line + "\n").encode())
                    while payload and not done.is_set():
                        try:
                            count = os.write(extractor.stdin.fileno(), payload)
                            payload = payload[count:]
                        except BlockingIOError:
                            done.wait(0.01)
            except (OSError, ValueError, LaunchError) as exc:
                errors.put(str(exc))

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        selector = selectors.DefaultSelector()
        selector.register(extractor.stdout, selectors.EVENT_READ)
        os.set_blocking(extractor.stdout.fileno(), False)
        output = b""
        try:
            while time.monotonic() < deadline:
                pending_service(record, proc)
                if not errors.empty():
                    raise LaunchError(f"URL extractor input failed: {errors.get()}")
                for key, _ in selector.select(0.01):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    output += chunk
                    while b"\n" in output:
                        line, output = output.split(b"\n", 1)
                        value = ANSI.sub("", line.decode("utf-8", "replace")).strip()
                        if value.startswith(("http://", "https://")):
                            return valid_url(value)
                if extractor.poll() is not None:
                    raise LaunchError(f"URL extractor exited with code {extractor.returncode}")
            raise LaunchError(f"URL extraction timed out after {conf['url_timeout_s']:g}s; service remains running")
        finally:
            done.set()
            selector.close()
            terminate_group(extractor.pid, signal.SIGTERM)
            try:
                extractor.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                terminate_group(extractor.pid, signal.SIGKILL)
                extractor.wait()
            terminate_group(extractor.pid, signal.SIGKILL)
            thread.join(timeout=0.2)
            try:
                extractor.stdin.close()
            except BrokenPipeError:
                error_log.write(b"[launcher] extractor input closed during cleanup\n")
            extractor.stdout.close()


def resolve_url(conf, safe, record, proc):
    deadline = time.monotonic() + conf["url_timeout_s"]
    if conf["url_source"] == "fixed":
        address = local_endpoint(conf["url"])
        while address and not connects(address):
            pending_service(record, proc)
            if time.monotonic() >= deadline:
                raise LaunchError(f"URL port did not listen within {conf['url_timeout_s']:g}s; service remains running")
            time.sleep(0.01)
        return conf["url"]
    path = STATE / safe / "app.log"
    if conf["url_source"] == "script":
        return script_url(conf, path, record, proc, deadline)
    pattern = re.compile(conf["url_regex"])
    for line in log_lines(path, record, proc, deadline):
        match = pattern.search(line)
        if match:
            value = match.group("url") if "url" in pattern.groupindex else match.group(1) if pattern.groups else match.group(0)
            if value:
                return valid_url(value.strip())
    raise LaunchError(f"URL regex did not match within {conf['url_timeout_s']:g}s; service remains running")


def launch(conf, force_start=False):
    safe = safe_name(conf["name"])
    directory = STATE / safe
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    with (directory / "launch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"{safe}: already starting")
            return
        with operation(safe):
            record = read_json(directory / "run.json")
            existing = same_process(record)
            external = (not record.get("owned") and record.get("url")
                        and local_endpoint(record["url"]) and connects(local_endpoint(record["url"])))
            reuse = bool(existing or external)
            proc = None
            if not reuse:
                address = local_endpoint(conf["url"]) if conf["url_source"] == "fixed" else None
                if address and connects(address):
                    record = dict(pid=None, pid_start_ticks=None, pgid=None, owned=False, url=conf["url"],
                                  started_epoch=None, error=None, exit_code=None)
                    reuse = True
                else:
                    log = directory / "app.log"
                    if log.exists():
                        os.replace(log, directory / "app.previous.log")
                    env = dict(os.environ, WEBAPP_NAME=conf["name"])
                    try:
                        with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb", buffering=0) as handle:
                            os.fchmod(handle.fileno(), 0o600)
                            proc = subprocess.Popen(["/bin/sh", "-c", conf["start_cmd"]], cwd=conf["cwd"],
                                                    stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                                                    start_new_session=True, env=env)
                    except OSError as exc:
                        raise LaunchError(f"service could not start: {exc}") from exc
                    current = process(proc.pid)
                    record = dict(pid=proc.pid, pid_start_ticks=current["start"] if current else None,
                                  pgid=proc.pid, owned=True, url=None, started_epoch=time.time(),
                                  spawned_at=time.monotonic(), error=None, exit_code=None)
            record.setdefault("run_id", f"{os.getpid()}:{time.monotonic_ns()}")
            save(safe, record)
        should_open = conf["auto_open"] or (reuse and not force_start)
        lookup = browser_lookup() if should_open else None
        try:
            if not record.get("url"):
                record["url"] = resolve_url(conf, safe, record, proc)
            browser = browser_binary(lookup) if should_open else None
            with operation(safe):
                current = read_json(directory / "run.json")
                if current.get("run_id") != record["run_id"]:
                    raise LaunchError("launch was cancelled before opening the PWA")
                if record.get("owned") and not same_process(record):
                    raise LaunchError("service stopped before opening the PWA")
                record["error"] = None
                if should_open:
                    open_pwa(conf, safe, record, browser)
                save(safe, record)
            print(f"{safe}: running")
        except (LaunchError, OSError) as exc:
            with operation(safe):
                current = read_json(directory / "run.json")
                if current.get("run_id") == record["run_id"]:
                    current["error"] = str(exc)
                    current["exit_code"] = proc.returncode if proc is not None and proc.poll() is not None else None
                    save(safe, current)
            raise
        finally:
            close_lookup(lookup)


def uses_profile(args, profile, executable):
    if f"--user-data-dir={profile}" in args or any(
            arg == "--user-data-dir" and i + 1 < len(args) and args[i+1] == profile for i, arg in enumerate(args)):
        return True
    # Chromium can rewrite argv into one process-title string. Restrict
    # that fallback to a browser executable and a complete profile field.
    return bool(len(args) == 1 and FAMILY.match(executable) and re.search(
        r"(?:^|\s)--user-data-dir(?:=|\s+)" + re.escape(profile) + r"(?=\s|$)", args[0]))


def profile_processes(safe, record):
    profile = str(PROFILES / safe)
    found = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().decode("utf-8", "replace").rstrip("\0").split("\0")
            executable = (entry / "exe").resolve(strict=True).name
        except OSError:
            continue
        if uses_profile(args, profile, executable):
            found.add(int(entry.name))
    window = same_process(record, "browser_pid", "browser_start_ticks")
    if window:
        found.add(window["pid"])
    return found


def group_running(pgid):
    if not pgid:
        return False
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            current = process(int(entry.name))
            if current and current["pgid"] == pgid:
                return True
    return False


def stop(name, grace):
    safe = safe_name(name)
    if not safe:
        raise LaunchError("app name has no letters or digits")
    if not math.isfinite(grace) or grace < 0:
        raise LaunchError("stop grace must be non-negative and finite")
    with operation(safe):
        record = read_json(STATE / safe / "run.json")
        targets = profile_processes(safe, record)
        service = same_process(record)
        group = record.get("pgid") if record.get("owned") and service and service["pgid"] == record.get("pgid") else None
        terminate_group(group, signal.SIGTERM)
        browser_groups = set()
        for pid in targets:
            current = process(pid)
            if current:
                if current["pgid"] == pid:
                    browser_groups.add(pid)
                    terminate_group(pid, signal.SIGTERM)
                else:
                    try: os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError: continue
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and (group_running(group) or any(process(pid) for pid in targets)):
            time.sleep(0.02)
        if group_running(group):
            terminate_group(group, signal.SIGKILL)
        for pgid in browser_groups:
            if group_running(pgid):
                terminate_group(pgid, signal.SIGKILL)
        for pid in profile_processes(safe, record):
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: continue
        for lock in (PROFILES / safe).glob("Singleton*"):
            lock.unlink(missing_ok=True)
        record.update(pid=None, pid_start_ticks=None, pgid=None, url=None, error=None, exit_code=None,
                      browser_pid=None, browser_start_ticks=None, owned=False, run_id=None)
        save(safe, record)
    print(f"{safe}: stopped")


def listeners():
    """One port/PID query per status batch, never a prerequisite for launch."""
    try:
        out = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchError(f"listener query failed: {exc}") from exc
    if out.returncode:
        raise LaunchError(f"listener query failed: {out.stderr.strip() or 'ss exited ' + str(out.returncode)}")
    ports = {}
    for line in out.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            address, raw_port = fields[3].rsplit(":", 1)
            port = int(raw_port)
        except ValueError:
            continue
        match = re.search(r"pid=(\d+)", line)
        ports.setdefault(port, []).append((address.strip("[]"), int(match.group(1)) if match else None))
    return ports


def status_record(path, ports, sampled):
    safe = path.stem
    raw = read_json(STATE / safe / "run.json")
    try:
        conf = config(path)
    except LaunchError as exc:
        return dict(safe=safe, name=safe, state="stopped", error=str(exc), cpu_pct=None, mem_mb=None)
    current = same_process(raw)
    url = raw.get("url") or (conf["url"] if conf["url_source"] == "fixed" else "")
    _, port = endpoint(url)
    address = local_endpoint(url)
    hosts = {"localhost": {"127.0.0.1", "::1", "0.0.0.0", "::", "*"},
             "127.0.0.1": {"127.0.0.1", "0.0.0.0", "*"}, "::1": {"::1", "::", "*"}}
    matching = [pid for host, pid in ports.get(port, []) if address and host in hosts[address[0]]]
    owner = next((found for pid in matching if (found := process(pid))), None)
    owned = raw.get("owned", False)
    if current and owner and owner["pgid"] == raw.get("pgid"):
        current = owner
    external = not owned and bool(raw.get("url")) and bool(matching)
    if external:
        current = owner
    live = bool(current) or external
    state = "starting" if live and not raw.get("url") and not raw.get("error") else "running" if live else "stopped"
    mem = None
    if current:
        try:
            pages = int(Path(f"/proc/{current['pid']}/statm").read_text().split()[1])
            mem = pages * os.sysconf("SC_PAGESIZE") / (1024 * 1024)
        except (OSError, ValueError, IndexError):
            mem = None
    return dict(safe=safe, name=conf["name"], state=state, pid=current["pid"] if current else None,
                pgid=raw.get("pgid"), pid_start_ticks=current["start"] if current else None, port=port,
                resolved_url=url, started_epoch=raw.get("started_epoch"), updated_epoch=time.time(),
                exit_code=raw.get("exit_code"), error=raw.get("error"), cpu_pct=None, mem_mb=mem,
                cpu_ticks=current["ticks"] if current else None, clock_ticks=os.sysconf("SC_CLK_TCK"),
                sampled_at=sampled)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("launch", "open", "start", "validate"):
        sub.add_parser(command).add_argument("name")
    sub.add_parser("config-read").add_argument("name")
    writer = sub.add_parser("config-write")
    writer.add_argument("name")
    writer.add_argument("--revision", required=True)
    writer.add_argument("--json", required=True)
    stopper = sub.add_parser("stop")
    stopper.add_argument("name"); stopper.add_argument("grace", nargs="?", type=float, default=3.0)
    sub.add_parser("status").add_argument("name", nargs="?")
    sub.add_parser("list")
    matcher = sub.add_parser("match")
    matcher.add_argument("--regex", required=True); matcher.add_argument("--line", required=True)
    args = parser.parse_args()
    try:
        if args.command == "config-read":
            print(json.dumps(read_edit_config(args.name), ensure_ascii=False))
        elif args.command == "config-write":
            try:
                request = json.loads(args.json)
            except ValueError as exc:
                raise EditError("validation", "Invalid JSON configuration request.") from exc
            print(json.dumps(write_edit_config(args.name, args.revision, request), ensure_ascii=False))
        elif args.command == "match":
            print(json.dumps(dict(url=match_url(args.regex, args.line)), ensure_ascii=False))
        elif args.command == "stop":
            stop(args.name, args.grace)
        elif args.command in ("list", "status"):
            sampled = time.monotonic()
            ports = listeners()
            if args.command == "status" and args.name:
                path = APPS / f"{safe_name(args.name)}.conf"
                if not path.is_file():
                    raise LaunchError(f"no app config for {args.name}")
                print(json.dumps(status_record(path, ports, sampled), ensure_ascii=False))
            else:
                print(json.dumps(dict(apps=[status_record(p, ports, sampled) for p in sorted(APPS.glob('*.conf'))]), ensure_ascii=False))
        else:
            path = Path(args.name).expanduser() if args.command in ("start", "validate") else APPS / f"{safe_name(args.name)}.conf"
            conf = config(path)
            if args.command == "validate":
                print("valid")
            else:
                launch(conf, force_start=args.command == "start")
        return 0
    except EditError as exc:
        print(json.dumps(dict(schema_version=1, code=exc.code, message=str(exc))))
        return 1
    except (LaunchError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

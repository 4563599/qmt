# coding: utf-8
"""Desktop launcher process control; no strategy calculations or Redis access."""
from collections import deque
import ctypes
from ctypes import wintypes
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback

from qmt_shm_bridge import CHANNEL, PROTOCOL, STALE_AFTER, SharedSlot, _channel_name
from stream_events import decode_log_event
from launch_date import normalize_reference


class EventBuffer:
    """Never stall a strategy on a slow UI. The complete output stays on disk."""
    def __init__(self, limit=8000):
        self.limit = limit
        self.items = deque()
        self.lock = threading.Lock()
        self.dropped = 0

    def put(self, kind, value, source=None):
        with self.lock:
            if len(self.items) >= self.limit:
                self.items.popleft()
                self.dropped += 1
            self.items.append({'kind': kind, 'value': value, 'source': source})

    def drain(self, count=400):
        with self.lock:
            result = []
            if self.dropped:
                result.append({'kind': 'overflow', 'value': self.dropped})
                self.dropped = 0
            for _ in range(min(count, len(self.items))):
                result.append(self.items.popleft())
            return result


class LogModel:
    def __init__(self):
        self.lines = deque(maxlen=6000)
        self.records = deque(maxlen=6000)
        self.details = deque(maxlen=3000)
        self.errors = self.warnings = 0
        self.states = {'panzhong': 'pending', 'ths': 'pending'}
        self.last_source = ''
        self.in_error = False
        self.error_sources = {}
        self.last_line_at = None

    def append(self, line, source='system'):
        line = line.rstrip('\r\n')
        self.last_line_at = time.monotonic()
        self.lines.append(line)
        marker = re.search(r'\[(?:START|RUNNING|DONE|CONFIG)\]\[([^]]+)\]', line)
        if marker:
            self.last_source = 'ths' if marker[1].casefold() == 'ths' else 'panzhong'
            for marker, state in (('[START]', 'initializing'), ('[RUNNING]', 'running'),
                                  ('[DONE]', 'completed')):
                if marker in line:
                    self.states[self.last_source] = state
        explicit = bool(re.search(r'^\[\d{4}-\d\d-\d\d |^\[(?:FAILED|START|RUNNING|DONE|LAUNCHER|STOPPED)', line))
        self.in_error = self.error_sources.get(source, False)
        is_error = bool(re.search(r'\[(?:ERROR|FAILED|SHM FAILED)\]', line))
        is_warning = '[WARN]' in line or '[WATCH]' in line
        if explicit and not is_error:
            self.in_error = False
        if is_error:
            self.errors += 1
            self.in_error = True
        if line.startswith('Traceback (most recent call last):'):
            if not self.in_error:
                self.errors += 1
            self.in_error = True
        if is_warning:
            self.warnings += 1
        level = 'error' if self.in_error else ('warning' if is_warning else 'info')
        self.error_sources[source] = self.in_error
        self.records.append({'source': source, 'text': line, 'level': level})
        if self.in_error or is_warning:
            self.details.append(line)
        return level


def read_bridge_status(channel=CHANNEL):
    """Only read the heartbeat slot; never compete with live market-data RPC."""
    try:
        with SharedSlot(_channel_name(channel) + '_status', 4096) as slot:
            status = slot.read()
        status = status if isinstance(status, dict) else {}
        age = time.time() - status.get('heartbeat', 0)
        config = status.get('strategy_config') or {}
        valid_busy = False
        if status.get('state') == 'busy' and age >= STALE_AFTER:
            with SharedSlot(_channel_name(channel) + '_request', 1024 * 1024) as slot:
                request = slot.read() or {}
            valid_busy = (request.get('server_id') == status.get('server_id')
                          and request.get('id') == status.get('last_request_id')
                          and isinstance(request.get('deadline'), (int, float))
                          and time.time() < request['deadline'])
        online = (status.get('protocol') == PROTOCOL and status.get('state') in ('ready', 'busy')
                  and (0 <= age < STALE_AFTER or valid_busy))
        return {'online': online, 'state': status.get('state', 'missing'),
                'age': age, 'version': config.get('version'),
                'check_trading_time': config.get('CHECK_TRADING_TIME'),
                'enabled': config.get('enabled', {}), 'reference_date': config.get('REFERENCE_DATE')}
    except Exception as error:
        return {'online': False, 'state': 'error', 'error': str(error)}


def quiet_is_unexpected(status, states, now=None):
    """Watchdog expectations only; never schedule or skip strategy callbacks."""
    now = now or datetime.now()
    enabled = status.get('enabled', {})
    if any(enabled.get(key, True) and state in ('pending', 'initializing')
           for key, state in states.items()):
        return True
    if status.get('reference_date'):
        return True
    if enabled.get('panzhong', True) and status.get('check_trading_time') is False:
        return True
    clock = now.hour * 100 + now.minute
    return now.weekday() < 5 and (925 <= clock <= 1130 or 1300 <= clock <= 1505)


def runner_is_active(channel=CHANNEL):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenMutexW(0x00100000, False, 'Local\\QmtPzRunner_' + channel)
    if handle:
        kernel.CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    if error not in (0, 2):
        raise OSError(error, 'Cannot inspect external runner instance lock')
    return False


def console_python():
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    if not executable.is_file():
        raise FileNotFoundError('Python runtime is missing: ' + str(executable))
    return str(executable)


class ManagedProcess:
    def __init__(self, root, events, log_dir=None, command=None, running_probe=runner_is_active):
        self.root, self.events = Path(root), events
        self.log_dir = Path(log_dir) if log_dir else self.root / 'logs' / 'launcher'
        self.command = command or [console_python(), '-X', 'utf8', '-X', 'faulthandler', '-u',
                                   str(self.root / 'external_runner.py'), '--run',
                                   '--strategy', 'both', '--control-stdin', '--ui-events']
        self.running_probe = running_probe
        self.process = self.reader = None
        self.log_path = None
        self.raw_log_path = None
        self.stop_requested = False
        self.started_at = None

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self):
        return self.process.pid if self.process is not None else None

    def start(self, reference_date=None):
        reference_date = normalize_reference(reference_date)
        command = list(self.command)
        if reference_date is not None:
            command.extend(['--reference-date', reference_date])
        if self.running or (self.reader is not None and self.reader.is_alive()) or self.running_probe():
            raise RuntimeError('An external runner is already running. Close the old window first.')
        folder = self.log_dir / datetime.now().strftime('%Y%m%d')
        folder.mkdir(parents=True, exist_ok=True)
        self.log_path = folder / (datetime.now().strftime('%H%M%S_%f') + '.log')
        self.raw_log_path = self.log_path.with_suffix('.raw.log')
        handle = self.log_path.open('w', encoding='utf-8', buffering=1)
        split_logs = {}
        try:
            split_logs['raw'] = self.raw_log_path.open('w', encoding='utf-8', buffering=1)
            for source in ('panzhong', 'ths', 'system'):
                split_logs[source] = self.log_path.with_suffix('.%s.log' % source).open(
                    'w', encoding='utf-8', buffering=1)
        except BaseException:
            handle.close()
            for item in split_logs.values(): item.close()
            raise
        self.stop_requested = False
        environment = os.environ.copy()
        environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        process = None
        try:
            handle.write('[LAUNCHER] %s command=%s\n' % (
                datetime.now().isoformat(timespec='seconds'), subprocess.list2cmdline(command)))
            process = subprocess.Popen(
                command, cwd=str(self.root), env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace', bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.process = process
            header = '[LAUNCHER] pid=%s\n' % process.pid
            handle.write(header)
        except BaseException:
            if process is not None:
                process.terminate()
                process.wait(timeout=10)
                process.stdout.close()
                process.stdin.close()
            handle.close()
            for item in split_logs.values(): item.close()
            raise
        self.started_at = time.monotonic()
        self.events.put('line', header)

        def collect():
            try:
                for line in iter(lambda: process.stdout.readline(65536), ''):
                    record = decode_log_event(line)
                    split_logs['raw'].write(record['text'] + '\n')
                    if not record.get('display', True):
                        continue
                    text = record.get('display_text', record['text']) + '\n'
                    handle.write(text)
                    split_logs[record['source']].write(text)
                    self.events.put('line', text, source=record['source'])
                code = process.wait()
            except Exception:
                detail = '[FAILED] Log collection failed\n' + traceback.format_exc()
                try:
                    handle.write(detail)
                except OSError:
                    pass
                self.events.put('line', detail)
                if process.poll() is None:
                    process.terminate()
                code = process.wait()
            finally:
                process.stdout.close()
                if process.stdin is not None:
                    process.stdin.close()
                for item in split_logs.values(): item.close()
            summary = {'returncode': code, 'pid': process.pid, 'command': command,
                       'log_path': str(self.log_path), 'raw_log_path': str(self.raw_log_path),
                       'stop_requested': self.stop_requested}
            try:
                handle.write('\n[LAUNCHER] exit_code=%s stop_requested=%s pid=%s\n' % (
                    code, self.stop_requested, process.pid))
                handle.close()
            except OSError as error:
                self.events.put('line', '[FAILED] Cannot save final log: ' + str(error))
            self.events.put('exit', summary)

        self.reader = threading.Thread(target=collect, daemon=True, name='qmt-gui-output')
        self.reader.start()

    def request_stop(self):
        if not self.running:
            return False
        if not self.stop_requested:
            self.stop_requested = True
            self.process.stdin.write('STOP\n')
            self.process.stdin.flush()
        return True

    def force_stop(self):
        if self.running:
            self.stop_requested = True
            self.process.terminate()

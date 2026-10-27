# coding: utf-8
"""SSE trading-calendar gate and one-shot notification for an existing GUI."""
import ctypes
from ctypes import wintypes
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

from qmt_shm_bridge import CHANNEL

SIGNAL_NAME = 'Local\\QmtLauncherAutoStart_' + CHANNEL
CHINA = timezone(timedelta(hours=8))


def china_now():
    return datetime.now(CHINA)


def fetch_calendar(year):
    # Use the user's existing Tushare authorization; never copy or reset tokens.
    import tushare as ts
    frame = ts.pro_api(timeout=8).trade_cal(
        exchange='SSE', start_date='%s0101' % year, end_date='%s1231' % year,
        fields='cal_date,is_open')
    return frame.to_dict(orient='records')


def validate_calendar(year, rows):
    result = {}
    expected = set()
    current = date(year, 1, 1)
    while current.year == year:
        expected.add(current.strftime('%Y%m%d'))
        current += timedelta(days=1)
    for row in rows:
        key = str(row['cal_date'])
        value = int(row['is_open'])
        if key not in expected or key in result or value not in (0, 1):
            raise ValueError('Invalid SSE trading calendar')
        result[key] = bool(value)
    if set(result) != expected:
        raise ValueError('SSE calendar does not cover the complete year')
    return result


class TradingCalendar:
    def __init__(self, root, fetcher=fetch_calendar):
        self.folder = Path(root) / 'runtime' / 'calendar'
        self.fetcher = fetcher
        self.loaded = {}

    def load(self, year):
        if year in self.loaded:
            return self.loaded[year]
        path = self.folder / ('sse_%s.json' % year)
        cached = None
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            cached = validate_calendar(year, payload['rows'])
            updated = datetime.fromisoformat(payload['updated_at'])
            if (china_now() - updated.astimezone(CHINA)).total_seconds() < 7 * 86400:
                self.loaded[year] = cached
                return cached
        except (OSError, ValueError, KeyError, TypeError):
            cached = None
        try:
            rows = self.fetcher(year)
            result = validate_calendar(year, rows)
            payload = {'exchange': 'SSE', 'source': 'Tushare trade_cal', 'year': year,
                       'updated_at': china_now().isoformat(),
                       'rows': [{'cal_date': key, 'is_open': int(value)} for key, value in sorted(result.items())]}
            self.folder.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding='utf-8')
            temporary.replace(path)
        except Exception:
            if cached is None:
                raise
            result = cached
        self.loaded[year] = result
        return result

    def is_session(self, day):
        return self.load(day.year)[day.strftime('%Y%m%d')]


def scheduled_decision(now, calendar):
    result = {'allowed': False, 'date': now.strftime('%Y-%m-%d')}
    clock = now.hour * 100 + now.minute
    if now.weekday() >= 5 or not 915 <= clock <= 1505:
        return dict(result, reason='outside_window')
    if not calendar.is_session(now.date()):
        return dict(result, reason='market_closed')
    return dict(result, allowed=True, reason='trading_day')


def record_schedule(root, message):
    folder = Path(root) / 'logs' / 'launcher' / china_now().strftime('%Y%m%d')
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'scheduled_start.log').open('a', encoding='utf-8') as handle:
        handle.write('[%s] %s\n' % (china_now().isoformat(timespec='seconds'), message))


class AutoStartSignal:
    def __init__(self, name=SIGNAL_NAME, existing=False):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateEventW.restype = wintypes.HANDLE
        self.kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self.kernel.SetEvent.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        if existing:
            self.kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel.OpenEventW.restype = wintypes.HANDLE
            self.handle = self.kernel.OpenEventW(0x00100002, False, name)
        else:
            self.handle = self.kernel.CreateEventW(None, False, False, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def request(self):
        if not self.kernel.SetEvent(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def consume(self):
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result == 0xffffffff:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def launch_scheduled_gui(root):
    """A short scheduled job; a window left open never blocks tomorrow's trigger."""
    try:
        with AutoStartSignal(existing=True) as signal:
            signal.request()
        record_schedule(root, 'Requested start in existing console; no duplicate process created.')
        return
    except FileNotFoundError:
        pass
    from external_runner import SingleInstance
    from qmt_shm_bridge import BridgeError
    try:
        with SingleInstance('desktop_ui_' + CHANNEL):
            pass
    except BridgeError:
        raise RuntimeError('Existing console needs to be reopened to enable scheduled start')
    python = Path(sys.executable).with_name('pythonw.exe')
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 1
    subprocess.Popen([str(python), '-X', 'utf8', str(Path(root) / 'launcher_gui.py'), '--auto-start'],
                     cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, startupinfo=startup)
    record_schedule(root, 'Opened console with validated trading-day automatic start.')

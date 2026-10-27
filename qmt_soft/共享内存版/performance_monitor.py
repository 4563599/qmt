# coding: utf-8
"""Independent, passive performance collection for the external QMT strategies.

Reads existing logs/CSV/PZ performance records; writes only shm:performance:*.
Never starts a strategy, changes its business data, or sends notifications.
"""
import argparse
import base64
import collections
import csv
from datetime import datetime, timedelta
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import threading
import time

import psutil
import redis
from qmt_shm_bridge import CHANNEL, SharedSlot, _channel_name

PREFIX = 'shm:performance'
RETENTION = 8 * 86400
MAX_ROWS = 20000
PUBLISH_LUA = '''
if redis.call('SADD', KEYS[2], ARGV[1]) == 0 then return 0 end
redis.call('LPUSH', KEYS[1], ARGV[2])
redis.call('LTRIM', KEYS[1], 0, ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
redis.call('EXPIRE', KEYS[2], ARGV[4])
return 1
'''


def number(value, integer=False):
    try:
        result = float(value)
        return int(result) if integer else result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def safe_error(value):
    text = str(value or '')
    text = re.sub(r'https?://\S+', '[链接已隐藏]', text)
    text = re.sub(r'(?i)(password|access_token|secret|token)\s*[=:]\s*\S+', r'\1=[已隐藏]', text)
    return text[:240]


def finish_record(row):
    row['id'] = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    return row


def decode_performance(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        value = json.loads(base64.b64decode(raw, validate=True))
    if not isinstance(value, dict):
        raise ValueError('Invalid performance record')
    return value


def normalize_record(strategy, raw, run_started, run_id):
    started = str(raw.get('started_at') or raw.get('time') or '')
    if not re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', started) or started < run_started:
        return None
    total = number(raw.get('total_seconds'))
    if total is None or total < 0:
        return None
    finished = str(raw.get('finished_at') or raw.get('failed_at') or raw.get('recorded_at') or '')
    if not finished:
        finished = (datetime.strptime(started, '%Y-%m-%d %H:%M:%S') + timedelta(seconds=total)).strftime('%Y-%m-%d %H:%M:%S')
    row = {'strategy': strategy, 'phase': 'cycle', 'run_id': run_id,
           'source': 'pz_redis' if strategy == 'panzhong' else 'ths_csv',
           'started_at': started, 'finished_at': finished,
           'date': str(raw.get('ref_date') or raw.get('date') or started[:10]).replace('-', '')[:8],
           'status': str(raw.get('status') or ('failed' if raw.get('failure_stage') else 'recorded')),
           'total_seconds': total, 'cycle_count': number(raw.get('cycle_count'), True),
           'actual_interval_seconds': number(raw.get('actual_interval_seconds')),
           'failed_stage': str(raw.get('failed_stage') or raw.get('failure_stage') or ''),
           'error': safe_error(raw.get('error')), 'warnings': safe_error(raw.get('warnings'))}
    for key in ('tick_seconds', 'history_seconds', 'filter_seconds', 'build_seconds',
                'cleaning_seconds', 'ths_seconds', 'close_overlay_seconds', 'redis_seconds',
                'ladder_seconds', 'pz1_cache_seconds', 'qy2_cache_seconds',
                'pz_redis_seconds', 'pz1_redis_seconds', 'qy1_redis_seconds', 'qy2_redis_seconds',
                'kline_history_seconds', 'kline_realtime_seconds',
                'qy1_kline_history_seconds', 'qy1_kline_realtime_seconds',
                'qy2_kline_history_seconds', 'qy2_kline_realtime_seconds', 'cleaning_redis_seconds'):
        row[key] = number(raw.get(key))
    if strategy == 'panzhong':
        timing_keys = ('pz_redis_seconds', 'pz1_redis_seconds', 'qy1_redis_seconds', 'qy2_redis_seconds',
                       'kline_history_seconds', 'kline_realtime_seconds', 'qy1_kline_history_seconds',
                       'qy1_kline_realtime_seconds', 'qy2_kline_history_seconds', 'qy2_kline_realtime_seconds')
        known = [row[k] for k in timing_keys if row[k] is not None]
        row['redis_seconds'] = sum(known) if known else None
    for key in ('scanned_count', 'candidate_count', 'selected_count', 'gain_candidate_count',
                'pz1_selected_count', 'qy1_selected_count', 'qy2_selected_count', 'success_count',
                'failed_count', 'purple_count', 'red_count', 'gain_only_count', 'busy_skip_count'):
        row[key] = number(raw.get(key), True)
    return finish_record(row)


class CsvTail:
    def __init__(self, path):
        self.path, self.offset, self.header, self.pending = Path(path), 0, None, b''

    def read(self):
        if not self.path.exists():
            return []
        if self.path.stat().st_size < self.offset:
            self.offset, self.header, self.pending = 0, None, b''
        with self.path.open('rb') as handle:
            handle.seek(self.offset)
            if self.header is None:
                line = handle.readline()
                if not line.endswith(b'\n'):
                    return []
                self.header = next(csv.reader([line.decode('utf-8-sig').strip()]))
            data = self.pending + handle.read(2 * 1024 * 1024)
            self.offset = handle.tell()
        end = data.rfind(b'\n')
        if end < 0:
            self.pending = data
            return []
        self.pending = data[end + 1:]
        rows = csv.reader(io.StringIO(data[:end + 1].decode('utf-8-sig')))
        return [dict(zip(self.header, row)) for row in rows if len(row) == len(self.header)]


class LogTracker:
    def __init__(self, strategy, run_started, run_id):
        self.strategy, self.run_started, self.run_id = strategy, run_started, run_id
        self.records, self.start, self.emitted = [], None, False
        self.state = {'initialized': False, 'history_loaded': 0, 'history_failed': 0,
                      'history_total': 0, 'history_seconds': None, 'latest_event': ''}

    def feed(self, line):
        found = re.match(r'^\[([\d-]+ [\d:]+)\] \[(\w+)\] (.*)', line)
        if not found:
            return
        at, level, message = found.groups()
        if at < self.run_started:
            return
        self.state['latest_event'] = at
        if '[START] PZ' in message or '连续同步程序初始化' in message:
            self.start = at
        progress = re.search(r'progress=(\d+)/(\d+) loaded=(\d+) failed=(\d+)', message)
        if progress:
            done, total, loaded, failed = map(int, progress.groups())
            self.state.update(history_progress=done, history_total=total, history_loaded=loaded, history_failed=failed)
        complete = re.search(r'completed loaded=(\d+) failed=(\d+) seconds=([\d.]+)', message)
        if complete:
            loaded, failed, seconds = complete.groups()
            self.state.update(history_loaded=int(loaded), history_failed=int(failed), history_seconds=float(seconds))
        if self.strategy == 'ths':
            history = re.search(r'完成：目标(\d+)只，成功(\d+)只，失败(\d+)只', message)
            if history:
                total, loaded, failed = map(int, history.groups())
                self.state.update(history_progress=total, history_total=total, history_loaded=loaded, history_failed=failed)
        ready = ('[TIMER]' in message if self.strategy == 'panzhong' else '清洗与K线发布成功' in message)
        if ready:
            self.state['initialized'] = True
            if self.start and not self.emitted:
                seconds = (datetime.strptime(at, '%Y-%m-%d %H:%M:%S') - datetime.strptime(self.start, '%Y-%m-%d %H:%M:%S')).total_seconds()
                self.records.append(finish_record({
                    'strategy': self.strategy, 'phase': 'initialization', 'source': 'strategy_log',
                    'run_id': self.run_id, 'date': at[:10].replace('-', ''),
                    'started_at': self.start, 'finished_at': at, 'status': 'success',
                    'total_seconds': seconds, 'history_seconds': self.state['history_seconds'],
                    'success_count': self.state['history_loaded'], 'failed_count': self.state['history_failed'],
                    'timing_note': '初始化总耗时按秒级日志时间计算'}))
                self.state['initialization_seconds'] = seconds
                self.state['initialized_at'] = at
                self.emitted = True


def load_source(path):
    spec = importlib.util.spec_from_file_location('monitor_source_' + hashlib.sha1(str(path).encode('utf-8')).hexdigest(), str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def redis_for(module):
    return redis.Redis(host=module.REDIS_HOST, port=module.REDIS_PORT, password=module.REDIS_PASSWORD,
                       db=module.REDIS_DB, decode_responses=True, socket_timeout=3, socket_connect_timeout=3)


class PerformanceMonitor:
    def __init__(self, pid, channel=CHANNEL, sources=None, reference_date=None):
        self.process = psutil.Process(pid)
        self.created = self.process.create_time()
        self.started = datetime.fromtimestamp(self.created).strftime('%Y-%m-%d %H:%M:%S')
        self.run_id = '%s-%s' % (pid, int(self.created))
        self.channel = channel
        base = Path(__file__).resolve().parent.parent
        paths = sources or {'盘中': base / '盘中.py', 'THS': base / 'ths筛选.py'}
        self.modules = {('ths' if label == 'THS' else 'panzhong'): load_source(path) for label, path in paths.items()}
        from strategy_runtime import apply_source_options, bridge_config
        with SharedSlot(_channel_name(channel) + '_status', 4096) as status_slot:
            config = bridge_config(status_slot.read())
        for kind, module in self.modules.items():
            apply_source_options(module, kind, config)
            if reference_date is not None:
                from launch_date import normalize_reference
                module.REFERENCE_DATE = normalize_reference(reference_date)
        self.clients = {key: redis_for(module) for key, module in self.modules.items()}
        self.publisher = redis_for(load_source(base / '盘中.py'))
        self.trackers = {key: LogTracker(key, self.started, self.run_id) for key in self.modules}
        self.csv, self.logs, self.pending, self.seen = {}, {}, collections.deque(maxlen=10000), set()
        self.latest = {}
        self.pz_heads, self.pz_lengths = {}, {}
        self.slot = SharedSlot(_channel_name(channel) + '_status', 4096)
        self.last_error = ''

    def enqueue(self, record):
        if record and record.get('phase') == 'cycle':
            previous = self.latest.get(record['strategy'])
            if not previous or record['started_at'] >= previous['started_at']:
                self.latest[record['strategy']] = record
        if record and record['id'] not in self.seen:
            self.pending.append(record)
            self.seen.add(record['id'])
            if len(self.seen) > 50000:
                self.seen = {item['id'] for item in self.pending}

    def read_logs(self, strategy, path):
        if not path.exists():
            return
        offset, carry = self.logs.get(str(path), (0, b''))
        size = path.stat().st_size
        if size < offset:
            offset, carry = 0, b''
        with path.open('rb') as handle:
            # Existing logs can be large; the latest initialization is at the end.
            if not offset and size > 1024 * 1024:
                handle.seek(size - 1024 * 1024)
                handle.readline()
            else:
                handle.seek(offset)
            data = carry + handle.read(1024 * 1024)
            offset = handle.tell()
        complete = data.rfind(b'\n')
        if complete >= 0:
            for line in data[:complete + 1].decode('utf-8', errors='replace').splitlines():
                self.trackers[strategy].feed(line)
            carry = data[complete + 1:]
        else:
            carry = data
        self.logs[str(path)] = (offset, carry)
        tracker = self.trackers[strategy]
        while tracker.records:
            self.enqueue(tracker.records.pop(0))

    def collect_pz(self, date):
        client = self.clients['panzhong']
        key = 'pz:performance:' + date
        head = client.lrange(key, 0, 0)
        if not head or head[0] == self.pz_heads.get(date):
            return
        length = client.llen(key)
        previous = self.pz_lengths.get(date)
        count = min(length - previous, 5000) if previous is not None and length > previous else 200
        for raw in reversed(client.lrange(key, 0, count - 1)):
            try:
                self.enqueue(normalize_record('panzhong', decode_performance(raw), self.started, self.run_id))
            except (ValueError, TypeError):
                continue
        self.pz_heads[date], self.pz_lengths[date] = head[0], length

    def step(self):
        now = datetime.now()
        dates = {now.strftime('%Y%m%d'), self.started[:10].replace('-', '')}
        for strategy, module in self.modules.items():
            reference_date = str(getattr(module, 'REFERENCE_DATE', '') or '')
            strategy_dates = dates | ({reference_date} if len(reference_date) == 8 else set())
            for date in sorted(strategy_dates):
                folder = Path(module.LOG_BASE_DIR) / date / module.STRATEGY_DIR_NAME
                self.read_logs(strategy, folder / '运行日志.log')
                if strategy == 'ths':
                    path = folder / '性能监控.csv'
                    reader = self.csv.setdefault(str(path), CsvTail(path))
                    latest_status = self.clients[strategy].hgetall(module._status_key(date))
                    for raw in reader.read():
                        if raw.get('time') == latest_status.get('start_time') and latest_status.get('state') in ('success', 'partial_success', 'failed'):
                            raw.update(status=latest_status['state'], failure_stage=latest_status.get('failure_stage', ''), error=latest_status.get('error', ''))
                        self.enqueue(normalize_record(strategy, raw, self.started, self.run_id))
                else:
                    self.collect_pz(date)
        while self.pending:
            row = self.pending[0]
            date = row['date']
            key = '%s:records:%s:%s' % (PREFIX, date, row['strategy'])
            self.publisher.eval(PUBLISH_LUA, 2, key, PREFIX + ':seen:' + date,
                                row['id'], json.dumps(row, ensure_ascii=False, allow_nan=False), MAX_ROWS - 1, RETENTION)
            self.pending.popleft()
        bridge = self.slot.read()
        heartbeat_age = time.time() - bridge.get('heartbeat', 0) if bridge else None
        states = {}
        for key, tracker in self.trackers.items():
            state = dict(tracker.state)
            state['last_cycle'] = self.latest.get(key)
            state['state'] = ('running' if self.modules[key].is_trading_session(now) else 'waiting') if state['initialized'] else 'initializing'
            states[key] = state
        status = {'online': True, 'pid': self.process.pid, 'run_id': self.run_id,
                  'run_started_at': self.started, 'date': now.strftime('%Y-%m-%d'),
                  'updated_at': now.strftime('%Y-%m-%d %H:%M:%S'), 'updated_epoch': time.time(),
                  'channel': self.channel, 'strategies': states,
                  'bridge': {'state': bridge.get('state') if bridge else 'missing',
                             'heartbeat_age': round(heartbeat_age, 3) if heartbeat_age is not None else None},
                  'last_error': self.last_error}
        self.publisher.set(PREFIX + ':runtime:' + self.channel, json.dumps(status, ensure_ascii=False), ex=20)
        self.last_error = ''

    def run(self, stop_event=None):
        from external_runner import SingleInstance
        stop = stop_event or threading.Event()
        try:
            with SingleInstance(self.channel + '_perf'):
                while not stop.is_set() and self.process.is_running() and self.process.create_time() == self.created:
                    try:
                        self.step()
                    except Exception as error:
                        # No credentials, webhook URLs, or strategy changes in diagnostics.
                        self.last_error = type(error).__name__
                    stop.wait(2)
        finally:
            self.slot.close()
            for client in list(self.clients.values()) + [self.publisher]:
                client.close()


def start_monitor(pid, channel=CHANNEL, sources=None, stop_event=None, reference_date=None):
    def work():
        from qmt_shm_bridge import BridgeError
        for attempt in range(6):
            try:
                PerformanceMonitor(pid, channel, sources, reference_date).run(stop_event)
                return
            except BridgeError:
                if stop_event is not None and stop_event.wait(1):
                    return
                if stop_event is None:
                    time.sleep(1)
            except Exception as error:
                print('[PERFORMANCE] Collector unavailable: ' + type(error).__name__, flush=True)
                return
        print('[PERFORMANCE] Another collector is still active.', flush=True)
    thread = threading.Thread(target=work, daemon=True, name='shared-memory-performance')
    thread.start()
    return thread


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--watch-pid', type=int, required=True)
    parser.add_argument('--channel', default=CHANNEL)
    args = parser.parse_args()
    PerformanceMonitor(args.watch_pid, args.channel).run()

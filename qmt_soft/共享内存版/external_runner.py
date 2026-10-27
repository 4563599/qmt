# coding: utf-8
"""Run the original scanner outside QMT; transport market data via shared memory."""
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import hashlib
import importlib.util
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import traceback

from qmt_shm_bridge import BridgeCancelled, BridgeError, CHANNEL, RpcClient
from strategy_runtime import (StrategySettings, bridge_config, enabled_sources,
                              interval_seconds, strategy_kind)
from launch_date import normalize_reference, parse_date


class SingleInstance:
    """A Windows session mutex; never a pid file or a network listener."""
    def __init__(self, channel):
        if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', channel):
            raise BridgeError('Invalid channel')
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateMutexW(None, False, 'Local\\QmtPzRunner_' + channel)
        error = ctypes.get_last_error()
        if not self.handle:
            raise BridgeError('Cannot create external-program instance lock')
        if error == 183:  # ERROR_ALREADY_EXISTS
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise BridgeError('An external runner/check is already open for this channel')

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def decode_market(value):
    if isinstance(value, dict):
        if value.get('__qmt_frame__') == 1:
            import pandas as pd
            return pd.DataFrame(value['data'], columns=value['columns'], index=value['index'])
        return {key: decode_market(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_market(item) for item in value]
    return value


class MarketContext:
    """The ContextInfo subset used by the original Panzhong and THS scripts."""
    def __init__(self, client):
        self.client = client
        self.known_stocks = set()
        self.metadata = {}
        self.module = None
        self.timers = []
        self._bridge_time_setting = None
        self.runtime = None
        self.initializing = False
        self.progress_targets = set()
        self._progress_phase = None
        self._progress_at = 0.0
        self._progress_completed = set()
        self._history_progress_started = False

    def _progress_log(self, text):
        logger = getattr(self.module, 'log', None)
        if callable(logger):
            logger('[加载进度] ' + text)
        else:
            print('[加载进度] ' + text, flush=True)

    def begin_initialization(self):
        self.initializing = True
        self._initialization_started = time.monotonic()
        self._progress_log('初始化开始，正在连接Redis、订阅行情并读取股票资料。')

    def end_initialization(self):
        self.initializing = False
        self._progress_log('初始化完成，耗时 %.1f 秒。' % (time.monotonic() - self._initialization_started))

    def _metadata_progress(self, required, after=False):
        if not self.initializing:
            return
        now = time.monotonic()
        if required == self._progress_phase and now - self._progress_at < 5 and not after:
            return
        total = len(self.known_stocks | self.progress_targets)
        completed_key = required
        could_complete = (after and len(self.metadata) >= total
                          and completed_key not in self._progress_completed)
        if required == self._progress_phase and now - self._progress_at < 5 and not could_complete:
            return
        done = sum(bool(item.get(required)) for item in self.metadata.values())
        if required == self._progress_phase and now - self._progress_at < 5 and done < total:
            return
        self._progress_phase, self._progress_at = required, now
        if done >= total:
            self._progress_completed.add(completed_key)
        name = '股票名称与基础资料' if required == 'name' else '连板基础资料'
        self._progress_log('正在读取%s %d/%d，已用 %.1f 秒。' % (
            name, done, total, now - self._initialization_started))

    def bind(self, module):
        self.module = module

    def configure_runtime(self, status, label):
        config = bridge_config(status)
        if config and config.get('version', 1) not in (1, 2):
            raise BridgeError('Unsupported bridge configuration version')
        if config and config.get('version') == 2:
            self.runtime = StrategySettings(self.module, strategy_kind(label), config)
            print('[CONFIG][%s] reference_date=%s; history_reload=%s' % (
                label, self.module.REFERENCE_DATE or 'realtime', self.runtime.reload_time), flush=True)
            return True
        return self.apply_bridge_settings(status)

    def apply_bridge_settings(self, status):
        if self.runtime is not None:
            config = bridge_config(status)
            if not config or config.get('version') != 2:
                raise BridgeError('Bridge runtime configuration disappeared; restart external program')
            self.runtime.update_check(config)
            return True
        if not self.module or not hasattr(self.module, 'CHECK_TRADING_TIME'):
            return False
        config = status.get('strategy_config') if isinstance(status, dict) else None
        if config is None:
            return False
        if not isinstance(config, dict):
            raise BridgeError('Invalid bridge strategy configuration')
        if 'CHECK_TRADING_TIME' not in config:
            return False
        setting = config['CHECK_TRADING_TIME']
        if not isinstance(setting, bool):
            raise BridgeError('Bridge CHECK_TRADING_TIME must be True or False')
        self.module.CHECK_TRADING_TIME = setting
        if self._bridge_time_setting is not setting:
            print('[CONFIG] CHECK_TRADING_TIME=%s (from QMT shared-memory bridge)' % setting,
                  flush=True)
            self._bridge_time_setting = setting
        return True

    def subscribe_whole_quote(self, markets):
        return self.client.call('subscribe_whole_quote', [list(markets)])

    def get_stock_list_in_sector(self, sector):
        stocks = self.client.call('get_stock_list_in_sector', [sector]) or []
        self.known_stocks.update(stocks)
        if self.initializing:
            self.progress_targets.update(stocks)
        return stocks

    def _metadata_for(self, stock, required):
        self.known_stocks.add(stock)
        self._metadata_progress(required)
        if stock not in self.metadata or not self.metadata[stock].get(required):
            missing = self.known_stocks - set(self.metadata)
            missing.add(stock)
            missing = sorted(missing)
            for offset in range(0, len(missing), 512):
                batch = missing[offset:offset + 512]
                data = self.client.call('get_metadata', [batch])
                if not isinstance(data, dict):
                    raise BridgeError('Invalid QMT metadata response')
                self.metadata.update(data)
                self._metadata_progress(required, after=True)
        if stock not in self.metadata:
            raise BridgeError('QMT did not return instrument metadata')
        return self.metadata[stock]

    def get_stock_name(self, stock):
        return self._metadata_for(stock, 'name').get('name')

    def get_instrument_detail(self, stock):
        return self._metadata_for(stock, 'detail').get('detail')

    get_instrumentdetail = get_instrument_detail

    def get_full_tick(self, stocks):
        if not stocks:
            return {}
        result = self.client.call('get_full_tick', [list(stocks)])
        if self.initializing and isinstance(result, dict) and any(stock in ('SH', 'SZ', 'BJ') for stock in stocks):
            self.progress_targets.update(key for key in result if str(key).endswith(('.SH', '.SZ', '.BJ')))
        return result

    def get_market_data_ex(self, fields, stocks, **kwargs):
        if not stocks:
            return {}
        if self.initializing and not self._history_progress_started:
            self._history_progress_started = True
            total = len(getattr(self.module, 'g_stock_pool', None) or stocks)
            self._progress_log('开始读取日K历史数据，目标 %d 只；下方保留分批加载进度。' % total)
        result = {}
        for offset in range(0, len(stocks), 512):
            data = self.client.call('get_market_data_ex',
                                    [list(fields), list(stocks[offset:offset + 512])], kwargs)
            result.update(decode_market(data))
        return result

    def run_time(self, callback, interval, start_time, market):
        if market not in ('SH', 'SZ', 'BJ'):
            raise BridgeError('Unsupported scheduler market')
        self.add_timer(callback, interval, start_time)

    def add_timer(self, callback, interval, start_time, now=None):
        if callback not in ('scan_stocks', 'reload_history_data', 'publish_ths_kline'):
            raise BridgeError('Unsupported scanner timer callback')
        if not self.module or not callable(getattr(self.module, callback, None)):
            raise BridgeError('Scanner timer callback is missing')
        now = now or datetime.now()
        if interval != '1nDay':
            step = timedelta(seconds=interval_seconds(interval))
            next_due = now + step
        elif interval == '1nDay':
            step = timedelta(days=1)
            if self.runtime is not None and callback == 'reload_history_data':
                start_time = start_time[:11] + self.runtime.reload_time
            clock = datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S').time()
            next_due = datetime.combine(now.date(), clock)
            if next_due <= now:
                next_due += step
        self.timers.append({'callback': callback, 'step': step, 'next': next_due})

    def run_due(self, now=None):
        self.apply_bridge_settings(self.client.assert_online())
        now = now or datetime.now()
        # History refresh wins when both callbacks are due at 09:15.
        for timer in sorted(self.timers, key=lambda item: item['callback'] != 'reload_history_data'):
            if now >= timer['next']:
                self.client.assert_online()
                if timer['step'] >= timedelta(days=1):
                    timer['next'] = datetime.combine(now.date(), timer['next'].time()) + timer['step']
                else:
                    timer['next'] = now + timer['step']
                getattr(self.module, timer['callback'])(self)


def load_strategy(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise BridgeError('Original scanner source is missing: ' + str(path))
    module_name = 'qmt_external_' + hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, 'init', None)):
        raise BridgeError('Original scanner has no init(ContextInfo) function')
    return module


def check_bridge(client):
    """No scanner import, no Redis access and no DingTalk message."""
    ping = client.call('ping')
    context = MarketContext(client)
    context.subscribe_whole_quote(['SH', 'SZ'])
    stock = '000001.SZ'
    tick = context.get_full_tick([stock])
    data = context.get_market_data_ex(['open', 'high', 'low', 'close', 'volume', 'amount'],
                                      [stock], period='1d', count=5, dividend_type='front')
    frame = data.get(stock)
    rows = len(frame) if frame is not None else 0
    has_tick = isinstance(tick, dict) and isinstance(tick.get(stock), dict) and bool(
        tick[stock].get('lastPrice'))
    if not has_tick or not rows:
        raise BridgeError('Shared memory works, but QMT returned no sample tick/history data')
    return {'transport': ping['channel_type'], 'has_tick': has_tick,
            'history_rows': rows, 'sample_stock': stock,
            'strategy_config': ping.get('strategy_config')}


def strategy_sources(strategy='panzhong', source=None):
    parent = Path(__file__).resolve().parent.parent
    if strategy not in ('panzhong', 'ths', 'both'):
        raise BridgeError('Unknown strategy selection')
    if strategy == 'both':
        if source is not None:
            raise BridgeError('--source is for a single strategy; omit it with --strategy both')
        return {'盘中': parent / '盘中.py', 'THS': parent / 'ths筛选.py'}
    label = '盘中' if strategy == 'panzhong' else 'THS'
    default = parent / ('盘中.py' if strategy == 'panzhong' else 'ths筛选.py')
    return {label: Path(source).resolve() if source is not None else default}


class StrategySession:
    """One original source module, independent globals, timers and worker."""
    def __init__(self, label, source, client, no_dingtalk=False, reference_date=None):
        self.label, self.source = label, Path(source)
        self.client, self.no_dingtalk = client, no_dingtalk
        self.reference_date = normalize_reference(reference_date)
        self.module = self.context = None
        self.state = 'pending'

    def run(self, stop):
        if stop.is_set():
            return
        self.state = 'initializing'
        self.module = load_strategy(self.source)
        self.context = MarketContext(self.client)
        self.context.bind(self.module)
        applied = self.context.configure_runtime(self.client.assert_online(), self.label)
        if self.reference_date is not None:
            self.module.REFERENCE_DATE = self.reference_date
            print('[CONFIG][%s] launcher_reference_date=%s; mode=%s' % (
                self.label, self.reference_date or 'realtime',
                'historical_once_no_alerts' if self.reference_date else 'original_realtime'), flush=True)
        if (self.no_dingtalk or self.reference_date) and hasattr(self.module, 'DINGTALK_ENABLED'):
            self.module.DINGTALK_ENABLED = False
        if hasattr(self.module, 'CHECK_TRADING_TIME') and not applied:
            print('[CONFIG][%s] Old bridge has no CHECK_TRADING_TIME setting; using source value %s. '
                  'Import the updated qmt_shm_bridge.py to control it from QMT.' %
                  (self.label, self.module.CHECK_TRADING_TIME), flush=True)
        print('[START][%s] %s' % (self.label, self.source.resolve()), flush=True)
        self.context.begin_initialization()
        self.module.init(self.context)
        if not getattr(self.module, 'g_initialized', False):
            raise BridgeError('Original strategy initialization did not finish')
        if hasattr(self.module, 'g_redis_client') and self.module.g_redis_client is None:
            raise BridgeError('Original strategy did not establish its Redis client')
        self.context.end_initialization()
        if not self.context.timers:
            if self.reference_date:
                if hasattr(self.module, 'g_historical_done') and not self.module.g_historical_done:
                    raise BridgeError('历史选股未成功完成，请查看盘中日志中的失败阶段和错误详情。')
                status_key = getattr(self.module, '_status_key', None)
                if strategy_kind(self.label) == 'ths' and callable(status_key):
                    raw = self.module.g_redis_client.hgetall(status_key(self.reference_date))
                    status = {(key.decode() if isinstance(key, bytes) else key):
                              (value.decode() if isinstance(value, bytes) else value)
                              for key, value in raw.items()}
                    if status.get('state') not in ('success', 'partial_success'):
                        raise BridgeError('同花顺历史补数未成功完成：' + str(
                            status.get('error') or status.get('state') or '没有完成记录'))
            self.state = 'completed'
            print('[DONE][%s] Historical mode completed.' % self.label, flush=True)
            return
        self.state = 'running'
        print('[RUNNING][%s] Original timers registered; keep QMT and this window open.' % self.label,
              flush=True)
        while not stop.is_set():
            self.context.run_due()
            stop.wait(.1)
        self.state = 'stopped'


def run_sessions(client, sources, no_dingtalk=False, stop_event=None, reference_date=None):
    """Independent strategy workers, sharing a serialized RPC client, never slots."""
    sources = enabled_sources(sources, client.assert_online())
    stop = stop_event if stop_event is not None else threading.Event()
    errors = queue.Queue()
    sessions = [StrategySession(label, source, client, no_dingtalk, reference_date)
                for label, source in sources.items()]
    if not sessions:
        raise BridgeError('No original strategy source selected')
    for session in sessions:
        if not session.source.is_file():
            raise BridgeError('Original strategy source is missing: ' + str(session.source))

    def work(session):
        try:
            session.run(stop)
        except BridgeCancelled:
            session.state = 'stopped'
        except Exception as error:
            session.state = 'failed'
            errors.put((session.label, error))
            stop.set()

    threads = [threading.Thread(target=work, args=(session,), daemon=True,
                                name='qmt-external-' + session.label)
               for session in sessions]
    try:
        for thread in threads:
            thread.start()
        while any(thread.is_alive() for thread in threads):
            if stop.wait(.1):
                break
    finally:
        stop.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join()
    if not errors.empty():
        label, error = errors.get_nowait()
        raise BridgeError('[%s] %s: %s' % (label, type(error).__name__, error)) from error
    return sessions


def run_strategy(client, source, no_dingtalk=False):
    return run_sessions(client, {'盘中': Path(source)}, no_dingtalk)


def control_commands(stream, stop):
    """Poll the Windows pipe without holding stdin's CRT lock during DLL loads."""
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        yield from stream
        return
    if os.name != 'nt':
        yield from stream
        return
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    peek = kernel.PeekNamedPipe
    peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    peek.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(descriptor)
    pending = b''
    while not stop.wait(.05):
        available = wintypes.DWORD()
        if not peek(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in (109, 232):  # Broken pipe / parent closed its write end.
                return
            raise OSError(error, 'Cannot read launcher control pipe')
        if not available.value:
            continue
        data = os.read(descriptor, min(available.value, 4096))
        if not data:
            return
        pending += data
        while b'\n' in pending:
            command, pending = pending.split(b'\n', 1)
            yield command.decode('utf-8', errors='replace')
        if len(pending) > 8192:
            raise ValueError('Launcher control command is too long')


def read_control_commands(stream, stop):
    """A private parent/child pipe, never a QMT network or file endpoint."""
    try:
        for command in control_commands(stream, stop):
            if command.strip().upper() == 'STOP':
                print('[CONTROL] Stop requested by desktop launcher.', flush=True)
                stop.set()
                return
    except Exception:
        print('[FAILED] Desktop launcher control pipe failed.', flush=True)
        traceback.print_exc()
    finally:
        # A closed GUI must not leave an orphan scanner sending notifications.
        stop.set()


def validate_history_date(reference_date):
    if reference_date:
        from launcher_schedule import TradingCalendar
        print('[DATE] 历史补数 %s：正在核对交易日，尚未开始写入数据。' % reference_date, flush=True)
        if not TradingCalendar(Path(__file__).resolve().parent).is_session(parse_date(reference_date)):
            raise ValueError('%s 不是交易日，未启动历史补数。' % reference_date)


def main(argv=None):
    parser = argparse.ArgumentParser(description='QMT shared-memory scanner/check')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Check market data only (the default)')
    mode.add_argument('--run', action='store_true', help='Run original scanner with Redis/DingTalk')
    parser.add_argument('--source', type=Path, help='Override source for a single strategy')
    parser.add_argument('--strategy', choices=('panzhong', 'ths', 'both'), default='panzhong')
    parser.add_argument('--channel', default=CHANNEL)
    parser.add_argument('--wait', type=float, default=60, help='Seconds to wait for QMT bridge')
    parser.add_argument('--no-dingtalk', action='store_true')
    parser.add_argument('--reference-date', help='YYYY-MM-DD or YYYYMMDD: historical once; empty: realtime')
    parser.add_argument('--control-stdin', action='store_true',
                        help='Desktop launcher: STOP or closed stdin requests a graceful exit')
    parser.add_argument('--ui-events', action='store_true', help='Source-tagged log transport for desktop UI')
    args = parser.parse_args(argv)
    try:
        args.reference_date = normalize_reference(args.reference_date)
    except ValueError as error:
        parser.error(str(error))
    if not 0 < args.wait <= 600:
        parser.error('--wait must be between 0 and 600 seconds')
    if args.ui_events:
        from stream_events import event_output
        paths = {strategy_kind(label): path for label, path in strategy_sources(args.strategy, args.source).items()}
        with event_output(source_paths=paths):
            return _run_main(args)
    return _run_main(args)


def _run_main(args):
    client = None
    monitor_stop = None
    stop = threading.Event()
    try:
        if args.run:
            validate_history_date(args.reference_date)
        with SingleInstance(args.channel):
            client = RpcClient(args.channel, stop_event=stop)
            if args.control_stdin:
                threading.Thread(target=read_control_commands, args=(sys.stdin, stop),
                                 daemon=True, name='qmt-launcher-control').start()
            print('[WAIT] Start qmt_shm_bridge in QMT. Channel: ' + args.channel, flush=True)
            client.wait_ready(args.wait)
            report = check_bridge(client)
            print('[CHECK OK] Anonymous shared memory; sample=%s; tick=yes; history_rows=%s.' % (
                report['sample_stock'], report['history_rows']), flush=True)
            if args.run:
                import pandas
                import redis
                import requests
                selected = enabled_sources(strategy_sources(args.strategy, args.source), client.assert_online())
                # Passive performance collection runs independently of calculations.
                from performance_monitor import start_monitor
                import os
                monitor_stop = threading.Event()
                start_monitor(os.getpid(), args.channel, selected, monitor_stop,
                              reference_date=args.reference_date)
                print('[MODE] Original logic, Redis, logs and configured alerts run in external Python.',
                      flush=True)
                run_sessions(client, selected, no_dingtalk=args.no_dingtalk, stop_event=stop,
                             reference_date=args.reference_date)
                if stop.is_set():
                    print('[STOPPED] External strategy workers have exited.', flush=True)
            else:
                print('[CHECK ONLY] Scanner/Redis/DingTalk were not started.', flush=True)
        return 0
    except KeyboardInterrupt:
        print('\n[STOPPED] External program stopped.', flush=True)
        return 0
    except Exception as error:
        print('[FAILED] %s: %s' % (type(error).__name__, error), flush=True)
        if args.control_stdin:
            traceback.print_exc()
        return 2
    finally:
        if monitor_stop is not None:
            monitor_stop.set()
        if client is not None:
            client.close()


if __name__ == '__main__':
    sys.exit(main())

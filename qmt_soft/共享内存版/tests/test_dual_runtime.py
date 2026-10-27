from datetime import datetime, timedelta
import concurrent.futures
import importlib
from pathlib import Path
import threading
import time
import types
import unittest
from unittest.mock import patch

import test_bridge
from test_bridge import ROOT
from test_runner import runner_module


class DualTransportTests(unittest.TestCase):
    setUp = test_bridge.RpcTests.setUp
    tearDown = test_bridge.RpcTests.tearDown

    def test_two_strategy_threads_share_rpc_without_crossed_responses(self):
        def request(index):
            stock = '000001.SZ' if index % 2 else '600000.SH'
            return stock, self.client.call('get_metadata', [[stock]])
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(request, range(12)))
        for stock, result in results:
            self.assertEqual(set(result), {stock})

    def test_busy_history_request_can_exceed_short_heartbeat_window(self):
        self.stop.set()
        self.thread.join(5)
        request_id = 'slow-history-request'
        status = self.client.status.read()
        status.update(state='busy', heartbeat=time.time() - 16,
                      last_request_id=request_id)
        self.server.status.write(status)
        self.client.request.write({'id': request_id, 'server_id': self.server.server_id,
                                   'deadline': time.time() + 60})
        self.assertEqual(self.client.assert_online()['state'], 'busy')

    def test_stale_busy_without_matching_request_still_fails(self):
        self.stop.set()
        self.thread.join(5)
        status = self.client.status.read()
        status.update(state='busy', heartbeat=time.time() - 16,
                      last_request_id='no-such-request')
        self.server.status.write(status)
        with self.assertRaises(self.b.BridgeTimeout):
            self.client.assert_online()


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.r = runner_module()

    def test_original_gbk_ths_loads_as_independent_module(self):
        pz = self.r.load_strategy(ROOT.parent / '盘中.py')
        ths = self.r.load_strategy(ROOT.parent / 'ths筛选.py')
        self.assertNotEqual(pz.__name__, ths.__name__)
        self.assertTrue(callable(ths.publish_ths_kline))
        self.assertIsNot(pz.g_cycle_lock if hasattr(pz, 'g_cycle_lock') else pz.g_scan_lock,
                         ths.g_cycle_lock)

    def test_ths_callback_and_daily_time_are_preserved(self):
        client = types.SimpleNamespace(assert_online=lambda: {})
        context = self.r.MarketContext(client)
        calls = []
        context.bind(types.SimpleNamespace(publish_ths_kline=lambda c: calls.append('ths'),
                                           reload_history_data=lambda c: calls.append('reload')))
        base = datetime(2026, 10, 26, 9, 24, 59)
        context.add_timer('reload_history_data', '1nDay', '2024-01-01 09:25:00', now=base)
        context.add_timer('publish_ths_kline', '1nSecond', '2024-01-01 09:25:00', now=base)
        context.run_due(base + timedelta(seconds=1))
        self.assertEqual(calls, ['reload', 'ths'])

    def test_profiles_keep_original_source_paths(self):
        self.assertTrue(hasattr(self.r, 'strategy_sources'), 'strategy selection is missing')
        selected = self.r.strategy_sources('both')
        self.assertEqual(list(selected), ['盘中', 'THS'])
        self.assertEqual(selected['盘中'], ROOT.parent / '盘中.py')
        self.assertEqual(selected['THS'], ROOT.parent / 'ths筛选.py')
        with self.assertRaises(self.r.BridgeError):
            self.r.strategy_sources('both', ROOT.parent / '盘中.py')

    def test_supervisor_starts_strategies_independently_and_stops_cleanly(self):
        self.assertTrue(hasattr(self.r, 'run_sessions'), 'dual-strategy supervisor is missing')
        stop = threading.Event()
        pz_started, ths_started = threading.Event(), threading.Event()
        calls = []
        class Client:
            def assert_online(self): return {}
        def loader(path):
            label = Path(path).stem
            module = types.SimpleNamespace(g_initialized=False, g_redis_client=object())
            def init(context):
                if label == '盘中':
                    pz_started.set()
                    if not ths_started.wait(2):
                        raise AssertionError('THS did not start while PZ was initializing')
                else:
                    ths_started.set()
                module.g_initialized = True
                context.add_timer('scan_stocks', '1nSecond', '2020-01-01 00:00:00')
                context.timers[0]['next'] = datetime.now() - timedelta(seconds=1)
            def scan(context):
                calls.append(label)
                if len(calls) == 2: stop.set()
            module.init, module.scan_stocks = init, scan
            return module
        with patch.object(self.r, 'load_strategy', side_effect=loader):
            self.r.run_sessions(Client(), {'盘中': ROOT.parent / '盘中.py', 'THS': ROOT.parent / 'ths筛选.py'},
                                stop_event=stop)
        self.assertEqual(set(calls), {'盘中', 'ths筛选'})

    def test_failed_ths_init_is_not_reported_as_success(self):
        self.assertTrue(hasattr(self.r, 'run_sessions'), 'dual-strategy supervisor is missing')
        # The original THS sets g_initialized before connecting Redis. Check both.
        module = types.SimpleNamespace(g_initialized=True, g_redis_client=None,
                                       init=lambda c: None)
        client = types.SimpleNamespace(assert_online=lambda: {})
        with patch.object(self.r, 'load_strategy', return_value=module):
            with self.assertRaises(self.r.BridgeError):
                self.r.run_sessions(client, {'THS': ROOT.parent / 'ths筛选.py'})


if __name__ == '__main__':
    unittest.main()

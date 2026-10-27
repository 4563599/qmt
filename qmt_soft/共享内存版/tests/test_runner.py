import ast
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import threading
import time
import types
import unittest
import uuid

from test_bridge import ROOT, FakeContext, bridge_module


def runner_module():
    path = ROOT / 'external_runner.py'
    assert path.is_file(), 'external runner has not been created'
    spec = importlib.util.spec_from_file_location('external_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.r = runner_module()
        self.b = bridge_module()
        self.channel = 'runner_' + uuid.uuid4().hex
        self.context = FakeContext()
        self.server = self.b.BridgeServer(self.context, self.channel)
        self.stop = threading.Event()
        def serve():
            while not self.stop.is_set():
                self.server.serve_once()
                time.sleep(.001)
        self.thread = threading.Thread(target=serve)
        self.thread.start()
        self.client = self.b.RpcClient(self.channel, timeout=5)
        self.client.wait_ready(2)

    def tearDown(self):
        self.stop.set()
        self.thread.join(10)
        self.client.close()
        self.server.close()

    def test_original_history_logic_gets_identical_data(self):
        context = self.r.MarketContext(self.client)
        frame = context.get_market_data_ex(['open', 'high', 'low', 'close', 'volume', 'amount'],
                                           ['000001.SZ'], period='1d', count=40)['000001.SZ']
        original = self.r.load_strategy(ROOT.parent / '盘中.py')
        direct = self.context.get_market_data_ex(['open', 'high', 'low', 'close', 'volume', 'amount'],
                                                 ['000001.SZ'], period='1d', count=40)['000001.SZ']
        self.assertEqual(original.history_from_frame(frame), original.history_from_frame(direct))

    def test_names_and_details_are_batched_and_cached(self):
        context = self.r.MarketContext(self.client)
        stocks = context.get_stock_list_in_sector('沪深A股')
        for stock in stocks:
            self.assertTrue(context.get_stock_name(stock))
            self.assertTrue(context.get_instrument_detail(stock))
        before = len(self.context.calls)
        self.assertEqual(context.get_stock_name('000001.SZ'), '平安银行')
        self.assertEqual(len(self.context.calls), before)
        self.assertEqual(len([c for c in self.context.calls if c[0] == 'name']), 4)

    def test_temporarily_missing_name_is_retried_like_original_ths(self):
        original_name = self.context.get_stock_name
        seen = {}
        def delayed_name(stock):
            seen[stock] = seen.get(stock, 0) + 1
            return None if seen[stock] == 1 else original_name(stock)
        self.context.get_stock_name = delayed_name
        context = self.r.MarketContext(self.client)
        self.assertIsNone(context.get_stock_name('000001.SZ'))
        self.assertEqual(context.get_stock_name('000001.SZ'), '平安银行')

    def test_timer_skips_missed_intervals_and_daily_rolls_forward(self):
        context = self.r.MarketContext(self.client)
        calls = []
        module = types.SimpleNamespace(scan_stocks=lambda c: calls.append('scan'),
                                       reload_history_data=lambda c: calls.append('daily'))
        context.bind(module)
        base = datetime(2026, 10, 26, 9, 14, 59)
        context.add_timer('scan_stocks', '1nSecond', '2020-01-01 00:00:00', now=base)
        context.add_timer('reload_history_data', '1nDay', '2020-01-01 09:15:00', now=base)
        context.run_due(base + timedelta(seconds=10))
        self.assertEqual(sorted(calls), ['daily', 'scan'])
        context.run_due(base + timedelta(seconds=10))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(t['next'] > base + timedelta(seconds=10) for t in context.timers))

    def test_live_status_is_required_before_any_timer_callback(self):
        context = self.r.MarketContext(self.client)
        calls = []
        context.bind(types.SimpleNamespace(scan_stocks=lambda c: calls.append(1)))
        base = datetime.now()
        context.add_timer('scan_stocks', '1nSecond', '2020-01-01 00:00:00', now=base)
        self.stop.set()
        self.thread.join(5)
        self.server.close()
        with self.assertRaises(self.r.BridgeError):
            context.run_due(base + timedelta(seconds=2))
        self.assertEqual(calls, [])

    def test_single_instance_lock_can_be_released(self):
        with self.r.SingleInstance(self.channel):
            with self.assertRaises(self.r.BridgeError):
                with self.r.SingleInstance(self.channel):
                    pass
        with self.r.SingleInstance(self.channel):
            pass

    def test_check_does_not_load_strategy_or_production_services(self):
        report = self.r.check_bridge(self.client)
        self.assertEqual(report['transport'], 'anonymous_named_memory')
        self.assertGreater(report['history_rows'], 0)
        self.assertTrue(report['has_tick'])


class CompatibilityTests(unittest.TestCase):
    def test_qmt_module_uses_python36_grammar_and_no_disk_or_network_calls(self):
        path = ROOT / 'qmt_shm_bridge.py'
        self.assertTrue(path.is_file(), 'QMT bridge has not been created')
        tree = ast.parse(path.read_text(encoding='utf-8'), feature_version=(3, 6))
        imports = set()
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(n.name.split('.')[0] for n in node.names)
            if isinstance(node, ast.Call):
                calls.add(ast.unparse(node.func))
        self.assertFalse(imports & {'socket', 'redis', 'requests', 'subprocess', 'ctypes', 'pathlib'})
        self.assertNotIn('open', calls)
        self.assertNotIn('eval', calls)
        self.assertNotIn('exec', calls)


if __name__ == '__main__':
    unittest.main()

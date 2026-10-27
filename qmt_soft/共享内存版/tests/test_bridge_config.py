from datetime import datetime, timedelta
import threading
import types
import unittest
from unittest.mock import patch

import test_bridge
from test_bridge import ROOT
from test_runner import runner_module


class BridgeConfigTransportTests(unittest.TestCase):
    setUp = test_bridge.RpcTests.setUp
    tearDown = test_bridge.RpcTests.tearDown

    def test_qmt_setting_is_published_in_status_and_ping(self):
        for setting in (False, True):
            with self.subTest(setting=setting), patch.object(self.b, 'CHECK_TRADING_TIME', setting):
                self.assertIs(self.client.call('ping')['strategy_config']['CHECK_TRADING_TIME'], setting)
                self.assertIs(self.client.assert_online()['strategy_config']['CHECK_TRADING_TIME'], setting)

    def test_market_requests_are_not_blocked_when_time_check_is_enabled(self):
        with patch.object(self.b, 'CHECK_TRADING_TIME', True):
            self.assertTrue(self.client.call('get_full_tick', [['000001.SZ']]))


class RunnerConfigTests(unittest.TestCase):
    def setUp(self):
        self.runner = runner_module()

    @staticmethod
    def status(setting):
        return {'strategy_config': {'CHECK_TRADING_TIME': setting}}

    def test_bridge_setting_is_applied_before_original_initialization(self):
        for setting in (True, False):
            module = types.SimpleNamespace(CHECK_TRADING_TIME=not setting,
                                           g_initialized=False, g_redis_client=object())
            observed = []

            def init(context):
                observed.append(module.CHECK_TRADING_TIME)
                module.g_initialized = True

            module.init = init
            client = types.SimpleNamespace(assert_online=lambda: self.status(setting))
            with self.subTest(setting=setting), patch.object(self.runner, 'load_strategy', return_value=module):
                session = self.runner.StrategySession('盘中', ROOT.parent / '盘中.py', client)
                session.run(threading.Event())
            self.assertEqual(observed, [setting])

    def test_timer_applies_latest_bridge_setting_before_scanning(self):
        status = self.status(True)
        client = types.SimpleNamespace(assert_online=lambda: status)
        observed = []
        module = types.SimpleNamespace(CHECK_TRADING_TIME=True)
        module.scan_stocks = lambda context: observed.append(module.CHECK_TRADING_TIME)
        context = self.runner.MarketContext(client)
        context.bind(module)
        base = datetime(2026, 10, 27, 16)
        context.add_timer('scan_stocks', '1nSecond', '2020-01-01 00:00:00', now=base)
        context.run_due(base + timedelta(seconds=1))
        status = self.status(False)
        context.run_due(base + timedelta(seconds=2))
        self.assertEqual(observed, [True, False])

    def test_loaded_scanner_uses_bridge_setting_without_editing_source(self):
        source = ROOT.parent / '盘中.py'
        before = source.read_bytes()
        module = self.runner.load_strategy(source)
        context = self.runner.MarketContext(None)
        context.bind(module)
        after_close = datetime(2026, 10, 27, 16)
        context.apply_bridge_settings(self.status(True))
        self.assertFalse(module.should_run_initial_scan(after_close))
        self.assertTrue(module.should_run_initial_scan(datetime(2026, 10, 27, 10)))
        self.assertTrue(module.should_run_initial_scan(after_close, historical_mode=True))
        context.apply_bridge_settings(self.status(False))
        self.assertTrue(module.should_run_initial_scan(after_close))
        self.assertEqual(source.read_bytes(), before)

    def test_old_bridge_keeps_original_setting(self):
        module = types.SimpleNamespace(CHECK_TRADING_TIME=False)
        context = self.runner.MarketContext(None)
        context.bind(module)
        self.assertFalse(context.apply_bridge_settings({}))
        self.assertIs(module.CHECK_TRADING_TIME, False)

    def test_invalid_setting_does_not_silently_enable_or_disable_scanning(self):
        module = types.SimpleNamespace(CHECK_TRADING_TIME=True)
        context = self.runner.MarketContext(None)
        context.bind(module)
        with self.assertRaises(self.runner.BridgeError):
            context.apply_bridge_settings(self.status('False'))
        self.assertIs(module.CHECK_TRADING_TIME, True)

    def test_ths_without_the_original_switch_is_unchanged(self):
        module = types.SimpleNamespace(REFERENCE_DATE='')
        context = self.runner.MarketContext(None)
        context.bind(module)
        self.assertFalse(context.apply_bridge_settings(self.status(False)))
        self.assertFalse(hasattr(module, 'CHECK_TRADING_TIME'))


if __name__ == '__main__':
    unittest.main()

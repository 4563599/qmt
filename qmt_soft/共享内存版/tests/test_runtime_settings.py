import ast
from copy import deepcopy
from datetime import datetime, timedelta
import threading
import tokenize
import types
import unittest
from unittest.mock import patch

from test_bridge import ROOT, bridge_module
from test_runner import runner_module
import strategy_runtime as runtime


class Clock(datetime):
    value = datetime(2026, 10, 27, 10)

    @classmethod
    def now(cls):
        return cls.value


def config(check=False):
    result = deepcopy(bridge_module().strategy_config())
    result['CHECK_TRADING_TIME'] = check
    return result


def fake_strategy(kind):
    calls = []
    module = types.SimpleNamespace(REFERENCE_DATE='', datetime=Clock,
        KLINE_TOTAL_DAYS=60, KLINE_HISTORY_DAYS=59, REDIS_QMT_KLINE_DAYS=60,
        HISTORY_REQUEST_DAYS=65, TIMER_INTERVAL_TEXT='1nSecond', PUBLISH_INTERVAL_TEXT='1nSecond')
    module.is_trading_session = lambda now: now.weekday() < 5 and (
        (9, 25) <= (now.hour, now.minute) <= (11, 30) or
        (13, 0) <= (now.hour, now.minute) <= (15, 5))
    module.reload_history_data = lambda context: calls.append(('reload', {}))
    if kind == 'panzhong':
        module.CHECK_TRADING_TIME = True

        def scan(context=None, **kwargs):
            calls.append(('scan', kwargs))
            return {'status': 'success'}

        module.scan_stocks = scan
    else:
        module.g_cycle_lock = threading.Lock()

        def cycle(context, **kwargs):
            calls.append(('ths', kwargs))
            return {'failed_count': 0, 'success_count': 2}

        module._publish_cycle_impl = cycle
        module.publish_ths_kline = lambda context: (
            module._publish_cycle_impl(context) if module.is_trading_session(Clock.now()) else None)
        module.run_with_cycle_lock = lambda lock, callback: callback()
        module._record_busy_skip = lambda now: None
    return module, calls


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.runner = runner_module()

    def test_all_safe_source_options_are_exposed_and_defaults_follow_source(self):
        bridge = bridge_module()
        for kind, name in (('panzhong', '盘中.py'), ('ths', 'ths筛选.py')):
            source = ROOT.parent / name
            module = self.runner.load_strategy(source)
            options = bridge.PANZHONG_OPTIONS if kind == 'panzhong' else bridge.THS_OPTIONS
            self.assertEqual(set(options), runtime.OPTION_NAMES[kind])
            self.assertTrue(all(value is None for value in options.values()))
            before = {key: getattr(module, key) for key in options}
            self.assertEqual(runtime.apply_source_options(module, kind, config()), {})
            self.assertEqual(before, {key: getattr(module, key) for key in options})

    def test_every_original_constant_is_exposed_derived_or_preserved(self):
        common = {'REFERENCE_DATE', 'CHECK_TRADING_TIME', 'KLINE_HISTORY_DAYS'}
        preserved = {
            'panzhong': {'REDIS_HOST', 'REDIS_PORT', 'REDIS_PASSWORD', 'REDIS_DB',
                'REDIS_KEY_PREFIX', 'PZ1_REDIS_KEY_PREFIX', 'QY1_REDIS_KEY_PREFIX',
                'QY2_REDIS_KEY_PREFIX', 'LIMIT_UP_LADDER_PREFIX', 'KLINE_HISTORY_PREFIX',
                'KLINE_REALTIME_PREFIX', 'QY1_KLINE_HISTORY_PREFIX', 'QY1_KLINE_REALTIME_PREFIX',
                'QY2_KLINE_HISTORY_PREFIX', 'QY2_KLINE_REALTIME_PREFIX', 'STATUS_KEY_PREFIX',
                'PERFORMANCE_KEY_PREFIX', 'DINGTALK_DEDUP_PREFIX', 'DINGTALK_CONDITION3_DEDUP_PREFIX',
                'DINGTALK_RECORD_PREFIX', 'DINGTALK_WEBHOOK', 'DINGTALK_CONDITION3_WEBHOOK',
                'DINGTALK_ROUTE_YIN20', 'DINGTALK_ROUTE_CONDITION3', 'DINGTALK_FIVE_PULL_CONDITIONS',
                'DINGTALK_SECURITY_KEYWORD'},
            'ths': {'REDIS_HOST', 'REDIS_PORT', 'REDIS_PASSWORD', 'REDIS_DB', 'THS_LATEST_KEY',
                'THS_KEY_PREFIX', 'THS_CLEANING_KEY_PREFIX', 'REDIS_QMT_KLINE_KEY_PREFIX',
                'PERFORMANCE_COLUMNS'},
        }
        for kind, filename in (('panzhong', '盘中.py'), ('ths', 'ths筛选.py')):
            with tokenize.open(str(ROOT.parent / filename)) as handle:
                tree = ast.parse(handle.read())
            names = {target.id for node in tree.body if isinstance(node, ast.Assign)
                     for target in node.targets if isinstance(target, ast.Name)
                     and target.id.isupper() and not target.id.startswith('_')}
            self.assertEqual(names - runtime.OPTION_NAMES[kind] - common - preserved[kind], set())
            methods = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Attribute)
                       and isinstance(node.func.value, ast.Name)
                       and node.func.value.id == 'ContextInfo'}
            self.assertEqual({name for name in methods if not hasattr(self.runner.MarketContext, name)}, set())

    def test_reference_date_applies_to_both_and_invalid_date_is_rejected(self):
        for kind in ('panzhong', 'ths'):
            module, _ = fake_strategy(kind)
            settings = config()
            settings['REFERENCE_DATE'] = '20261023'
            runtime.apply_source_options(module, kind, settings)
            self.assertEqual(module.REFERENCE_DATE, '20261023')
            settings['REFERENCE_DATE'] = '20260230'
            with self.assertRaises(runtime.BridgeError):
                runtime.apply_source_options(module, kind, settings)
            self.assertEqual(module.REFERENCE_DATE, '20261023')

    def test_kline_length_updates_derived_history_lengths(self):
        for kind, name in (('panzhong', 'KLINE_TOTAL_DAYS'), ('ths', 'REDIS_QMT_KLINE_DAYS')):
            module, _ = fake_strategy(kind)
            settings = config()
            settings['options'][kind][name] = 120
            runtime.apply_source_options(module, kind, settings)
            self.assertEqual(getattr(module, name), 120)
            self.assertEqual(module.HISTORY_REQUEST_DAYS, 125)
            if kind == 'panzhong':
                self.assertEqual(module.KLINE_HISTORY_DAYS, 119)

    def test_protected_output_keys_and_credentials_cannot_be_overridden(self):
        module, _ = fake_strategy('panzhong')
        for name in ('REDIS_KEY_PREFIX', 'REDIS_PASSWORD', 'DINGTALK_WEBHOOK'):
            settings = config()
            settings['options']['panzhong'][name] = 'must-not-be-applied'
            with self.assertRaises(runtime.BridgeError):
                runtime.apply_source_options(module, 'panzhong', settings)
            self.assertFalse(hasattr(module, name))

    def test_invalid_tuning_does_not_partly_mutate_settings(self):
        module, _ = fake_strategy('panzhong')
        settings = config()
        settings['options']['panzhong'].update(KLINE_TOTAL_DAYS=120, HISTORY_REQUEST_DAYS=30)
        with self.assertRaises(runtime.BridgeError):
            runtime.apply_source_options(module, 'panzhong', settings)
        self.assertEqual(module.KLINE_TOTAL_DAYS, 60)

    def test_strategy_selection_respects_enabled_flags(self):
        settings = config()
        settings['enabled']['ths'] = False
        sources = {'盘中': ROOT.parent / '盘中.py', 'THS': ROOT.parent / 'ths筛选.py'}
        self.assertEqual(list(runtime.enabled_sources(sources, {'strategy_config': settings})), ['盘中'])
        settings['enabled']['panzhong'] = False
        with self.assertRaises(runtime.BridgeError):
            runtime.enabled_sources(sources, {'strategy_config': settings})

    def test_scanner_interval_and_reload_clock_are_applied(self):
        module, _ = fake_strategy('panzhong')
        settings = config()
        settings['options']['panzhong']['TIMER_INTERVAL_TEXT'] = '3nSecond'
        settings['history_reload_times']['panzhong'] = '09:20:00'
        context = self.runner.MarketContext(None)
        context.bind(module)
        context.configure_runtime({'strategy_config': settings}, '盘中')
        now = datetime(2026, 10, 27, 9)
        context.add_timer('scan_stocks', module.TIMER_INTERVAL_TEXT, '2020-01-01 00:00:00', now=now)
        context.add_timer('reload_history_data', '1nDay', '2020-01-01 09:15:00', now=now)
        self.assertEqual(context.timers[0]['next'], now + timedelta(seconds=3))
        self.assertEqual(context.timers[1]['next'], now.replace(hour=9, minute=20))
        for invalid in ('0nSecond', 'bad', '4000nSecond', False):
            with self.assertRaises(runtime.BridgeError):
                runtime.interval_seconds(invalid)

    def test_config_contains_no_credentials_or_output_prefix_changes(self):
        names = set(config()['options']['panzhong']) | set(config()['options']['ths'])
        self.assertFalse(any('PASSWORD' in name or 'WEBHOOK' in name or 'KEY_PREFIX' in name for name in names))

    def test_no_dingtalk_command_line_override_stays_stronger_than_bridge_option(self):
        module, _ = fake_strategy('panzhong')
        module.DINGTALK_ENABLED = True
        module.g_initialized = False
        module.g_redis_client = object()
        seen = []

        def init(context):
            seen.append(module.DINGTALK_ENABLED)
            module.g_initialized = True

        module.init = init
        settings = config()
        settings['options']['panzhong']['DINGTALK_ENABLED'] = True
        client = types.SimpleNamespace(assert_online=lambda: {'strategy_config': settings})
        with patch.object(self.runner, 'load_strategy', return_value=module):
            session = self.runner.StrategySession('盘中', ROOT.parent / '盘中.py', client, no_dingtalk=True)
            session.run(threading.Event())
        self.assertEqual(seen, [False])

    def test_callback_functions_and_original_time_rules_are_not_replaced(self):
        for kind, filename, label, callbacks in (
            ('panzhong', '盘中.py', '盘中', ('scan_stocks', 'reload_history_data', 'should_run_initial_scan')),
            ('ths', 'ths筛选.py', 'THS', ('publish_ths_kline', '_publish_cycle_impl', 'is_trading_session')),
        ):
            module = self.runner.load_strategy(ROOT.parent / filename)
            before = {name: getattr(module, name) for name in callbacks}
            context = self.runner.MarketContext(None)
            context.bind(module)
            context.configure_runtime({'strategy_config': config(False)}, label)
            for name, function in before.items():
                self.assertIs(getattr(module, name), function)
            if kind == 'panzhong':
                self.assertTrue(module.should_run_initial_scan(datetime(2026, 10, 27, 16)))
                self.assertTrue(module.should_run_initial_scan(datetime(2026, 10, 27, 8)))
                self.assertTrue(module.should_run_initial_scan(datetime(2026, 10, 27, 12)))
            else:
                self.assertFalse(hasattr(module, 'CHECK_TRADING_TIME'))
                self.assertFalse(module.is_trading_session(datetime(2026, 10, 27, 16)))


if __name__ == '__main__':
    unittest.main()

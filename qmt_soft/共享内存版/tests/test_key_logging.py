import contextlib
import io
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_bridge import ROOT


class KeyLogPolicyTests(unittest.TestCase):
    def setUp(self):
        from log_policy import KeyLogPolicy
        self.policy = KeyLogPolicy()

    def line(self, body, level='INFO', day='2026-10-27'):
        return '[%s 10:00:00] [%s] %s' % (day, level, body)

    def test_missing_history_prints_once_per_stock_source_and_day(self):
        message = self.line('[历史预加载失败] 920296.BJ 没有有效日K', 'WARN')
        self.assertIsNotNone(self.policy.select('ths', message, now=0))
        self.assertIsNone(self.policy.select('ths', message, now=600))
        self.assertIsNotNone(self.policy.select('panzhong', message, now=601))
        self.assertIsNotNone(self.policy.select('ths', message.replace('2026-10-27', '2026-10-28'), now=602))

    def test_repeated_missing_build_and_empty_retry_batches_do_not_flood(self):
        self.policy.select('ths', self.line('[历史预加载失败] 920296.BJ 没有有效日K', 'WARN'), now=0)
        self.assertIsNone(self.policy.select('ths', self.line('[构建失败] 920296.BJ', 'WARN'), now=1))
        self.assertIsNone(self.policy.select('ths', self.line('[历史预加载] 批次 1/1，成功 0，失败 1'), now=2))
        self.assertIsNotNone(self.policy.select('ths', self.line('[历史预加载] 批次 1/1，成功 5，失败 0'), now=3))

    def test_pz_failure_list_only_shows_new_missing_stocks(self):
        first = self.line('[HISTORY] failed sample=001246.SZ,301718.SZ', 'WARN')
        self.assertIsNotNone(self.policy.select('panzhong', first, now=0))
        self.assertIsNone(self.policy.select('panzhong', first, now=50))
        later = self.line('[HISTORY] failed sample=001246.SZ,920296.BJ', 'WARN')
        shown = self.policy.select('panzhong', later, now=100)
        self.assertIn('920296.BJ', shown)
        self.assertNotIn('001246.SZ', shown)

    def test_one_cycle_summary_per_30_seconds_but_exceptions_are_immediate(self):
        summary = self.line('[CYCLE] date=20261027 cycle=1 selected=2 pz1=3 qy1=1 qy2=4 total=2.500s')
        self.assertIsNotNone(self.policy.select('panzhong', summary, now=0))
        self.assertIsNone(self.policy.select('panzhong', summary, now=5))
        self.assertIsNotNone(self.policy.select('panzhong', summary, now=30))
        error = self.line('[CYCLE] failed stage=redis error=unavailable', 'ERROR')
        self.assertEqual(self.policy.select('panzhong', error, now=31), error)
        self.assertEqual(self.policy.select('panzhong', error, now=32), error)
        self.assertEqual(self.policy.select('panzhong', 'Traceback (most recent call last):', now=32),
                         'Traceback (most recent call last):')

    def test_debug_round_details_are_hidden_but_progress_and_notifications_remain(self):
        for tag in ('CYCLE-START', 'FILTER', 'CONDITION', '5PULL-GROUP', 'SELECTED-TOP'):
            self.assertIsNone(self.policy.select('panzhong', self.line('[' + tag + '] details'), now=0))
        for body in ('[加载进度] 正在读取连板基础资料 5300/5608', '[HISTORY] progress=300/5029',
                     '[DINGTALK] result=success', '[DINGTALK] result=failed'):
            self.assertIsNotNone(self.policy.select('panzhong', self.line(body), now=1))

    def test_repeated_performance_issue_is_warning_not_fatal_error(self):
        message = self.line('[性能警告] 1秒频率无法稳定完成', 'ERROR')
        shown = self.policy.select('ths', message, now=0)
        self.assertIn('[WARN]', shown)
        self.assertNotIn('[ERROR]', shown)
        self.assertIsNone(self.policy.select('ths', message, now=1))
        self.assertIsNotNone(self.policy.select('ths', message, now=300))

    def test_failed_performance_record_is_not_hidden_as_routine_performance(self):
        message = self.line('[PERF] Redis failure record failed: connection refused', 'WARN')
        self.assertEqual(self.policy.select('panzhong', message, now=0), message)


class ProgressTests(unittest.TestCase):
    def test_metadata_progress_has_initial_intermediate_and_completion(self):
        from external_runner import MarketContext
        module = types.SimpleNamespace(log=Mock())
        client = types.SimpleNamespace(call=Mock(return_value={
            '000001.SZ': {'name': 'sample', 'detail': {'PreClose': 10}}}))
        context = MarketContext(client)
        context.bind(module)
        context.begin_initialization()
        context.known_stocks.add('000001.SZ')
        self.assertEqual(context.get_stock_name('000001.SZ'), 'sample')
        context.end_initialization()
        messages = [call.args[0] for call in module.log.call_args_list]
        self.assertTrue(any('0/1' in line for line in messages))
        self.assertTrue(any('1/1' in line for line in messages))
        self.assertTrue(any('初始化完成' in line for line in messages))

    def test_beijing_discovery_adds_progress_target_without_changing_metadata_cache(self):
        from external_runner import MarketContext
        context = MarketContext(types.SimpleNamespace(call=Mock(return_value={'920296.BJ': {}})))
        context.bind(types.SimpleNamespace(log=Mock()))
        context.begin_initialization()
        context.get_full_tick(['BJ'])
        self.assertIn('920296.BJ', context.progress_targets)
        self.assertNotIn('920296.BJ', context.known_stocks)

    def test_runtime_market_calls_do_not_emit_initialization_progress(self):
        from external_runner import MarketContext
        module = types.SimpleNamespace(log=Mock())
        context = MarketContext(types.SimpleNamespace(call=Mock(return_value={'000001.SZ': {'name': 'sample'}})))
        context.bind(module)
        context.get_stock_name('000001.SZ')
        module.log.assert_not_called()

    def test_growing_name_universe_does_not_print_completion_for_every_stock(self):
        from external_runner import MarketContext
        module = types.SimpleNamespace(log=Mock())
        def call(method, args):
            return {stock: {'name': stock, 'detail': {'PreClose': 10}} for stock in args[0]}
        context = MarketContext(types.SimpleNamespace(call=call))
        context.bind(module)
        with patch('external_runner.time.monotonic', return_value=100):
            context.begin_initialization()
            for index in range(20):
                context.get_stock_name('%06d.SZ' % index)
        self.assertLessEqual(module.log.call_count, 3)

    def test_empty_ths_pool_uses_actual_request_size_for_history_progress(self):
        from external_runner import MarketContext
        module = types.SimpleNamespace(log=Mock(), g_stock_pool=[])
        context = MarketContext(types.SimpleNamespace(call=Mock(return_value={})))
        context.bind(module)
        context.begin_initialization()
        context.get_market_data_ex(['close'], ['000001.SZ'], period='1d', count=20)
        messages = [call.args[0] for call in module.log.call_args_list]
        self.assertTrue(any('目标 1 只' in message for message in messages))
        self.assertFalse(any('目标 0 只' in message for message in messages))


if __name__ == '__main__':
    unittest.main()

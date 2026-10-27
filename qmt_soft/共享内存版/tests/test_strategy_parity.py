import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import test_bridge
from test_bridge import ROOT
from test_runner import runner_module
from strategy_fixtures import (FrozenDatetime, MarketFixture, MemoryRedis,
                               business_snapshot, normalize, ths_snapshot)


class ParityTests(unittest.TestCase):
    def setUp(self):
        self.r = runner_module()
        self.b = test_bridge.bridge_module()
        import uuid
        self.channel = 'parity_' + uuid.uuid4().hex
        self.market = MarketFixture()
        self.server = self.b.BridgeServer(self.market, self.channel)
        self.stop = threading.Event()
        def serve():
            while not self.stop.is_set():
                self.server.serve_once()
                self.stop.wait(.001)
        self.thread = threading.Thread(target=serve)
        self.thread.start()
        self.client = self.b.RpcClient(self.channel, timeout=5)
        self.client.wait_ready(2)
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.stop.set(); self.thread.join(10)
        self.client.close(); self.server.close(); self.tmp.cleanup()

    def configured(self, filename, label):
        module = self.r.load_strategy(ROOT.parent / filename)
        store = MemoryRedis()
        if filename.startswith('ths'): ths_snapshot(store)
        module.redis = types.SimpleNamespace(Redis=lambda **kwargs: store)
        module.datetime = FrozenDatetime
        module.DINGTALK_ENABLED = False
        module.LOG_BASE_DIR = str(Path(self.tmp.name) / label)
        messages = []
        module.log = lambda message, level='INFO', date_text=None: messages.append((level, message))
        return module, store, messages

    def pair(self, filename, bridge_settings=None):
        direct, left, left_logs = self.configured(filename, 'direct')
        remote, right, right_logs = self.configured(filename, 'shared_memory')
        context = self.r.MarketContext(self.client); context.bind(remote)
        if bridge_settings is not None:
            from strategy_runtime import apply_source_options
            kind = 'ths' if filename.startswith('ths') else 'panzhong'
            apply_source_options(direct, kind, bridge_settings)
            if hasattr(direct, 'CHECK_TRADING_TIME'):
                direct.CHECK_TRADING_TIME = bridge_settings['CHECK_TRADING_TIME']
            context.configure_runtime({'strategy_config': bridge_settings}, 'THS' if kind == 'ths' else '盘中')
            reference = bridge_settings.get('REFERENCE_DATE')
            if kind == 'ths' and reference:
                for store in (left, right):
                    snapshot = json.loads(store.get('ths:20261026'))
                    for stock in snapshot['stocks']:
                        stock['snapshot_time'] = '%s-%s-%s 15:00:00' % (
                            reference[:4], reference[4:6], reference[6:])
                    store.set('ths:' + reference, json.dumps(snapshot, ensure_ascii=False))
        direct_context = types.SimpleNamespace(
            subscribe_whole_quote=self.market.subscribe_whole_quote,
            get_stock_list_in_sector=self.market.get_stock_list_in_sector,
            get_stock_name=self.market.get_stock_name,
            get_instrument_detail=self.market.get_instrument_detail,
            get_market_data_ex=self.market.get_market_data_ex,
            get_full_tick=self.market.get_full_tick,
            run_time=lambda *args: None)
        # Do not start the source's notification daemons in tests. All actual
        # Redis and HTTP connections are forbidden, even on unexpected paths.
        with patch('threading.Thread.start'), \
             patch('socket.socket.connect', side_effect=AssertionError('Real network forbidden')) as net, \
             patch('requests.post', side_effect=AssertionError('Real DingTalk forbidden')) as ding:
            direct.init(direct_context)
            remote.init(context)
            self.assertFalse(net.called); self.assertFalse(ding.called)
        self.assertTrue(direct.g_initialized and remote.g_initialized)
        self.assertEqual(direct.g_stock_pool, remote.g_stock_pool)
        self.assertEqual(direct.g_history_data, remote.g_history_data)
        self.assertFalse([x for x in left_logs + right_logs if x[0] == 'ERROR'],
                         [x for x in left_logs + right_logs if x[0] == 'ERROR'])
        return direct, remote, left, right, context

    def test_panzhong_scan_and_redis_business_payloads_match_original(self):
        direct, remote, left, right, context = self.pair('盘中.py')
        self.assertGreater(remote.g_scan_count, 0)
        self.assertEqual(business_snapshot(left), business_snapshot(right))
        self.assertEqual(left.ttls, right.ttls)
        self.assertEqual(len(context.timers), 2)
        self.assertTrue(any(key.startswith('pz:') for key in right.values))

    def test_ths_full_cycle_cleaning_kline_and_csv_match_original(self):
        direct, remote, left, right, context = self.pair('ths筛选.py')
        self.assertGreater(remote.g_cycle_count, 0)
        self.assertEqual(business_snapshot(left), business_snapshot(right))
        self.assertEqual(left.ttls, right.ttls)
        cleaned = right.hgetall('thscleaning:20261026')
        self.assertIn('000001', cleaned)
        self.assertIn('600000', cleaned)
        self.assertIn('_scan_info', cleaned)
        self.assertIn('qmt:kline:20261026', right.values)
        self.assertEqual({t['callback'] for t in context.timers},
                         {'reload_history_data', 'publish_ths_kline'})
        self.assertEqual(context.timers[0]['next'].time().strftime('%H:%M'), '09:25')
        csv_files = list(Path(self.tmp.name).rglob('性能监控.csv'))
        self.assertEqual(len(csv_files), 2)

    def test_panzhong_alert_format_and_redis_dedup_remain_original(self):
        module, store, logs = self.configured('盘中.py', 'ding')
        module.DINGTALK_ENABLED = True
        module.g_redis_client = store
        sent = []
        module.send_dingtalk_markdown = lambda title, text, webhook=None: sent.append((title, text)) or True
        result = {'name': '模拟股票', 'code': '000001', 'ref_gain': 8.0, 'qiangchi': 1,
                  'yin_count_10d': 2, 'selected_at': '2026-10-26 10:00:00'}
        self.assertTrue(module.enqueue_stock_alert(result, '20261026'))
        self.assertFalse(module.enqueue_stock_alert(result, '20261026'))
        task = module.g_dingtalk_queue.get_nowait()
        self.assertTrue(module.process_dingtalk_task(task))
        self.assertFalse(module.enqueue_stock_alert(result, '20261026'))
        self.assertEqual(len(sent), 1)
        self.assertIn('模拟股票', sent[0][1])
        self.assertIn('000001', store.smembers(module.dingtalk_dedup_key('20261026', 'condition3')))
        # The persisted dedup also prevents duplicates after the external app restarts.
        second, unused, messages = self.configured('盘中.py', 'ding-restarted')
        second.g_redis_client, second.DINGTALK_ENABLED = store, True
        self.assertFalse(second.enqueue_stock_alert(result, '20261026'))

    def test_both_real_sources_initialize_concurrently_over_same_bridge(self):
        pz, store, pz_logs = self.configured('盘中.py', 'both-pz')
        ths, unused_store, ths_logs = self.configured('ths筛选.py', 'both-ths')
        ths_snapshot(store)
        ths.redis = types.SimpleNamespace(Redis=lambda **kwargs: store)
        pz.dingtalk_worker = lambda: None
        pz.dingtalk_persist_worker = lambda: None
        stop, initialized = threading.Event(), threading.Event()
        def watch():
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if (pz.g_initialized and 'thscleaning:20261026' in store.values
                        and ths.g_history_data):
                    initialized.set(); stop.set(); return
                time.sleep(.01)
            stop.set()
        watcher = threading.Thread(target=watch)
        watcher.start()
        def loader(path):
            return pz if Path(path).name == '盘中.py' else ths
        try:
            with patch.object(self.r, 'load_strategy', side_effect=loader), \
                 patch('socket.socket.connect', side_effect=AssertionError('Real network forbidden')) as net, \
                 patch('requests.post', side_effect=AssertionError('Real DingTalk forbidden')) as ding:
                sessions = self.r.run_sessions(self.client, self.r.strategy_sources('both'),
                                                stop_event=stop)
                self.assertFalse(net.called); self.assertFalse(ding.called)
            self.assertTrue(initialized.is_set(), 'both original strategies did not finish')
            self.assertEqual(len(sessions), 2)
            self.assertEqual([len(s.context.timers) for s in sessions], [2, 2])
            self.assertIn('qmt:kline:20261026', store.values)
            self.assertTrue(any(key.startswith('pz:') for key in store.values))
            self.assertFalse([x for x in pz_logs + ths_logs if x[0] == 'ERROR'])
        finally:
            stop.set(); watcher.join(10)

    def test_ths_color_rules_boundary_and_close_overlay_are_preserved(self):
        module, store, logs = self.configured('ths筛选.py', 'close')
        for yin_count in [0, 4, 5, 6, 7]:
            result = module.classify_cleaning_stock(
                {'yin_count_20': yin_count, 'is_quanyang_10d': 1,
                 'high_gain_10d_gte_5': 1}, {'ths_change_percent': 4.49})
            self.assertEqual(result['group_color'], 'purple' if yin_count <= 4 else 'red' if yin_count <= 6 else '')
        self.assertTrue(module.classify_cleaning_stock({}, {'ths_change_percent': 4.5})['selected'])
        direct_ticks = self.market.get_full_tick(self.market.stocks)
        remote_ticks = copy.deepcopy(direct_ticks)
        context = self.r.MarketContext(self.client)
        after_close = FrozenDatetime.value.replace(hour=15, minute=30)
        before = module.overlay_close_after_market(self.market, direct_ticks, self.market.stocks,
                                                   '20261026', after_close)
        other, unused, messages = self.configured('ths筛选.py', 'close-remote')
        after = other.overlay_close_after_market(context, remote_ticks, self.market.stocks,
                                                 '20261026', after_close)
        self.assertEqual(before, after)
        self.assertEqual(direct_ticks, remote_ticks)

    def test_approved_source_revisions_are_not_rewritten_by_bridge(self):
        # Approved eighth PZ1 entry route and candidate update, 2026-10-27.
        expected = {'盘中.py': '512f6728b0eaa7da204d57aa81082d332744ca0c1d2cdf50f4a35f0760db3c63',
                    'ths筛选.py': '5cd6af226618e34809e53d657a1c7a06c6af0527cda56eec72527c48050b54f3'}
        for filename, digest in expected.items():
            self.assertEqual(hashlib.sha256((ROOT.parent / filename).read_bytes()).hexdigest(), digest)

    def test_full_bridge_settings_preserve_both_intraday_business_outputs(self):
        settings = copy.deepcopy(self.b.strategy_config())
        settings['CHECK_TRADING_TIME'] = False
        for filename in ('盘中.py', 'ths筛选.py'):
            with self.subTest(filename=filename), patch.object(FrozenDatetime, 'value',
                    FrozenDatetime(2026, 10, 26, 10)):
                direct, remote, left, right, _ = self.pair(filename, settings)
                self.assertEqual(business_snapshot(left), business_snapshot(right))
                self.assertEqual(left.ttls, right.ttls)

    def test_false_continues_multiple_after_hours_cycles_with_identical_payloads(self):
        settings = copy.deepcopy(self.b.strategy_config())
        settings['CHECK_TRADING_TIME'] = False
        with patch.object(FrozenDatetime, 'value', FrozenDatetime(2026, 10, 26, 16)):
            direct, remote, left, right, context = self.pair('盘中.py', settings)
            self.assertEqual(remote.g_scan_count, 1)
            context.timers = [timer for timer in context.timers if timer['callback'] == 'scan_stocks']
            context.timers[0]['next'] = FrozenDatetime.value
            assert_online = self.client.assert_online

            def status():
                result = assert_online()
                result['strategy_config'] = settings
                return result

            with patch.object(self.client, 'assert_online', side_effect=status), \
                 patch('socket.socket.connect', side_effect=AssertionError('Real network forbidden')), \
                 patch('requests.post', side_effect=AssertionError('Real DingTalk forbidden')):
                for index in range(3):
                    direct.scan_stocks(direct.g_context)
                    context.run_due(FrozenDatetime.value + timedelta(seconds=index + 1))
            self.assertEqual(remote.g_scan_count, 4)
            self.assertEqual(direct.g_scan_count, remote.g_scan_count)
            self.assertEqual(business_snapshot(left), business_snapshot(right))

    def test_reference_date_still_runs_once_for_both_strategies(self):
        settings = copy.deepcopy(self.b.strategy_config())
        settings['REFERENCE_DATE'] = '20261023'
        for filename in ('盘中.py', 'ths筛选.py'):
            with self.subTest(filename=filename):
                direct, remote, left, right, context = self.pair(filename, settings)
                self.assertEqual(context.timers, [])
                self.assertEqual(business_snapshot(left), business_snapshot(right))
                if filename == '盘中.py':
                    self.assertEqual(remote.scan_stocks(context), 'historical_done')

    def test_ths_after_close_retains_its_own_original_time_rules(self):
        settings = copy.deepcopy(self.b.strategy_config())
        settings['CHECK_TRADING_TIME'] = False
        with patch.object(FrozenDatetime, 'value', FrozenDatetime(2026, 10, 26, 16)):
            direct, remote, left, right, context = self.pair('ths筛选.py', settings)
            self.assertEqual(remote.g_cycle_count, 1)
            for _ in range(3):
                direct.publish_ths_kline(direct.g_context)
                remote.publish_ths_kline(context)
            self.assertEqual(remote.g_cycle_count, 1)
            self.assertEqual(remote.g_cycle_count, direct.g_cycle_count)
            self.assertEqual(business_snapshot(left), business_snapshot(right))


if __name__ == '__main__':
    unittest.main()

import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from test_bridge import ROOT


def monitor():
    path = ROOT / 'performance_monitor.py'
    assert path.exists(), 'shared-memory performance collector has not been created'
    spec = importlib.util.spec_from_file_location('performance_monitor', path)
    m = importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m


class MonitorTests(unittest.TestCase):
    def setUp(self): self.m = monitor()

    def test_legacy_pz_record_is_not_relabelled_as_current_run(self):
        old = {'started_at':'2026-10-26 10:00:00','finished_at':'2026-10-26 10:00:01','status':'success','total_seconds':1}
        self.assertIsNone(self.m.normalize_record('panzhong', old, '2026-10-26 22:06:41', 'test-run'))

    def test_original_pz_base64_records_are_decoded(self):
        import base64
        raw={'started_at':'2026-10-27 09:30:00','finished_at':'2026-10-27 09:30:01',
             'status':'success','total_seconds':.82,'tick_seconds':.4}
        encoded=base64.b64encode(json.dumps(raw).encode('utf-8')).decode('ascii')
        self.assertTrue(hasattr(self.m,'decode_performance'),'PZ original base64 decoder is missing')
        self.assertEqual(self.m.decode_performance(encoded),raw)

    def test_csv_without_outcome_does_not_claim_business_success(self):
        raw={'time':'2026-10-26 22:06:45','total_seconds':'1.2'}
        row=self.m.normalize_record('ths',raw,'2026-10-26 22:06:41','test-run')
        self.assertEqual(row['status'],'recorded')

    def test_unchanged_pz_list_does_not_download_old_rows_every_poll(self):
        raw=json.dumps({'started_at':'2026-10-26 10:00:00','total_seconds':1})
        class Client:
            def __init__(self):self.ends=[]
            def lrange(self,key,start,end):self.ends.append(end);return [raw]
            def llen(self,key):return 1000
        obj=object.__new__(self.m.PerformanceMonitor)
        client=Client();obj.clients={'panzhong':client};obj.pz_heads={};obj.pz_lengths={}
        obj.started='2026-10-26 22:06:41';obj.run_id='test';obj.enqueue=lambda record:None
        self.assertTrue(hasattr(obj,'collect_pz'),'incremental PZ collection is missing')
        obj.collect_pz('20261026');obj.collect_pz('20261026')
        self.assertEqual(client.ends.count(199),1)

    def test_real_ths_csv_timings_are_kept_and_finish_is_derived(self):
        raw = {'time':'2026-10-26 22:06:45','ref_date':'20261026','total_seconds':'22.514',
               'history_seconds':'2.110','tick_seconds':'.914','build_seconds':'.117',
               'cleaning_seconds':'.136','redis_seconds':'.926','candidate_count':'185',
               'selected_count':'26','success_count':'26','failed_count':'0'}
        row = self.m.normalize_record('ths', raw, '2026-10-26 22:06:41', 'test-run')
        self.assertEqual(row['total_seconds'], 22.514)
        self.assertEqual(row['finished_at'], '2026-10-26 22:07:07')
        self.assertEqual(row['tick_seconds'], .914)
        self.assertEqual(row['selected_count'], 26)
        self.assertIsNone(row['cycle_count'])
        self.assertEqual(row['source'], 'ths_csv')

    def test_incremental_csv_reader_does_not_repeat_or_read_partial_line(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'性能监控.csv'
            p.write_text('time,total_seconds\n2026-10-26 10:00:00,1.25\n',encoding='utf-8-sig')
            reader=self.m.CsvTail(p)
            self.assertEqual(len(reader.read()),1)
            self.assertEqual(reader.read(),[])
            with p.open('ab') as f:f.write(b'2026-10-26 10:00:01,2')
            self.assertEqual(reader.read(),[])
            with p.open('ab') as f:f.write(b'.5\n')
            self.assertEqual(reader.read()[0]['total_seconds'],'2.5')

    def test_initialization_uses_actual_log_times_and_history_progress(self):
        tracker=self.m.LogTracker('panzhong','2026-10-26 22:06:41','test-run')
        for line in ['[2026-10-26 22:06:43] [INFO] [START] PZ intraday scanner',
                     '[2026-10-26 22:09:01] [INFO] [HISTORY] progress=1500/5029 loaded=1498 failed=2',
                     '[2026-10-26 22:10:00] [INFO] [HISTORY] completed loaded=5020 failed=9 seconds=84.098',
                     '[2026-10-26 22:10:00] [INFO] [TIMER] 09:15 history reload and 1-second scan driver registered']:
            tracker.feed(line)
        self.assertTrue(tracker.state['initialized'])
        self.assertEqual(tracker.records[0]['total_seconds'],197)
        self.assertEqual(tracker.records[0]['history_seconds'],84.098)
        self.assertEqual(tracker.state['history_loaded'],5020)

    def test_monitor_reads_configured_log_path_and_reference_date_without_live_io(self):
        from copy import deepcopy
        from test_bridge import bridge_module
        settings = deepcopy(bridge_module().strategy_config())
        settings['REFERENCE_DATE'] = '20261023'
        status = {'strategy_config': settings, 'state': 'ready', 'heartbeat': 0}

        class Slot:
            def __init__(self, *args): pass
            def read(self): return status
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): self.close()

        client = types.SimpleNamespace(set=lambda *args, **kwargs: None, close=lambda: None)
        process = types.SimpleNamespace(pid=123, create_time=lambda: 1793059200)
        with tempfile.TemporaryDirectory() as folder:
            settings['options']['panzhong']['LOG_BASE_DIR'] = folder
            with patch.object(self.m, 'SharedSlot', Slot), \
                 patch.object(self.m, 'redis_for', return_value=client), \
                 patch.object(self.m.psutil, 'Process', return_value=process):
                collector = self.m.PerformanceMonitor(123, sources={'盘中': ROOT.parent / '盘中.py'})
                self.assertEqual(collector.modules['panzhong'].LOG_BASE_DIR, folder)
                self.assertEqual(collector.modules['panzhong'].REFERENCE_DATE, '20261023')
                dates, paths = [], []
                collector.collect_pz = dates.append
                collector.read_logs = lambda strategy, path: paths.append(path)
                collector.step()
                self.assertIn('20261023', dates)
                self.assertTrue(all(Path(folder) in path.parents for path in paths))
                overridden = self.m.PerformanceMonitor(123, sources={'盘中': ROOT.parent / '盘中.py'},
                                                      reference_date='20261022')
                self.assertEqual(overridden.modules['panzhong'].REFERENCE_DATE, '20261022')
                realtime = self.m.PerformanceMonitor(123, sources={'盘中': ROOT.parent / '盘中.py'},
                                                    reference_date='')
                self.assertEqual(realtime.modules['panzhong'].REFERENCE_DATE, '')


if __name__=='__main__':unittest.main()

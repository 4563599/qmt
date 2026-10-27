import contextlib
from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock, MagicMock, patch

from test_bridge import ROOT


def calendar_rows(year):
    current = date(year, 1, 1)
    result = []
    while current.year == year:
        key = current.strftime('%Y%m%d')
        result.append({'cal_date': key, 'is_open': int(current.weekday() < 5 and key != '20261001')})
        current += timedelta(days=1)
    return result


class SourceStreamTests(unittest.TestCase):
    def test_interleaved_threads_keep_their_own_lines(self):
        from stream_events import event_output, decode_log_event
        output = io.StringIO()
        first, second = threading.Event(), threading.Event()
        with event_output(output, output):
            def pz():
                print('PZ partial', end='')
                first.set()
                second.wait(2)
                print(' complete')
            def ths():
                first.wait(2)
                print('THS complete')
                second.set()
            workers = [threading.Thread(target=pz, name='qmt-external-盘中'),
                       threading.Thread(target=ths, name='qmt-external-THS')]
            for worker in workers: worker.start()
            for worker in workers: worker.join(3)
        records = [decode_log_event(line) for line in output.getvalue().splitlines()]
        by_source = {record['source']: record['text'] for record in records}
        self.assertEqual(by_source['panzhong'], 'PZ partial complete')
        self.assertEqual(by_source['ths'], 'THS complete')

    def test_legacy_text_is_preserved_as_system_output(self):
        from stream_events import decode_log_event
        self.assertEqual(decode_log_event('plain output\n'),
                         {'source': 'system', 'text': 'plain output'})

    def test_interleaved_error_stacks_do_not_leak_between_sources(self):
        from launcher_core import LogModel
        model = LogModel()
        self.assertEqual(model.append('[ERROR] pz failure', source='panzhong'), 'error')
        self.assertEqual(model.append('[INFO] ths healthy', source='ths'), 'info')
        self.assertEqual(model.append('  File "pz.py", line 12', source='panzhong'), 'error')
        self.assertEqual(model.errors, 1)

    def test_background_worker_source_uses_strategy_stack(self):
        from stream_events import event_output, decode_log_event
        output = io.StringIO()
        namespace = {'__file__': str(ROOT.parent / 'ths筛选.py')}
        exec(compile("def log():\n    print('background ths')\n", namespace['__file__'], 'exec'), namespace)
        with event_output(output, output):
            namespace['log']()
        self.assertEqual(decode_log_event(output.getvalue())['source'], 'ths')


class TradingScheduleTests(unittest.TestCase):
    def setUp(self):
        import launcher_schedule
        self.schedule = launcher_schedule
        self.temp = tempfile.TemporaryDirectory()
        self.fetcher = Mock(return_value=calendar_rows(2026))
        self.calendar = launcher_schedule.TradingCalendar(Path(self.temp.name), fetcher=self.fetcher)

    def tearDown(self):
        self.temp.cleanup()

    def test_true_trade_day_and_holiday_are_not_just_weekdays(self):
        self.assertTrue(self.calendar.is_session(date(2026, 10, 28)))
        self.assertFalse(self.calendar.is_session(date(2026, 10, 1)))
        self.assertFalse(self.calendar.is_session(date(2026, 10, 24)))
        self.assertEqual(self.fetcher.call_count, 1)

    def test_missing_or_incomplete_calendar_fails_closed(self):
        self.fetcher.return_value = calendar_rows(2026)[:10]
        with self.assertRaises(ValueError):
            self.calendar.is_session(date(2026, 10, 28))
        self.fetcher.side_effect = RuntimeError('calendar offline')
        with self.assertRaises(RuntimeError):
            self.calendar.is_session(date(2026, 10, 28))

    def test_scheduled_start_only_in_daytime_window(self):
        for hour, minute, allowed in ((9, 14, False), (9, 15, True), (15, 5, True), (15, 6, False)):
            decision = self.schedule.scheduled_decision(datetime(2026, 10, 28, hour, minute), self.calendar)
            self.assertEqual(decision['allowed'], allowed)
        self.assertFalse(self.schedule.scheduled_decision(datetime(2026, 10, 1, 9, 15), self.calendar)['allowed'])

    def test_unavailable_remote_can_use_valid_complete_cache(self):
        self.calendar.is_session(date(2026, 10, 28))
        path = self.calendar.folder / 'sse_2026.json'
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['updated_at'] = '2020-01-01T00:00:00+08:00'
        path.write_text(json.dumps(payload), encoding='utf-8')
        cached = self.schedule.TradingCalendar(Path(self.temp.name), fetcher=Mock(side_effect=RuntimeError('offline')))
        self.assertTrue(cached.is_session(date(2026, 10, 28)))
        cached.fetcher.assert_called_once()

    def test_existing_window_can_receive_one_shot_start_signal(self):
        name = 'Local\\QmtGuiTest_' + uuid.uuid4().hex
        with self.schedule.AutoStartSignal(name) as receiver:
            self.assertFalse(receiver.consume())
            with self.schedule.AutoStartSignal(name) as sender:
                sender.request()
            self.assertTrue(receiver.consume())
            self.assertFalse(receiver.consume())

    def test_scheduled_job_signals_existing_gui_without_launching_another(self):
        with patch.object(self.schedule, 'AutoStartSignal') as signal:
            with patch.object(self.schedule.subprocess, 'Popen') as popen:
                self.schedule.launch_scheduled_gui(Path(self.temp.name))
        signal.return_value.__enter__.return_value.request.assert_called_once()
        popen.assert_not_called()

    def test_scheduled_job_detaches_gui_so_it_does_not_block_next_days_trigger(self):
        with patch.object(self.schedule, 'AutoStartSignal', side_effect=FileNotFoundError):
            with patch('external_runner.SingleInstance'), patch.object(self.schedule.subprocess, 'Popen') as popen:
                self.schedule.launch_scheduled_gui(Path(self.temp.name))
        popen.assert_called_once()
        self.assertIn('--auto-start', popen.call_args.args[0])


if __name__ == '__main__':
    unittest.main()

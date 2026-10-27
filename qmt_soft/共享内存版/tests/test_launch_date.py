import copy
from datetime import date
import io
import contextlib
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bridge import ROOT, bridge_module


class LaunchDateTests(unittest.TestCase):
    def test_date_input_accepts_iso_and_compact_but_rejects_invalid_and_future(self):
        from launch_date import parse_date, selection_reference
        today = date(2026, 10, 27)
        for text in ('2026-10-23', '20261023', ' 2026-10-23 '):
            self.assertEqual(parse_date(text, today), date(2026, 10, 23))
            self.assertEqual(selection_reference(text, today), '20261023')
        self.assertEqual(selection_reference('2026-10-27', today), '')
        for text in ('', '2026-02-30', '2026102', '2026-1-2', '2026-10-28', None):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_date(text, today)

    def test_optional_runner_reference_keeps_default_and_explicit_realtime_distinct(self):
        from launch_date import normalize_reference
        self.assertIsNone(normalize_reference(None))
        self.assertEqual(normalize_reference(''), '')
        self.assertEqual(normalize_reference('2026-10-23'), '20261023')

    def test_selected_reference_wins_over_bridge_for_both_strategy_workers(self):
        import external_runner as runner
        config = copy.deepcopy(bridge_module().strategy_config())
        config['REFERENCE_DATE'] = '20261022'
        before = copy.deepcopy(config)
        seen = []
        def loader(path):
            module = types.SimpleNamespace(REFERENCE_DATE='', DINGTALK_ENABLED=True,
                                           CHECK_TRADING_TIME=True, g_initialized=False)
            def init(context):
                seen.append((Path(path).stem, module.REFERENCE_DATE, module.DINGTALK_ENABLED))
                context.get_market_data_ex(['close'], ['000001.SZ'], period='1d',
                                           count=65, end_time=module.REFERENCE_DATE)
                module.g_initialized = True
            module.init = init
            return module
        calls = []
        client = types.SimpleNamespace(assert_online=lambda: {'strategy_config': config},
                                       call=lambda method, args, kwargs: calls.append((method, kwargs)) or {})
        with patch.object(runner, 'load_strategy', side_effect=loader):
            sessions = runner.run_sessions(client, runner.strategy_sources('both'),
                                           reference_date='20261023')
        self.assertEqual({row[1] for row in seen}, {'20261023'})
        self.assertEqual({row[2] for row in seen}, {False})
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(row[1]['end_time'] == '20261023' for row in calls))
        self.assertEqual({session.state for session in sessions}, {'completed'})
        self.assertEqual(config, before)

    def test_explicit_realtime_clears_bridge_history_without_disabling_alerts(self):
        import external_runner as runner
        module = types.SimpleNamespace(REFERENCE_DATE='20261023', DINGTALK_ENABLED=True,
                                       g_initialized=False)
        module.init = lambda context: setattr(module, 'g_initialized', True)
        client = types.SimpleNamespace(assert_online=lambda: {})
        with patch.object(runner, 'load_strategy', return_value=module):
            session = runner.StrategySession('盘中', ROOT.parent / '盘中.py', client,
                                             reference_date='')
            session.run(threading.Event())
        self.assertEqual(module.REFERENCE_DATE, '')
        self.assertTrue(module.DINGTALK_ENABLED)

    def test_failed_historical_scanner_is_not_reported_as_completed(self):
        import external_runner as runner
        module = types.SimpleNamespace(REFERENCE_DATE='', g_initialized=False, g_historical_done=False)
        module.init = lambda context: setattr(module, 'g_initialized', True)
        client = types.SimpleNamespace(assert_online=lambda: {})
        with patch.object(runner, 'load_strategy', return_value=module):
            session = runner.StrategySession('盘中', ROOT.parent / '盘中.py', client,
                                             reference_date='20261023')
            with self.assertRaisesRegex(runner.BridgeError, '未成功完成'):
                session.run(threading.Event())
        self.assertNotEqual(session.state, 'completed')

    def test_ths_history_checks_the_selected_dates_completion_record(self):
        import external_runner as runner
        from unittest.mock import MagicMock
        redis_client = MagicMock()
        module = types.SimpleNamespace(REFERENCE_DATE='', g_initialized=False,
                                       g_redis_client=redis_client, _status_key=lambda day: 'status:' + day)
        module.init = lambda context: setattr(module, 'g_initialized', True)
        client = types.SimpleNamespace(assert_online=lambda: {})
        with patch.object(runner, 'load_strategy', return_value=module):
            redis_client.hgetall.return_value = {b'state': b'failed', b'error': b'missing source data'}
            session = runner.StrategySession('THS', ROOT.parent / 'ths筛选.py', client,
                                             reference_date='20261023')
            with self.assertRaisesRegex(runner.BridgeError, 'missing source data'):
                session.run(threading.Event())
            redis_client.hgetall.assert_called_once_with('status:20261023')
            redis_client.hgetall.return_value = {'state': 'success'}
            session.run(threading.Event())
            self.assertEqual(session.state, 'completed')

    def test_closed_session_is_rejected_before_bridge_or_strategy_is_opened(self):
        import external_runner as runner
        with patch.object(runner, 'validate_history_date', side_effect=ValueError('非交易日')), \
             patch.object(runner, 'RpcClient') as rpc, contextlib.redirect_stdout(io.StringIO()):
            result = runner.main(['--run', '--reference-date', '20261025'])
        self.assertEqual(result, 2)
        rpc.assert_not_called()

    def test_history_date_uses_exchange_calendar_not_weekday_guess(self):
        from external_runner import validate_history_date
        with patch('launcher_schedule.TradingCalendar') as calendar:
            calendar.return_value.is_session.return_value = False
            with self.assertRaisesRegex(ValueError, '不是交易日'):
                validate_history_date('20261001')
            calendar.return_value.is_session.assert_called_once_with(date(2026, 10, 1))
            calendar.reset_mock()
            validate_history_date('')
            calendar.assert_not_called()
    def test_cli_reference_is_forwarded_to_sessions(self):
        import external_runner as runner
        from unittest.mock import MagicMock
        client = MagicMock()
        client.attach_mock(MagicMock(return_value={}), 'assert_online')
        with patch.object(runner, 'SingleInstance', return_value=MagicMock()), \
             patch.object(runner, 'RpcClient', return_value=client), \
             patch.object(runner, 'validate_history_date'), \
             patch.object(runner, 'check_bridge', return_value={'sample_stock': '000001.SZ', 'history_rows': 5}), \
             patch('performance_monitor.start_monitor'), \
             patch.object(runner, 'run_sessions') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(['--run', '--strategy', 'both', '--reference-date', '2026-10-23']), 0)
        self.assertEqual(run.call_args.kwargs['reference_date'], '20261023')

    def test_process_date_argument_is_per_start_and_logged(self):
        from launcher_core import ManagedProcess, EventBuffer
        import sys
        with tempfile.TemporaryDirectory() as folder:
            controller = ManagedProcess(ROOT, EventBuffer(), log_dir=folder,
                command=[sys.executable, '-c', 'import sys; print(repr(sys.argv[1:]))'],
                running_probe=lambda: False)
            original = list(controller.command)
            controller.start(reference_date='20261023')
            controller.reader.join(5)
            self.assertFalse(controller.running)
            self.assertIn("['--reference-date', '20261023']", controller.log_path.read_text(encoding='utf-8'))
            self.assertEqual(controller.command, original)
            controller.start(reference_date='')
            controller.reader.join(5)
            self.assertIn("['--reference-date', '']", controller.log_path.read_text(encoding='utf-8'))


class DatePickerGuiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import launcher_gui
        self.root = tk.Tk()
        self.root.withdraw()
        self.temp = tempfile.TemporaryDirectory()
        self.override = patch.object(launcher_gui, 'ROOT', Path(self.temp.name))
        self.override.start()
        self.app = launcher_gui.LauncherApp(self.root, demo=True)

    def tearDown(self):
        self.app.date_picker.close_calendar()
        self.root.update_idletasks()
        self.app._destroy()
        self.override.stop()
        self.temp.cleanup()

    def test_calendar_selection_and_typed_date_share_the_same_field(self):
        picker = self.app.date_picker
        self.root.deiconify()
        self.root.update()
        picker.value.set('20261023')
        picker._normalize()
        self.assertEqual(picker.value.get(), '2026-10-23')
        picker.open_calendar()
        self.root.update_idletasks()
        self.assertEqual(len(picker.day_buttons), 42)
        self.assertEqual(picker.month, date(2026, 10, 1))
        button = next(button for button in picker.day_buttons if str(button['text']) == '22')
        button.invoke()
        self.assertIsNone(picker.popup)
        self.assertEqual(picker.value.get(), '2026-10-22')
        self.assertIn('不推送', self.app.date_summary['text'])

    def test_calendar_closes_and_field_locks_while_running(self):
        picker = self.app.date_picker
        self.root.deiconify()
        self.root.update()
        picker.open_calendar()
        picker.set_enabled(False)
        self.assertIsNone(picker.popup)
        self.assertEqual(str(picker.entry['state']), 'disabled')
        picker.open_calendar()
        self.assertIsNone(picker.popup)
        picker.set_enabled(True)
        self.assertEqual(str(picker.entry['state']), 'normal')

    def test_start_sends_selected_date_and_today_retains_realtime(self):
        from launch_date import china_today
        self.app.demo = False
        self.app.controller.log_path = Path(self.temp.name) / 'test.log'
        for day, expected in ((date(2026, 10, 23), '20261023'), (china_today(), '')):
            self.app.date_picker.set_date(day)
            with patch.object(self.app.controller, 'start') as start:
                self.app.start()
            start.assert_called_once_with(reference_date=expected)
            self.assertEqual(self.app.run_reference, expected)

    def test_invalid_date_never_starts_process(self):
        self.app.demo = False
        self.app.date_picker.value.set('2026-02-30')
        with patch.object(self.app.controller, 'start') as start, \
             patch('launcher_gui.messagebox.showerror') as error:
            self.app.start()
        start.assert_not_called()
        error.assert_called_once()

    def test_historical_completion_is_distinguished_from_manual_stop(self):
        self.app.demo = False
        self.app.run_reference = '20261023'
        self.app._exited({'returncode': 0, 'stop_requested': False})
        self.app._refresh()
        self.assertEqual(self.app.state, 'completed')
        self.assertEqual(self.app.badge['text'], '历史任务完成')
        self.app._exited({'returncode': 0, 'stop_requested': True})
        self.assertEqual(self.app.state, 'stopped')

    def test_scheduled_start_resets_previous_manual_history_selection(self):
        from datetime import datetime
        self.app.demo = False
        self.app.date_picker.set_date(date(2026, 10, 23))
        now = datetime(2026, 10, 27, 9, 15)
        with patch('launcher_gui.china_now', return_value=now):
            self.app.request_auto_start({'allowed': True, 'date': '2026-10-27'})
        self.assertEqual(self.app.date_picker.value.get(), '2026-10-27')

    def test_controls_and_popup_fit_without_resizing_buttons(self):
        self.root.deiconify()
        for width, height in ((1180, 790), (900, 640)):
            self.root.geometry('%dx%d' % (width, height))
            self.root.update()
            picker = self.app.date_picker
            before = picker.button.winfo_width(), picker.button.winfo_height()
            picker.open_calendar()
            self.root.update_idletasks()
            popup = picker.popup
            self.assertLessEqual(popup.winfo_rootx() + popup.winfo_width(), popup.winfo_screenwidth())
            self.assertLessEqual(popup.winfo_rooty() + popup.winfo_height(), popup.winfo_screenheight())
            picker._move(-1)
            picker._move(1)
            self.root.update_idletasks()
            self.assertEqual((picker.button.winfo_width(), picker.button.winfo_height()), before)
            picker.close_calendar()
            for widget in (picker, self.app.date_summary, self.app.start_button, self.app.notebook):
                self.assertLessEqual(widget.winfo_rootx() - self.root.winfo_rootx() + widget.winfo_width(), width)
                self.assertLessEqual(widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height(), height)


if __name__ == '__main__':
    unittest.main()

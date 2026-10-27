import io
import contextlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

from test_bridge import ROOT, FakeContext, bridge_module


class LauncherModelTests(unittest.TestCase):
    def setUp(self):
        import launcher_core
        self.core = launcher_core

    def test_traceback_and_recovered_errors_remain_visible(self):
        model = self.core.LogModel()
        model.append('[RUNNING][THS] Original timers registered')
        model.append('[2026-10-27 10:00:00] [ERROR] publish failed')
        model.append('Traceback (most recent call last):')
        model.append('  File "scanner.py", line 15, in publish')
        model.append('TypeError: missing value')
        model.append('[2026-10-27 10:00:01] [INFO] recovered')
        self.assertEqual(model.errors, 1)
        self.assertIn('line 15', '\n'.join(model.details))
        self.assertIn('TypeError: missing value', '\n'.join(model.details))
        self.assertNotIn('recovered', '\n'.join(model.details))

    def test_warning_and_strategy_states_are_independent(self):
        model = self.core.LogModel()
        model.append('[START][盘中] original.py')
        model.append('[RUNNING][THS] timers registered')
        model.append('[2026-10-27 10:00:00] [WARN] slow cycle')
        self.assertEqual(model.states['panzhong'], 'initializing')
        self.assertEqual(model.states['ths'], 'running')
        self.assertEqual(model.warnings, 1)
        self.assertEqual(model.errors, 0)

    def test_bounded_queue_reports_overflow_without_blocking_reader(self):
        events = self.core.EventBuffer(limit=3)
        for index in range(10):
            events.put('line', str(index))
        drained = events.drain(20)
        self.assertEqual([e['value'] for e in drained if e['kind'] == 'line'], ['7', '8', '9'])
        self.assertTrue(any(e['kind'] == 'overflow' and e['value'] == 7 for e in drained))

    def test_passive_bridge_status_does_not_touch_request_slot(self):
        b = bridge_module()
        channel = 'gui_' + uuid.uuid4().hex
        with b.SharedSlot(b._channel_name(channel) + '_status', 4096) as slot:
            slot.write({'protocol': 1, 'state': 'ready', 'heartbeat': time.time(),
                        'strategy_config': {'version': 2}})
            with patch.object(b.RpcClient, 'call', side_effect=AssertionError('active RPC')):
                status = self.core.read_bridge_status(channel)
            self.assertTrue(status['online'])
            self.assertEqual(status['version'], 2)

    def test_stale_bridge_is_not_reported_as_online(self):
        b = bridge_module()
        channel = 'stale_gui_' + uuid.uuid4().hex
        with b.SharedSlot(b._channel_name(channel) + '_status', 4096) as slot:
            slot.write({'protocol': 1, 'state': 'ready', 'heartbeat': time.time() - 90})
            self.assertFalse(self.core.read_bridge_status(channel)['online'])

    def test_long_valid_market_request_is_not_a_false_bridge_alarm(self):
        b = bridge_module()
        channel = 'busy_gui_' + uuid.uuid4().hex
        with b.SharedSlot(b._channel_name(channel) + '_status', 4096) as status:
            with b.SharedSlot(b._channel_name(channel) + '_request', 1024 * 1024) as request:
                status.write({'protocol': 1, 'state': 'busy', 'heartbeat': time.time() - 20,
                              'server_id': 'bridge1', 'last_request_id': 'request1'})
                request.write({'server_id': 'bridge1', 'id': 'request1', 'deadline': time.time() + 60})
                self.assertTrue(self.core.read_bridge_status(channel)['online'])

    def test_quiet_watchdog_respects_original_time_switch_without_scheduling(self):
        states = {'panzhong': 'running', 'ths': 'running'}
        now = datetime(2026, 10, 27, 16, 0)
        self.assertFalse(self.core.quiet_is_unexpected({'check_trading_time': True}, states, now))
        self.assertTrue(self.core.quiet_is_unexpected({'check_trading_time': False}, states, now))
        self.assertTrue(self.core.quiet_is_unexpected({}, {'panzhong': 'initializing'}, now))
        self.assertTrue(self.core.quiet_is_unexpected({}, states, now.replace(hour=10)))


class LauncherProcessTests(unittest.TestCase):
    def setUp(self):
        import launcher_core
        self.core = launcher_core
        self.temp = tempfile.TemporaryDirectory()
        self.events = self.core.EventBuffer()
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            if controller.running:
                controller.force_stop()
            if controller.process is not None:
                controller.process.wait(timeout=10)
            if controller.reader is not None:
                controller.reader.join(10)
        self.temp.cleanup()

    def make_controller(self, source):
        controller = self.core.ManagedProcess(
            ROOT, self.events, log_dir=Path(self.temp.name),
            command=[sys.executable, '-X', 'utf8', '-u', '-c', source],
            running_probe=lambda: False)
        self.controllers.append(controller)
        return controller

    def test_hidden_process_captures_chinese_stderr_and_graceful_stop(self):
        controller = self.make_controller(
            "import sys; print('测试日志', flush=True); "
            "print('warning on stderr', file=sys.stderr, flush=True); "
            "print('control='+sys.stdin.readline().strip(), flush=True)")
        controller.start()
        self.assertTrue(controller.running)
        self.assertTrue(controller.request_stop())
        controller.reader.join(10)
        self.assertFalse(controller.reader.is_alive())
        output = controller.log_path.read_text(encoding='utf-8')
        self.assertIn('测试日志', output)
        self.assertIn('warning on stderr', output)
        self.assertIn('control=STOP', output)
        event = next(e for e in self.events.drain(100) if e['kind'] == 'exit')
        self.assertEqual(event['value']['returncode'], 0)
        self.assertTrue(event['value']['stop_requested'])

    def test_failed_exit_includes_code_command_pid_and_log_file(self):
        controller = self.make_controller("raise RuntimeError('simulated failure')")
        controller.start()
        controller.reader.join(10)
        events = self.events.drain(100)
        details = next(e['value'] for e in events if e['kind'] == 'exit')
        self.assertNotEqual(details['returncode'], 0)
        self.assertGreater(details['pid'], 0)
        self.assertIn('RuntimeError', controller.log_path.read_text(encoding='utf-8'))
        self.assertEqual(details['log_path'], str(controller.log_path))
        self.assertEqual(details['command'], controller.command)

    def test_existing_runner_is_not_terminated_or_duplicated(self):
        controller = self.make_controller('pass')
        controller.running_probe = lambda: True
        with self.assertRaisesRegex(RuntimeError, 'already'):
            controller.start()
        self.assertIsNone(controller.process)

    def test_full_log_survives_bounded_screen_buffer(self):
        controller = self.make_controller("for i in range(400): print('row', i)")
        controller.events = self.core.EventBuffer(limit=10)
        controller.start()
        controller.reader.join(10)
        content = controller.log_path.read_text(encoding='utf-8')
        self.assertIn('row 0', content)
        self.assertIn('row 399', content)
        self.assertIn('exit_code=0', content)

    def test_source_tagged_stdout_is_saved_in_separate_files(self):
        import json
        records = [{'source': 'panzhong', 'text': 'pz only'}, {'source': 'ths', 'text': 'ths only'}]
        payload = '\n'.join('@QMT_LOG@' + json.dumps(record) for record in records)
        controller = self.make_controller('print(%r)' % payload)
        controller.start()
        controller.reader.join(10)
        self.assertEqual(controller.log_path.with_suffix('.panzhong.log').read_text(encoding='utf-8'), 'pz only\n')
        self.assertEqual(controller.log_path.with_suffix('.ths.log').read_text(encoding='utf-8'), 'ths only\n')
        body = '\n'.join(line for line in controller.log_path.read_text(encoding='utf-8').splitlines()
                         if not line.startswith('[LAUNCHER]'))
        self.assertNotIn('@QMT_LOG@', body)

    def test_hidden_details_are_kept_raw_but_not_printed_or_saved_as_key_logs(self):
        import json
        records = [{'source': 'ths', 'text': 'visible', 'display': True},
                   {'source': 'ths', 'text': 'repeated missing history', 'display': False},
                   {'source': 'ths', 'text': '[ERROR] performance', 'display': True,
                    'display_text': '[WARN] performance'}]
        payload = '\n'.join('@QMT_LOG@' + json.dumps(record) for record in records)
        controller = self.make_controller('print(%r)' % payload)
        controller.start()
        controller.reader.join(10)
        raw = controller.raw_log_path.read_text(encoding='utf-8')
        shown = controller.log_path.with_suffix('.ths.log').read_text(encoding='utf-8')
        self.assertIn('repeated missing history', raw)
        self.assertNotIn('repeated missing history', shown)
        self.assertIn('[ERROR] performance', raw)
        self.assertIn('[WARN] performance', shown)
        self.assertNotIn('[ERROR]', shown)
        event_text = '\n'.join(e['value'] for e in self.events.drain(100) if e['kind'] == 'line')
        self.assertNotIn('repeated missing history', event_text)

    def test_real_runner_cancels_before_bridge_is_available(self):
        controller = self.core.ManagedProcess(
            ROOT, self.events, log_dir=Path(self.temp.name), running_probe=lambda: False,
            command=[sys.executable, '-X', 'utf8', '-u', str(ROOT / 'external_runner.py'),
                     '--check', '--control-stdin', '--wait', '30',
                     '--channel', 'gui_cancel_' + uuid.uuid4().hex])
        self.controllers.append(controller)
        controller.start()
        controller.request_stop()
        controller.reader.join(5)
        self.assertFalse(controller.reader.is_alive())
        self.assertEqual(controller.process.returncode, 0)
        self.assertIn('[STOPPED]', controller.log_path.read_text(encoding='utf-8'))

    def test_real_gui_check_imports_numpy_without_blocking_on_stdin(self):
        b = bridge_module()
        channel = 'gui_numpy_' + uuid.uuid4().hex
        server = b.BridgeServer(FakeContext(), channel)
        stop = threading.Event()
        def serve():
            while not stop.is_set():
                server.serve_once()
                stop.wait(.002)
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        controller = self.core.ManagedProcess(
            ROOT, self.events, log_dir=Path(self.temp.name), running_probe=lambda: False,
            command=[sys.executable, '-X', 'utf8', '-u', str(ROOT / 'external_runner.py'),
                     '--check', '--control-stdin', '--ui-events', '--wait', '5', '--channel', channel])
        self.controllers.append(controller)
        try:
            controller.start()
            controller.reader.join(8)
            self.assertFalse(controller.reader.is_alive(), 'NumPy import blocked by stdin listener')
            self.assertEqual(controller.process.returncode, 0)
            self.assertIn('[CHECK OK]', controller.log_path.read_text(encoding='utf-8'))
        finally:
            if controller.running:
                controller.force_stop()
                controller.reader.join(5)
            stop.set()
            worker.join(5)
            server.close()


class RunnerControlTests(unittest.TestCase):
    def test_gui_mode_retains_full_startup_error_traceback(self):
        import external_runner
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with patch.object(external_runner.threading, 'Thread'):
                code = external_runner.main(['--check', '--control-stdin', '--wait', '.05',
                                             '--channel', 'gui_fail_' + uuid.uuid4().hex])
        self.assertEqual(code, 2)
        self.assertIn('[FAILED]', stdout.getvalue())
        self.assertIn('Traceback', stderr.getvalue())
        self.assertIn('wait_ready', stderr.getvalue())

    def test_stop_and_stdin_close_set_stop_event(self):
        import external_runner
        for stream in (io.StringIO('STOP\n'), io.StringIO('')):
            stop = threading.Event()
            external_runner.read_control_commands(stream, stop)
            self.assertTrue(stop.is_set())

    def test_cancelled_rpc_wait_exits_without_waiting_for_timeout(self):
        b = bridge_module()
        channel = 'cancel_' + uuid.uuid4().hex
        stop = threading.Event()
        client = b.RpcClient(channel, timeout=30, stop_event=stop)
        stop.set()
        try:
            with self.assertRaises(b.BridgeCancelled):
                client.wait_ready(30)
        finally:
            client.close()

    def test_cancelled_worker_is_not_reported_as_failed(self):
        import external_runner
        b = bridge_module()
        stop = threading.Event()
        client = type('Client', (), {'assert_online': lambda self: {}})()
        def cancelled(session, event):
            event.set()
            raise b.BridgeCancelled('user stopped')
        with patch.object(external_runner.StrategySession, 'run', cancelled):
            sessions = external_runner.run_sessions(
                client, {'THS': ROOT.parent / 'ths筛选.py'}, stop_event=stop)
        self.assertEqual(sessions[0].state, 'stopped')

    def test_inflight_rpc_can_be_cancelled_without_stopping_qmt(self):
        b = bridge_module()
        channel = 'inflight_' + uuid.uuid4().hex
        stop = threading.Event()
        server = b.BridgeServer(object(), channel)
        client = b.RpcClient(channel, timeout=30, stop_event=stop)
        client.wait_ready(2)
        timer = threading.Timer(.1, stop.set)
        timer.start()
        try:
            started = time.monotonic()
            with self.assertRaises(b.BridgeCancelled):
                client.call('ping')
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(server.status.read()['state'], 'ready')
        finally:
            timer.join(2)
            client.close()
            server.close()


if __name__ == '__main__':
    unittest.main()

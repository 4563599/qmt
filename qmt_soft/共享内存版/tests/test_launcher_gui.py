import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from test_bridge import ROOT


class LauncherGuiTests(unittest.TestCase):
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
        self.root.update_idletasks()
        self.app._destroy()
        self.override.stop()
        self.temp.cleanup()

    def test_preview_never_starts_real_strategy(self):
        self.assertIsNone(self.app.controller.process)
        self.assertEqual(str(self.app.start_button['state']), 'normal')
        self.app._refresh()
        self.assertEqual(str(self.app.start_button['state']), 'disabled')

    def test_error_details_show_complete_traceback(self):
        self.app._line('[FAILED] RuntimeError: simulated failure\n'
                       'Traceback (most recent call last):\n'
                       '  File "fake.py", line 19, in main\n'
                       'RuntimeError: simulated failure')
        content = self.app.error_text.get('1.0', 'end-1c')
        self.assertIn('line 19', content)
        self.assertIn('RuntimeError', content)
        self.assertEqual(str(self.app.notebook.select()), self.app.error_tab)

    def test_search_and_clear_do_not_stop_or_reset_strategy_state(self):
        self.app.search_var.set('REDIS')
        self.app.rebuild_logs()
        content = self.app.log_text.get('1.0', 'end-1c')
        self.assertIn('[REDIS]', content)
        self.assertNotIn('[HISTORY]', content)
        states = dict(self.app.model.states)
        self.app.clear_display()
        self.assertEqual(self.app.log_text.get('1.0', 'end-1c'), '')
        self.assertEqual(self.app.model.states, states)

    def test_active_process_exit_requires_stop_confirmation(self):
        controller = type('Controller', (), {'running': True})()
        self.app.controller = controller
        with patch('launcher_gui.messagebox.askyesno', return_value=False):
            self.app.close()
        self.assertFalse(self.app.closing)

    def test_primary_controls_fit_default_and_compact_windows(self):
        self.root.deiconify()
        for width, height in ((1180, 790), (900, 640)):
            self.root.geometry('%dx%d' % (width, height))
            self.root.update()
            for widget in (self.app.start_button, self.app.stop_button, self.app.notebook,
                           self.app.log_path_label, self.app.clock_label):
                self.assertTrue(widget.winfo_viewable(), '%sx%s %s' % (width, height, widget))
                bottom = widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height()
                right = widget.winfo_rootx() - self.root.winfo_rootx() + widget.winfo_width()
                self.assertLessEqual(bottom, self.root.winfo_height())
                self.assertLessEqual(right, self.root.winfo_width())

    def test_strategy_logs_stay_in_separate_views_and_records(self):
        self.app.clear_display()
        self.app._line('[INFO] pz only', source='panzhong')
        self.app._line('[INFO] ths only', source='ths')
        self.app._line('[INFO] system only')
        for source, expected in (('panzhong', 'pz only'), ('ths', 'ths only'), ('system', 'system only')):
            text = self.app.log_views[source].get('1.0', 'end-1c')
            self.assertIn(expected, text)
            self.assertEqual(len(text.splitlines()), 1)
        self.app.rebuild_logs()
        self.assertNotIn('ths only', self.app.log_views['panzhong'].get('1.0', 'end'))

    def test_selected_tabs_and_changed_stop_text_do_not_resize_controls(self):
        self.root.deiconify()
        self.root.update()
        button_size = (self.app.stop_button.winfo_width(), self.app.stop_button.winfo_height())
        notebook_size = (self.app.notebook.winfo_reqwidth(), self.app.notebook.winfo_reqheight())
        for index in range(4):
            self.app.notebook.select(index)
            self.root.update_idletasks()
            self.assertEqual((self.app.notebook.winfo_reqwidth(), self.app.notebook.winfo_reqheight()), notebook_size)
        self.app.stop_button.configure(text='强制结束')
        self.root.update_idletasks()
        self.assertEqual((self.app.stop_button.winfo_width(), self.app.stop_button.winfo_height()), button_size)

    def test_automatic_start_waits_for_bridge_and_runs_only_once(self):
        self.app.demo = False
        decision = {'allowed': True, 'date': '2026-10-28'}
        now = datetime(2026, 10, 28, 9, 15)
        with patch('launcher_gui.china_now', return_value=now), patch.object(self.app, 'start') as start:
            self.app.bridge_status = {'online': False}
            self.app.request_auto_start(decision)
            self.app._attempt_auto_start()
            start.assert_not_called()
            self.app.bridge_status['online'] = True
            self.app._attempt_auto_start()
            self.app._attempt_auto_start()
            start.assert_called_once()

    def test_stop_cancels_waiting_auto_start(self):
        self.app.auto_date = '2026-10-28'
        self.app.stop()
        self.assertIsNone(self.app.auto_date)


if __name__ == '__main__':
    unittest.main()

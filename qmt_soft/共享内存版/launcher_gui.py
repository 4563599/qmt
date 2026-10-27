# coding: utf-8
"""Native Windows control panel for the external QMT scanner."""
import argparse
import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import traceback

from launcher_core import (EventBuffer, LogModel, ManagedProcess, quiet_is_unexpected,
                           read_bridge_status, runner_is_active)
from external_runner import SingleInstance
from qmt_shm_bridge import BridgeError, CHANNEL
from launcher_schedule import (AutoStartSignal, TradingCalendar, china_now, launch_scheduled_gui,
                               record_schedule, scheduled_decision)
from date_picker import DatePicker
from launch_date import parse_date, selection_reference

ROOT = Path(__file__).resolve().parent
COLORS = {'bg': '#f3f5f7', 'paper': '#ffffff', 'text': '#222a32', 'muted': '#67737d',
          'line': '#dce2e7', 'accent': '#08796b', 'soft': '#e4f3ef',
          'error': '#b33342', 'error_bg': '#fff0f1', 'warning': '#986317',
          'warning_bg': '#fff5df'}
STATE_NAMES = {'pending': '等待启动', 'initializing': '初始化中', 'running': '运行中',
               'completed': '历史任务完成', 'stopped': '已停止', 'failed': '运行异常'}


class LauncherApp:
    def __init__(self, root, demo=False):
        self.root, self.demo = root, demo
        self.events = EventBuffer()
        self.model = LogModel()
        self.controller = ManagedProcess(ROOT, self.events)
        self.monitor_stop = threading.Event()
        self.closing = False
        self.stopping_at = None
        self.force_offered = False
        self.existing_runner = False
        self.bridge_status = {}
        self.watch_reason = None
        self.state = 'idle'
        self.ui_log = None
        self.auto_date = None
        self.run_reference = None
        self.auto_signal = None if demo else AutoStartSignal()
        self.filter_var = tk.StringVar(value='全部')
        self.search_var = tk.StringVar()
        self.follow_var = tk.BooleanVar(value=True)
        self._build()
        self.root.report_callback_exception = self._callback_error
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        if demo:
            self._demo()
        else:
            threading.Thread(target=self._watch, daemon=True, name='qmt-gui-watch').start()
        self.poll_job = self.root.after(80, self._poll)

    def _build(self):
        root = self.root
        root.title('QMT 策略控制台')
        root.geometry('1180x790')
        root.minsize(900, 640)
        root.configure(bg=COLORS['bg'])
        root.option_add('*Font', ('Microsoft YaHei UI', 10))
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=(14, 8),
                        background=COLORS['paper'], foreground=COLORS['text'],
                        bordercolor=COLORS['line'], focuscolor=COLORS['accent'], width=8,
                        borderwidth=1, relief='flat')
        style.map('TButton', background=[('active', '#edf1f4')],
                  foreground=[('disabled', '#9aa3aa')])
        style.configure('Primary.TButton', background=COLORS['accent'], foreground='white',
                        bordercolor=COLORS['accent'])
        style.map('Primary.TButton', background=[('disabled', '#cbd5d6'), ('active', '#096758')],
                  foreground=[('disabled', '#7b898b')])
        style.configure('Stop.TButton', foreground=COLORS['error'])
        style.configure('TNotebook', background=COLORS['paper'], borderwidth=0)
        style.configure('TNotebook.Tab', font=('Microsoft YaHei UI', 10), padding=(17, 9),
                        background=COLORS['bg'], foreground=COLORS['muted'])
        style.map('TNotebook.Tab', background=[('selected', 'white')],
                  foreground=[('selected', COLORS['text'])])
        tab_font = ('Microsoft YaHei UI', 10)
        style.configure('Logs.TNotebook', background='white', borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure('Logs.TNotebook.Tab', font=tab_font, padding=(18, 10),
                        borderwidth=0, background='#f1f4f6', foreground=COLORS['muted'])
        style.map('Logs.TNotebook.Tab',
                  background=[('selected', COLORS['soft']), ('active', '#edf2f3')],
                  foreground=[('selected', COLORS['accent']), ('active', COLORS['text'])],
                  expand=[('selected', (0, 0, 0, 0)), ('!selected', (0, 0, 0, 0))],
                  padding=[('selected', (18, 10)), ('!selected', (18, 10))],
                  font=[('selected', tab_font), ('!selected', tab_font)])
        style.configure('TCheckbutton', background=COLORS['paper'], foreground=COLORS['muted'])
        style.configure('TCombobox', padding=(7, 5))
        style.configure('Calendar.TButton', font=('Segoe MDL2 Assets', 12), padding=(7, 7), width=3)

        header = tk.Frame(root, bg=COLORS['paper'])
        header.pack(fill='x')
        head = tk.Frame(header, bg=COLORS['paper'])
        head.pack(fill='x', padx=26, pady=(21, 18))
        tk.Label(head, text='QMT 策略控制台', bg='white', fg=COLORS['text'],
                 font=('Microsoft YaHei UI', 19, 'bold')).pack(side='left')
        self.badge = tk.Label(head, text='未启动', bg='#edf0f3', fg=COLORS['muted'],
                              padx=12, pady=5, width=12, font=('Microsoft YaHei UI', 10, 'bold'))
        self.badge.pack(side='right')
        self.process_info = tk.Label(head, text='盘中 + 同花顺', bg='white', fg=COLORS['muted'])
        self.process_info.pack(side='right', padx=18)
        tk.Frame(root, height=1, bg=COLORS['line']).pack(fill='x')

        status = tk.Frame(root, bg=COLORS['paper'])
        status.pack(fill='x', padx=22, pady=(18, 0))
        status.grid_columnconfigure((0, 2, 4), weight=1, uniform='status')
        self.status_widgets = {}
        for column, (key, name, value, detail) in enumerate((
                ('bridge', 'QMT 共享内存', '检查中', '仅读取心跳，不发送行情请求'),
                ('panzhong', '盘中选股', '等待启动', '原策略配置'),
                ('ths', '同花顺', '等待启动', '原策略配置'))):
            cell = tk.Frame(status, bg='white')
            cell.grid(row=0, column=column * 2, sticky='nsew', padx=20, pady=17)
            tk.Label(cell, text=name, bg='white', fg=COLORS['muted'], anchor='w').pack(fill='x')
            value_widget = tk.Label(cell, text=value, bg='white', fg=COLORS['text'], anchor='w',
                                    font=('Microsoft YaHei UI', 15, 'bold'))
            value_widget.pack(fill='x', pady=(6, 5))
            detail_widget = tk.Label(cell, text=detail, bg='white', fg=COLORS['muted'], anchor='w',
                                     font=('Microsoft YaHei UI', 9))
            detail_widget.pack(fill='x')
            self.status_widgets[key] = (value_widget, detail_widget)
            if column < 2:
                tk.Frame(status, width=1, bg=COLORS['line']).grid(
                    row=0, column=column * 2 + 1, sticky='ns', pady=20)

        self.notice = tk.Label(root, text='请先在 QMT 运行 qmt_shm_bridge.py，再启动外部程序。',
                               bg=COLORS['bg'], fg=COLORS['muted'], anchor='w', justify='left',
                               padx=2, pady=11, font=('Microsoft YaHei UI', 9))
        self.notice.pack(fill='x', padx=24)
        datebar = tk.Frame(root, bg=COLORS['bg'])
        datebar.pack(fill='x', padx=22, pady=(0, 12))
        tk.Label(datebar, text='运行日期', bg=COLORS['bg'], fg=COLORS['text']).pack(side='left', padx=(0, 10))
        self.date_picker = DatePicker(datebar, COLORS, command=self._date_changed)
        self.date_picker.pack(side='left')
        self.today_button = ttk.Button(datebar, text='今天', width=5,
            command=lambda: self.date_picker.set_date(china_now().date()))
        self.today_button.pack(side='left', padx=8)
        self.date_summary = tk.Label(datebar, bg=COLORS['bg'], fg=COLORS['muted'],
                                     font=('Microsoft YaHei UI', 9))
        self.date_summary.pack(side='left', padx=8)
        self._date_changed()
        toolbar = tk.Frame(root, bg=COLORS['bg'])
        toolbar.pack(fill='x', padx=22, pady=(0, 13))
        self.start_button = ttk.Button(toolbar, text='启动', style='Primary.TButton', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(toolbar, text='停止', style='Stop.TButton',
                                      command=self.stop, state='disabled', width=10)
        self.stop_button.pack(side='left', padx=(8, 0))
        ttk.Button(toolbar, text='打开日志', command=self.open_log).pack(side='left', padx=(18, 0))
        ttk.Button(toolbar, text='清空显示', command=self.clear_display).pack(side='left', padx=8)
        ttk.Button(toolbar, text='退出', command=self.close).pack(side='right')
        self.summary = tk.Label(toolbar, text='异常 0  ·  警告 0', bg=COLORS['bg'], fg=COLORS['muted'])
        self.summary.pack(side='right', padx=18)

        surface = tk.Frame(root, bg=COLORS['paper'], highlightthickness=1,
                           highlightbackground=COLORS['line'])
        surface.pack(fill='both', expand=True, padx=22)
        filters = tk.Frame(surface, bg='white')
        filters.pack(fill='x', padx=13, pady=10)
        combo = ttk.Combobox(filters, textvariable=self.filter_var, values=('全部', '警告', '错误'),
                             state='readonly', width=7)
        combo.pack(side='left')
        combo.bind('<<ComboboxSelected>>', lambda event: self.rebuild_logs())
        tk.Label(filters, text='查找', bg='white', fg=COLORS['muted']).pack(side='left', padx=(17, 7))
        search = ttk.Entry(filters, textvariable=self.search_var, width=26)
        search.pack(side='left')
        search.bind('<Return>', lambda event: self.rebuild_logs())
        ttk.Button(filters, text='查找', command=self.rebuild_logs, padding=(9, 3)).pack(side='left', padx=6)
        ttk.Checkbutton(filters, text='自动滚动', variable=self.follow_var).pack(side='right')
        self.notebook = ttk.Notebook(surface, style='Logs.TNotebook')
        self.notebook.pack(fill='both', expand=True)
        self.log_views = {}
        for source, title in (('panzhong', '盘中日志'), ('ths', '同花顺日志'), ('system', '系统日志')):
            page = tk.Frame(self.notebook, bg='white')
            self.notebook.add(page, text=title)
            self.log_views[source] = self._text_area(page)
        self.log_text = self.log_views['panzhong']
        error_page = tk.Frame(self.notebook, bg='white')
        self.notebook.add(error_page, text='异常详情')
        self.error_tab = str(error_page)

        error_toolbar = tk.Frame(error_page, bg='white')
        error_toolbar.pack(fill='x', padx=13, pady=10)
        tk.Label(error_toolbar, text='错误堆栈与运行信息', bg='white', fg=COLORS['muted']).pack(side='left')
        ttk.Button(error_toolbar, text='复制详情', command=self.copy_details, padding=(10, 3)).pack(side='right')
        self.error_text = self._text_area(error_page)

        footer = tk.Frame(root, bg=COLORS['bg'])
        footer.pack(side='bottom', before=surface, fill='x', padx=24, pady=(9, 12))
        self.log_path_label = tk.Label(footer, text='日志：尚未启动', bg=COLORS['bg'],
                                       fg=COLORS['muted'], anchor='w', font=('Microsoft YaHei UI', 9))
        self.log_path_label.pack(side='left', fill='x', expand=True)
        self.clock_label = tk.Label(footer, bg=COLORS['bg'], fg=COLORS['muted'],
                                    font=('Microsoft YaHei UI', 9))
        self.clock_label.pack(side='right')
        root.bind('<Configure>', lambda event: self.notice.configure(wraplength=max(600, root.winfo_width() - 50))
                  if event.widget is root else None)

    def _text_area(self, parent):
        frame = tk.Frame(parent, bg='white')
        frame.pack(fill='both', expand=True, padx=(12, 5), pady=(0, 9))
        scroll = ttk.Scrollbar(frame, orient='vertical')
        scroll.pack(side='right', fill='y')
        text = tk.Text(frame, wrap='word', state='disabled', bg='white', fg=COLORS['text'],
                       font=('Microsoft YaHei UI', 10), borderwidth=0, highlightthickness=0,
                       padx=3, pady=3, spacing1=2, spacing3=4, selectbackground='#cce8e2',
                       yscrollcommand=scroll.set, undo=False)
        text.pack(fill='both', expand=True)
        scroll.configure(command=text.yview)
        text.tag_configure('error', foreground=COLORS['error'])
        text.tag_configure('warning', foreground=COLORS['warning'])
        text.tag_configure('system', foreground=COLORS['accent'])
        return text

    def _append_text(self, widget, line, tag='info'):
        widget.configure(state='normal')
        widget.insert('end', line.rstrip('\r\n') + '\n', tag)
        count = int(widget.index('end-1c').split('.')[0])
        if count > 6500:
            widget.delete('1.0', '%d.0' % (count - 6000))
        if self.follow_var.get():
            widget.see('end')
        widget.configure(state='disabled')

    def _visible(self, line, level):
        mode = self.filter_var.get()
        return ((mode == '全部' or mode == '警告' and level == 'warning'
                 or mode == '错误' and level == 'error')
                and self.search_var.get().casefold() in line.casefold())

    def _line(self, line, source='system'):
        source = source if source in self.log_views else 'system'
        for part in line.rstrip('\r\n').split('\n'):
            level = self.model.append(part, source=source)
            if self._visible(part, level):
                self._append_text(self.log_views[source], part, level)
            if level in ('error', 'warning'):
                label = {'panzhong': '盘中', 'ths': '同花顺', 'system': '系统'}[source]
                self._append_text(self.error_text, '[%s] %s' % (label, part), level)
            if '[FAILED]' in part:
                self.notebook.select(self.error_tab)
        self.summary.configure(text='异常 %d  ·  警告 %d' % (self.model.errors, self.model.warnings),
                               fg=COLORS['error'] if self.model.errors else COLORS['muted'])

    def _system(self, line, level='INFO'):
        text = '[%s] [%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), level, line)
        self._line(text)
        try:
            if self.ui_log is None:
                folder = ROOT / 'logs' / 'launcher' / datetime.now().strftime('%Y%m%d')
                folder.mkdir(parents=True, exist_ok=True)
                self.ui_log = (folder / ('ui_' + datetime.now().strftime('%H%M%S_%f') + '.log')).open(
                    'a', encoding='utf-8', buffering=1)
            self.ui_log.write(text + '\n')
        except OSError:
            self.notice.configure(text='界面日志写入失败，请检查磁盘空间和目录权限。', fg=COLORS['error'])

    def _callback_error(self, kind, error, tb):
        self._system('界面异常\n' + ''.join(traceback.format_exception(kind, error, tb)), 'ERROR')
        self.notebook.select(self.error_tab)

    def _watch(self):
        signal = self.auto_signal
        try:
            while not self.monitor_stop.is_set():
                status = read_bridge_status()
                try:
                    status['external_active'] = runner_is_active()
                except OSError as error:
                    status['external_active'] = True
                    status['error'] = str(error)
                self.events.put('health', status)
                if signal.consume():
                    try:
                        decision = scheduled_decision(china_now(), TradingCalendar(ROOT))
                        self.events.put('auto_start', decision)
                    except Exception:
                        self.events.put('line', '[ERROR] 自动启动交易日核对失败\n' + traceback.format_exc())
                self.monitor_stop.wait(1)
        finally:
            signal.close()

    def _poll(self):
        for event in self.events.drain():
            kind, value = event['kind'], event['value']
            if kind == 'line':
                self._line(value, source=event.get('source') or 'system')
            elif kind == 'health':
                self.bridge_status = value
                self.existing_runner = bool(value.get('external_active') and not self.controller.running)
            elif kind == 'overflow':
                self._system('界面日志过多，已省略较早的 %d 条；完整日志仍保存在本地。' % value, 'WARN')
            elif kind == 'exit':
                self._exited(value)
            elif kind == 'auto_start':
                self.request_auto_start(value)
        self._attempt_auto_start()
        self._refresh()
        if self.closing and not self.controller.running and not (
                self.controller.reader and self.controller.reader.is_alive()):
            self._destroy()
            return
        self.poll_job = self.root.after(80, self._poll)

    def _date_changed(self):
        if not hasattr(self, 'date_summary'):
            return
        try:
            historical = bool(selection_reference(self.date_picker.value.get(), china_now().date()))
            self.date_summary.configure(text='历史补数 · 执行一次 · 不推送' if historical else '当天实时 · 原时段控制',
                                        fg=COLORS['accent'] if historical else COLORS['muted'])
        except ValueError:
            self.date_summary.configure(text='请输入有效日期', fg=COLORS['error'])

    def _refresh(self):
        bridge = self.bridge_status
        online = bridge.get('online', False)
        value, detail = self.status_widgets['bridge']
        value.configure(text='已连接' if online else '未连接',
                        fg=COLORS['accent'] if online else COLORS['muted'])
        if online:
            detail.configure(text='心跳 %.1fs  ·  配置 v%s' % (bridge.get('age', 0), bridge.get('version') or '?'))
        else:
            detail.configure(text=bridge.get('error') or '等待 QMT 桥接策略')
        active = self.controller.running
        if active:
            elapsed = int(time.monotonic() - self.controller.started_at)
            self.process_info.configure(text='PID %s  ·  %02d:%02d:%02d' % (
                self.controller.pid, elapsed // 3600, elapsed // 60 % 60, elapsed % 60))
            if self.stopping_at is not None:
                self._badge('正在停止', 'warning')
                self.notice.configure(text='正在结束外部策略；QMT 桥接保持运行。', fg=COLORS['muted'])
                if time.monotonic() - self.stopping_at > 15 and not self.force_offered:
                    self.force_offered = True
                    self.stop_button.configure(text='强制结束', state='normal')
                    self._system('停止等待已超过15秒，可点击“强制结束”终止本界面启动的外部进程。', 'WARN')
            else:
                idle = time.monotonic() - (self.model.last_line_at or self.controller.started_at)
                unexpected_quiet = idle > 90 and quiet_is_unexpected(bridge, self.model.states)
                reason = 'bridge' if not online else ('quiet' if unexpected_quiet else None)
                self._badge('运行提醒' if reason else '运行中', 'warning' if reason else 'normal')
                if reason and reason != self.watch_reason:
                    text = ('桥接心跳不可用，请检查 QMT；外部程序仍由自身超时机制处理。'
                            if reason == 'bridge' else '超过90秒没有新日志，请检查初始化进度或异常详情。')
                    self._system(text, 'WARN')
                self.watch_reason = reason
                self.notice.configure(text=('桥接心跳暂不可用，正在等待恢复。' if reason == 'bridge'
                                            else '超过90秒没有新日志，请检查运行情况。' if reason == 'quiet'
                                            else '历史补数：%s；两份策略各执行一次，不发送钉钉。' % self.run_reference if self.run_reference
                                            else '策略沿用原配置；初始化、扫描及推送日志在下方实时显示。'),
                                      fg=COLORS['warning'] if reason else COLORS['muted'])
        elif self.demo:
            self._badge('界面预览', 'idle')
            self.process_info.configure(text='隔离预览 · 未启动策略')
            self.notice.configure(text='界面预览：没有启动选股，没有连接 Redis，没有发送推送。', fg=COLORS['muted'])
        elif self.existing_runner:
            self._badge('其他窗口运行中', 'warning')
            self.process_info.configure(text='未接管旧进程')
            self.notice.configure(text='检测到旧外部程序仍在运行。请先关闭旧窗口，再用此界面启动；不会自动结束旧进程。',
                                  fg=COLORS['warning'])
        elif self.state in ('stopped', 'failed', 'completed'):
            self._badge('运行异常' if self.state == 'failed' else '历史任务完成' if self.state == 'completed' else '已停止',
                        'error' if self.state == 'failed' else 'idle')
        else:
            self._badge('未启动', 'idle')
            self.notice.configure(text='请先在 QMT 运行 qmt_shm_bridge.py，再点击“启动”。', fg=COLORS['muted'])
        pending_exit = self.state == 'running' and self.controller.process is not None and not active
        date_enabled = not (active or pending_exit or self.existing_runner or self.closing or self.auto_date)
        self.date_picker.set_enabled(date_enabled)
        self.today_button.configure(state='normal' if date_enabled else 'disabled')
        if date_enabled and self.date_picker.follow_today:
            today = china_now().date().isoformat()
            if self.date_picker.value.get() != today:
                self.date_picker.value.set(today)
        self.start_button.configure(state='disabled' if active or pending_exit or self.existing_runner or self.demo or self.closing else 'normal')
        if self.stopping_at is None:
            self.stop_button.configure(state='normal' if active or self.auto_date else 'disabled')
        if self.auto_date and not active:
            self._badge('等待自动启动', 'warning')
            self.notice.configure(text='交易日自动启动已就绪，等待 QMT 行情桥连接；可点“停止”取消。', fg=COLORS['warning'])
        for key in ('panzhong', 'ths'):
            state = self.model.states[key]
            widget, sub = self.status_widgets[key]
            widget.configure(text=STATE_NAMES[state], fg=COLORS['accent'] if state == 'running' else COLORS['text'])
            description = '原策略配置 · 实时日志' if state == 'running' else '原策略配置'
            if self.existing_runner:
                description = '请在旧窗口查看'
            elif self.run_reference:
                description = '参考日期：%s · 历史补数' % self.run_reference
            elif key == 'panzhong' and bridge.get('check_trading_time') is not None:
                description = '时段控制：%s' % ('开启' if bridge['check_trading_time'] else '关闭，持续循环')
            sub.configure(text=description)
        self.clock_label.configure(text=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    def _badge(self, text, kind):
        fg, bg = {'normal': (COLORS['accent'], COLORS['soft']),
                  'warning': (COLORS['warning'], COLORS['warning_bg']),
                  'error': (COLORS['error'], COLORS['error_bg']),
                  'idle': (COLORS['muted'], '#edf0f3')}[kind]
        self.badge.configure(text=text, fg=fg, bg=bg)

    def start(self):
        if self.demo or self.closing:
            return
        if self.controller.running or self.existing_runner:
            return
        try:
            selected = parse_date(self.date_picker.value.get(), china_now().date())
            reference = selection_reference(selected.isoformat(), china_now().date())
        except ValueError as error:
            messagebox.showerror('日期不正确', str(error), parent=self.root)
            self.date_picker.entry.focus_set()
            return
        self.date_picker.set_date(selected)
        self.auto_date = None
        try:
            self.model = LogModel()
            self.stopping_at = None
            self.force_offered = False
            self.stop_button.configure(text='停止')
            self.controller.start(reference_date=reference)
            self.run_reference = reference
            self.state = 'running'
            self.log_path_label.configure(text='日志：' + str(self.controller.log_path.relative_to(ROOT)))
            if reference:
                self._system('启动历史补数：%s；盘中与同花顺各执行一次，更新对应日期Redis，不发送钉钉。' % selected.isoformat())
            else:
                self._system('启动当天实时盘中与同花顺；Redis、推送和时间控制均沿用现有配置。')
            self._system('日志模式：关键日志；运行摘要每30秒一次；重复历史缺失不刷屏，原始详情保留在.raw.log。')
        except Exception:
            self.state = 'failed'
            self._system('启动失败\n' + traceback.format_exc(), 'ERROR')
            self.notebook.select(self.error_tab)

    def request_auto_start(self, decision):
        if self.demo or self.closing:
            return
        if not decision.get('allowed'):
            self._system('当前不是交易日自动启动时段，本次已跳过。')
            return
        self.root.deiconify()
        self.root.lift()
        if self.controller.running or self.existing_runner:
            self._system('自动启动检查：程序已经运行，不重复启动。')
            return
        self.auto_date = decision['date']
        self.date_picker.set_date(china_now().date())
        self._system('已收到交易日09:15自动启动请求，等待 QMT 行情桥就绪。')

    def _attempt_auto_start(self):
        if not self.auto_date or self.closing or self.demo:
            return
        now = china_now()
        if self.auto_date != now.strftime('%Y-%m-%d') or now.hour * 100 + now.minute > 1505:
            self.auto_date = None
            self._system('自动启动等待已超出当日交易时段，本次取消。', 'WARN')
        elif self.controller.running or self.existing_runner:
            self.auto_date = None
        elif self.bridge_status.get('online'):
            self.auto_date = None
            self.date_picker.set_date(now.date())
            self.start()

    def stop(self):
        if self.auto_date:
            self.auto_date = None
            self._system('已取消本次等待中的自动启动。')
            if not self.controller.running:
                return
        if self.force_offered:
            if messagebox.askyesno('强制结束', '仅结束此界面启动的外部 Python 进程，不会停止 QMT。\n未完成的一轮数据写入可能被中断。是否继续？', parent=self.root):
                self.controller.force_stop()
                self._system('已请求强制结束外部进程。', 'WARN')
            return
        try:
            if self.controller.request_stop():
                self.stopping_at = time.monotonic()
                self.stop_button.configure(state='disabled')
                self._system('已请求正常停止，正在等待策略工作线程退出。')
        except Exception:
            self._system('正常停止请求失败\n' + traceback.format_exc(), 'ERROR')
            self.force_offered = True
            self.stop_button.configure(text='强制结束', state='normal')

    def _exited(self, value):
        requested = value['stop_requested']
        failed = value['returncode'] != 0
        historical_done = bool(self.run_reference and not requested and not failed)
        self.state = 'failed' if failed else 'completed' if historical_done else 'stopped'
        for key in self.model.states:
            if self.model.states[key] != 'completed':
                self.model.states[key] = 'failed' if failed else 'stopped'
        self.stopping_at = None
        self.force_offered = False
        self.stop_button.configure(text='停止', state='disabled')
        self._system('外部程序已退出，退出码 %s。\n%s' % (
            value['returncode'], json.dumps(value, ensure_ascii=False, indent=2)), 'ERROR' if failed else 'INFO')
        self.notice.configure(text='外部程序已停止，QMT 未受影响。' if requested else
                              '历史补数 %s 已完成，QMT 桥接保持运行。' % self.run_reference if historical_done else
                              '程序异常退出，请查看“异常详情”。' if failed else '本次运行已结束。',
                              fg=COLORS['error'] if failed else COLORS['muted'])
        if failed:
            self.notebook.select(self.error_tab)

    def rebuild_logs(self):
        for widget in self.log_views.values():
            self._clear_widget(widget)
        for record in self.model.records:
            if self._visible(record['text'], record['level']):
                self._append_text(self.log_views[record['source']], record['text'], record['level'])

    @staticmethod
    def _clear_widget(widget):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.configure(state='disabled')

    def clear_display(self):
        for widget in self.log_views.values():
            self._clear_widget(widget)
        self._clear_widget(self.error_text)
        self.model.lines.clear()
        self.model.records.clear()
        self.model.details.clear()
        self.model.errors = self.model.warnings = 0
        self.model.in_error = False
        self.model.error_sources.clear()
        self.summary.configure(text='异常 0  ·  警告 0', fg=COLORS['muted'])

    def copy_details(self):
        detail = self.error_text.get('1.0', 'end-1c')
        self.root.clipboard_clear()
        self.root.clipboard_append(detail)

    def open_log(self):
        folder = self.controller.log_path.parent if self.controller.log_path else ROOT / 'logs' / 'launcher'
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except OSError:
            self._system('打开日志目录失败\n' + traceback.format_exc(), 'ERROR')

    def close(self):
        if self.closing:
            return
        if self.controller.running:
            if not messagebox.askyesno('退出控制台', '退出前将停止此界面启动的外部程序。\nQMT 桥接不会停止。是否继续？', parent=self.root):
                return
            self.closing = True
            self.stop()
        else:
            self._destroy()

    def _destroy(self):
        self.monitor_stop.set()
        if self.poll_job:
            self.root.after_cancel(self.poll_job)
            self.poll_job = None
        if self.ui_log:
            self.ui_log.close()
        self.root.destroy()

    def _demo(self):
        self.bridge_status = {'online': True, 'age': .2, 'version': 2}
        lines = [
            '[CONFIG][盘中] reference_date=realtime; history_reload=09:15:00',
            '[START][盘中] 盘中.py',
            '[START][THS] ths筛选.py',
            '[2026-10-27 09:17:40] [INFO] [HISTORY] completed loaded=5020 failed=9 seconds=303.468',
            '[RUNNING][盘中] Original timers registered; keep QMT and this window open.',
            '[RUNNING][THS] Original timers registered; keep QMT and this window open.',
            '[2026-10-27 09:36:55] [INFO] [REDIS] pz=14; pz1=434; qy1=4; qy2=45',
            '[2026-10-27 09:36:55] [INFO] [CYCLE] cycle=53 market=5029 total=4.223s',
            '[2026-10-27 09:36:55] [WARN] [PERF] QMT snapshot exceeds 1 second',
            '[2026-10-27 09:36:57] [INFO] [第93轮] 清洗与K线发布成功：候选458只，入选26只，2.269s',
        ]
        for line in lines:
            source = 'ths' if '[THS]' in line or '[第93轮]' in line else 'panzhong'
            self._line(line, source=source)
        self._system('界面预览：未启动策略，未连接 Redis，未发送推送。')


def main():
    parser = argparse.ArgumentParser(description='QMT desktop strategy launcher')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--demo', action='store_true', help='Isolated UI preview; no strategy process')
    mode.add_argument('--scheduled', action='store_true', help='Short trading-day scheduled job')
    mode.add_argument('--auto-start', action='store_true', help='Open GUI after trading-calendar validation')
    parser.add_argument('--quit-after', type=float, help=argparse.SUPPRESS)
    args = parser.parse_args()
    decision = None
    if args.scheduled or args.auto_start:
        try:
            decision = scheduled_decision(china_now(), TradingCalendar(ROOT))
            record_schedule(ROOT, json.dumps(decision, ensure_ascii=True))
            if not decision['allowed']:
                return 0
            if args.scheduled:
                launch_scheduled_gui(ROOT)
                return 0
        except Exception:
            record_schedule(ROOT, 'Automatic start cancelled.\n' + traceback.format_exc())
            return 2
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (OSError, AttributeError):
        pass
    root = tk.Tk()
    lock = None
    try:
        if not args.demo:
            try:
                lock = SingleInstance('desktop_ui_' + CHANNEL)
            except BridgeError:
                root.withdraw()
                if args.auto_start:
                    try:
                        with AutoStartSignal(existing=True) as signal:
                            signal.request()
                    except OSError:
                        record_schedule(ROOT, 'Existing console needs to be reopened to enable scheduled start.')
                else:
                    messagebox.showinfo('QMT 策略控制台', '控制台已经打开，请使用现有窗口。', parent=root)
                root.destroy()
                return
        app = LauncherApp(root, demo=args.demo)
        if decision:
            app.request_auto_start(decision)
        if args.quit_after:
            root.after(int(args.quit_after * 1000), app.close)
        root.mainloop()
    except Exception:
        details = traceback.format_exc()
        folder = ROOT / 'logs' / 'launcher'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'startup_error.log').write_text(details, encoding='utf-8')
        messagebox.showerror('启动失败', details, parent=root)
        raise
    finally:
        if lock:
            lock.__exit__()


if __name__ == '__main__':
    raise SystemExit(main() or 0)

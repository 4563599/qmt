# coding: utf-8
"""Small editable date field with a native Tk calendar popup."""
import calendar
from datetime import date
import tkinter as tk
from tkinter import ttk

from launch_date import china_today, parse_date


class DatePicker(tk.Frame):
    def __init__(self, parent, colors, command=None):
        super().__init__(parent, bg=colors['bg'])
        self.colors, self.command = colors, command
        self.value = tk.StringVar(value=china_today().isoformat())
        self.popup = None
        self.enabled = True
        self.follow_today = True
        self.entry = ttk.Entry(self, textvariable=self.value, width=13,
                               font=('Microsoft YaHei UI', 10))
        self.entry.pack(side='left', ipady=5)
        self.button = ttk.Button(self, text='\ue787', width=3, style='Calendar.TButton',
                                 command=self.open_calendar)
        self.button.pack(side='left', padx=(4, 0))
        self.value.trace_add('write', self._changed)
        self.entry.bind('<Return>', self._normalize)
        self.entry.bind('<FocusOut>', self._normalize)

    def _changed(self, *unused):
        self.follow_today = self.value.get().strip().replace('-', '') == china_today().strftime('%Y%m%d')
        if self.command:
            self.command()

    def _normalize(self, event=None):
        try:
            self.set_date(parse_date(self.value.get()))
        except ValueError:
            pass

    def set_date(self, selected):
        self.value.set(selected.isoformat())

    def set_enabled(self, enabled):
        if self.enabled == enabled:
            return
        self.enabled = enabled
        self.entry.configure(state='normal' if enabled else 'disabled')
        self.button.configure(state='normal' if enabled else 'disabled')
        if not enabled:
            self.close_calendar()

    def close_calendar(self):
        if self.popup is not None:
            self.popup.destroy()
            self.popup = None

    def open_calendar(self):
        if not self.enabled:
            return
        if self.popup is not None:
            self.close_calendar()
            return
        try:
            self.selected = parse_date(self.value.get())
        except ValueError:
            self.selected = china_today()
        self.month = self.selected.replace(day=1)
        self.popup = tk.Toplevel(self)
        self.popup.withdraw()
        self.popup.overrideredirect(True)
        self.popup.transient(self.winfo_toplevel())
        self.popup.configure(bg=self.colors['line'])
        body = tk.Frame(self.popup, bg='white', padx=12, pady=10)
        body.pack(padx=1, pady=1)
        header = tk.Frame(body, bg='white')
        header.pack(fill='x')
        self.previous = ttk.Button(header, text='\ue76b', width=3,
                                   style='Calendar.TButton', command=lambda: self._move(-1))
        self.previous.pack(side='left')
        self.month_label = tk.Label(header, bg='white', fg=self.colors['text'],
                                    font=('Microsoft YaHei UI', 11, 'bold'), width=15)
        self.month_label.pack(side='left', padx=5)
        self.next = ttk.Button(header, text='\ue76c', width=3,
                               style='Calendar.TButton', command=lambda: self._move(1))
        self.next.pack(side='right')
        self.grid_frame = tk.Frame(body, bg='white')
        self.grid_frame.pack(pady=(10, 6))
        for index, name in enumerate('一二三四五六日'):
            tk.Label(self.grid_frame, text=name, bg='white', fg=self.colors['muted'],
                     font=('Microsoft YaHei UI', 9), width=4).grid(row=0, column=index, pady=(0, 5))
        self.day_buttons = []
        for index in range(42):
            button = tk.Button(self.grid_frame, width=4, height=1, borderwidth=0,
                               relief='flat', font=('Microsoft YaHei UI', 10), takefocus=True)
            button.grid(row=index // 7 + 1, column=index % 7, padx=1, pady=2, ipady=4)
            self.day_buttons.append(button)
        bottom = tk.Frame(body, bg='white')
        bottom.pack(fill='x')
        ttk.Button(bottom, text='今天', width=6, command=lambda: self._choose(china_today())).pack(side='left')
        ttk.Button(bottom, text='关闭', width=6, command=self.close_calendar).pack(side='right')
        self._render_month()
        self.popup.update_idletasks()
        width, height = self.popup.winfo_reqwidth(), self.popup.winfo_reqheight()
        x = max(0, min(self.winfo_rootx(), self.winfo_screenwidth() - width))
        y = self.winfo_rooty() + self.winfo_height() + 5
        if y + height > self.winfo_screenheight():
            y = max(0, self.winfo_rooty() - height - 5)
        self.popup.geometry('+%d+%d' % (x, y))
        self.popup.bind('<Escape>', lambda event: self.close_calendar())
        self.popup.bind('<FocusOut>', lambda event: self.after_idle(self._dismiss_unfocused))
        self.popup.deiconify()
        self.popup.focus_set()

    def _dismiss_unfocused(self):
        if self.popup is not None:
            focus = self.focus_get()
            if focus is None or focus.winfo_toplevel() != self.popup:
                self.close_calendar()

    def _move(self, step):
        year, month = divmod(self.month.year * 12 + self.month.month - 1 + step, 12)
        if not 1 <= year <= 9999:
            return
        self.month = date(year, month + 1, 1)
        self._render_month()

    def _render_month(self):
        self.month_label.configure(text='%d年 %d月' % (self.month.year, self.month.month))
        self.next.configure(state='disabled' if self.month >= china_today().replace(day=1) else 'normal')
        offset, days = calendar.monthrange(self.month.year, self.month.month)
        for index, button in enumerate(self.day_buttons):
            day = index - offset + 1
            valid = 1 <= day <= days
            current = self.month.replace(day=day) if valid else None
            active = valid and current <= china_today()
            selected = valid and current == self.selected
            today = valid and current == china_today()
            button.configure(text=str(day) if valid else '',
                state='normal' if active else 'disabled',
                bg=self.colors['accent'] if selected else self.colors['soft'] if today else 'white',
                fg='white' if selected else self.colors['text'],
                disabledforeground='#bbc2c8', activebackground=self.colors['soft'],
                activeforeground=self.colors['accent'],
                command=lambda value=current: self._choose(value))

    def _choose(self, selected):
        self.set_date(selected)
        self.close_calendar()
        self.entry.focus_set()

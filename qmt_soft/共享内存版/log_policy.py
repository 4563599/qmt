# coding: utf-8
"""Key console messages only. Raw logs and strategy calculations are untouched."""
from collections import defaultdict
from datetime import datetime
import re
import time

STOCK = re.compile(r'\d{6}\.(?:SH|SZ|BJ)')


class KeyLogPolicy:
    def __init__(self, summary_seconds=30, warning_seconds=300):
        self.summary_seconds = summary_seconds
        self.warning_seconds = warning_seconds
        self.days = {}
        self.missing = defaultdict(set)
        self.last = {}

    def _due(self, key, now, seconds):
        previous = self.last.get(key)
        if previous is not None and now - previous < seconds:
            return False
        self.last[key] = now
        if len(self.last) > 2048:
            self.last.pop(next(iter(self.last)))
        return True

    def select(self, source, line, now=None):
        now = time.monotonic() if now is None else now
        if source == 'system' or not line.strip():
            return line
        stamp = re.match(r'^\[(\d{4}-\d{2}-\d{2}) ', line)
        day = stamp[1] if stamp else datetime.now().strftime('%Y-%m-%d')
        if self.days.get(source) != day:
            self.days[source] = day
            self.missing[source].clear()
        body = re.sub(r'^\[\d{4}-\d{2}-\d{2} [^]]+\]\s*\[(?:INFO|WARN|ERROR|DEBUG)\]\s*', '', line)
        is_performance = ('[性能警告]' in body or body.startswith('[PERF]') and 'exceeds' in body)
        if '[ERROR]' in line and not is_performance:
            return line

        if '[历史预加载失败]' in body or '[HISTORY] failed sample=' in body:
            stocks = STOCK.findall(body)
            if stocks:
                new = [stock for stock in stocks if stock not in self.missing[source]]
                self.missing[source].update(stocks)
                if not new:
                    return None
                if len(new) != len(stocks):
                    prefix = line[:line.index(body)]
                    return prefix + '[历史数据缺失] 本次新增：' + ','.join(new)
                return line
            return line if self._due((source, day, body), now, 86400) else None

        if body.startswith('[构建失败]'):
            stocks = STOCK.findall(body)
            unknown = [stock for stock in stocks if stock not in self.missing[source]]
            if stocks and not unknown:
                return None
            if stocks and len(stocks) != len(unknown):
                return line[:line.index(body)] + '[构建失败] ' + ','.join(unknown)

        if body.startswith('[历史预加载]'):
            counts = re.search(r'成功\s*(\d+).*失败\s*(\d+)', body)
            if counts and int(counts[1]) == 0 and 0 < int(counts[2]) <= len(self.missing[source]):
                return None
            return line

        if is_performance:
            if not self._due((source, day, 'performance', body), now, self.warning_seconds):
                return None
            return line.replace('[ERROR]', '[WARN]', 1)

        if '[WARN]' in line and ('failed' in body.casefold() or '失败' in body):
            return line

        if body.startswith(('[CYCLE-START]', '[FILTER]', '[CONDITION]', '[5PULL-GROUP]',
                            '[SELECTED-TOP]', '[清洗]', '[清洗入选]')):
            return None
        if body.startswith('[REDIS] pz=') or body.startswith('[性能] 本轮'):
            return None
        if body.startswith('[CONFIG]') and any(key in body for key in (
                'optimization=', 'pricing=', 'pz_secondary=', 'dingtalk_routes=')):
            return None
        if body and set(body) == {'='}:
            return None
        if body.startswith('[CYCLE]') or re.match(r'^\[第\d+轮\].*清洗与K线发布', body):
            return line if self._due((source, day, 'summary'), now, self.summary_seconds) else None
        if '[WARN]' in line and not any(key in body for key in ('DINGTALK', '加载进度')):
            return line if self._due((source, day, 'warning', body), now, 60) else None
        return line

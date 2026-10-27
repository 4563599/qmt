# coding: utf-8
"""Source-aware stdout transport, without changing original strategy loggers."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import threading
from log_policy import KeyLogPolicy

PREFIX = '@QMT_LOG@'
SOURCES = {'panzhong', 'ths', 'system'}


def decode_log_event(line):
    if line.startswith(PREFIX):
        try:
            record = json.loads(line[len(PREFIX):])
            if record.get('source') in SOURCES and isinstance(record.get('text'), str):
                result = {'source': record['source'], 'text': record['text']}
                if isinstance(record.get('display'), bool):
                    result['display'] = record['display']
                if isinstance(record.get('display_text'), str):
                    result['display_text'] = record['display_text']
                return result
        except (ValueError, AttributeError):
            pass
    return {'source': 'system', 'text': line.rstrip('\r\n')}


class EventWriter:
    def __init__(self, target, source_paths=None):
        self.target = target
        self.lock = threading.RLock()
        self.pending = {}
        self.policy = KeyLogPolicy()
        parent = Path(__file__).resolve().parent.parent
        paths = source_paths or {'panzhong': parent / '盘中.py', 'ths': parent / 'ths筛选.py'}
        self.sources = {str(Path(path).resolve()).casefold(): source for source, path in paths.items()}

    def source(self):
        name = threading.current_thread().name
        if name == 'qmt-external-THS':
            return 'ths'
        if name == 'qmt-external-盘中':
            return 'panzhong'
        frame = sys._getframe(1)
        try:
            for _ in range(24):
                path = str(frame.f_globals.get('__file__', '')).casefold()
                if path in self.sources:
                    return self.sources[path]
                frame = frame.f_back
                if frame is None:
                    break
        finally:
            del frame
        return 'system'

    def emit(self, source, text):
        selected = self.policy.select(source, text)
        value = {'source': source, 'text': text, 'display': selected is not None}
        if selected is not None and selected != text:
            value['display_text'] = selected
        record = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        self.target.write(PREFIX + record + '\n')
        self.target.flush()

    def write(self, text):
        if not text:
            return 0
        source = self.source()
        key = (threading.get_ident(), source)
        with self.lock:
            pending = self.pending.get(key, '') + text
            while '\n' in pending:
                line, pending = pending.split('\n', 1)
                self.emit(source, line.rstrip('\r'))
            if len(pending) >= 65536:
                self.emit(source, pending)
                pending = ''
            if pending:
                self.pending[key] = pending
            else:
                self.pending.pop(key, None)
        return len(text)

    def flush(self, all_threads=False):
        with self.lock:
            keys = [key for key in self.pending if all_threads or key[0] == threading.get_ident()]
            for key in keys:
                self.emit(key[1], self.pending.pop(key))
            self.target.flush()


class EventStream:
    def __init__(self, writer, original):
        self.writer, self.original = writer, original

    def write(self, text):
        return self.writer.write(text)

    def flush(self):
        self.writer.flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


@contextmanager
def event_output(stdout=None, stderr=None, source_paths=None):
    previous_out, previous_err = sys.stdout, sys.stderr
    stdout = stdout if stdout is not None else previous_out
    stderr = stderr if stderr is not None else previous_err
    writer = EventWriter(stdout, source_paths)
    sys.stdout, sys.stderr = EventStream(writer, stdout), EventStream(writer, stderr)
    try:
        yield
    finally:
        writer.flush(all_threads=True)
        sys.stdout, sys.stderr = previous_out, previous_err

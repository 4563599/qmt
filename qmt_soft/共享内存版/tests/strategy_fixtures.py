import base64
import copy
from datetime import datetime
import fnmatch
import json


class FrozenDatetime(datetime):
    value = datetime(2026, 10, 26, 10, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.value if tz is None else cls.value.replace(tzinfo=tz)


class MarketFixture:
    def __init__(self):
        import pandas as pd
        self.stocks = ['000001.SZ', '600000.SH']
        self.names = {'000001.SZ': '平安银行', '600000.SH': '浦发银行'}
        self.dates = pd.bdate_range(end='2026-10-23', periods=65)
        self.calls = []

    def subscribe_whole_quote(self, markets):
        return None

    def get_stock_list_in_sector(self, sector):
        return list(self.stocks) if sector == '沪深A股' else []

    def get_stock_name(self, stock):
        return self.names[stock]

    def get_instrument_detail(self, stock):
        return {'InstrumentName': self.names[stock], 'PreClose': 11.3,
                'UpStopPrice': 12.43, 'DownStopPrice': 10.17}

    def get_full_tick(self, stocks):
        return {stock: {'lastPrice': 12.204, 'lastClose': 11.3, 'preClose': 11.3,
                        'open': 11.35, 'high': 12.204, 'low': 11.34,
                        'volume': 8000, 'amount': 9000000,
                        'timetag': '20261026 10:00:00', 'time': 1792980000000,
                        'bidPrice': [12.2, 12.19], 'askPrice': [12.21, 12.22]}
                for stock in stocks if stock in self.stocks}

    def get_market_data_ex(self, fields, stocks, **kwargs):
        import pandas as pd
        self.calls.append(('history', kwargs.copy()))
        count = kwargs.get('count', 65)
        closing = count == 1 and kwargs.get('dividend_type') == 'none'
        dates = pd.DatetimeIndex(['2026-10-26']) if closing else self.dates[-count:]
        result = {}
        for stock in stocks:
            rows = []
            for i, date in enumerate(dates):
                base = 10 + (65 - len(dates) + i) * .02
                values = {'open': base, 'high': base + .04, 'low': base - .005,
                          'close': base + .02, 'preClose': base,
                          'volume': 10000 + i * 20, 'amount': 1000000 + i * 2000}
                if closing:
                    values.update(open=11.35, high=12.22, low=11.34, close=12.204)
                rows.append([values[f] for f in fields])
            result[stock] = pd.DataFrame(rows, columns=fields, index=dates)
        return result


class MemoryPipeline:
    def __init__(self, store):
        self.store, self.commands = store, []

    def __getattr__(self, name):
        if not callable(getattr(self.store, name, None)):
            raise AttributeError(name)
        def queue(*args, **kwargs):
            self.commands.append((name, args, kwargs))
            return self
        return queue

    def execute(self):
        commands, self.commands = self.commands, []
        return [getattr(self.store, name)(*args, **kwargs) for name, args, kwargs in commands]


class MemoryRedis:
    def __init__(self):
        self.values, self.ttls = {}, {}

    def ping(self): return True
    def pipeline(self, **kwargs): return MemoryPipeline(self)
    def get(self, key): return self.values.get(key)
    def set(self, key, value, **kwargs): self.values[key] = value; return True
    def hkeys(self, key): return list(self.values.get(key, {}))
    def hgetall(self, key): return dict(self.values.get(key, {}))
    def smembers(self, key): return set(self.values.get(key, set()))

    def hset(self, key, field=None, value=None, mapping=None, **kwargs):
        target = self.values.setdefault(key, {})
        before = len(target)
        if mapping is not None:
            target.update({str(k): str(v) for k, v in mapping.items()})
        if field is not None:
            target[str(field)] = str(value)
        return len(target) - before

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return int(key in self.values)

    def rename(self, old, new):
        self.values[new] = self.values.pop(old)
        if old in self.ttls:
            self.ttls[new] = self.ttls.pop(old)
        return True

    def sadd(self, key, *values):
        target = self.values.setdefault(key, set())
        before = len(target)
        target.update(str(x) for x in values)
        return len(target) - before

    def lpush(self, key, *values):
        target = self.values.setdefault(key, [])
        for value in values: target.insert(0, value)
        return len(target)

    def keys(self, pattern): return [key for key in self.values if fnmatch.fnmatch(key, pattern)]
    def scan_iter(self, match='*', **kwargs): return iter(self.keys(match))
    def close(self): pass

    def execute_command(self, command, *args):
        # Exercise the source's documented full-HSET fallback; no real Redis.
        raise NotImplementedError('Lua not enabled in isolated memory fixture')


def ths_snapshot(store):
    store.set('ths:latest', 'ths:20261026')
    store.set('ths:20261026', json.dumps({
        'stocks': [
            {'code': '000001', 'name': '平安银行', 'board_name': '测试板块',
             'change_percent': 8.0, 'price': 12.204, 'snapshot_time': '2026-10-26 10:00:00'},
            {'code': '600000', 'name': '浦发银行', 'board_name': '测试板块',
             'change_percent': 5.5, 'price': 12.204, 'snapshot_time': '2026-10-26 10:00:00'}],
        'boards': [{'board_name': '测试板块', 'limit_up_count': 3}]}, ensure_ascii=False))


def normalize(value):
    """Compare business data; elapsed-time measurements are inherently different."""
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in sorted(value.items())
                if not str(k).endswith('_seconds') and k not in ('seconds', 'throughput_mb_s')}
    if isinstance(value, (list, tuple)):
        return [normalize(x) for x in value]
    if isinstance(value, set): return sorted(value)
    if isinstance(value, str):
        try: return normalize(json.loads(value))
        except (ValueError, TypeError): pass
        try: return normalize(json.loads(base64.b64decode(value, validate=True)))
        except (ValueError, TypeError, UnicodeError): pass
    return value


def business_snapshot(store):
    return {key: normalize(value) for key, value in sorted(store.values.items())
            if 'performance' not in key and ':tmp:' not in key}

import importlib.util
import importlib
import json
import mmap
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def bridge_module():
    path = ROOT / 'qmt_shm_bridge.py'
    assert path.is_file(), 'shared-memory implementation has not been created'
    return importlib.import_module('qmt_shm_bridge')


class FakeContext:
    def __init__(self):
        # QMT already has pandas loaded when its history API returns a frame.
        import pandas
        self.calls = []

    def get_stock_list_in_sector(self, sector):
        self.calls.append(('sector', sector))
        return ['000001.SZ', '600000.SH']

    def get_stock_name(self, stock):
        self.calls.append(('name', stock))
        return '平安银行' if stock == '000001.SZ' else '浦发银行'

    def get_instrument_detail(self, stock):
        return {'InstrumentName': self.get_stock_name(stock), 'PreClose': 10.0}

    def get_full_tick(self, stocks):
        return {s: {'lastPrice': 10.8, 'lastClose': 10.0,
                    'timetag': '20261026 10:00:00', 'bidPrice': [10.79]} for s in stocks}

    def get_market_data_ex(self, fields, stocks, **kwargs):
        import numpy as np
        import pandas as pd
        self.calls.append(('history', tuple(stocks), kwargs))
        return {s: pd.DataFrame({f: np.arange(1, 41, dtype=float) for f in fields},
                                index=pd.date_range('2026-08-01', periods=40))
                for s in stocks}

    def subscribe_whole_quote(self, markets):
        return None


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.b = bridge_module()
        self.name = 'Local\\QmtShmTest_' + uuid.uuid4().hex

    def test_separate_process_reads_unicode_and_arrays(self):
        with self.b.SharedSlot(self.name, 8192) as slot:
            slot.write({'name': '共享内存测试', 'rows': [[1, 2.5, None]]})
            code = ('import json,sys;sys.path.insert(0,sys.argv[1]);'
                    'from qmt_shm_bridge import SharedSlot;'
                    's=SharedSlot(sys.argv[2],8192);'
                    'print(json.dumps(s.read(),ensure_ascii=True));s.close()')
            result = subprocess.run([sys.executable, '-c', code, str(ROOT), self.name],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'name': '共享内存测试',
                                                         'rows': [[1, 2.5, None]]})

    def test_oversize_preserves_previous_frame(self):
        with self.b.SharedSlot(self.name, 1024) as slot:
            slot.write({'valid': 1})
            with self.assertRaises(self.b.FrameTooLarge):
                slot.write({'oversize': os.urandom(10000).hex()})
            self.assertEqual(slot.read(), {'valid': 1})

    def test_corrupt_and_incomplete_frames_are_ignored(self):
        with self.b.SharedSlot(self.name, 1024) as slot:
            slot.write({'value': 'ok'})
            slot.mapping[self.b.HEADER_SIZE] ^= 1
            self.assertIsNone(slot.read())
            slot.write({'value': 'restored'})
            slot.mapping[8:16] = (101).to_bytes(8, 'little')
            self.assertIsNone(slot.read())


class RpcTests(unittest.TestCase):
    def setUp(self):
        self.b = bridge_module()
        self.channel = 'test_' + uuid.uuid4().hex
        self.context = FakeContext()
        self.server = self.b.BridgeServer(self.context, self.channel)
        self.stop = threading.Event()

        def serve():
            while not self.stop.is_set():
                self.server.serve_once()
                time.sleep(.001)

        self.thread = threading.Thread(target=serve)
        self.thread.start()
        self.client = self.b.RpcClient(self.channel, timeout=2)
        self.client.wait_ready(2)

    def tearDown(self):
        self.stop.set()
        self.thread.join(5)
        self.client.close()
        self.server.close()

    def test_ping_and_history_round_trip(self):
        self.assertEqual(self.client.call('ping')['protocol'], 1)
        encoded = self.client.call('get_market_data_ex',
                                   [['open', 'close'], ['000001.SZ']],
                                   {'period': '1d', 'count': 40, 'dividend_type': 'front'})
        frame = encoded['000001.SZ']
        self.assertEqual(frame['__qmt_frame__'], 1)
        self.assertEqual(frame['columns'], ['open', 'close'])
        self.assertEqual(frame['index'][0], '20260801')
        self.assertEqual(frame['data'][-1], [40.0, 40.0])

    def test_metadata_is_bulk_and_nonfinite_is_json_safe(self):
        data = self.client.call('get_metadata', [['000001.SZ', '600000.SH']])
        self.assertEqual(data['000001.SZ']['name'], '平安银行')
        self.assertEqual(self.b.to_wire(float('nan')), None)
        self.assertEqual(self.b.to_wire(float('inf')), None)

    def test_trading_and_arbitrary_methods_are_rejected(self):
        for method in ['passorder', 'order_stock', '__getattribute__', 'open']:
            with self.assertRaises(self.b.RemoteError):
                self.client.call(method)

    def test_request_validation_limits_scope(self):
        with self.assertRaises(self.b.RemoteError):
            self.client.call('get_market_data_ex',
                             [['close'], ['000001.SZ']], {'period': 'tick', 'count': -1})
        self.assertFalse(any(c[0] == 'history' for c in self.context.calls))

    def test_expired_request_does_not_call_context(self):
        before = len(self.context.calls)
        self.client.request.write({'protocol': 1, 'server_id': self.server.server_id,
                                   'id': uuid.uuid4().hex, 'deadline': time.time() - 1,
                                   'method': 'get_stock_list_in_sector',
                                   'args': ['沪深A股'], 'kwargs': {}})
        time.sleep(.05)
        self.assertEqual(len(self.context.calls), before)

    def test_timeout_never_reuses_old_response(self):
        self.client.call('ping')
        self.stop.set()
        self.thread.join(5)
        self.client.timeout = .1
        with self.assertRaises(self.b.BridgeTimeout):
            self.client.call('get_stock_list_in_sector', ['沪深A股'])

    def test_wait_does_not_repeatedly_decode_previous_response(self):
        self.client.call('ping')
        self.stop.set()
        self.thread.join(5)
        self.client.timeout = .1
        original = self.client.response.read
        decodes = []
        def counted_read():
            decodes.append(1)
            return original()
        self.client.response.read = counted_read
        with self.assertRaises(self.b.BridgeTimeout):
            self.client.call('get_full_tick', [['000001.SZ']])
        self.assertLessEqual(len(decodes), 1,
                             'Waiting must not repeatedly decompress the previous full-market frame')

    def test_server_restart_is_detected(self):
        status = self.client.status.read()
        status['server_id'] = 'replacement'
        self.stop.set()
        self.thread.join(5)
        self.server.status.write(status)
        with self.assertRaises(self.b.BridgeRestarted):
            self.client.call('ping')

    def test_duplicate_server_is_rejected(self):
        with self.assertRaises(self.b.BridgeError):
            self.b.BridgeServer(self.context, self.channel)

    def test_transient_incomplete_status_does_not_stop_strategy(self):
        healthy = self.client.status.read()
        with patch.object(self.client.status, 'read', side_effect=[None, None, healthy]):
            self.assertEqual(self.client.assert_online()['server_id'], self.server.server_id)

    def test_persistently_missing_status_still_stops_strategy(self):
        with patch.object(self.client.status, 'read', return_value=None):
            with self.assertRaises(self.b.BridgeError):
                self.client.assert_online()

    def test_real_stopped_status_is_not_retried_as_healthy(self):
        stopped=self.client.status.read();stopped['state']='stopped'
        with patch.object(self.client.status,'read',return_value=stopped):
            with self.assertRaises(self.b.BridgeError):self.client.assert_online()


if __name__ == '__main__':
    unittest.main()

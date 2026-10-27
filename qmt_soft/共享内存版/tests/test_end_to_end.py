import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import uuid

from test_bridge import ROOT, FakeContext, bridge_module


class ProcessTests(unittest.TestCase):
    def test_original_init_and_scan_with_memory_only_redis(self):
        # This child has fake quotes and a fake Redis object. No production I/O.
        code = '''
import sys,threading,time,types,json
sys.path.insert(0,sys.argv[1])
sys.path.insert(0,sys.argv[1]+'/tests')
from pathlib import Path
from test_bridge import FakeContext
from qmt_shm_bridge import BridgeServer,RpcClient
from external_runner import MarketContext,load_strategy
from unittest.mock import patch
class MemoryRedis:
 def __init__(self): self.commands=[]
 def ping(self): return True
 def pipeline(self,**kwargs): return self
 def hkeys(self,*args): return []
 def smembers(self,*args): return set()
 def execute(self): return []
 def __getattr__(self,name):
  if name not in ('hset','expire','rename','lpush','sadd'): raise AttributeError(name)
  def record(*args,**kwargs):
   self.commands.append(name)
   return self
  return record
channel='scan_'+sys.argv[2]
server=BridgeServer(FakeContext(),channel)
done=threading.Event()
def pump():
 while not done.is_set():
  server.serve_once()
  time.sleep(.001)
thread=threading.Thread(target=pump)
thread.start()
client=RpcClient(channel,timeout=5)
try:
 client.wait_ready(2)
 module=load_strategy(Path(sys.argv[1]).parent/'盘中.py')
 store=MemoryRedis()
 module.redis=types.SimpleNamespace(Redis=lambda **kwargs:store)
 module.DINGTALK_ENABLED=False
 module.LOG_ENABLED=False
 module.CHECK_TRADING_TIME=False
 context=MarketContext(client)
 context.bind(module)
 # Assert even an unexpected path cannot connect to a real service during this test.
 with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')) as net:
  with patch('requests.post',side_effect=AssertionError('DingTalk forbidden')) as ding:
   module.init(context)
   assert module.g_initialized, 'original scanner did not initialize'
   assert module.g_scan_count>=1, 'original scanner did not scan'
   assert len(module.g_history_data)==2, 'history was not populated'
   assert len(context.timers)==2, 'original timers were not registered'
   assert store.commands, 'scanner output was not sent to fake Redis'
   assert not net.called and not ding.called, 'unexpected real I/O attempt'
 print(json.dumps({'initialized':True,'scans':module.g_scan_count,
                   'stocks':len(module.g_stock_pool),'fake_redis_commands':len(store.commands)}))
finally:
 done.set()
 thread.join(10)
 client.close()
 server.close()
'''
        result = subprocess.run([sys.executable, '-X', 'utf8', '-u', '-c', code,
                                 str(ROOT), uuid.uuid4().hex], capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])['initialized'])

    def test_actual_cli_check_with_fake_qmt_process(self):
        channel = 'e2e_' + uuid.uuid4().hex
        code = '''
import sys,time
sys.path.insert(0,sys.argv[1])
sys.path.insert(0,sys.argv[1]+'/tests')
from qmt_shm_bridge import BridgeServer
from test_bridge import FakeContext
server=BridgeServer(FakeContext(),sys.argv[2])
print('READY',flush=True)
try:
 while True:
  server.serve_once()
  time.sleep(.001)
finally:
 server.close()
'''
        fake = subprocess.Popen([sys.executable, '-X', 'utf8', '-u', '-c', code, str(ROOT), channel],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8')
        try:
            self.assertEqual(fake.stdout.readline().strip(), 'READY')
            result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'external_runner.py'),
                                     '--check', '--wait', '5', '--channel', channel,
                                     '--source', str(ROOT / 'does-not-exist.py')],
                                    capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('[CHECK OK]', result.stdout)
            self.assertIn('Scanner/Redis/DingTalk were not started', result.stdout)
        finally:
            fake.terminate()
            fake.communicate(timeout=10)

    def test_cli_without_qmt_fails_cleanly_without_source_import(self):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'external_runner.py'),
                                 '--check', '--wait', '.1', '--channel', 'absent_' + uuid.uuid4().hex,
                                 '--source', str(ROOT / 'does-not-exist.py')],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('No live QMT shared-memory bridge', result.stdout)
        self.assertNotIn('Traceback', result.stderr)

    def test_qmt_entrypoint_registers_and_serves_market_data(self):
        b = bridge_module()
        original_channel = b.CHANNEL
        b.CHANNEL = 'entry_' + uuid.uuid4().hex
        context = FakeContext()
        registered = []
        context.run_time = lambda *args: registered.append(args)
        client = None
        try:
            b.init(context)
            self.assertEqual(registered[0][0:2], ('qmt_shm_poll', '1nSecond'))
            client = b.RpcClient(b.CHANNEL, timeout=2)
            client.wait_ready(.5)
            result = []
            thread = threading.Thread(target=lambda: result.append(client.call('ping')))
            thread.start()
            while thread.is_alive():
                b.qmt_shm_poll(context)
                time.sleep(.002)
            thread.join(5)
            self.assertEqual(result[0]['protocol'], 1)
        finally:
            b.stop(context)
            if client:
                client.close()
            b.CHANNEL = original_channel

    def test_large_compressed_frame_round_trip(self):
        b = bridge_module()
        # 6,000 instruments, prices/depth arrays; enough to exercise full-market payloads.
        payload = {'%06d.SZ' % i: {'lastPrice': i / 100.0 + 1,
                                    'bidPrice': [i / 100.0 + x for x in range(5)],
                                    'name': '全市场测试股票%s' % i} for i in range(6000)}
        with b.SharedSlot('Local\\LargeShmTest_' + uuid.uuid4().hex, 4 * 1024 * 1024) as slot:
            slot.write(payload)
            self.assertEqual(slot.read(), payload)

    def test_approved_scanner_revision_keeps_ascii_gbk_compatibility(self):
        source = (ROOT.parent / '盘中.py').read_bytes()
        self.assertTrue(source.isascii())
        digest = hashlib.sha256(source).hexdigest()
        # Approved eighth PZ1 entry route and candidate update, 2026-10-27.
        self.assertEqual(digest, '512f6728b0eaa7da204d57aa81082d332744ca0c1d2cdf50f4a35f0760db3c63')


if __name__ == '__main__':
    unittest.main()

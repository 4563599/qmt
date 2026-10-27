import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_github
import sync_desktop


class ExportTests(unittest.TestCase):
    def test_multiline_credentials_removed_but_strategy_kept(self):
        data = (b'import os\nREDIS_PASSWORD = "private-password"\n'
                b'DINGTALK_WEBHOOK = (\n "https://example.invalid/"\n "private-token"\n)\n'
                b'DINGTALK_CONDITION3_WEBHOOK = "second-token"\n'
                b'def rule(close, opened):\n    return close >= opened\n')
        clean = sync_desktop.sanitize(data, sync_desktop.SCANNERS[0])
        self.assertNotIn(b'private-password', clean)
        self.assertNotIn(b'private-token', clean)
        self.assertNotIn(b'second-token', clean)
        before, after = ast.parse(data).body[-1], ast.parse(clean).body[-1]
        self.assertEqual(ast.dump(before), ast.dump(after))

    def test_gbk_source_encoding_is_preserved(self):
        data = '# coding: gbk\nimport os\nREDIS_PASSWORD="private"\nname="测试"\n'.encode('gbk')
        clean = sync_desktop.sanitize(data, sync_desktop.SCANNERS[1])
        self.assertIn('name="测试"', clean.decode('gbk'))
        compile(clean, 'ths.py', 'exec')

    def test_missing_expected_config_stops_export(self):
        with self.assertRaises(RuntimeError):
            sync_desktop.sanitize(b'import os\n', sync_desktop.SCANNERS[0])


class PublishTests(unittest.TestCase):
    def test_literal_credential_is_blocked(self):
        with self.assertRaises(RuntimeError):
            publish_github.validate_blob('qmt_soft/strategy.py', b'REDIS_PASSWORD="secret"\n')

    def test_environment_config_is_allowed(self):
        publish_github.validate_blob('qmt_soft/strategy.py', b'import os\nREDIS_PASSWORD=os.environ.get("REDIS_PASSWORD", "")\n')

    def test_non_qmt_soft_change_is_blocked(self):
        with patch.object(publish_github, 'git', return_value=b'web/app.py\0'):
            with self.assertRaises(RuntimeError):
                publish_github.changes('base', 'head')

    def test_runtime_and_private_files_are_blocked(self):
        for name in ('qmt_soft/logs/a.log', 'qmt_soft/.env', 'qmt_soft/.env.production',
                     'qmt_soft/settings.local.json', 'qmt_soft/runtime/calendar.json'):
            with self.subTest(name=name), patch.object(publish_github, 'git', return_value=name.encode() + b'\0'):
                with self.assertRaises(RuntimeError):
                    publish_github.changes('base', 'head')


if __name__ == '__main__':
    unittest.main()

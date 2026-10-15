import base64
import json
import os
import re
import shutil
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def client():
    import app as flask_app

    flask_app.app.config['TESTING'] = True
    with flask_app.app.test_client() as test_client:
        yield test_client


def redis_with(hashes):
    redis_client = MagicMock()
    redis_client.exists.side_effect = lambda key: key in hashes
    redis_client.type.side_effect = lambda key: b'hash' if key in hashes else b'none'
    redis_client.hscan.side_effect = lambda key, *args, **kwargs: (0, {
        code.encode(): base64.b64encode(json.dumps(record).encode('utf-8'))
        for code, record in hashes.get(key, {}).items()
    })
    redis_client.keys.side_effect = lambda pattern: [
        key.encode() for key in hashes if key.startswith(pattern.rstrip('*'))
    ]
    return redis_client


def test_second_page_adds_pz1_groups_without_changing_pz_groups(client):
    redis_client = redis_with({
        'pz:20260930': {
            '000001': {'name': 'old', 'yin_count_10d': 2, 'ref_gain': 8,
                       'condition_3_met': 1, 'yi_yin_yi_yang': 1},
        },
        'pz1:20260930': {
            '_scan_info': {'yi_yin_yi_yang': 1},
            '000002': {'name': 'low gain', 'yin_count_10d': 1, 'ref_gain': -2,
                       'yi_yin_yi_yang': 1, 'qiangchi': True,
                       'five_pull_all_yin_up': 1, 'ref_is_limit_up': False,
                       'custom_field': 'preserve', 'updated_at': '2026-09-30 15:02:00'},
            '000003': {'yin_count_10d': 0, 'today_change': 2.3, 'ref_gain': 1,
                       'yi_yin_yi_yang': '1', 'qiangchi': 1, 'is_quanyang_10d': 1,
                       'is_limit_up': 1},
            '000004': {'yin_count_10d': 1, 'ref_gain': 3, 'yi_yin_yi_yang': 1},
            '000005': {'yin_count_10d': 1, 'ref_gain': 4, 'yi_yin_yi_yang': 0,
                       'qiangchi': 0},
        },
        'pz1:20260929': {'000006': {'yi_yin_yi_yang': 1, 'qiangchi': 1}},
    })
    with patch('app.get_redis_client', return_value=redis_client):
        response = client.get('/api/pz1/focus?date=2026-09-30&page=2')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['redis_key'] == 'pz:20260930'
    assert payload['extra_redis_key'] == 'pz1:20260930'
    assert payload['count'] == 4
    assert payload['updated_at'] == '2026-09-30 15:02:00'
    assert [item['code'] for item in payload['groups']['condition_3_met']] == ['000001']
    assert [item['code'] for item in payload['groups']['yi_yin_yi_yang']] == [
        '000003', '000004', '000002',
    ]
    assert [item['code'] for item in payload['groups']['qiangchi']] == ['000003', '000002']
    selected = payload['groups']['qiangchi'][1]
    assert selected['ref_gain'] == -2
    assert selected['custom_field'] == 'preserve'
    assert selected['five_pull_all_yin_up'] == 1
    assert selected['ref_is_limit_up'] is False
    assert payload['groups']['qiangchi'][0]['ref_gain'] == 2.3
    assert payload['groups']['qiangchi'][0]['ref_is_limit_up'] is True


@pytest.mark.parametrize('hashes,expected_codes', [
    ({'pz:20260930': {'000001': {'condition_3_met': 1}}}, ['000001']),
    ({'pz1:20260930': {'000002': {'yi_yin_yi_yang': 1}}}, ['000002']),
])
def test_second_page_works_with_either_source_missing(client, hashes, expected_codes):
    with patch('app.get_redis_client', return_value=redis_with(hashes)):
        response = client.get('/api/pz1/focus?date=2026-09-30&page=2')
    assert response.status_code == 200
    groups = response.get_json()['groups']
    assert sorted({item['code'] for items in groups.values() for item in items}) == expected_codes


def test_second_page_does_not_fall_back_to_another_date(client):
    redis_client = redis_with({'pz1:20260929': {'000002': {'yi_yin_yi_yang': 1}}})
    with patch('app.get_redis_client', return_value=redis_client):
        response = client.get('/api/pz1/focus?date=2026-09-30&page=2')
    assert response.status_code == 404


def test_second_page_dates_include_both_sources_newest_first(client):
    redis_client = redis_with({
        'pz:20260929': {}, 'pz1:20260930': {}, 'pz1:20260929': {},
        'pz1:kline:20260930': {},
    })
    with patch('app.get_redis_client', return_value=redis_client):
        response = client.get('/api/pz1/focus/dates?page=2')
        latest = client.get('/api/pz1/focus?page=2')
    assert response.get_json()['dates'] == ['2026-09-30', '2026-09-29']
    assert latest.status_code == 200
    assert latest.get_json()['date'] == '2026-09-30'


def test_second_page_receives_same_daily_push_log_newest_first(client):
    redis_client = redis_with({
        'pz1:20261009': {'000001': {'qiangchi': 1}},
        'dingtalk:pz:records:20261009': {
            'first': {'code': '000001', 'name': 'Earlier', 'ref_gain': 5.5,
                      'dingtalk_sent_at': '2026-10-09 09:30:00',
                      'five_pull_all_yin_up': 1},
            'second': {'code': '000002', 'name': 'Latest', 'ref_gain': 6.5,
                       'dingtalk_sent_at': '2026-10-09 10:30:00',
                       'five_pull_all_yin_eaten_next': 1},
            'pending': {'code': '000003', 'name': 'Not sent'},
        },
        'dingtalk:pz:records:20261008': {
            'old': {'code': '000004', 'dingtalk_sent_at': '2026-10-08 15:00:00'},
        },
    })
    with patch('app.get_redis_client', return_value=redis_client):
        second = client.get('/api/pz1/focus?date=2026-10-09&page=2').get_json()
        third = client.get('/api/pz1/focus?date=2026-10-09&page=3').get_json()
    assert second['push_log_redis_key'] == 'dingtalk:pz:records:20261009'
    logs = second['groups']['push_log']
    assert [item['code'] for item in logs] == ['000002', '000001']
    assert logs == third['groups']['push_log']
    assert logs[0]['push_reasons'] == ['5拉阴被吃']
    assert logs[1]['push_reasons'] == ['5拉阴线都向上']
    redis_client.hset.assert_not_called()
    redis_client.set.assert_not_called()


def test_page_has_extra_lists_and_independent_limit_buttons(client):
    html = client.get('/重点关注').get_data(as_text=True)
    assert html.index('id="focusPage2Extra"') < html.index('id="focusSheetWrap"')
    assert '5拉一阴一阳' in html and '5拉强吃' in html
    assert 'id="focusNotLimitUpOnly"' in html
    assert re.search(r'<button[^>]+id="focusLimitUpOnly"[^>]*>涨停</button>', html)
    assert re.search(r'<button[^>]+id="focusNotLimitUpOnly"[^>]*>未涨停</button>', html)
    assert "{ field: 'is_quanyang_10d', label: '没有阴线' }" in html
    assert 'data-focus-extra-group="push_log"' in html
    assert 'state.focusPage === 2 ? rawPushLog.filter(filterStock) : []' in html
    assert 'id="focusPushLogPanel"' not in html
    assert 'is-third-page' not in html
    assert 'bindStockInteractions(focusPage2Extra)' in html
    if shutil.which('node'):
        script = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)[-1]
        result = subprocess.run(['node', '--check', '-'], input=script,
                                text=True, capture_output=True)
        assert result.returncode == 0, result.stderr


def test_focus_typography_is_slightly_smaller_without_scaling_the_layout(client):
    html = client.get('/重点关注').get_data(as_text=True)
    style = re.search(r'<style>(.*?)</style>', html, re.S).group(1)
    assert 'body.focus-mode { --focus-text-scale: .94; }' in style
    assert 'font-size: calc(13px * var(--focus-text-scale, 1));' in style
    assert 'font-size: calc(14px * var(--focus-text-scale, 1));' in style
    assert not re.search(r'font-size:\s*\d+px;', style)
    assert 'grid-template-columns: repeat(6, minmax(0, 1fr));' in style
    assert 'grid-column: span 2;' in style
    assert 'zoom:' not in style and 'transform: scale' not in style
    push_log_html = client.get('/推送日志').get_data(as_text=True)
    assert '<body class="focus-mode">' in html
    assert '<body class="focus-mode">' not in push_log_html

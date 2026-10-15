import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def fallback_app(monkeypatch):
    import app as flask_app

    monkeypatch.setattr(
        flask_app, '_REVIEW_BUILDER_MODULE',
        flask_app._make_redis_review_fallback_builder(),
    )
    monkeypatch.setitem(flask_app.app.config, 'TESTING', True)
    return flask_app


def test_fallback_widths_match_review_rules(fallback_app):
    values = [
        ['股票' * 20, [{'type': 'text', 'text': '名称' * 15}]],
        ['x' * 200, ''],
    ]
    widths = fallback_app._focus_calculate_column_widths(
        SimpleNamespace(), values, 3,
    )
    assert widths == [1000, 528, 184]


def test_review_without_optional_builder_functions(fallback_app, monkeypatch):
    redis_client = MagicMock()
    redis_client.get.return_value = json.dumps({
        'boards': [{'board_name': '测试板块', 'change_percent': 6.5,
                    'limit_up_count': 5}],
    })
    redis_client.hgetall.side_effect = lambda key: {
        '000001': json.dumps({'code': '000001', 'name': '测试股票',
                              'yin_count_20': 3, 'ref_gain': 5.5}),
    } if key == 'pz1:20261009' else {}
    monkeypatch.setattr(fallback_app, 'get_redis_client', lambda: redis_client)

    response = fallback_app.app.test_client().get(
        '/api/pz1/focus/redis?date=2026-10-09&sheet=review',
    )
    result = response.get_json()
    assert response.status_code == 200
    assert result['success'] is True
    assert result['source'] == 'redis'
    assert len(result['column_widths_px']) == result['columns'] == 8
    assert len(result['row_heights_px']) == result['rows']
    assert '测试板块' in result['values'][2]
    assert any('测试股票' in str(row) for row in result['values'])


@pytest.mark.parametrize('cached_dimensions', [False, True])
def test_cls_without_optional_builder_functions(
    fallback_app, monkeypatch, cached_dimensions,
):
    content = {
        'values': [['股票名称', '代码'], ['测试股票', '000001']],
        'style_groups': [{'ranges': ['A1:B1'], 'style': {
            'font': {'bold': True}, 'foreColor': '#9333EA',
        }}],
    }
    if cached_dimensions:
        content.update(column_widths_px=[240, 120], row_heights_px=[36, 32])
    redis_client = MagicMock()
    redis_client.get.return_value = json.dumps(content)
    redis_client.hgetall.return_value = {}
    monkeypatch.setattr(fallback_app, 'get_redis_client', lambda: redis_client)

    response = fallback_app.app.test_client().get(
        '/api/pz1/focus/redis?date=2026-10-09&sheet=cls',
    )
    result = response.get_json()
    assert response.status_code == 200
    assert result['success'] is True
    assert result['values'] == content['values']
    assert result['style_cells'][0][0]['color'] == '#9333EA'
    assert result['column_widths_px'] == ([240, 120] if cached_dimensions else [184, 184])
    assert result['row_heights_px'] == ([36, 32] if cached_dimensions else [32, 32])


def test_full_builder_preserves_dimensions_and_styles(monkeypatch):
    import app as flask_app

    builder = SimpleNamespace(
        last_nonempty_row=lambda values: len(values),
        calculate_column_widths=lambda values, columns: [240] * columns,
        style_payload=lambda sheet, rows, layout: [{
            'ranges': ['!A1:A1'],
            'style': {'backColor': '#F5F0F8', 'font': {'bold': True}},
        }],
    )
    monkeypatch.setattr(flask_app, '_REVIEW_BUILDER_MODULE', builder)
    result = flask_app._focus_grid_payload(
        [['标题', '']], '复盘', date(2026, 10, 9), layout={},
    )
    assert result['column_widths_px'] == [240, 240]
    assert result['style_cells'][0][0]['background'] == '#F5F0F8'
    assert result['style_cells'][0][1]['background'] == '#FFFFFF'


@pytest.fixture
def full_review_payload(monkeypatch):
    import app as flask_app
    import feishu_daily_review as builder

    monkeypatch.setattr(flask_app, '_REVIEW_BUILDER_MODULE', builder)
    monkeypatch.setattr(builder, 'load_daily_qy2', lambda run_date: ('', [], 'test'))
    redis_client = MagicMock()
    redis_client.get.side_effect = lambda key: json.dumps({'boards': []}) if key.startswith('ths:') else None
    redis_client.hgetall.return_value = {}
    monkeypatch.setattr(flask_app, 'get_redis_client', lambda: redis_client)
    response = flask_app.app.test_client().get(
        '/api/pz1/focus/redis?date=2026-10-12&sheet=review',
    )
    payload = response.get_json()
    assert response.status_code == 200, payload
    assert payload['success'] is True
    redis_client.hset.assert_not_called()
    redis_client.set.assert_not_called()
    return payload


def test_review_notes_match_reference_columns_and_warning_colors(full_review_payload):
    payload = full_review_payload
    priorities = {
        row[0].split('.', 1)[0]: index
        for index, row in enumerate(payload['values'])
        if row[0].startswith('优先级')
    }
    assert set(priorities) == {'优先级1', '优先级2', '优先级3', '优先级4', '优先级5'}
    first = payload['values'][priorities['优先级1']]
    assert '厂子' not in first[0]
    assert first[1:3] == ['厂子或者一字（有没有）', '1.一阴一阳']
    assert payload['values'][priorities['优先级4']][1] == '厂子或者一字（有没有）'
    assert payload['values'][priorities['优先级5']][1] == '中科海讯，润和软件'
    for column, warning in ((3, '涨幅不要太大'), (4, '中体产业吃亏')):
        segments = payload['rich_cells'][priorities['优先级2']][column]
        assert any(warning in segment['text'] and segment.get('color', '').upper() == '#F54A45'
                   for segment in segments)
        assert any(segment.get('color', '').upper() == '#9333EA' for segment in segments)


def test_review_priority_styles_keep_stock_examples_black(full_review_payload):
    payload = full_review_payload
    assert any(row[0].startswith('优先级5.') for row in payload['values'])
    for index, row in enumerate(payload['values']):
        if not row[0].startswith(('优先级', '低位优先级')):
            continue
        heading = payload['style_cells'][index][0]
        assert heading['color'] == '#9333EA'
        assert heading['background'] == '#FFF152'
        assert heading['bold'] is True
        assert heading['fontSize'] == pytest.approx(11 * 4 / 3)
        if row[0].startswith('优先级5.'):
            examples = payload['style_cells'][index][1]
            assert examples['color'] == '#000000'
            assert examples['bold'] is False
            assert examples['fontSize'] == pytest.approx(10 * 4 / 3)


def test_dates_include_stock_snapshots_without_ths(fallback_app, monkeypatch):
    redis_client = MagicMock()
    snapshots = {
        'ths:*': [b'ths:20261009', b'ths:latest'],
        'pz:*': [b'pz:kline:history:20261013'],
        'pz1:*': [b'pz1:20261012'],
        'qy2:*': [b'qy2:20261012', b'qy2:20269999'],
        'cls:limit_up:*': [b'cls:limit_up:20261008'],
            'dingtalk:pz:records:*': [],
    }
    redis_client.scan_iter.side_effect = lambda **kw: snapshots[kw['match']]
    monkeypatch.setattr(fallback_app, 'get_redis_client', lambda: redis_client)

    response = fallback_app.app.test_client().get('/api/pz1/focus/redis/dates')
    assert response.get_json() == {
        'success': True,
        'dates': ['2026-10-12', '2026-10-09', '2026-10-08'],
    }
    redis_client.hset.assert_not_called()
    redis_client.set.assert_not_called()


def test_cls_reads_each_date_and_saves_only_web_overlay(fallback_app, monkeypatch):
    redis_client = MagicMock()
    raw = {
        'cls:limit_up:20261009': json.dumps({'values': [['October 9']]}),
        'cls:limit_up:20261012': json.dumps({'values': [['October 12']]}),
    }
    edits = {}
    redis_client.get.side_effect = raw.get
    redis_client.hgetall.side_effect = lambda key: edits.get(key, {})
    redis_client.hset.side_effect = lambda key, cell, value: edits.setdefault(key, {}).update({cell: value})
    monkeypatch.setattr(fallback_app, 'get_redis_client', lambda: redis_client)
    client = fallback_app.app.test_client()
    assert client.get('/api/pz1/focus/redis?date=2026-10-09&sheet=cls').get_json()['values'] == [['October 9']]
    response = client.put('/api/pz1/focus/redis', json={
        'date': '2026-10-09', 'sheet': 'cls',
        'changes': [{'row': 1, 'column': 1, 'value': 'My note'}],
    })
    assert response.status_code == 200
    assert response.get_json()['raw_data_unchanged'] is True
    redis_client.hset.assert_called_once_with('focus:web:edits:cls:20261009', '1:1', 'My note')
    assert client.get('/api/pz1/focus/redis?date=2026-10-09&sheet=cls').get_json()['values'] == [['My note']]
    assert client.get('/api/pz1/focus/redis?date=2026-10-12&sheet=cls').get_json()['values'] == [['October 12']]
    raw['cls:limit_up:20261012'] = json.dumps({'values': [['Latest October 12']]})
    assert client.get('/api/pz1/focus/redis?date=2026-10-12&sheet=cls').get_json()['values'] == [['Latest October 12']]
    redis_client.set.assert_not_called()


def test_workbook_refresh_follows_latest_and_preserves_edits():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for frontend refresh tests')
    html = (Path(__file__).parents[1] / 'templates' / 'pz_push_log.html').read_text(encoding='utf-8')
    load_dates = html.split('        async function loadDates(', 1)[1].split(
        '        async function loadRecords(', 1)[0]
    refresh = html.split('        async function refreshRedisWorkbookPages()', 1)[1].split(
        "        dateSelect.addEventListener('change'", 1)[0]
    script = '''
const assert = require('node:assert/strict');
const FOCUS_MODE = true, API_BASE = '', PAGE_LABEL = 'test';
const state = {focusPage: 7, datesRequestId: 0, date: '2026-10-09',
    loading: false, workbookDatePinned: false};
const dateSelect = {};
const escapeHtml = value => value;
let dirty = false, calls = 0;
const focusExcelHasUnsavedChanges = () => dirty;
const fetch = async () => ({ok: true, json: async () => ({success: true,
    dates: ['2026-10-12', '2026-10-09']})});
const loadRecords = async force => { assert.equal(force, true); calls++; };
''' + 'async function loadDates(' + load_dates + 'async function refreshRedisWorkbookPages()' + refresh + '''
(async () => {
    await refreshRedisWorkbookPages();
    assert.equal(state.date, '2026-10-12');
    assert.equal(calls, 1);
    state.date = '2026-10-09'; state.workbookDatePinned = true;
    state.focusPage = 8;
    await refreshRedisWorkbookPages();
    assert.equal(state.date, '2026-10-09');
    assert.equal(calls, 2);
    dirty = true; state.workbookDatePinned = false;
    await refreshRedisWorkbookPages();
    assert.equal(state.date, '2026-10-09');
    assert.equal(calls, 2);
    dirty = false; state.loading = true;
    await refreshRedisWorkbookPages();
    assert.equal(calls, 2);
    state.loading = false; state.focusPage = 6;
    await refreshRedisWorkbookPages();
    assert.equal(calls, 2);
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr

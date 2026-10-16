import base64
import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
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
    client = MagicMock()
    client.exists.side_effect = lambda key: key in hashes
    client.type.side_effect = lambda key: b'hash' if key in hashes else b'none'
    client.hscan.side_effect = lambda key, *args, **kwargs: (0, {
        code.encode(): base64.b64encode(json.dumps(record).encode('utf-8'))
        for code, record in hashes.get(key, {}).items()
    })
    return client


def test_third_page_filters_each_independent_flag_and_sorts_by_gain(client):
    hashes = {
        'pz1:20261016': {
            '_scan_info': {'yi_yin_yi_yang_10d': 1, 'yi_yin_yi_yang_20d': 1},
            '600001': {'ref_gain': 3, 'yi_yin_yi_yang_10d': 1,
                       'yi_yin_yi_yang_20d': 1, 'yin_count_20': 9, 'is_ref_yang': 0},
            '600002': {'ref_gain': 99, 'today_change': 2, 'yi_yin_yi_yang_10d': '1',
                       'yi_yin_yi_yang_20d': 'true'},
            '600003': {'ref_gain': 8, 'yi_yin_yi_yang_10d': True,
                       'yi_yin_yi_yang_20d': True},
            '600004': {'ref_gain': 9, 'yi_yin_yi_yang_10d': 1,
                       'yi_yin_yi_yang_20d': 0, 'condition_3_met': 1},
            '600005': {'ref_gain': 9, 'yi_yin_yi_yang_10d': 1,
                       'five_pull_yin_pull_next_or_eaten': 1},
            '600006': {'ref_gain': 9, 'yi_yin_yi_yang': 1, 'qiangchi': 1,
                       'is_quanyang_10d': 1},
            '600007': {'ref_gain': 9, 'yi_yin_yi_yang_10d': 'false',
                       'yi_yin_yi_yang_20d': 1},
            '600008': {'ref_gain': -2, 'yi_yin_yi_yang_10d': 1,
                       'yi_yin_yi_yang_20d': 1, 'high_gain_10d_count': 0},
            '600009': {'ref_gain': 3, 'yi_yin_yi_yang_10d': 1,
                       'yi_yin_yi_yang_20d': 1},
        },
        'pz:20261016': {'600010': {'ref_gain': 99, 'yi_yin_yi_yang_10d': 1,
                                  'yi_yin_yi_yang_20d': 1}},
        'qy1:20261016': {'600020': {'ref_gain': 6}},
        'pz1:20261015': {'600011': {'yi_yin_yi_yang_10d': 1,
                                   'yi_yin_yi_yang_20d': 1}},
    }
    before = copy.deepcopy(hashes)
    redis_client = redis_with(hashes)
    with patch('app.get_redis_client', return_value=redis_client):
        response = client.get('/api/pz1/focus?date=2026-10-16&page=3')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['redis_key'] == 'pz1:20261016'
    groups = payload['groups']
    stocks = groups['yi_yin_yi_yang_10d']
    assert [stock['code'] for stock in stocks] == [
        '600004', '600005', '600003', '600001', '600009', '600002', '600008',
    ]
    assert [stock['ref_gain'] for stock in stocks] == [9, 9, 8, 3, 3, 2, -2]
    assert stocks[3]['yin_count_20'] == 9
    assert stocks[-1]['high_gain_10d_count'] == 0
    assert [stock['code'] for stock in groups['yi_yin_yi_yang_20d']] == [
        '600007', '600003', '600001', '600009', '600002', '600008',
    ]
    assert 'independent_yin_yang_10d_20d' not in groups
    assert [stock['code'] for stock in groups['condition_3_met']] == ['600004']
    assert [stock['code'] for stock in groups['five_pull_yin_pull_next_or_eaten']] == ['600005']
    assert [stock['code'] for stock in groups['qy1_all']] == ['600020']
    assert groups['high_gain_prev3_short_shadow'] == []
    assert payload['count'] == 9
    assert hashes == before
    redis_client.hset.assert_not_called()
    redis_client.set.assert_not_called()
    redis_client.delete.assert_not_called()


def test_third_page_each_flag_is_independent_and_legacy_flags_do_not_infer_a_match(client):
    redis_client = redis_with({'pz1:20261016': {
        '600001': {'yi_yin_yi_yang_10d': 1},
        '600002': {'yi_yin_yi_yang_20d': 1},
        '600003': {'yi_yin_yi_yang': 1, 'qiangchi': 1, 'is_quanyang_10d': 1},
    }})
    with patch('app.get_redis_client', return_value=redis_client):
        payload = client.get('/api/pz1/focus?date=2026-10-16&page=3').get_json()
    assert payload['success'] is True
    assert [stock['code'] for stock in payload['groups']['yi_yin_yi_yang_10d']] == ['600001']
    assert [stock['code'] for stock in payload['groups']['yi_yin_yi_yang_20d']] == ['600002']


def test_third_page_does_not_fall_back_to_another_date(client):
    redis_client = redis_with({'pz1:20261015': {
        '600001': {'yi_yin_yi_yang_10d': 1, 'yi_yin_yi_yang_20d': 1},
    }})
    with patch('app.get_redis_client', return_value=redis_client):
        response = client.get('/api/pz1/focus?date=2026-10-16&page=3')
    assert response.status_code == 404
    assert response.get_json()['groups'] == {}


def test_new_list_precedes_existing_lists_and_reuses_first_page_rows():
    source = (Path(__file__).resolve().parents[1] / 'templates' / 'pz_push_log.html').read_text(encoding='utf-8')
    assert source.index('id="focusPage3Independent"') < source.index('id="focusSheetWrap"')
    assert 'function focusListStockRow(stock, valueText, belowClass' in source
    assert 'return focusListStockRow(stock, valueText, belowClass);' in source
    assert 'function renderFocusPage3Independent(groups, filterStock)' in source
    assert 'data-independent-field' in source
    assert "page3IndependentField: 'yi_yin_yi_yang_10d'" in source
    assert 'state.page3IndependentField = field' in source
    assert 'focusPage3Independent.hidden = !(FOCUS_MODE && state.focusPage === 3)' in source
    assert 'bindStockInteractions(focusPage3IndependentRows)' in source
    assert "focusPage3IndependentRows?.contains(anchor) ? 'page3-independent'" in source
    assert "openRegion === 'page3-independent'" in source


def test_third_page_tab_label_and_mobile_kline_positioning():
    from html.parser import HTMLParser

    class TabParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active = False
            self.label = ''
            self.attrs = {}

        def handle_starttag(self, tag, attrs):
            if tag == 'button' and dict(attrs).get('id') == 'focusPage3':
                self.active = True
                self.attrs = dict(attrs)

        def handle_data(self, data):
            if self.active:
                self.label += data

        def handle_endtag(self, tag):
            if tag == 'button':
                self.active = False

    source = (Path(__file__).resolve().parents[1] / 'templates' / 'pz_push_log.html').read_text(encoding='utf-8')
    parser = TabParser()
    parser.feed(source)
    assert parser.label == '\u0031\u9634\u0031\u9633'
    assert parser.attrs['data-focus-page'] == '3'
    mobile_positioning = source.split('function positionMobileFocusKline(', 1)[1].split('function positionKline()', 1)[0]
    assert 'focusPage3IndependentRows?.contains(state.openKlineAnchor)' in mobile_positioning
    assert "closest('.focus-page1-stock-row')" in mobile_positioning


def test_independent_lists_have_renamed_and_grouped_modes(client):
    html = client.get('/重点关注?page=3').get_data(as_text=True)
    assert '10日独立一阴一阳' not in html
    assert '20日独立一阴一阳' not in html
    for label in ('10日1阴1阳', '20日1阴1阳',
                  '10日1阴1阳阴线次数', '20日1阴1阳阴线次数'):
        assert label in html
    assert 'id="focusPage3IndependentValueHeading"' in html


def test_independent_grouped_lists_use_their_own_counts_and_gain_order(client):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is unavailable')
    html = client.get('/重点关注?page=3').get_data(as_text=True)
    script = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)[-1]
    config = script[script.index('const FOCUS_PAGE3_INDEPENDENT_COLUMNS ='):script.index('const FOCUS_SHAPE_TAGS =')]
    helpers = script[script.index('function focusStockGain('):script.index('const PAGE1_METRICS =')]
    renderer = script[script.index('function focusListStockRow('):script.index('const BOARD_REASON_LABELS =')]
    harness = r"""
const assert = require('node:assert/strict');
const state = {page3IndependentField: 'yi_yin_yi_yang_10d'};
const focusPage3IndependentRows = {innerHTML: ''};
const focusPage3IndependentGroups = {innerHTML: ''};
const focusPage3IndependentCount = {textContent: ''};
const focusPage3IndependentHeading = {textContent: ''};
const focusPage3IndependentValueHeading = {textContent: ''};
const escapeHtml = value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const stocks = [
    {code: '000001', name: 'A', yin_count_10d: 2, yin_count_20: 5, ref_gain: 9},
    {code: '000002', name: 'B', yin_count_10d: 0, yin_count_20: 7, ref_gain: 3},
    {code: '000003', name: 'C', yin_count_10d: 2, yin_count_20: 5, ref_gain: 99, today_change: 5},
    {code: '000004', name: 'D', yin_count_10d: 1, yin_count_20: 2, ref_gain: -2},
    {code: '000005', name: 'E', yin_count_10d: 2, yin_count_20: 5, ref_gain: 5},
    {code: '000006', name: 'F', yin_count_10d: null, yin_count_20: null, ref_gain: 20},
];
const groups = {
    yi_yin_yi_yang_10d: stocks,
    yi_yin_yi_yang_20d: stocks.filter(stock => stock.code !== '000001'),
};
const original = JSON.stringify(groups);
"""
    checks = r"""
const keep = () => true;
const codes = rows => rows.map(row => row.code);
assert.deepEqual(FOCUS_PAGE3_INDEPENDENT_COLUMNS.map(column => column.title), [
    '10日1阴1阳', '20日1阴1阳', '10日1阴1阳阴线次数', '20日1阴1阳阴线次数',
]);
assert.deepEqual(codes(renderFocusPage3Independent(groups, keep)), [
    '000006', '000001', '000003', '000005', '000002', '000004',
]);
assert.equal(focusPage3IndependentValueHeading.textContent, '20天阴线破5次数');
state.page3IndependentField = 'yi_yin_yi_yang_10d_by_yin_count';
assert.deepEqual(codes(renderFocusPage3Independent(groups, keep)), [
    '000002', '000004', '000001', '000003', '000005', '000006',
]);
assert.equal(focusPage3IndependentHeading.textContent, '10日1阴1阳阴线次数');
assert.equal(focusPage3IndependentValueHeading.textContent, '10日阴线次数');
assert.equal(focusPage3IndependentCount.textContent, '6只');
assert(focusPage3IndependentRows.innerHTML.includes('10日阴线=2（3只）'));
assert(focusPage3IndependentRows.innerHTML.includes('10日阴线：未统计（1只）'));
assert.equal((focusPage3IndependentRows.innerHTML.match(/class="stock-group-head"/g) || []).length, 4);
assert.equal((focusPage3IndependentRows.innerHTML.match(/data-stock-code=/g) || []).length, 6);
assert(focusPage3IndependentRows.innerHTML.includes('role="button" tabindex="0"'));
state.page3IndependentField = 'yi_yin_yi_yang_20d_by_yin_count';
assert.deepEqual(codes(renderFocusPage3Independent(groups, keep)), [
    '000004', '000003', '000005', '000002', '000006',
]);
assert.equal(focusPage3IndependentValueHeading.textContent, '20日阴线次数');
assert(focusPage3IndependentRows.innerHTML.includes('20日阴线=5（2只）'));
assert.deepEqual(codes(renderFocusPage3Independent(groups, stock => stock.code !== '000003')), [
    '000004', '000005', '000002', '000006',
]);
assert(focusPage3IndependentRows.innerHTML.includes('20日阴线=5（1只）'));
state.page3IndependentField = 'yi_yin_yi_yang_20d';
assert.deepEqual(codes(renderFocusPage3Independent(groups, keep)), [
    '000006', '000003', '000005', '000002', '000004',
]);
assert.equal(focusPage3IndependentValueHeading.textContent, '20天阴线破5次数');
assert(!focusPage3IndependentRows.innerHTML.includes('stock-group-head'));
assert.deepEqual(renderFocusPage3Independent({}, keep), []);
assert(focusPage3IndependentRows.innerHTML.includes('暂无符合当前条件的股票'));
assert.equal(JSON.stringify(groups), original);
"""
    result = subprocess.run([node, '-'], input=harness + config + helpers + renderer + checks,
                            text=True, capture_output=True, check=False, encoding='utf-8')
    assert result.returncode == 0, result.stderr

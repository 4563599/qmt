import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def client():
    import app as flask_app
    flask_app.app.config['TESTING'] = True
    with flask_app.app.test_client() as c:
        yield c


def _ths_payload():
    return json.dumps({
        'type': 'ths_bankuai',
        'redis_key': 'ths:20260503',
        'date': '20260503',
        'snapshot': {'status': 'ok', 'stock_count': 2},
        'counts': {'board_count': 1, 'captured_board_count': 1, 'stock_count': 2},
        'boards': [{'rank': 2, 'board_name': '赛马概念', 'change_percent_raw': '+3.69%', 'limit_up_count': 2}],
        'stocks': [
            {'board_name': '赛马概念', 'code': '600158', 'name': '中体产业', 'change_percent_raw': '+10.03%', 'price_raw': '11.85'},
            {'board_name': '赛马概念', 'code': '600195', 'name': '中牧股份', 'change_percent_raw': '-0.92%', 'price_raw': '7.52'},
        ],
        'boards_summary': [{
            'board_name': '赛马概念',
            'stock_count': 2,
            'stocks': [
                {'board_name': '赛马概念', 'code': '600158', 'name': '中体产业', 'change_percent_raw': '+10.03%', 'price_raw': '11.85'},
                {'board_name': '赛马概念', 'code': '600195', 'name': '中牧股份', 'change_percent_raw': '-0.92%', 'price_raw': '7.52'},
            ],
        }],
        'updated_at': '2026-05-03T15:38:40',
    }, ensure_ascii=False)


def test_ths_page_renders_two_list_app(client):
    resp = client.get('/同花顺板块')
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert '同花顺板块' in html
    assert 'ths-layout' in html
    assert 'boardList' in html
    assert 'stockList' in html
    assert '/api/ths/data' in html
    assert '.stock-row' in html
    assert 'stock-tooltip' in html
    assert 'showKline(event' in html
    assert 'webquoteklinepic.eastmoney.com' in html
    assert 'background:#10b981' not in html
    assert 'background: #10b981' not in html


def test_ths_page_has_top_timing_summary(client):
    resp = client.get('/同花顺板块')
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'timingSummary' in html
    assert '关键耗时' in html
    assert 'renderTimings' in html
    assert '打开/前置同花顺' in html
    assert '导出板块当前数据' in html
    assert '逐板块导出当前数据' in html


def test_ths_page_has_start_and_end_time_in_header_only(client):
    resp = client.get('/同花顺板块')
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert '开始：${startText ||' in html
    assert '结束：${endText ||' in html
    assert 'snapshot.start_time' in html
    assert 'snapshot.end_time' in html
    assert "entries.push(['开始时间'" not in html
    assert "entries.push(['结束时间'" not in html


def test_ths_header_scan_text_only_shows_times_and_status(client):
    resp = client.get('/同花顺板块')
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert '左侧板块 · 右侧对应个股 · 每轮采集覆盖最新数据' not in html
    assert '开始：${startText ||' in html
    assert '结束：${endText ||' in html
    assert 'statusText' in html
    assert '板块${counts.captured_board_count' not in html
    assert '个股${counts.stock_count' not in html


def test_ths_api_reads_latest_redis_payload(client):
    mock_r = MagicMock()
    mock_r.get.side_effect = lambda key: {'ths:latest': 'ths:20260503', 'ths:20260503': _ths_payload()}.get(key)

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ths/data')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['redis_key'] == 'ths:20260503'
    assert data['date'] == '2026-05-03'
    assert data['boards_summary'][0]['board_name'] == '赛马概念'
    assert data['boards_summary'][0]['stocks'][1]['name'] == '中牧股份'


def test_ths_dates_reads_compact_keys(client):
    mock_r = MagicMock()
    mock_r.keys.return_value = ['ths:20260503', 'ths:20260502', 'ths:latest', 'ths:20260503:timestamp']
    mock_r.get.return_value = 'ths:20260503'

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ths/dates')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['latest_date'] == '2026-05-03'
    assert data['dates'] == ['2026-05-03', '2026-05-02']


def test_jpgz_ths_nav_button_is_not_green(client):
    resp = client.get('/极品关注')
    html = resp.get_data(as_text=True)
    start = html.index('href="/同花顺板块"')
    snippet = html[start:start + 180]

    assert '同花顺板块' in snippet
    assert '#10b981' not in snippet

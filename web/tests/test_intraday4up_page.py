import base64
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


def _b64(payload):
    return base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('utf-8')


def test_intraday4up_stocks_sorted_by_four_day_gain(client):
    mock_r = MagicMock()
    mock_r.exists.return_value = True
    mock_r.type.return_value = 'hash'
    mock_r.hgetall.return_value = {
        '000002': _b64({
            'code': '000002',
            'name': '高涨幅',
            'stock_code_with_market': '000002.SZ',
            'gain_4d_pct': 18.5,
            'max_rebound_4d': 9.1,
            'min_pass_metric_4d': 6.8,
            'pass_days': 4,
            'day_details': [
                {'date': '20260515', 'prev_close': 10.0, 'ref_or_close': 10.5},
            ],
        }),
        '000001': _b64({
            'code': '000001',
            'name': '低涨幅',
            'stock_code_with_market': '000001.SZ',
            'gain_4d_pct': 6.2,
            'max_rebound_4d': 8.2,
            'min_pass_metric_4d': 7.0,
            'pass_days': 4,
            'day_details': [
                {'date': '20260515', 'prev_close': 20.0, 'ref_or_close': 19.0},
            ],
        }),
        '_scan_info': _b64({
            'last_scan_duration_sec': 2.345,
            'last_scan_end_time': '2026-05-15 14:58:00',
            'stock_count': 2,
        }),
    }

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/intraday4up/stocks?date=2026-05-15')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['date'] == '20260515'
    assert data['redis_key'] == 'intraday4up:20260515'
    assert [item['code'] for item in data['data']] == ['000001', '000002']
    assert data['data'][0]['gain_4d_pct'] == pytest.approx(6.2)
    assert data['data'][0]['today_change'] == pytest.approx(-5.0)
    assert data['data'][0]['max_rebound_4d'] == pytest.approx(8.2)
    assert data['data'][0]['min_pass_metric_4d'] == pytest.approx(7.0)
    assert data['scan_info']['last_scan_duration_sec'] == pytest.approx(2.345)


def test_intraday4up_dates_formats_redis_dates(client):
    mock_r = MagicMock()
    mock_r.keys.return_value = ['intraday4up:20260515', 'intraday4up:timestamp', 'intraday4up:20260514']

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/intraday4up/dates')
        data = json.loads(resp.data)

    assert data == {
        'success': True,
        'dates': ['2026-05-15', '2026-05-14'],
    }


def test_intraday4up_template_contains_nav_and_column_style(client):
    resp = client.get('/连续4天拉升')
    html = resp.get_data(as_text=True)

    assert '连续4天拉升' in html
    assert 'intraday4up:{yyyymmdd}' in html
    assert '/api/intraday4up/dates' in html
    assert '/api/intraday4up/stocks' in html
    assert "sort(function(a, b) {" in html
    assert "Number(a.gain_4d_pct || 0) - Number(b.gain_4d_pct || 0)" in html
    assert '.col-header.eq-three' in html
    assert 'stock-row-header' in html
    assert 'gain_4d_pct' in html
    assert '涨跌幅' in html
    assert '4天累计涨幅' in html
    assert '最大拉升' not in html
    assert 'max_rebound_4d' not in html
    assert '最弱通过' not in html
    assert 'min_pass_metric_4d' not in html

    lszty_resp = client.get('/连续涨停')
    lszty_html = lszty_resp.get_data(as_text=True)
    lszty_idx = lszty_html.index('<a href="/连续涨停" class="nav-btn nav-btn-lszty">连续涨停</a>')
    intraday_idx = lszty_html.index('<a href="/连续4天拉升" class="nav-btn nav-btn-intraday4up"')
    assert lszty_idx < intraday_idx

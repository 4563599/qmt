import base64
import json
import os
import re
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


def test_ztqhp_stocks_exposes_core_fields(client):
    """涨停前横盘接口应返回页面分组所需的核心字段"""
    mock_r = MagicMock()
    mock_r.exists.return_value = True
    mock_r.type.return_value = 'hash'
    payload = {
        'code': '000001',
        'name': '平安银行',
        'stock_code_with_market': '000001.SZ',
        'max_n': 6,
        'hb_range_pct': 7.25,
        'hb_high_close': 12.58,
        'hb_low_close': 11.73,
        'ref_gain': 9.97,
        'ref_close': 12.68,
    }
    scan_info = {
        'last_scan_duration_sec': 1.671,
        'last_scan_end_time': '2026-04-23 22:07:26',
    }
    mock_r.hgetall.return_value = {
        '000001': base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
        '_scan_info': base64.b64encode(json.dumps(scan_info, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
    }

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ztqhp/stocks?date=2026-04-23')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['date'] == '20260423'
    assert data['data'][0]['code'] == '000001'
    assert data['data'][0]['name'] == '平安银行'
    assert data['data'][0]['max_n'] == 6
    assert data['data'][0]['hb_range_pct'] == pytest.approx(7.25)
    assert data['data'][0]['hb_high_close'] == pytest.approx(12.58)
    assert data['data'][0]['hb_low_close'] == pytest.approx(11.73)
    assert data['data'][0]['ref_gain'] == pytest.approx(9.97)
    assert data['data'][0]['ref_close'] == pytest.approx(12.68)
    assert data['scan_info']['last_scan_duration_sec'] == pytest.approx(1.671)
    assert data['scan_info']['last_scan_end_time'] == '2026-04-23 22:07:26'


def test_ztqhp_dates_formats_redis_dates(client):
    """涨停前横盘日期接口应把 Redis key 日期格式化为 YYYY-MM-DD"""
    mock_r = MagicMock()
    mock_r.keys.return_value = ['ztqhp:20260423', 'ztqhp:20260422']

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ztqhp/dates')
        data = json.loads(resp.data)

    assert data == {
        'success': True,
        'dates': ['2026-04-23', '2026-04-22'],
    }


def test_ztqhp_template_groups_and_nav_button(client):
    """涨停前横盘页面应按 max_n 分表，并在首页与阴后强之间出现导航按钮"""
    resp = client.get('/涨停前横盘')
    html = resp.get_data(as_text=True)

    assert '涨停前横盘' in html
    assert 'Redis: ztqhp' in html
    assert '横盘天数大于5' in html
    assert '横盘天数5' in html
    assert '横盘天数4' in html
    assert '横盘天数3' in html
    assert "safeStocks.filter(s => Number(s.max_n || 0) > 5)" in html
    assert "safeStocks.filter(s => Number(s.max_n || 0) === 5)" in html
    assert "safeStocks.filter(s => Number(s.max_n || 0) === 4)" in html
    assert "safeStocks.filter(s => Number(s.max_n || 0) === 3)" in html
    assert "今日任务列表" in html
    assert "一字特征" in html
    assert "厂子特征" in html
    assert "横盘调整特征" in html
    assert "未添加,请完成今日任务!" in html
    assert "task-context-menu" in html
    assert "task-ios-overlay" in html
    assert "添加一字特征" in html
    assert "添加厂子特征" in html
    assert "添加横盘调整特征" in html
    assert "删除股票" in html
    assert "/api/yhy/tasks?date=${_currentDate}" in html
    assert "/api/yhy/tasks/add" in html
    assert "/api/yhy/tasks/remove" in html
    assert "source_page: getTaskSourcePageName()" in html
    assert "getVisibleTaskStocksForCurrentPage" in html
    assert "return '涨停前横盘';" in html
    assert "buildStackedColumn(" in html
    assert "task_type === groupMeta.key" in html
    assert "showTaskContextMenu" in html
    assert "showTaskDeleteMenu" in html
    assert '/api/ztqhp/dates' in html
    assert '/api/ztqhp/stocks' in html
    assert 'scanInfoText' in html
    assert '扫描耗时' in html
    assert '上次扫描时间:' in html
    assert 'updateScanInfoDisplay' in html
    assert 'columns-grid' in html
    assert 'metric-column' in html
    assert 'stock-row-header' in html
    stock_styles = '\n'.join(re.findall(r'\.stock-row[^{}]*\{[^}]*\}', html))
    assert stock_styles
    assert '#0f766e' not in stock_styles
    assert '#14b8a6' not in stock_styles
    assert '#27ae60' not in stock_styles

    yhy_resp = client.get('/阴后强')
    yhy_html = yhy_resp.get_data(as_text=True)
    home_idx = yhy_html.index('<a href="/" class="nav-btn nav-btn-home">首页</a>')
    ztqhp_idx = yhy_html.index('<a href="/涨停前横盘" class="nav-btn" style="background:#2563eb;">涨停前横盘</a>')
    yhy_idx = yhy_html.index('<a href="/阴后强" class="nav-btn nav-btn-yhy" style="font-weight:700;">阴后强</a>')
    assert home_idx < ztqhp_idx < yhy_idx
    assert '/5拉升20阴少' not in yhy_html
    assert '/5拉升小于2阴' not in yhy_html
    assert '/5拉升3光脚' not in yhy_html
    assert '/极品关注' in yhy_html
    assert '/涨停前连续强' not in yhy_html
    assert '/首板涨停' not in yhy_html

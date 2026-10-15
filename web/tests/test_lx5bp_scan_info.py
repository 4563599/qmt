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


def test_lx5bp_stocks_returns_scan_info(client):
    """连续5不碰接口应返回归一化后的 scan_info"""
    mock_r = MagicMock()
    payload = {
        'code': '000001',
        'name': '平安银行',
        'n_param': 5,
        'ref_gain': 3.21,
        'three_day_near_barefoot_yang_count': 2,
    }
    scan_info = {
        'last_scan_duration_sec': 1.671,
        'last_scan_end_time': '2026-04-23 22:07:26',
    }

    def exists_side_effect(key):
        return key == 'lx5bp:20260423'

    def type_side_effect(key):
        return 'hash' if key == 'lx5bp:20260423' else None

    def hscan_side_effect(key, cursor, count=100):
        if key == 'lx5bp:20260423':
            return 0, {
                '000001': base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
                '_scan_info': base64.b64encode(json.dumps(scan_info, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
            }
        return 0, {}

    mock_r.exists.side_effect = exists_side_effect
    mock_r.type.side_effect = type_side_effect
    mock_r.hscan.side_effect = hscan_side_effect
    mock_r.ping.return_value = True

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/lx5bp/stocks?date=2026-04-23')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['data'][0]['code'] == '000001'
    assert data['data'][0]['three_day_near_barefoot_yang_count'] == 2
    assert data['scan_info']['last_scan_duration_sec'] == pytest.approx(1.671)
    assert data['scan_info']['last_scan_end_time'] == '2026-04-23 22:07:26'


def test_lx5bp_template_contains_scan_info_display(client):
    """连续5不碰页面应包含扫描耗时和今日任务列表逻辑"""
    resp = client.get('/连续5不碰')
    html = resp.get_data(as_text=True)

    assert 'scanInfoText' in html
    assert '扫描耗时--s，扫描完成时间:--，Redis更新:--' in html
    assert '.page-title .scan-info-text { color: #2563eb; font-size: 13px; font-weight: 700; }' in html
    assert 'updateScanInfoDisplay' in html
    assert 'formatScanDuration' in html
    assert 'formatScanEndTime' in html
    assert 'grid-template-columns: 220px 320px 320px 320px 250px;' in html
    assert '表3 · 三天强势K' in html
    assert '几乎光脚' in html
    assert 'three_day_near_barefoot_yang_count' in html
    assert '今日重点关注股票' in html
    assert '今日次重点关注' in html
    assert 'buildLx5bpFocusAlertPanel' in html
    assert 'buildLx5bpSecondaryFocusAlertPanel' in html
    assert 'buildLx5bpFocusAlertPanels' in html
    assert 'isLx5bpFocusStock' in html
    assert 'isLx5bpSecondaryFocusStock' in html
    assert 'isLx5bpTable4Stock(stock) && Number(stock.three_day_near_barefoot_yang_count || 0) >= 2 && Number(stock.three_day_strong_k_count || 0) === 3' in html
    assert 'Number(stock.three_day_near_barefoot_yang_count || 0) >= 2' in html
    assert 'Number(stock.three_day_strong_k_count || 0) === 3' in html
    assert 'Number(stock.ref_high_gain_gte_6_5_and_yang || 0) === 1' in html
    assert '!isLx5bpFocusStock(stock)' in html
    assert 'getLx5bpLowerShadowSortValue(a) - getLx5bpLowerShadowSortValue(b)' in html
    assert 'focus-alert-panels' in html
    assert 'stock-row-focus-alert' in html
    assert 'task-side-panel' in html
    assert '今日任务列表' in html
    assert 'task-context-menu' in html
    assert 'task-ios-overlay' in html
    assert "const YHY_TASK_API_BASE = '/api/yhy/tasks';" in html
    assert "return '连续5不碰';" in html
    assert "source_page: 'lx5bp'" in html
    assert 'await loadTaskStocks(_currentDate);' in html
    assert '/api/yhy/tasks/add' in html
    assert '/api/yhy/tasks/remove' in html
    assert "row.addEventListener('mouseenter', function(e)" in html
    assert 'showKline(e, stock.code, stock.name);' in html
    assert "row.addEventListener('mouseleave', hideKline);" in html
    assert "row.addEventListener('mousemove', moveTooltip);" in html

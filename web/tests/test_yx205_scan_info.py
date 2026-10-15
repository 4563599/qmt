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


def test_yx205_stocks_returns_scan_info_from_three_keys(client):
    """20阴少跌少横盘接口应从 yx205/yx2051/yx2052 里返回归一化后的 scan_info"""
    mock_r = MagicMock()
    payload = {
        'code': '000001',
        'name': '平安银行',
        'yin_count': 3,
        'today_change': 4.56,
    }
    scan_info_main = {
        'last_scan_duration_sec': 1.671,
        'last_scan_end_time': '2026-04-23 22:07:26',
    }
    scan_info_other = {
        'last_scan_duration_sec': 1.672,
        'last_scan_end_time': '2026-04-23 22:07:20',
    }

    def exists_side_effect(key):
        return key in {'yx205:20260423', 'yx2051:20260423', 'yx2052:20260423'}

    def type_side_effect(key):
        return 'hash' if exists_side_effect(key) else None

    def hscan_side_effect(key, cursor, count=100):
        if key == 'yx205:20260423':
            return 0, {
                '000001': base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
                '_scan_info': base64.b64encode(json.dumps(scan_info_main, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
            }
        if key == 'yx2051:20260423':
            return 0, {
                '_scan_info': base64.b64encode(json.dumps(scan_info_other, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
            }
        if key == 'yx2052:20260423':
            return 0, {
                '_scan_info': base64.b64encode(json.dumps(scan_info_other, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
            }
        return 0, {}

    mock_r.exists.side_effect = exists_side_effect
    mock_r.type.side_effect = type_side_effect
    mock_r.hscan.side_effect = hscan_side_effect
    mock_r.ping.return_value = True

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/yx205/stocks?date=2026-04-23')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['data'][0]['code'] == '000001'
    assert data['scan_info']['last_scan_duration_sec'] == pytest.approx(1.671)
    assert data['scan_info']['last_scan_end_time'] == '2026-04-23 22:07:26'


def test_yx205_template_contains_scan_info_display(client):
    """20阴少跌少横盘页面应包含扫描耗时和右侧今日任务列表逻辑"""
    resp = client.get('/20阴少跌少横盘')
    html = resp.get_data(as_text=True)

    assert 'scanInfoText' in html
    assert '扫描耗时--s，上次扫描时间:--' in html
    assert '.page-title .scan-info-text { color: #2563eb; font-size: 13px; font-weight: 700; }' in html
    assert 'updateScanInfoDisplay' in html
    assert 'formatScanDuration' in html
    assert 'formatScanEndTime' in html
    assert 'task-side-panel' in html
    assert '今日任务列表' in html
    assert 'task-context-menu' in html
    assert 'task-ios-overlay' in html
    assert "const YHY_TASK_API_BASE = '/api/yhy/tasks';" in html
    assert 'myStock:task' in html
    assert "return '20阴少跌少横盘';" in html
    assert "source_page: 'yx205'" in html
    assert 'await loadTaskStocks(currentDate);' in html
    assert '/api/yhy/tasks/add' in html
    assert '/api/yhy/tasks/remove' in html
    assert "row.addEventListener('contextmenu'" in html
    assert "row.addEventListener('mouseenter', function(e)" in html
    assert 'showKline(e, stock.code, stock.name);' in html
    assert "row.addEventListener('mouseleave', hideKline);" in html
    assert "row.addEventListener('mousemove', moveTooltip);" in html

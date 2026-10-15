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


def test_lszty_stocks_returns_scan_info(client):
    """连续涨停接口应返回 Redis _scan_info 元信息"""
    mock_r = MagicMock()
    payload = {
        'code': '600001',
        'name': '测试连板',
        'group': '连续涨停',
        'ref_gain': 9.99,
        'consecutive_count': 3,
        'board_type': '主板',
    }
    scan_info = {
        'last_scan_duration_sec': 0.333,
        'last_scan_end_time': '2026-04-23 23:36:01',
    }

    mock_r.exists.return_value = True
    mock_r.type.return_value = 'hash'
    mock_r.hscan.side_effect = [
        (
            0,
            {
                '600001': base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
                '_scan_info': base64.b64encode(json.dumps(scan_info, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
            },
        )
    ]

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/lszty/stocks?date=2026-04-23')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['data'][0]['code'] == '600001'
    assert data['scan_info']['last_scan_duration_sec'] == pytest.approx(0.333)
    assert data['scan_info']['last_scan_end_time'] == '2026-04-23 23:36:01'


def test_lszty_template_contains_scan_info_display(client):
    """连续涨停页面应包含扫描耗时和右侧今日任务列表逻辑"""
    resp = client.get('/连续涨停')
    html = resp.get_data(as_text=True)

    assert 'scanInfoText' in html
    assert '扫描耗时--s，上次扫描时间:--' in html
    assert 'updateScanInfoDisplay' in html
    assert 'formatScanDuration' in html
    assert 'formatScanEndTime' in html
    assert 'task-side-panel' in html
    assert '今日任务列表' in html
    assert 'task-context-menu' in html
    assert 'task-ios-overlay' in html
    assert "const YHY_TASK_API_BASE = '/api/yhy/tasks';" in html
    assert "return '连续涨停';" in html
    assert "source_page: 'lszty'" in html
    assert '/api/yhy/tasks/add' in html
    assert '/api/yhy/tasks/remove' in html
    assert 'await loadTaskStocks(currentDate);' in html
    assert "row.addEventListener('mouseenter', function(e)" in html
    assert 'showKline(e, stock.code, stock.name);' in html
    assert "row.addEventListener('mouseleave', hideKline);" in html
    assert "row.addEventListener('mousemove', moveTooltip);" in html
    assert '.scan-info-text { color: #2563eb; font-size: 13px; font-weight: 700; }' in html


def test_lszty_stocks_returns_first_limit_and_new_flags(client):
    """连续涨停接口应返回首次涨停组和两个新增布尔字段"""
    mock_r = MagicMock()
    payload_first = {
        'code': '600002',
        'name': '首次样本',
        'group': '首次涨停',
        'ref_gain': 9.88,
        'board_type': '主板',
        'limit_ratio': 10.0,
        'high_gain_count': 6,
        'high_gain_days_gte5': True,
        'all_yin_pull_prev_or_next': True,
    }
    payload_continue = {
        'code': '600003',
        'name': '连续样本',
        'group': '连续涨停',
        'ref_gain': 10.01,
        'consecutive_count': 2,
        'board_type': '主板',
        'high_gain_count': 4,
        'high_gain_days_gte5': False,
        'all_yin_pull_prev_or_next': True,
    }

    mock_r.exists.return_value = True
    mock_r.type.return_value = 'hash'
    mock_r.hscan.side_effect = [
        (
            0,
            {
                '600002': base64.b64encode(json.dumps(payload_first, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
                '600003': base64.b64encode(json.dumps(payload_continue, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
            },
        )
    ]

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/lszty/stocks?date=2026-04-23')
        data = json.loads(resp.data)

    assert data['success'] is True
    first = next(item for item in data['data'] if item['group'] == '首次涨停')
    assert first['code'] == '600002'
    assert first['high_gain_count'] == 6
    assert first['high_gain_days_gte5'] is True
    assert first['all_yin_pull_prev_or_next'] is True


def test_lszty_template_uses_group_contains_matching(client):
    """连续涨停页面分组应兼容 group='首次涨停,最多一阴' 这类逗号分隔多组值"""
    resp = client.get('/连续涨停')
    html = resp.get_data(as_text=True)

    assert 'function groupIncludes(stock, target)' in html
    assert "String(stock.group || '')" in html
    assert ".split(',')" in html
    assert "const group1 = stocksData.filter(s => groupIncludes(s, '最多一阴'));" in html
    assert "const group2 = stocksData.filter(s => groupIncludes(s, '连续涨停'));" in html
    assert "const group3 = stocksData.filter(s => groupIncludes(s, '首次涨停'));" in html


def test_lszty_template_contains_first_limit_and_new_columns(client):
    """连续涨停页面应包含更新后的列配置、特别关注组和紫色高亮逻辑"""
    resp = client.get('/连续涨停')
    html = resp.get_data(as_text=True)

    assert '特别关注组' in html
    assert '特别关注连板' in html
    assert '特别关注首板' in html
    assert '暂无数据' in html
    assert '首次涨停' in html
    assert '5拉升' in html
    assert '阴前后拉' in html
    assert 'highlight-purple-row' in html
    assert 'high_gain_days_gte5' in html
    assert 'all_yin_pull_prev_or_next' in html
    assert '<th>极品K%</th><th>全阳</th><th>阴线向上</th><th>阴线都强</th><th>5拉升</th><th>阴前后拉</th>' in html
    assert '<th>编号</th><th>股票代码</th><th>股票名称</th><th>参考日涨幅</th><th>连板数</th><th>极品K%</th><th>5拉升</th><th>阴前后拉</th>' in html
    assert '<th>上次涨停</th>' not in html
    assert '<th>N天数</th>' not in html
    assert '<th>阴线数</th>' not in html
    assert '<th>板块</th>' not in html
    assert '<th>K线图</th>' not in html
    assert '<th>涨停幅度</th>' not in html

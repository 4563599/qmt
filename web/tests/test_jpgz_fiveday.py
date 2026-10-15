import base64
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def client():
    import app as flask_app
    flask_app.app.config['TESTING'] = True
    with flask_app.app.test_client() as c:
        yield c


def _task_payload(code, name, task_type, gain, source_pages=None):
    return base64.b64encode(json.dumps({
        'code': code,
        'name': name,
        'task_type': task_type,
        'ref_gain': gain,
        'today_change': gain,
        'market_cap': 123.4,
        'source_page': '阴后强',
        'source_pages': source_pages or ['阴后强'],
    }, ensure_ascii=False).encode('utf-8')).decode('utf-8')


def test_homepage_redirects_to_five_pull(client):
    """首页保持当前默认的5拉升入口。"""
    resp = client.get('/', follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert resp.headers['Location'].endswith('/5%E6%8B%89%E5%8D%87')


def test_jpgz_fiveday_endpoint_returns_recent_task_groups(client):
    """极品关注最近5日接口应返回按日期和任务类型分组的数据"""
    mock_r = MagicMock()

    def keys_side_effect(pattern):
        if pattern == 'yhy:*':
            return ['yhy:20260327', 'yhy:20260326', 'yhy:20260325', 'yhy:20260324', 'yhy:20260323', 'yhy:20260322']
        return []

    def exists_side_effect(key):
        return key in {
            'myStock:task:20260327',
            'myStock:task:20260326',
        }

    def type_side_effect(key):
        return 'hash' if exists_side_effect(key) else None

    def hscan_side_effect(key, cursor, count=100):
        if key == 'myStock:task:20260327':
            return 0, {
                '000001': _task_payload('000001', '平安银行', 'yizi', 3.21, ['阴后强']),
                '000002': _task_payload('000002', '万科A', 'changzi', 5.67, ['涨停前横盘']),
            }
        if key == 'myStock:task:20260326':
            return 0, {
                '000003': _task_payload('000003', '国农科技', 'hengpan', 8.88, ['5拉升:阴线都向上']),
            }
        return 0, {}

    mock_r.keys.side_effect = keys_side_effect
    mock_r.exists.side_effect = exists_side_effect
    mock_r.type.side_effect = type_side_effect
    mock_r.hscan.side_effect = hscan_side_effect

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/jpgz/fiveday')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['dates'] == ['2026-03-27', '2026-03-26', '2026-03-25', '2026-03-24', '2026-03-23']
    assert data['data']['2026-03-27']['yizi'][0]['code'] == '000001'
    assert data['data']['2026-03-27']['changzi'][0]['code'] == '000002'
    assert data['data']['2026-03-27']['hengpan'] == []
    assert data['data']['2026-03-26']['hengpan'][0]['code'] == '000003'
    assert data['data']['2026-03-25']['yizi'] == []


def test_jpgz_template_contains_task_fiveday_layout(client):
    """极品关注页面模板应包含最近5个交易日任务视图、删除入口和实时刷新逻辑"""
    resp = client.get('/极品关注')
    html = resp.get_data(as_text=True)

    assert '最近5个交易日 · 今日任务列表' in html
    assert '一字特征' in html
    assert '厂子特征' in html
    assert '横盘调整特征' in html
    assert '手动录入' in html
    assert "/api/jpgz/fiveday" in html
    assert "/api/yhy/tasks/manual-lookup" in html
    assert "按一字特征 / 厂子特征 / 横盘调整特征分组显示</span>" in html
    assert "onclick=\"handleManualEntry(_allDates[_currentDateIdx] || '')\"" in html
    assert "source_pages" in html
    assert '.stock-source {' in html
    assert 'white-space: normal;' in html
    assert 'word-break: break-all;' in html
    assert 'overflow-wrap: anywhere;' in html
    assert 'task-context-menu' in html
    assert '删除股票' in html
    assert '/api/yhy/tasks/remove' in html
    assert '/api/yhy/tasks/add' in html
    assert 'showManualCodeInputDialog' in html
    assert 'showManualTaskTypeDialog' in html
    assert 'lookupManualTaskStock' in html
    assert 'persistManualTaskStock' in html
    assert "source_page: '手动录入'" in html
    assert 'showTaskDeleteMenu' in html
    assert 'showTaskDeleteConfirmDialog' in html
    assert 'removeTaskStock' in html
    assert 'isTradingTime' in html
    assert 'refreshRealtimeChanges' in html
    assert 'setInterval(refreshRealtimeChanges, 3000)' in html
    assert '/api/yx205/realtime' in html
    assert '@media (max-width: 768px)' in html
    assert 'mobile no-scroll baseline' in html
    assert 'overflow-x: hidden !important' in html


def test_jpgz_fiveday_endpoint_redis_error_returns_failure(client):
    """Redis 异常时返回 success=False"""
    with patch('app.get_redis_client', side_effect=Exception('connection refused')):
        resp = client.get('/api/jpgz/fiveday')
        data = json.loads(resp.data)
    assert data['success'] is False
    assert 'error' in data


def test_yhy_manual_lookup_endpoint_returns_tushare_stock_info(client):
    """手动录入查询接口应根据代码返回名称、涨跌幅、市值和带市场后缀代码"""
    mock_pro = MagicMock()
    mock_pro.stock_basic.return_value = pd.DataFrame([{
        'ts_code': '600000.SH',
        'symbol': '600000',
        'name': '浦发银行'
    }])
    mock_pro.daily_basic.return_value = pd.DataFrame([{
        'ts_code': '600000.SH',
        'total_mv': 1234500
    }])
    mock_quote_df = pd.DataFrame([{
        'name': '浦发银行',
        'pre_close': '10.00',
        'price': '10.50',
        'open': '10.20'
    }])

    with patch('app.ts.pro_api', return_value=mock_pro), \
         patch('app.ts.get_realtime_quotes', return_value=mock_quote_df):
        resp = client.post('/api/yhy/tasks/manual-lookup', json={'code': '600000'})
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['data']['code'] == '600000'
    assert data['data']['name'] == '浦发银行'
    assert data['data']['stock_code_with_market'] == '600000.SH'
    assert data['data']['market_cap'] == 123.45
    assert data['data']['today_change'] == pytest.approx(5.0)

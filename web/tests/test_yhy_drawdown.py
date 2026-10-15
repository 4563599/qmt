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


def test_yhy_stocks_exposes_market_cap_and_scan_info(client):
    """阴后强接口应透传市值字段并单独返回扫描元信息"""
    mock_r = MagicMock()
    mock_r.exists.return_value = True
    mock_r.type.return_value = 'hash'
    payload = {
        'code': '000001',
        'name': '平安银行',
        'ref_gain': 4.56,
        'today_change': 4.56,
        'ref_limit_up': 1,
        'max_drawdown_10d': -3.2,
        'all_yin_up': 1,
        'all_yin_no_touch': 0,
        'ref_high_gain_ok': 1,
        'market_cap': 123.45,
    }
    scan_info = {
        'last_scan_duration_sec': 3.0,
        'last_scan_end_time': '2026-04-22 15:20:30',
    }
    mock_r.hgetall.return_value = {
        '000001': base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
        '_scan_info': base64.b64encode(json.dumps(scan_info, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
    }

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/yhy/stocks?date=2026-04-21')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['data'][0]['today_change'] == pytest.approx(4.56)
    assert data['data'][0]['max_drawdown_10d'] == pytest.approx(-3.2)
    assert data['data'][0]['market_cap'] == pytest.approx(123.45)
    assert data['data'][0]['all_yin_up'] is True
    assert data['data'][0]['all_yin_no_touch'] is False
    assert data['data'][0]['ref_high_gain_ok'] is True
    assert data['scan_info']['last_scan_duration_sec'] == pytest.approx(3.0)
    assert data['scan_info']['last_scan_end_time'] == '2026-04-22 15:20:30'


def test_yhy_scan_info_endpoint_returns_scan_info_only(client):
    """阴后强扫描信息接口应只返回扫描元信息"""
    mock_r = MagicMock()
    mock_r.exists.return_value = True
    mock_r.type.return_value = 'hash'
    scan_info = {
        'last_scan_duration_sec': 12.438,
        'last_scan_end_time': '2026-04-22 15:02:37',
    }
    mock_r.hget.return_value = base64.b64encode(json.dumps(scan_info, ensure_ascii=False).encode('utf-8')).decode('utf-8')

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/yhy/scan-info?date=2026-04-22')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['scan_info']['last_scan_duration_sec'] == pytest.approx(12.438)
    assert data['scan_info']['last_scan_end_time'] == '2026-04-22 15:02:37'


def test_yhy_tasks_endpoint_returns_manual_tasks(client):
    """阴后强任务接口应返回按日期存储的手动任务股票列表"""
    mock_r = MagicMock()
    mock_r.exists.return_value = True
    mock_r.type.return_value = 'hash'
    task_payload = {
        'task_type': 'yizi',
        'code': '000001',
        'name': '平安银行',
        'stock_code_with_market': '000001.SZ',
        'ref_gain': 8.12,
        'today_change': 8.55,
        'market_cap': 123.45,
        'added_at': '2026-04-25 10:20:30',
        'gain_confirm_level': 'lt35',
        'source_page': 'yhy',
        'source_pages': ['yhy', 'lszty'],
    }
    mock_r.hgetall.return_value = {
        '000001': base64.b64encode(json.dumps(task_payload, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
    }

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/yhy/tasks?date=2026-04-25')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['count'] == 1
    assert data['data'][0]['task_type'] == 'yizi'
    assert data['data'][0]['code'] == '000001'
    assert data['data'][0]['stock_code_with_market'] == '000001.SZ'
    assert data['data'][0]['market_cap'] == pytest.approx(123.45)
    assert data['data'][0]['gain_confirm_level'] == 'lt35'
    assert data['data'][0]['source_page'] == '阴后强'
    assert data['data'][0]['source_pages'] == ['阴后强', '连续涨停']


def test_yhy_add_task_endpoint_writes_base64_hash_payload(client):
    """阴后强任务新增接口应写入 myStock:task:YYYYMMDD hash"""
    mock_r = MagicMock()
    mock_r.hget.return_value = None

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.post('/api/yhy/tasks/add', json={
            'date': '2026-04-25',
            'task_type': 'changzi',
            'code': '600000',
            'name': '浦发银行',
            'stock_code_with_market': '600000.SH',
            'ref_gain': 5.6,
            'today_change': 7.8,
            'market_cap': 222.3,
            'gain_confirm_level': 'gte35',
        })
        data = json.loads(resp.data)

    assert data['success'] is True
    mock_r.hset.assert_called_once()
    call_args = mock_r.hset.call_args
    assert call_args.args[0] == 'myStock:task:20260425'
    assert call_args.args[1] == '600000'
    stored_payload = json.loads(base64.b64decode(call_args.args[2]).decode('utf-8'))
    assert stored_payload['task_type'] == 'changzi'
    assert stored_payload['name'] == '浦发银行'
    assert stored_payload['gain_confirm_level'] == 'gte35'
    assert stored_payload['market_cap'] == pytest.approx(222.3)
    assert stored_payload['source_page'] == '阴后强'
    assert stored_payload['source_pages'] == ['阴后强']


def test_yhy_add_task_endpoint_merges_source_pages_from_existing_record(client):
    """阴后强任务新增接口对同一股票应合并旧 source_pages 并去重"""
    mock_r = MagicMock()
    existing_payload = {
        'task_type': 'changzi',
        'code': '600000',
        'name': '浦发银行',
        'source_page': 'lszty',
        'source_pages': ['lszty', 'yhy'],
    }
    mock_r.hget.return_value = base64.b64encode(json.dumps(existing_payload, ensure_ascii=False).encode('utf-8')).decode('utf-8')

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.post('/api/yhy/tasks/add', json={
            'date': '2026-04-25',
            'task_type': 'changzi',
            'code': '600000',
            'name': '浦发银行',
        })
        data = json.loads(resp.data)

    assert data['success'] is True
    stored_payload = json.loads(base64.b64decode(mock_r.hset.call_args.args[2]).decode('utf-8'))
    assert stored_payload['source_page'] == '阴后强'
    assert stored_payload['source_pages'] == ['连续涨停', '阴后强']



def test_yhy_add_task_endpoint_accepts_changchangzi_and_screening_fields(client):
    """任务新增接口应支持厂厂字特征及筛选页所需字段。"""
    mock_r = MagicMock()
    mock_r.hget.return_value = None

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.post('/api/yhy/tasks/add', json={
            'date': '2026-04-30',
            'task_type': 'changchangzi',
            'code': '688628',
            'name': '优利德',
            'stock_code_with_market': '688628.SH',
            'source_page': 'ztorgj',
            'all_yin_no_touch': True,
            'all_yin_cross_star': True,
            'all_yin_up': False,
            'layang': True,
            'high_gain_10d_count': 7,
            'days_since_top_day': 2,
            'strong_adjust_k_pct': 3.5,
            'cum_high_to_ma5_pct': 9.1,
        })
        data = json.loads(resp.data)

    assert data['success'] is True
    stored_payload = json.loads(base64.b64decode(mock_r.hset.call_args.args[2]).decode('utf-8'))
    assert stored_payload['task_type'] == 'changchangzi'
    assert stored_payload['all_yin_no_touch'] is True
    assert stored_payload['all_yin_cross_star'] is True
    assert stored_payload['all_yin_up'] is False
    assert stored_payload['layang'] is True
    assert stored_payload['high_gain_10d_count'] == 7
    assert stored_payload['days_since_top_day'] == 2
    assert stored_payload['strong_adjust_k_pct'] == pytest.approx(3.5)
    assert stored_payload['cum_high_to_ma5_pct'] == pytest.approx(9.1)


def test_yhy_add_task_endpoint_marks_jipin_focus_without_losing_feature(client):
    """二轮筛选添加极品关注时应保留一轮特征并记录关注原因。"""
    mock_r = MagicMock()
    existing_payload = {
        'task_type': 'changchangzi',
        'code': '688628',
        'name': '优利德',
        'source_page': '涨停或光脚:K线测试',
        'source_pages': ['涨停或光脚:K线测试'],
    }
    mock_r.hget.return_value = base64.b64encode(json.dumps(existing_payload, ensure_ascii=False).encode('utf-8')).decode('utf-8')

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.post('/api/yhy/tasks/add', json={
            'date': '2026-04-30',
            'task_type': 'changchangzi',
            'code': '688628',
            'name': '优利德',
            'source_page': 'ztorgj',
            'jipin_focus': True,
            'focus_reason': '阴线都是十字星且不碰',
        })
        data = json.loads(resp.data)

    assert data['success'] is True
    stored_payload = json.loads(base64.b64decode(mock_r.hset.call_args.args[2]).decode('utf-8'))
    assert stored_payload['task_type'] == 'changchangzi'
    assert stored_payload['jipin_focus'] is True
    assert stored_payload['focus_reason'] == '阴线都是十字星且不碰'
    assert stored_payload['focus_reasons'] == ['阴线都是十字星且不碰']


def test_yhy_add_task_endpoint_accepts_custom_source_page(client):
    """共享任务新增接口应支持其他页面传入 source_page"""
    mock_r = MagicMock()
    mock_r.hget.return_value = None

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.post('/api/yhy/tasks/add', json={
            'date': '2026-04-25',
            'task_type': 'hengpan',
            'code': '000001',
            'name': '平安银行',
            'source_page': 'ztqhp',
        })
        data = json.loads(resp.data)

    assert data['success'] is True
    stored_payload = json.loads(base64.b64decode(mock_r.hset.call_args.args[2]).decode('utf-8'))
    assert stored_payload['source_page'] == '涨停前横盘'
    assert stored_payload['source_pages'] == ['涨停前横盘']


def test_yhy_remove_task_endpoint_deletes_hash_field(client):
    """阴后强任务删除接口应删除 myStock:task:YYYYMMDD hash 中对应股票"""
    mock_r = MagicMock()

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.post('/api/yhy/tasks/remove', json={
            'date': '2026-04-25',
            'code': '600000',
        })
        data = json.loads(resp.data)

    assert data['success'] is True
    mock_r.hdel.assert_called_once_with('myStock:task:20260425', '600000')


def test_yx205_realtime_logs_tencent_batch_payload_and_parse_summary(client):
    """阴后强实时接口应打印腾讯批量原始返回与解析摘要，便于排查刷新滞后"""
    mock_response = MagicMock()
    mock_response.text = 'v_sz000001="51~平安银行~000001~10.50~10.00~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0";'
    mock_response.encoding = 'gbk'

    with patch('app.requests.get', return_value=mock_response), patch('builtins.print') as mock_print:
        resp = client.post('/api/yx205/realtime', json={'codes': ['000001']})
        data = json.loads(resp.data)

    assert data['success'] is True
    logged = '\n'.join(' '.join(str(arg) for arg in call.args) for call in mock_print.call_args_list)
    assert '[yx205][realtime] start' in logged
    assert 'codes=1' in logged
    assert 'raw_lines=' in logged
    assert 'parsed code=000001' in logged
    assert 'change=5.0' in logged
    assert '[yx205][realtime] done' in logged


def test_yx205_realtime_batches_long_code_lists(client):
    """阴后强实时接口应分批请求腾讯，避免股票数多时 URL 过长导致实时涨幅为空"""
    codes = [f'600{i:03d}' for i in range(200)] + ['000001']

    def fake_get(url, timeout=10):
        query = url.split('q=', 1)[1]
        tencent_codes = [item for item in query.split(',') if item]
        lines = []
        for tc in tencent_codes:
            code = tc[2:]
            price = '105.50' if code == '000001' else '101.00'
            lines.append(f'v_{tc}="51~测试~{code}~{price}~100.00~0";')
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '\n'.join(lines)
        return resp

    with patch('app.requests.get', side_effect=fake_get) as mock_get:
        resp = client.post('/api/yx205/realtime', json={'codes': codes})
        data = json.loads(resp.data)

    assert data['success'] is True
    assert mock_get.call_count == 2
    assert data['data']['000001']['change'] == pytest.approx(5.5)
    assert data['debug']['chunk_count'] == 2
    assert data['debug']['chunk_size'] == 200
    assert data['debug']['parsed_count'] == len(codes)


def test_yhy_template_adds_table5_and_four_column_layout(client):
    """阴后强页面应显示表5、任务列表、右键菜单、iOS确认弹窗以及现有布局"""
    resp = client.get('/阴后强')
    html = resp.get_data(as_text=True)

    assert '@media (max-width: 768px)' in html
    assert 'grid-template-columns: 1fr' in html
    assert '.table-wrap { overflow-x: hidden; }' in html
    assert 'table-layout: fixed' in html
    assert 'white-space: normal' in html
    assert 'overflow-wrap: anywhere' in html
    assert 'mobile density tuning' in html
    assert '.page-title h1 { font-size: 18px; }' in html
    assert '.stock-table td { font-size: 11px; padding: 4px 3px; }' in html
    assert '.metric-cell,' in html
    assert '.metric-cell span {' in html
    assert 'font-size: 11px;' in html
    assert '.s-name { font-size: 13px; font-weight: 500; color: #1d1d1f; text-align: center; white-space: nowrap; overflow: visible; text-overflow: clip; }' in html
    assert '.name-cell { vertical-align: middle; white-space: nowrap; }' in html
    assert '.change-cell,' in html
    assert '.final-value-cell {' in html
    assert 'white-space: nowrap !important;' in html
    assert '.change-cell span,' in html
    assert '.final-value-cell span {' in html
    assert '.compact-digit { font-size: 10px; }' in html
    assert '.stock-table th {' in html
    assert 'color: #111827;' in html
    assert '.stock-table th:nth-child(4),' in html
    assert '.stock-table td:nth-child(3) { width: 56px; }' in html
    assert '.stock-table td:nth-child(4) { width: 28px; }' in html
    assert '.stock-table td:nth-child(5) { width: 34px; }' in html
    assert '.stock-table td:nth-child(7) { width: 64px; }' in html
    assert '.stock-table td:nth-child(8) { width: 64px; }' in html
    assert 'word-break: keep-all;' in html
    assert '<colgroup>' in html
    assert '<col style="width:28px">' in html
    assert '<col style="width:34px">' in html
    assert '<col style="width:48px">' in html
    assert '<col style="width:64px">' in html
    assert '<col style="width:56px">' in html
    assert '.container { max-width: 1680px; margin: 0 auto; }' in html
    assert 'grid-template-columns: repeat(4, minmax(0, 1fr));' in html
    assert '.stacked-column { display: flex; flex-direction: column; gap: 8px; min-width: 0; }' in html
    assert 'font-size: 10px;' in html
    assert 'font-size: 13px;' in html
    assert '表1 · 全部股票涨跌幅排序' in html
    assert '表2 · 阴线形态分组' in html
    assert '表3 · 涨停股10天回撤' in html
    assert '表4 · 涨停股10天累计' in html
    assert '表5 · 参考日最高涨幅≥6.5%' in html
    assert '筛选 ref_high_gain_ok=true，按涨跌幅由高到低排序' in html
    assert '涨幅≥7.5%组' in html
    assert '3秒刷新' in html
    assert 'const TOP_GAIN_THRESHOLD = 7.5;' in html
    assert 'function buildTopLiveGroup' in html
    assert 'function refreshTopGroups' in html
    assert '表2 · 涨停股10天回撤' not in html
    assert '表3 · 涨停股10天累计' not in html
    assert '表4 · 阴线形态分组' not in html
    assert '阴线都不碰&阴线都不碰' in html
    assert '阴线都向上' in html
    assert '阴线都不碰' in html
    assert '阴线都向上&阴线都不碰' in html
    assert 'stocks.filter(s => s.all_yin_up && s.all_yin_no_touch)' in html
    assert 'stocks.filter(s => s.all_yin_up)' in html
    assert 'stocks.filter(s => s.all_yin_no_touch)' in html
    assert 'stocks.filter(s => s.ref_high_gain_ok && !s.ref_limit_up)' in html
    assert 'hideTopLiveGroup: true' in html
    assert 'excludeTopLiveFromTable: true' in html
    assert 'const topLiveCodes = new Set(getTopLiveStocks(stocks).map(function(s) { return s.code; }));' in html
    assert 'excludeTopLiveFromTable: false' in html
    assert '筛选 all_yin_no_touch=true' not in html
    assert '筛选 all_yin_up=true 且 all_yin_no_touch=true' not in html
    assert '筛选 all_yin_up=true' not in html
    assert 'all_yin_jpyk_next' not in html
    assert 'all_yin_pull_next' not in html
    assert 'all_yin_pull_prev_or_next' not in html
    assert 'yhq-badge-yhq' not in html
    assert 'yhq-badge-yhl' not in html
    assert 'yhq-badge-yqhl' not in html
    assert 'yhq-badge-up' in html
    assert 'yhq-badge-no-touch' in html
    assert 'yhq-badge-both' in html
    assert 'white-space: nowrap;' in html
    assert '.yhq-badge { white-space: nowrap !important; }' in html
    assert '导出Excel' in html
    assert 'function exportTableToExcel' in html
    assert 'exportKey: \'limit_up_drawdown\'' in html
    assert 'function getDisplayChangeValue' in html
    assert 'function getShanghaiNowParts()' in html
    assert 'function isTradingSessionNow()' in html
    assert 'function shouldUseRealtimeStocks()' in html
    assert 'function getDrawdown10Value' in html
    assert 'function getTableSortState' in html
    assert 'function toggleDrawdownSort' in html
    assert 'function gains10Class' in html
    assert 'max_drawdown_10d' in html
    assert 'stocks.filter(s => s.ref_limit_up)' in html
    assert '按10天累计由低到高排序' in html
    assert "'#', '股票名称', '股票代码', '涨跌', '阴线', '拉升', '市值', '10天回撤', finalTitle" in html
    assert '<th>市值</th>' in html
    assert '<th class="sortable-drawdown"' in html
    assert 'table-head-sort' in html
    assert 'table-head-sort-label' in html
    assert '<span class="table-head-sort"><span class="table-head-sort-label">10天回撤</span><button type="button" class="sort-toggle-btn"' in html
    assert '↕' in html
    assert "formatMarketCap(s.market_cap)" in html
    assert 'return Math.abs(toNumber(value, 0)) >= 10 ? \'highlight-red\' : \'highlight-purple\';' in html
    assert 'return toNumber(value, 0) > 35 ? \'highlight-red\' : \'metric-dark\';' in html
    assert '_drawdownSortStates = {};' in html
    assert "扫描耗时" in html
    assert "上次扫描时间:" in html
    assert "scanInfoText" in html
    assert "/api/yhy/scan-info?date=${currentDate}" in html
    assert "/api/yhy/tasks?date=${currentDate}" in html
    assert "/api/yhy/tasks/add" in html
    assert "/api/yhy/tasks/remove" in html
    assert "setInterval(refreshScanInfo, 1000)" in html
    assert "setInterval(refreshRealtime, 3000)" in html
    assert "if (!shouldUseRealtimeStocks()) { console.log('[refreshRealtime] 跳过：非盘中最新日期'); return; }" in html
    assert "timeZone: 'Asia/Shanghai'" in html
    assert "const isTradingTime = (totalMinutes >= 570 && totalMinutes < 690) || (totalMinutes >= 780 && totalMinutes < 900);" in html
    assert "setInterval(silentRefreshAll, 3000)" not in html
    assert "今日任务列表" in html
    assert '.task-section { border-color: #ddd6fe; box-shadow: 0 4px 14px rgba(124,58,237,0.12); }' in html
    assert '.task-section .table-header { background: linear-gradient(135deg, #8b5cf6, #7c3aed); }' in html
    assert '.task-section .group-header { background: #f5f3ff; }' in html
    assert "layout.appendChild(buildStackedColumn(" in html
    assert "'table3'," in html
    assert "一字特征" in html
    assert "厂子特征" in html
    assert "横盘调整特征" in html
    assert "未添加,请完成今日任务!" in html
    assert "contextmenu" in html
    assert "添加一字特征" in html
    assert "添加厂子特征" in html
    assert "添加横盘调整特征" in html
    assert "删除股票" in html
    assert "确认删除该股票吗？" in html
    assert "10天K线特征确认" in html
    assert "10天涨幅确认" in html
    assert "涨幅过大" in html
    assert "getVisibleTaskStocksForCurrentPage" in html
    assert "return '阴后强';" in html
    assert '.task-ios-body .task-kline-list {' in html
    assert 'color: #7c3aed;' in html
    assert 'font-weight: 700;' in html
    assert 'bodyHtml: \'<div class="task-warn-text">涨幅过大</div>\'' in html
    assert '.task-ios-body .task-warn-text { color: #dc2626; font-weight: 800; font-size: 18px;' in html
    assert "myStock:task" in html
    assert '极品K：光脚阳线、光头阳线、涨停、十字星' not in html
    assert '<th>极品K</th>' not in html
    assert '<th>参考</th>' not in html
    assert '参考日K线特征' not in html

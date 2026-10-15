import os
import sys
import json
import time
from io import BytesIO
from zipfile import ZipFile
from unittest.mock import MagicMock, patch

import openpyxl
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def client():
    import app as flask_app
    flask_app.app.config['TESTING'] = True
    with flask_app.app.test_client() as c:
        yield c


def test_ztorgj_realtime_batches_tencent_requests(client):
    """涨停或光脚实时接口应分批请求腾讯，避免超长 URL 导致全量实时涨幅为空"""
    import app as flask_app

    codes = [f'600{i:03d}' for i in range(200)] + ['603311', '000001']

    def fake_get(url, timeout=10):
        query = url.split('q=', 1)[1]
        tencent_codes = [item for item in query.split(',') if item]
        lines = []
        for tc in tencent_codes:
            code = tc[2:]
            price = '105.45' if code == '603311' else '101.00'
            pre_close = '100.00'
            lines.append(f'v_{tc}="1~测试~{code}~{price}~{pre_close}~100";')
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '\n'.join(lines)
        return resp

    with patch.object(flask_app.requests, 'get', side_effect=fake_get) as mock_get:
        resp = client.post('/api/ztorgj/realtime', json={'codes': codes})
        data = json.loads(resp.data)

    assert data['success'] is True
    assert mock_get.call_count == 2
    assert data['data']['603311']['change'] == pytest.approx(5.45)
    assert data['data']['000001']['change'] == pytest.approx(1.0)
    assert data['debug']['chunk_count'] == 2
    assert data['debug']['chunk_size'] == 200
    assert data['debug']['parsed_count'] == len(codes)
    assert data['debug']['missing_count'] == 0


def test_tencent_batch_quotes_chunks_long_code_lists(client):
    """通用腾讯批量行情函数应分批，避免首页和复用页面出现 414"""
    import app as flask_app

    codes = [f'600{i:03d}' for i in range(201)]

    def fake_get(url, timeout=3):
        query = url.split('q=', 1)[1]
        tencent_codes = [item for item in query.split(',') if item]
        lines = []
        for tc in tencent_codes:
            code = tc[2:]
            fields = ['1', '测试', code, '101.00', '100.00'] + ['0'] * 27 + ['1.00']
            lines.append(f'v_{tc}="' + '~'.join(fields) + '";')
        resp = MagicMock()
        resp.status_code = 200
        resp.text = ''.join(lines)
        return resp

    with patch.object(flask_app.requests, 'get', side_effect=fake_get) as mock_get:
        result = flask_app.get_tencent_batch_quotes(codes)

    assert mock_get.call_count == 2
    assert len(result) == len(codes)
    assert result['600000']['change_pct'] == pytest.approx(1.0)
    assert result['600200']['price'] == pytest.approx(101.0)


def test_ztorgj_stocks_exposes_high_gain_fields(client):
    """涨停或光脚接口应同时返回含 huiche6 在内的九套数据并透传新字段"""
    mock_r = MagicMock()
    existing_keys = {
        'ztorgj:20260421',
        'yxcs:20260421',
        'ruoyinxian:20260421',
        'ztgjcs:20260421',
        '20tupo:20260421',
        'yangxian:20260421',
        'yangxian1:20260421',
        '5bupeng:20260421',
        'lsyin:20260421',
        'lsyin:quanyang:20260421',
        'huiche6:20260421',
        'lstz:20260421',
    }
    mock_r.exists.side_effect = lambda key: key in existing_keys
    mock_r.type.side_effect = lambda key: 'hash' if key in existing_keys else None

    scan_info = json.dumps({
        'last_scan_duration_sec': 1.671,
        'last_scan_end_time': '2026-04-23 22:07:26',
    }, ensure_ascii=False)
    snapshot_info = json.dumps({
        'last_snapshot_time': '2026-04-23 22:07:28',
        'last_snapshot_status': 'success',
        'last_snapshot_count': 91,
        'last_snapshot_duration_ms': 38.6,
        'last_snapshot_error': '',
        'overall_status': 'partial_fail',
        'overall_error': 'huiche6 pipeline timeout',
        'snapshot_interval_sec': 5
    }, ensure_ascii=False)

    def hscan_side_effect(key, cursor, count=100):
        payloads = {
            'ztorgj:20260421': {
                '000001': json.dumps({
                    'code': '000001',
                    'name': '平安银行',
                    'ref_gain': 3.21,
                    'weak_yin_20d': 2,
                    'weak_yin_10d': 1,
                    'high_gain_10d_count': 6,
                    'high_gain_10d_gte_5': 1,
                    'high_gain5_and_yin_pull': 1
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            },
            'yxcs:20260421': {
                '000002': json.dumps({
                    'code': '000002',
                    'name': '万科A',
                    'yin_count_20': 4,
                    'weak_yin_20d': 2,
                    'ref_gain': 5.67,
                    'max_gain': 7.1,
                    'is_limit_up': 1,
                    'is_bare_foot': 0,
                    'is_bare_foot_yang': 0,
                    'is_ref_yang': 1,
                    'ref_is_limit_up': True,
                    'ref_high_gain_ok': True,
                    'ref_high_gain_gte_6_5_and_yang': True,
                    'ref_low_above_ma5': 1,
                    'ref_high_gain_gte_6_5_yang_low_above_ma5': True,
                    'is_high_gain': 1,
                    'yin_count_10d': 2,
                    'yin_close_below_ma5_count_10d': 1,
                    'yin_close_below_ma5_lte_2_10d': 1,
                    'yin_body_pct_sum_10d': 2.86,
                    'is_quanyang_10d': 0,
                    'dingtalk_pushed': 1,
                    'dingtalk_push_time': '2026-04-21 09:25:00',
                    'dingtalk_push_title': '股票预警：阴线次数等于4'
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            },
            'ruoyinxian:20260421': {
                '000008': json.dumps({
                    'code': '000008',
                    'name': '神州高铁',
                    'stock_code_with_market': '000008.SZ',
                    'board_type': '主板',
                    'limit_ratio': 10,
                    'ref_gain': 6.78,
                    'max_gain': 7.26,
                    'is_limit_up': 1,
                    'is_bare_foot': 1,
                    'is_bare_foot_yang': 1,
                    'is_high_gain': 1,
                    'is_ref_yang': 1,
                    'is_20d_new_high': 1,
                    'yin_count_20': 4,
                    'weak_yin_20d': 3,
                    'low_above_ma5_20d_count': 14,
                    'cum_gain_20d_pct': 18.25
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            },
            'ztgjcs:20260421': {
                '000003': json.dumps({
                    'code': '000003',
                    'name': 'PT测试',
                    'limit_up_count_20': 3,
                    'bare_foot_count_20': 2,
                    'weak_yin_20d': 1,
                    'ref_gain': 4.32,
                    'max_gain': 7.05,
                    'is_limit_up': 0,
                    'is_bare_foot': 1,
                    'is_high_gain': 1
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            },
            '20tupo:20260421': {
                '000004': json.dumps({
                    'code': '000004',
                    'name': '国华网安',
                    'ref_gain': 10.01,
                    'today_change': 9.87,
                    'current_price': 15.21,
                    'max_gain': 10.35,
                    'ref_close': 15.23,
                    'max_close_20': 15.23,
                    'gain_20d': 18.6,
                    'limit_up_count_20': 4,
                    'is_limit_up': 1,
                    'ref_is_limit_up': True,
                    'is_ref_yang': 1,
                    'ref_high_gain_ok': True,
                    'ref_high_gain_gte_6_5_and_yang': True,
                    'ref_low_above_ma5': 1,
                    'ref_high_gain_gte_6_5_yang_low_above_ma5': True,
                    'yin_count_10d': 2,
                    'yin_close_below_ma5_count_10d': 1,
                    'yin_close_below_ma5_lte_2_10d': 1,
                    'yin_body_pct_sum_10d': 3.45,
                    'is_quanyang_10d': 0
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            },
            'yangxian:20260421': {
                '000005': json.dumps({
                    'code': '000005',
                    'name': '世纪星源',
                    'ref_gain': 9.98,
                    'max_gain': 10.24,
                    'consecutive_days': 7,
                    'is_limit_up': 1,
                    'is_high_gain': 1
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            },
            'yangxian1:20260421': {
                '000006': json.dumps({
                    'code': '000006',
                    'name': '深振业A',
                    'ref_gain': 8.88,
                    'max_gain': 9.25,
                    'consecutive_days': 6,
                    'is_limit_up': 0,
                    'is_high_gain': 1
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            },
            '5bupeng:20260421': {
                '000007': json.dumps({
                    'code': '000007',
                    'name': '全新好',
                    'stock_code_with_market': 'sz000007',
                    'board_type': '主板',
                    'limit_ratio': 10,
                    'ref_gain': 6.12,
                    'max_gain': 6.98,
                    'strong_no_touch_10d_count': 5,
                    'low_above_ma5_10d_count': 5,
                    'is_limit_up': 1,
                    'is_bare_foot_yang': 0,
                    'ref_high_gain_gte_685': 1
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            },
            'lsyin:20260421': {
                '000010': json.dumps({
                    'code': '000010',
                    'name': '美丽生态',
                    'stock_code_with_market': '000010.SZ',
                    'board_type': '主板',
                    'limit_ratio': 10,
                    'ref_gain': 5.88,
                    'max_gain': 8.26,
                    'is_limit_up': 0,
                    'is_high_gain': 1,
                    'yin_count_10d': 3,
                    'lsyin_sum': 4.321,
                    'lsyin_avg': 1.440333,
                    'high_gain_10d_gte_5': 1,
                    'yin_pull_prev_or_next': 1,
                    'high_gain5_and_yin_pull': 1,
                    'is_quanyang_10d': 0
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            },
            'lsyin:quanyang:20260421': {
                '000011': json.dumps({
                    'code': '000011',
                    'name': '深物业A',
                    'stock_code_with_market': '000011.SZ',
                    'board_type': '主板',
                    'limit_ratio': 10,
                    'ref_gain': 7.77,
                    'max_gain': 8.58,
                    'is_limit_up': 0,
                    'is_high_gain': 1,
                    'yin_count_10d': 0,
                    'lsyin_sum': 0,
                    'lsyin_avg': 0,
                    'is_quanyang_10d': 1,
                    'lsyin_group': 'quanyang'
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            },
            'huiche6:20260421': {
                '000009': json.dumps({
                    'code': '000009',
                    'name': '中国宝安',
                    'stock_code_with_market': '000009.SZ',
                    'board_type': '主板',
                    'limit_ratio': 10,
                    'ref_gain': 6.66,
                    'max_gain': 7.25,
                    'is_limit_up': 0,
                    'is_bare_foot': 0,
                    'is_bare_foot_yang': 0,
                    'is_high_gain': 1,
                    'ref_high_gain_shape_ok': 1,
                    'max_drawdown_10d': 5.45,
                    'low_above_ma5_count': 8,
                    'cum_high_to_ma5_pct': 12.48,
                    'high_gain_10d_count': 5,
                    'all_yin_cross_star': 1,
                    'all_yin_no_touch': 1,
                    'all_yin_up': 1,
                    'days_since_top_day': 3,
                    'top_day_close_below_count': 1,
                    'adjust_no_break': 0,
                    'strong_adjust_k_pct': 66.67,
                    'is_breakout': 1,
                    'breakout_gain_pct': 4.56,
                    'gain_10d_pct': 21.35,
                    'yin_count_10': 6
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            },
            'lstz:20260421': {
                '000012': json.dumps({
                    'code': '000012',
                    'name': '南玻A',
                    'stock_code_with_market': '000012.SZ',
                    'board_type': '主板',
                    'limit_ratio': 10,
                    'ref_gain': 4.11,
                    'max_gain': 6.88,
                    'is_limit_up': 0,
                    'is_bare_foot': 1,
                    'is_high_gain': 1,
                    'lstz_new_high': 1,
                    'lstz_new_high_pct': 3.21,
                    'yin_below_ma5_count': 1,
                    'low_above_ma5_20d_count': 16,
                    'cum_gain_20d_pct': 22.34
                }, ensure_ascii=False),
                '_scan_info': scan_info,
                '_snapshot_info': snapshot_info
            }
        }
        return 0, payloads[key]

    mock_r.hscan.side_effect = hscan_side_effect

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ztorgj/stocks/2026-04-21')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['datasets']['ztorgj'][0]['high_gain_10d_count'] == 6
    assert data['datasets']['ztorgj'][0]['high_gain_10d_gte_5'] == 1
    assert data['datasets']['ztorgj'][0]['high_gain5_and_yin_pull'] == 1
    assert data['datasets']['yxcs'][0]['yin_count_20'] == 4
    assert data['datasets']['yxcs'][0]['is_bare_foot'] == 0
    assert data['datasets']['yxcs'][0]['is_ref_yang'] == 1
    assert data['datasets']['yxcs'][0]['ref_is_limit_up'] is True
    assert data['datasets']['yxcs'][0]['ref_high_gain_ok'] is True
    assert data['datasets']['yxcs'][0]['ref_high_gain_gte_6_5_and_yang'] is True
    assert data['datasets']['yxcs'][0]['ref_low_above_ma5'] == 1
    assert data['datasets']['yxcs'][0]['ref_high_gain_gte_6_5_yang_low_above_ma5'] is True
    assert data['datasets']['yxcs'][0]['yin_count_10d'] == 2
    assert data['datasets']['yxcs'][0]['yin_close_below_ma5_count_10d'] == 1
    assert data['datasets']['yxcs'][0]['yin_close_below_ma5_lte_2_10d'] == 1
    assert data['datasets']['yxcs'][0]['yin_body_pct_sum_10d'] == pytest.approx(2.86)
    assert data['datasets']['yxcs'][0]['is_high_gain'] == 1
    assert data['datasets']['yxcs'][0]['dingtalk_pushed'] == 1
    assert data['datasets']['yxcs'][0]['dingtalk_push_title'] == '股票预警：阴线次数等于4'
    assert data['datasets']['ruoyinxian'][0]['weak_yin_20d'] == 3
    assert data['datasets']['ruoyinxian'][0]['is_bare_foot'] == 1
    assert data['datasets']['ruoyinxian'][0]['is_bare_foot_yang'] == 1
    assert data['datasets']['ruoyinxian'][0]['is_ref_yang'] == 1
    assert data['datasets']['ruoyinxian'][0]['is_20d_new_high'] == 1
    assert data['datasets']['ruoyinxian'][0]['low_above_ma5_20d_count'] == 14
    assert data['datasets']['ruoyinxian'][0]['cum_gain_20d_pct'] == pytest.approx(18.25)
    assert data['datasets']['ztgjcs'][0]['limit_up_count_20'] == 3
    assert data['datasets']['ztgjcs'][0]['bare_foot_count_20'] == 2
    assert data['datasets']['ztgjcs'][0]['is_bare_foot'] == 1
    assert data['datasets']['ztgjcs'][0]['is_bare_foot_yang'] == 0
    assert data['datasets']['20tupo'][0]['gain_20d'] == pytest.approx(18.6)
    assert data['datasets']['20tupo'][0]['max_close_20'] == pytest.approx(15.23)
    assert data['datasets']['20tupo'][0]['limit_up_count_20'] == 4
    assert data['datasets']['20tupo'][0]['today_change'] == pytest.approx(9.87)
    assert data['datasets']['20tupo'][0]['current_price'] == pytest.approx(15.21)
    assert data['datasets']['20tupo'][0]['ref_is_limit_up'] is True
    assert data['datasets']['20tupo'][0]['is_ref_yang'] == 1
    assert data['datasets']['20tupo'][0]['ref_high_gain_ok'] is True
    assert data['datasets']['20tupo'][0]['ref_high_gain_gte_6_5_and_yang'] is True
    assert data['datasets']['20tupo'][0]['ref_low_above_ma5'] == 1
    assert data['datasets']['20tupo'][0]['ref_high_gain_gte_6_5_yang_low_above_ma5'] is True
    assert data['datasets']['20tupo'][0]['yin_close_below_ma5_count_10d'] == 1
    assert data['datasets']['20tupo'][0]['yin_close_below_ma5_lte_2_10d'] == 1
    assert data['datasets']['20tupo'][0]['yin_body_pct_sum_10d'] == pytest.approx(3.45)
    assert data['datasets']['20tupo'][0]['is_quanyang_10d'] == 0
    assert data['datasets']['yangxian'][0]['consecutive_days'] == 7
    assert data['datasets']['yangxian'][0]['is_limit_up'] == 1
    assert data['datasets']['yangxian'][0]['is_high_gain'] == 1
    assert data['datasets']['yangxian1'][0]['consecutive_days'] == 6
    assert data['datasets']['yangxian1'][0]['is_high_gain'] == 1
    assert data['datasets']['5bupeng'][0]['strong_no_touch_10d_count'] == 5
    assert data['datasets']['5bupeng'][0]['low_above_ma5_10d_count'] == 5
    assert data['datasets']['5bupeng'][0]['max_gain'] == pytest.approx(6.98)
    assert data['datasets']['5bupeng'][0]['is_limit_up'] == 1
    assert data['datasets']['5bupeng'][0]['ref_high_gain_gte_685'] == 1
    assert data['datasets']['5bupeng'][0]['is_high_gain'] == 1
    assert data['datasets']['lsyin'][0]['lsyin_sum'] == pytest.approx(4.321)
    assert data['datasets']['lsyin'][0]['lsyin_avg'] == pytest.approx(1.440333)
    assert data['datasets']['lsyin'][0]['yin_count_10d'] == 3
    assert data['datasets']['lsyin'][0]['high_gain_10d_gte_5'] == 1
    assert data['datasets']['lsyin'][0]['yin_pull_prev_or_next'] == 1
    assert data['datasets']['lsyin'][0]['high_gain5_and_yin_pull'] == 1
    assert data['datasets']['lsyin'][0]['is_quanyang_10d'] == 0
    assert data['datasets']['lsyin_quanyang'][0]['ref_gain'] == pytest.approx(7.77)
    assert data['datasets']['lsyin_quanyang'][0]['yin_count_10d'] == 0
    assert data['datasets']['lsyin_quanyang'][0]['lsyin_avg'] == 0
    assert data['datasets']['lsyin_quanyang'][0]['is_quanyang_10d'] == 1
    assert data['datasets']['lsyin_quanyang'][0]['lsyin_group'] == 'quanyang'
    assert data['datasets']['huiche6'][0]['is_high_gain'] == 1
    assert data['datasets']['huiche6'][0]['ref_high_gain_shape_ok'] == 1
    assert data['datasets']['huiche6'][0]['max_drawdown_10d'] == pytest.approx(5.45)
    assert data['datasets']['huiche6'][0]['low_above_ma5_count'] == 8
    assert data['datasets']['huiche6'][0]['cum_high_to_ma5_pct'] == pytest.approx(12.48)
    assert data['datasets']['huiche6'][0]['all_yin_cross_star'] == 1
    assert data['datasets']['huiche6'][0]['all_yin_no_touch'] == 1
    assert data['datasets']['huiche6'][0]['all_yin_up'] == 1
    assert data['datasets']['huiche6'][0]['days_since_top_day'] == 3
    assert data['datasets']['huiche6'][0]['top_day_close_below_count'] == 1
    assert data['datasets']['huiche6'][0]['adjust_no_break'] == 0
    assert data['datasets']['huiche6'][0]['strong_adjust_k_pct'] == pytest.approx(66.67)
    assert data['datasets']['huiche6'][0]['is_breakout'] == 1
    assert data['datasets']['huiche6'][0]['breakout_gain_pct'] == pytest.approx(4.56)
    assert data['datasets']['huiche6'][0]['gain_10d_pct'] == pytest.approx(21.35)
    assert data['datasets']['huiche6'][0]['yin_count_10'] == 6
    assert data['datasets']['lstz'][0]['low_above_ma5_20d_count'] == 16
    assert data['datasets']['lstz'][0]['cum_gain_20d_pct'] == pytest.approx(22.34)
    assert all(item['code'] != '_snapshot_info' for items in data['datasets'].values() for item in items)
    assert data['scan_info']['last_scan_duration_sec'] == pytest.approx(1.671)
    assert data['scan_info']['last_scan_end_time'] == '2026-04-23 22:07:26'
    assert data['snapshot_info']['last_snapshot_time'] == '2026-04-23 22:07:28'
    assert data['snapshot_info']['last_snapshot_status'] == 'success'
    assert data['snapshot_info']['last_snapshot_count'] == 91
    assert data['snapshot_info']['last_snapshot_duration_ms'] == pytest.approx(38.6)
    assert data['snapshot_info']['overall_status'] == 'partial_fail'
    assert data['snapshot_info']['overall_error'] == 'huiche6 pipeline timeout'
    assert data['snapshot_info']['snapshot_interval_sec'] == 5
    assert data['snapshot_version'] == '2026-04-23 22:07:28'
    assert data['is_snapshot_stable'] is False
    assert data['snapshot_reason'] == 'overall_snapshot_not_success'


def _mock_ruoyinxian_redis(payloads):
    mock_r = MagicMock()
    mock_r.exists.side_effect = lambda key: key == 'ruoyinxian:20260421'
    mock_r.type.side_effect = lambda key: 'hash' if key == 'ruoyinxian:20260421' else None
    mock_r.hscan.return_value = (0, payloads)
    return mock_r


def _mock_kline_df(code='000001.SZ'):
    return pd.DataFrame([
        {'ts_code': code, 'trade_date': '20260417', 'open': 10.0, 'high': 10.8, 'low': 9.8, 'close': 10.5},
        {'ts_code': code, 'trade_date': '20260420', 'open': 10.5, 'high': 11.2, 'low': 10.2, 'close': 10.9},
        {'ts_code': code, 'trade_date': '20260421', 'open': 10.9, 'high': 11.4, 'low': 10.7, 'close': 11.1},
    ])


def _mock_qmt_kline_payload(code='000001.SZ', name='测试股票'):
    rows = []
    for row in _mock_kline_df(code).to_dict('records'):
        rows.append({
            'ts_code': code,
            'trade_date': row['trade_date'],
            'open': row['open'],
            'high': row['high'],
            'low': row['low'],
            'close': row['close'],
            'pre_close': 0,
            'pct_chg': 0,
            'vol': 0,
            'amount': 0,
        })
    return json.dumps({
        'code': code.split('.')[0],
        'ts_code': code,
        'name': name,
        'rows': rows,
    }, ensure_ascii=False)


def test_ztorgj_weak20_kline_excel_exports_current_new_high_list(client):
    """20日弱阴线首列导出应支持按当前新高筛选并插入K线图片"""
    payloads = {
        '000001': json.dumps({'code': '000001', 'name': '平安银行', 'weak_yin_20d': 2, 'yin_count_20': 3, 'is_20d_new_high': 1}, ensure_ascii=False),
        '000002': json.dumps({'code': '000002', 'name': '万科A', 'weak_yin_20d': 1, 'yin_count_20': 4, 'is_20d_new_high': 0}, ensure_ascii=False),
        '000003': json.dumps({'code': '000003', 'name': '国华网安', 'weak_yin_20d': 1, 'yin_count_20': 2, 'is_20d_new_high': 1}, ensure_ascii=False),
    }
    mock_r = _mock_ruoyinxian_redis(payloads)
    qmt_payloads = {
        ('qmt:kline:20260421', '000001'): _mock_qmt_kline_payload('000001.SZ', '平安银行'),
        ('qmt:kline:20260421', '000003'): _mock_qmt_kline_payload('000003.SZ', '国华网安'),
    }
    mock_r.hget.side_effect = lambda key, field: qmt_payloads.get((key, field))

    with patch('app.get_redis_client', return_value=mock_r), patch('app.ts.pro_api') as pro_api:
        resp = client.get('/api/ztorgj/weak20/kline-excel?date=2026-04-21&new_high_only=1')

    pro_api.assert_not_called()
    assert resp.status_code == 200
    assert resp.headers['Content-Disposition'].startswith('attachment;')
    assert resp.headers['Content-Type'].startswith('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    wb = openpyxl.load_workbook(BytesIO(resp.data))
    ws = wb.active
    assert ws['A1'].value == '股票名称'
    assert ws['B1'].value == 'K线图'
    assert [ws['A2'].value, ws['A3'].value] == ['国华网安', '平安银行']
    assert ws['A4'].value is None

    with ZipFile(BytesIO(resp.data)) as zf:
        media_files = [name for name in zf.namelist() if name.startswith('xl/media/') and name.endswith('.png')]
    assert len(media_files) == 2


def test_ztorgj_weak20_kline_excel_keeps_row_when_one_kline_fails(client):
    """单只股票K线失败时应保留该股票行并写入失败提示"""
    payloads = {
        '000001': json.dumps({'code': '000001', 'name': '平安银行', 'weak_yin_20d': 1, 'yin_count_20': 3, 'is_20d_new_high': 1}, ensure_ascii=False),
        '000002': json.dumps({'code': '000002', 'name': '万科A', 'weak_yin_20d': 2, 'yin_count_20': 4, 'is_20d_new_high': 0}, ensure_ascii=False),
    }
    mock_r = _mock_ruoyinxian_redis(payloads)
    qmt_payloads = {
        ('qmt:kline:20260421', '000001'): _mock_qmt_kline_payload('000001.SZ', '平安银行'),
    }
    mock_r.hget.side_effect = lambda key, field: qmt_payloads.get((key, field))

    with patch('app.get_redis_client', return_value=mock_r), patch('app.ts.pro_api') as pro_api:
        resp = client.get('/api/ztorgj/weak20/kline-excel?date=2026-04-21')

    pro_api.assert_not_called()
    assert resp.status_code == 200
    wb = openpyxl.load_workbook(BytesIO(resp.data))
    ws = wb.active
    assert [ws['A2'].value, ws['A3'].value] == ['平安银行', '万科A']
    assert ws['B3'].value == 'K线获取失败'

    with ZipFile(BytesIO(resp.data)) as zf:
        media_files = [name for name in zf.namelist() if name.startswith('xl/media/') and name.endswith('.png')]
    assert len(media_files) == 1


def test_ztorgj_weak20_kline_excel_job_reports_progress_and_downloads(client):
    """异步导出任务应能轮询进度，并在完成后下载Excel"""
    import app as flask_app
    flask_app._ztorgj_weak20_excel_jobs.clear()
    payloads = {
        '000001': json.dumps({'code': '000001', 'name': '平安银行', 'weak_yin_20d': 1, 'yin_count_20': 3, 'is_20d_new_high': 1}, ensure_ascii=False),
        '000002': json.dumps({'code': '000002', 'name': '万科A', 'weak_yin_20d': 2, 'yin_count_20': 4, 'is_20d_new_high': 1}, ensure_ascii=False),
    }
    mock_r = _mock_ruoyinxian_redis(payloads)
    qmt_payloads = {
        ('qmt:kline:20260421', '000001'): _mock_qmt_kline_payload('000001.SZ', '平安银行'),
        ('qmt:kline:20260421', '000002'): _mock_qmt_kline_payload('000002.SZ', '万科A'),
    }
    mock_r.hget.side_effect = lambda key, field: qmt_payloads.get((key, field))

    with patch('app.get_redis_client', return_value=mock_r), patch('app.ts.pro_api') as pro_api:
        start_resp = client.post('/api/ztorgj/weak20/kline-excel/start?date=2026-04-21&new_high_only=1')
        assert start_resp.status_code == 200
        start_data = json.loads(start_resp.data)
        assert start_data['success'] is True
        job_id = start_data['job_id']

        progress_data = {}
        for _ in range(40):
            progress_resp = client.get(f'/api/ztorgj/weak20/kline-excel/progress/{job_id}')
            assert progress_resp.status_code == 200
            progress_data = json.loads(progress_resp.data)
            assert progress_data['success'] is True
            assert progress_data['total'] in (0, 2)
            assert 0 <= progress_data['percent'] <= 100
            if progress_data['status'] == 'completed':
                break
            time.sleep(0.1)

        assert progress_data['status'] == 'completed'
        assert progress_data['total'] == 2
        assert progress_data['current'] == 2
        assert progress_data['percent'] == 100
        download_resp = client.get(f'/api/ztorgj/weak20/kline-excel/download/{job_id}')

    pro_api.assert_not_called()
    assert download_resp.status_code == 200
    wb = openpyxl.load_workbook(BytesIO(download_resp.data))
    assert wb.active['A2'].value == '平安银行'


def test_ztorgj_realtime_returns_latest_change(client):
    """涨停或光脚实时接口应返回按代码映射的最新涨跌幅"""
    mock_response = MagicMock()
    mock_response.text = '\n'.join([
        'v_sz000001="51~平安银行~000001~11.23~10.00~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0";',
        'v_sz000002="51~万科A~000002~9.50~10.00~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0";'
    ])
    mock_response.encoding = 'gbk'

    with patch('app.requests.get', return_value=mock_response):
        resp = client.post('/api/ztorgj/realtime', json={'codes': ['000001', '000002']})
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['data']['000001']['change'] == pytest.approx(12.3)
    assert data['data']['000002']['change'] == pytest.approx(-5.0)


def test_ztorgj_template_contains_high_gain_section(client):
    """页面模板应包含新增的 阴线大于5 tab、强不碰次数 tab 和独立任务来源"""
    resp = client.get('/涨停或光脚')
    html = resp.get_data(as_text=True)

    assert "20日阴线次数" in html
    assert "extraClass: 'yin20-main-column'" in html
    assert ".metric-column.yin20-main-column .stock-row.dual-counts span:nth-child(3)" in html
    assert "color: #dc2626;" in html
    assert "20日涨停次数" in html
    assert "20日光脚次数" in html
    assert "20日弱阴线" in html
    assert "20日突破" in html
    assert (
        html.index("{ key: 'yin20', label: '20日阴线次数'")
        < html.index("{ key: 'break20', label: '20日突破'")
        < html.index("{ key: 'weak20', label: '20日弱阴线'")
        < html.index("{ key: 'limit20', label: '20日涨停次数'")
        < html.index("{ key: 'bare20', label: '20日光脚次数'")
    )
    assert "连续阳线" in html
    assert "连续阳线或者阴线大于等于5日均价" in html
    assert "回撤小于6.6" in html
    assert "强不碰次数" in html
    assert "阴线统计" in html
    assert "10日弱阴线" not in html
    assert "yxcs" in html
    assert "ruoyinxian" in html
    assert "ztgjcs" in html
    assert "20tupo" in html
    assert "yangxian" in html
    assert "yangxian1" in html
    assert "huiche6" in html
    assert "lsyin" in html
    assert "lsyin_quanyang" in html
    assert "'5bupeng': []" in html
    assert "'huiche6': []" in html
    assert "ruoyinxian: []" in html
    assert "metricKey: 'high_gain_10d_count'" in html
    assert "metricLabel: '高涨次数'" in html
    assert "high_gain_10d_count" in html
    assert "label: '全部股票'" in html
    assert html.count("sub: ''") >= 4
    assert "label: '拉升涨停'" in html
    assert "label: '拉升调整大于5'" not in html
    assert "label: '拉升且阳线'" in html
    assert "strong_adjust_k_pct" in html
    assert "label: '拉升非涨停'" in html
    assert "sub: '全部股票按高涨次数由高到低分组显示'" not in html
    assert "sub: '仅显示参考日光脚阳线股票，按高涨次数由高到低分组显示'" not in html
    assert "background: linear-gradient(135deg, #8b5cf6, #db2777);" in html
    assert "datasetKey: '5bupeng'" in html
    assert "key: 'lsyinStats'" in html
    assert "datasetKey: 'lsyin'" in html
    assert "datasetKey: 'lsyin_quanyang'" in html
    assert "metricKey: 'lsyin_avg'" in html
    assert "metricLabel: '阴线均值'" in html
    assert "全是阳线" in html
    assert "label: '涨停股'" in html
    assert "label: '非涨停股'" in html
    assert "label: '当日光脚阳线', sub:" not in html
    assert "label: '5拉升'" in html
    assert "cls: 'metric-5ls'" in html
    assert "筛选 high_gain_10d_gte_5 = 1，按20日弱阴线由少到大分组显示" in html
    assert "filter: function(s) { return Number(s.high_gain_10d_gte_5 || 0) === 1; }" in html
    assert "groupFn: groupByWeakYin20" in html
    assert "lsyin 组内涨停股按阴线均值由高到低显示" in html
    assert "lsyin 组内非涨停股按阴线均值由高到低显示" in html
    assert "Number(stock.is_limit_up || 0) === 1" in html
    assert "Number(stock.is_limit_up || 0) !== 1" in html
    assert "function renderLsyinBadges(stock)" in html
    assert "lsyin-badge lsyin-badge-rise5" in html
    assert "lsyin-badge lsyin-badge-yinpull" in html
    assert "lsyin-badge lsyin-badge-combo" in html
    assert "if (Number(stock && stock.high_gain5_and_yin_pull || 0) === 1)" in html
    assert "return '<span class=\"lsyin-badge lsyin-badge-combo\">5拉升阴前后拉</span>';" in html
    assert ">5拉升<" in html
    assert ">阴前后拉<" in html
    assert ">5拉升阴前后拉<" in html
    assert "stock-badge-row" in html
    assert "s-name-text" in html
    assert "_activeTab !== 'lsyinStats'" in html
    assert ".lsyin-badge-yinpull { background: #b45309; }" in html
    assert "background: linear-gradient(135deg, #dc2626, #f97316);" in html
    assert "background: linear-gradient(135deg, #0891b2, #0f766e);" not in html
    assert "compareRefGainDesc" in html
    assert "datasetKey: 'huiche6'" in html
    assert "datasetKey: 'ruoyinxian'" in html
    assert "metricKey: 'strong_no_touch_10d_count'" in html
    assert "metricKey: 'weak_yin_20d'" in html
    assert "仅显示参考日光脚阳线股票，按高涨次数由高到低分组显示" not in html
    assert "layang" in html
    assert "buildHuiche6AllYinTable" not in html
    assert "调整时间" in html
    assert "{ key: 'is_breakout', label: '是否突破'" not in html
    assert "强调整K%" in html
    assert "is_breakout" not in html
    assert "top_day_close_below_count" in html
    assert "breakout_gain_pct" not in html
    assert "days_since_top_day" in html
    assert "function compareStrongAdjustPctThenAdjustDaysDesc(a, b)" in html
    assert "var diff = Number(b.strong_adjust_k_pct || 0) - Number(a.strong_adjust_k_pct || 0);" in html
    assert "return Number(b.days_since_top_day || 0) - Number(a.days_since_top_day || 0);" in html
    assert "显示全部" in html
    assert "显示突破" not in html
    assert "筛选跌破天数" in html
    assert "跌破天数小于等于1" in html
    assert "跌破天数小于等于2" in html
    assert "筛选调整时间" in html
    assert "function getHuiche6ColumnFilter(columnId)" in html
    assert "function applyHuiche6ColumnFilter(stocks, columnId)" in html
    assert "function showHuiche6AdjustFilterDialog(columnId)" in html
    assert "breakoutOnly" not in html
    assert "maxBelowTopDayCloseCount" in html
    assert "minAdjustDays" in html
    assert "adjust_no_break" in html
    assert "调整不破" in html
    assert "function buildHuiche6AdjustNoBreakTable(stocks)" in html
    assert "Number(s.adjust_no_break || 0) === 1" in html
    assert "Number(s.high_gain_10d_count || 0) >= 3" in html
    assert "Number(s.max_gain || 0) >= 6.50" in html
    assert "Number(s.ref_high_gain_shape_ok || 0) === 1" in html
    assert "Number(s.is_limit_up || 0) === 1" in html
    assert "sortStocksByDisplayGain(matchedStocks)" in html
    assert "阴线特征调整大于5" not in html
    assert "最近10日阴线特征" not in html
    assert "4组股票均按高涨次数由高到低分组显示" not in html
    assert "extraMetrics" in html
    assert "label: '调整时间'" in html
    assert "label: '新高调整>=4天'" in html
    assert "从调整突破第一列筛选 lstz_new_high=1 且调整时间>=4天，按阴线跌破MA5次数分组，组内按涨跌幅由大到小排序" in html
    assert "filter: function(s) { return Number(s.lstz_new_high || 0) === 1 && Number(s.days_since_top_day || 0) >= 4; }" in html
    assert "groupFn: groupByTopDayRefDrawdownNonZeroFirst" in html
    assert "sortFn: compareTopDayRefDrawdownGroups" in html
    assert "function groupByTopDayRefDrawdownNonZeroFirst(sorted)" in html
    assert "function compareTopDayRefDrawdownGroups(a, b)" in html
    assert "if (nonZeroRows.length) groups.push({ label: '回撤非0', stocks: nonZeroRows });" in html
    assert "回撤=0" in html
    assert "回撤>0" not in html
    assert "label: '高涨调整>=4天'" in html
    assert "从调整突破中筛选 is_high_gain=1 且调整时间>=4天，按段内回撤分组排序" in html
    assert "从调整突破中筛选 is_high_gain=1 且调整时间>=4天，按阴线跌破MA5次数分组" not in html
    assert "metricKey: 'yin_below_ma5_count'" in html
    assert "Number(v || 0) + '次'" in html
    assert "filter: function(s) { return Number(s.high_gain_10d_count || 0) >= 4 && Number(s.max_gain || 0) >= 6.50 && Number(s.ref_high_gain_shape_ok || 0) === 1 && Number(s.is_limit_up || 0) === 1; }" in html
    assert "metricKey: 'max_drawdown_10d'" in html
    assert "metricLabel: '调整幅度'" in html
    assert "groupFn: groupAsSingleGroup" in html
    assert "preserveOrder: true" in html
    assert "var aDrawdown = Number(a.max_drawdown_10d || 0);" in html
    assert "var bDrawdown = Number(b.max_drawdown_10d || 0);" in html
    assert "if (aDrawdown <= 0 && bDrawdown > 0) return 1;" in html
    assert "if (aDrawdown > 0 && bDrawdown <= 0) return -1;" in html
    assert "var diff = aDrawdown - bDrawdown;" in html
    assert "if (diff !== 0) return diff;" in html
    assert "return compareDisplayGainDesc(a, b);" in html
    assert "huiche6-adjust-debug" not in html
    assert "return Number(s.layang5days || 0) === 1;" not in html
    assert "filterId: 'huiche6-bare-foot'" in html
    assert "return Number(s.layang || 0) === 1;" in html
    assert "label: '拉升且阳线',\n            sub: '',\n            cls: 'metric-bare',\n            filter: function(s) { return Number(s.layang || 0) === 1; },\n            filterId: 'huiche6-bare-foot'" in html
    assert "label: '拉升非涨停',\n            sub: '',\n            cls: 'metric-limit',\n            filter: function(s) { return Number(s.high_gain_10d_count || 0) >= 4 && Number(s.max_gain || 0) >= 6.50 && Number(s.ref_high_gain_shape_ok || 0) === 1 && Number(s.is_limit_up || 0) === 0; },\n            filterId: 'huiche6-limit-up',\n            metricKey: 'max_drawdown_10d',\n            metricLabel: '调整幅度',\n            asc: true,\n            groupFn: groupAsSingleGroup,\n            sortFn: compareHuiche6AdjustAscZeroLast,\n            preserveOrder: true" in html
    assert html.count("sortFn: compareHuiche6AdjustAscZeroLast") >= 2
    assert "label: '参考日涨停',\n            sub: '',\n            cls: 'metric-limit',\n            filter: function(s) { return Number(s.is_limit_up || 0) === 1; },\n            filterId: 'huiche6-limit-up'" not in html
    assert "{ key: 'strong_adjust_k_pct', label: '强调整K%', fmt: function(v) { return Math.round(Number(v || 0)) + '%'; } }" in html
    assert "metricLabel: '高涨次数'" in html
    assert "fmtVal: function(v) { return Number(v || 0); }" in html
    assert "fmt: function(v) { return Number(v || 0); }" in html
    assert "fmt: function(v) { return Math.round(Number(v || 0)) + '%'; }" in html
    assert "'<span>高涨次数</span>' +" in html
    assert "'<span>调整时间</span>' +" in html
    assert "'<span>是否突破</span>' +" not in html
    assert "'<span>强调整K%</span>';" in html
    assert "'<span class=\"high-gain-count\">' + highGainCount + '</span>' +" in html
    assert "'<span class=\"high-gain-metric\">' + adjustDays + '</span>' +" in html
    assert "'<span class=\"high-gain-metric\">' + strongAdjustPct.toFixed(2) + '%</span>'" not in html
    assert "'<span class=\"high-gain-metric\">' + Math.round(strongAdjustPct) + '%</span>'" in html
    assert "function renderHuiche6StrongAdjustColumn(colDiv, col, showDualCounts)" not in html
    assert "var outerGroups = groupByExactDesc((col.source || []).slice(), 'strong_adjust_k_pct');" not in html
    assert "var highGainGroups = groupByExactDesc((group.stocks || []).slice(), 'high_gain_10d_count');" not in html
    assert "10日强不碰次数" in html
    assert "参考日最高涨幅>=6.85%" in html
    assert "return `涨停或光脚:${tabConfig.label}`;" in html
    assert "涨停或光脚:强不碰次数" not in html
    assert "datasets.ztorgj" in html
    assert "section.datasetKey || tabConfig.datasetKey" in html
    assert "datasets && datasets[sectionDatasetKey]" in html
    assert "_activeTab = 'yin20'" in html
    assert "20日阴线次数由小到大分组" in html
    assert "compareYin20ThenWeak20" in html
    assert "compareWeak20ThenYin20" in html
    assert "compareByTabMetricWithTieBreaker" in html
    assert "if (_activeTab !== 'huiche6' && _activeTab !== 'weak20') {" in html
    assert "return Number(v || 0) + '次';" in html
    assert "grid.appendChild(buildHuiche6AdjustNoBreakTable(source));" in html
    assert html.index("label: '全部股票'") < html.index("label: '拉升涨停'")
    assert html.index("filterId: 'huiche6-layang5days'") < html.index("filterId: 'huiche6-limit-up'")
    assert html.index("filterId: 'huiche6-limit-up'") < html.index("filterId: 'huiche6-bare-foot'")
    assert "_activeTab !== 'yin20' && _activeTab !== 'weak20'" in html
    assert "20日阴线次数</span><span>20日弱阴线次数" in html
    assert "Number(s.yin_count_20 || 0)" in html
    assert "Number(s.weak_yin_20d || 0)" in html
    assert "重点关注：20日阴线次数<=4" in html
    assert "focus-group-box" in html
    assert "shouldHighlightYin20FocusGroup" in html
    assert "今日重点关注" in html
    assert "今日次重点关注" in html
    assert "buildFocusAlertPanel" in html
    assert "buildSecondaryFocusAlertPanel" in html
    assert "buildFocusAlertPanels" in html
    assert "stock.dingtalk_push_title || '股票预警'" not in html
    assert "focus-alert-reason" not in html
    assert "'<span>最高 ' + Number(stock.max_gain || 0).toFixed(2)" not in html
    assert "'<span>最高 ' + getRefHighGainValue(stock).toFixed(2)" not in html
    assert "function renderFocusAlertGain(gain)" in html
    assert "focus-alert-gain ' + focusGainClass" in html
    assert "renderFocusAlertGain(gain)" in html
    assert "if (_activeTab === 'yin20') {" in html
    assert "getFocusAlertStocks" in html
    assert "getSecondaryFocusAlertStocks" in html
    assert "isFocusAlertCandidate" in html
    assert "if (!stock || stock.yin_count_20 == null || stock.max_gain == null) return false;" in html
    assert "Number(stock.yin_count_20) <= 3" in html
    assert "Number(stock.max_gain) >= 6.85" in html
    assert "var source = ((datasets && datasets.yxcs) || []);" in html
    assert "source.forEach(function(stock)" in html
    assert "阴线次数<=3 · 参考日最高涨幅>=6.85%" in html
    assert "Number(stock && stock.dingtalk_pushed || 0) === 1 && Number(stock.yin_count_20 || 0) === 3" not in html
    assert "isSecondaryFocusAlertCandidate" in html
    assert "Number(stock.yin_count_20 || 0) === 4" in html
    assert "getRefHighGainValue(stock) >= 6.85" in html
    assert "isFocusAlertStock" in html
    assert "focus-alert-panels" in html
    assert "focus-alert-side" in html
    assert "columns-grid-yin20-focus" in html
    assert ".columns-grid.columns-grid-yin20-focus" in html
    assert "grid.classList.add('columns-grid-yin20-focus');" in html
    assert "grid.appendChild(buildFocusAlertPanels(datasets, true));" in html
    assert "wrapper.appendChild(buildFocusAlertPanels(datasets));" not in html
    assert "if (_activeTab === 'yin20') {\n        wrapper.appendChild(buildFocusAlertPanels(datasets));\n    }" not in html
    assert html.index("grid.appendChild(buildFocusAlertPanels(datasets, true));") < html.index("label: section.label")
    assert "focus-alert-panel" in html
    assert "stock-row-focus-alert" in html
    assert "['metric-all', 'metric-limit', 'metric-bare', 'metric-high', 'metric-5ls', 'zt-yin20', 'gj-yin20']" in html
    assert "重点关注：20日弱阴线<=3" in html
    assert "isFocusGroupLabel" in html
    assert "全部股票按20日弱阴线次数由小到大分组显示" in html
    assert "默认仅显示阳线且20日新高，并筛选所选日期当日涨停股" in html
    assert "仅显示所选日期光脚阳线股，按20日弱阴线次数由小到大分组" not in html
    assert "默认仅显示阳线且20日新高，并筛选所选日期最高涨幅>=6.85%的股票" in html
    assert "isWeak20YangAndNewHighStock(s) && getRefHighGainValue(s) >= 6.85" in html
    assert "isWeak20YangAndNewHighStock(s) && Number(s.is_limit_up || 0) === 1" in html
    assert "function renderWeak20NewHighFilters(header, col)" in html
    assert "function applyWeak20NewHighFilter(stocks, col)" in html
    assert "function isWeak20YangAndNewHighStock(stock)" in html
    assert "isTrueFlag(stock && stock.is_ref_yang) && isTrueFlag(stock && stock.is_20d_new_high)" in html
    assert "return isTrueFlag(s && s.is_20d_new_high);" in html
    assert "显示全部" in html
    assert "显示新高" in html
    assert "默认：阳线+20日新高" in html
    assert "_weak20ColumnNewHighFilters" in html
    assert "renderWeak20NewHighFilters(header, col);" in html
    assert "col.source = applyWeak20NewHighFilter(col.source, col);" in html
    assert "导出Excel" in html
    assert "function exportWeak20FirstColumnKlineExcel(button, col, progressEl)" in html
    assert "weak20-export-progress" in html
    assert "导出进度：" in html
    assert "/api/ztorgj/weak20/kline-excel" in html
    assert "function isKlinePageNewHighFilterActive(col)" in html
    assert "return _activeTab === 'weak20' && isWeak20NewHighFilterActive(col);" in html
    assert "params.set('new_high_only', isKlinePageNewHighFilterActive(col) ? '1' : '0');" in html
    assert "/api/ztorgj/weak20/kline-excel/start" in html
    assert "/api/ztorgj/weak20/kline-excel/progress/" in html
    assert "/api/ztorgj/weak20/kline-excel/download/" in html
    assert "导出中 ' + current + '/' + total + ' ' + percent + '%'" in html
    assert "await waitWeak20ExportProgress(800);" in html
    assert "显示K线图" in html
    assert "function openWeak20KlinePage(col)" in html
    assert "/涨停或光脚/弱阴线K线" in html
    assert "params.set('max_weak_yin_20d', '3');" in html
    assert "function shouldRenderKlinePageButton(col)" in html
    assert "col.label === '当日涨停' || col.label === '最高涨幅>=6.85%'" in html
    assert "function buildKlineOrderedCodesFromGroups(groups)" in html
    assert "function buildKlineOrderedStocksFromGroups(groups)" in html
    assert "klineOrderedStocks: buildKlineOrderedStocksFromGroups(groups)" in html
    assert "stocks: buildKlineOrderedStocksForPage(col)" in html
    assert "window.open(url, '_blank', 'noopener');" in html
    assert "return String(a.code || '').localeCompare(String(b.code || ''));" in html
    assert "function buildWeak20KlineOrderedCodes(col)" in html
    assert "localStorage.setItem(orderToken" in html
    assert "params.set('order_token', orderToken);" in html
    assert "20日涨停次数由大到小分组" in html
    assert "20日光脚次数由大到小分组" in html
    assert "创20日收盘新高阳线股，按涨跌幅由高到低排序" in html
    assert "20天累计涨幅" in html
    assert "按最近10日阴线实体百分比累加由低到高排序" in html
    assert "function createBreak20YinCountSection(label, sub, filter)" in html
    assert ".col-header.metric-yin-count" in html
    assert ".group-label.metric-yin-count" in html
    assert ".col-header.metric-yin-count { background: linear-gradient(135deg, #f59e0b, #d97706); }" in html
    assert ".group-label.metric-yin-count { background: rgba(217, 119, 6, 0.92); }" in html
    assert "groupFn: groupBreak20YinCountAsc" in html
    assert "function groupBreak20YinCountAsc(sorted)" in html
    assert "return groupByExactAsc(sorted, 'yin_count_20');" in html
    assert "20日突破股票按20日阴线次数由小到大分组显示" in html
    assert "columns-grid-break20" in html
    assert ".columns-grid.columns-grid-break20" in html
    assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in html
    assert ".container.container-break20" in html
    assert "max-width: 1820px" in html
    assert ".container.container-break20 .nav-buttons,\n        .container.container-break20 .page-title,\n        .container.container-break20 .tab-switcher {\n            margin-left: 160px;" in html
    assert "containerEl.classList.toggle('container-break20', isBreak20GlobalFilterTab());" in html
    assert "阴线累加同范围股票按20日阴线次数由小到大分组显示" not in html
    assert html.count("createBreak20YinCountSection(") == 2
    first_yin_count_col = html.index("20日突破股票按20日阴线次数由小到大分组显示")
    assert html.index("label: '20日突破'") < first_yin_count_col
    assert first_yin_count_col < html.index("label: '阴线累加'")
    assert html.index("label: '阴线累加'") < html.index("label: '5拉升'", first_yin_count_col)
    assert "let _break20GlobalFilters = {" in html
    assert "highGainYang: true" in html
    assert "limitUp: false" in html
    assert "yinCloseBelowMa5Lte2: false" in html
    assert "bareFootYang: false" in html
    assert "bareFootNoTouch: false" in html
    assert "bareFootLimitUp: false" in html
    assert ".break20-layout" in html
    assert ".break20-filter-rail" in html
    assert ".break20-layout {\n            position: relative;" in html
    assert "padding-left: 160px;" in html
    assert ".break20-filter-rail {\n            width: 112px;" in html
    assert "position: absolute;\n            left: 24px;\n            top: 0;" in html
    assert "min-height: 42px;\n            padding: 8px 8px;" in html
    assert "font-size: 12px;\n            line-height: 1.3;" in html
    assert "min-width: 88px; min-height: 36px; padding: 7px 10px; font-size: 11px;" in html
    assert "position: fixed;\n            left: 12px;\n            top: 120px;" not in html
    assert "flex: 0 0 88px;" not in html
    assert "function appendBreak20GlobalFilterButtons(box)" in html
    assert "function renderBreak20GlobalFilterRail()" in html
    assert "function clearBreak20GlobalFilters()" in html
    assert "Object.keys(_break20GlobalFilters).forEach(function(key) {" in html
    assert "_break20GlobalFilters[key] = false;" in html
    assert "function appendBreak20ShowAllButton(box)" in html
    assert "btn.textContent = '显示全部';" in html
    assert "appendBreak20ShowAllButton(rail);" in html
    assert "break20Layout.appendChild(renderBreak20GlobalFilterRail());" in html
    assert "break20Layout.appendChild(grid);" in html
    assert "function applyBreak20GlobalFilters(stocks)" in html
    assert "col.source = applyBreak20GlobalFilters(col.source);" in html
    assert "label: '极品光脚拉升不碰'" in html
    assert "按 is_bare_foot_yang + ref_low_above_ma5 筛选" in html
    assert "if (_break20GlobalFilters.bareFootNoTouch && !(isTrueFlag(stock && stock.is_bare_foot_yang) && isTrueFlag(stock && stock.ref_low_above_ma5))) return false;" in html
    assert "label: '光脚涨停'" in html
    assert "按 is_bare_foot_yang + ref_is_limit_up/is_limit_up 筛选" in html
    assert "if (_break20GlobalFilters.bareFootLimitUp && !(isTrueFlag(stock && stock.is_bare_foot_yang) && (isTrueFlag(stock && stock.ref_is_limit_up) || isTrueFlag(stock && stock.is_limit_up)))) return false;" in html
    assert "label: '光脚拉升'" in html
    assert "variant: 'purple'" in html
    assert ".break20-filter-rail .col-filter-btn.purple-filter" in html
    assert ".break20-filter-rail .col-filter-btn.purple-filter.purple-active" in html
    assert "label: '是否拉升阳'" in html
    assert "ref_high_gain_gte_6_5_and_yang" in html
    assert "label: '是否涨停'" in html
    assert "按 ref_is_limit_up/is_limit_up 筛选" in html
    assert "if (_break20GlobalFilters.limitUp && !(isTrueFlag(stock && stock.ref_is_limit_up) || isTrueFlag(stock && stock.is_limit_up))) return false;" in html
    assert html.index("label: '极品光脚拉升不碰'") < html.index("label: '光脚涨停'") < html.index("label: '光脚拉升'")
    assert html.index("label: '光脚拉升'") < html.index("label: '是否拉升阳'") < html.index("label: '是否涨停'") < html.index("label: '阴线跌破5日线<=2'")
    assert "label: '阴线跌破5日线<=2'" in html
    assert "yin_close_below_ma5_lte_2_10d" in html
    assert "label: '是否光脚拉升'" not in html
    assert "is_bare_foot_yang" in html
    assert "toggleBreak20GlobalFilter(item.key);" in html
    assert "renderBreak20GlobalFilters" not in html
    assert "renderBreak20YinSumFilter" not in html
    assert "break20YinSumToggle" not in html
    assert "_break20YinSumHighGainYangOnly" not in html
    assert "label: '拉升且不碰'" in html
    assert "ref_high_gain_gte_6_5_yang_low_above_ma5" in html
    assert html.index("label: '拉升且不碰'") < html.index("{ key: 'weak20'")
    assert "label: '拉升阳线'" not in html
    assert "label: '5拉升'" in html
    assert "筛选最近10日最高涨幅>=6.5%天数>=5，上方按涨跌幅排序，下方按阴线累加排序" in html
    assert "filter: function(s) { return isTrueFlag(s && s.high_gain_10d_gte_5); }" in html
    assert "groupFn: groupBreak20FiveRise" in html
    assert "function groupBreak20FiveRise(sorted)" in html
    assert "{ label: '涨跌幅排序', stocks: gainRows }" in html
    assert "{ label: '阴线累加排序', stocks: yinBodyRows, metricKey: 'yin_body_pct_sum_10d', metricLabel: '阴线累加', fmtVal: formatPctValue }" in html
    assert "yinBodyRows.sort(compareYinBodyPctSumAsc);" in html
    assert "function getGroupMetricKey(col, group)" in html
    assert "return (group && group.metricKey) || (col && col.key);" in html
    assert "function getGroupMetricLabel(col, group)" in html
    assert "return (group && group.metricLabel) || (col && col.metricLabel);" in html
    assert "function formatGroupMetricValue(col, group, stock)" in html
    assert "return group.fmtVal(value, stock, group);" in html
    assert "getGroupMetricLabel(col, group) + '</span>'" in html
    assert "function renderBreak20Badges(stock)" in html
    assert "isTrueFlag(stock && stock.ref_high_gain_gte_6_5_yang_low_above_ma5)" in html
    assert "break20-badge break20-badge-lowma5" in html
    assert "break20-badge break20-badge-yinpull" in html
    assert "拉升且不碰" in html
    assert "阴前后拉" in html
    assert "ref_high_gain_gte_6_5_and_yang" in html
    assert "label: '阴线累加'" in html
    assert "yin_body_pct_sum_10d" in html
    assert "function compareYinBodyPctSumAsc(a, b)" in html
    assert "groupFn: groupBreak20YinSumByQuanyang" in html
    assert "function groupBreak20YinSumByQuanyang(sorted)" in html
    assert "{ label: '全阳', stocks: quanyangRows }" in html
    assert "{ label: '阴线累加', stocks: otherRows }" in html
    assert "ref_is_limit_up" in html
    assert "筛选参考日涨停股，按20日阴线次数由小到大分组显示" not in html
    assert "metricKey: 'yin_count_20'" in html
    assert "metricLabel: '20日阴线次数'" in html
    assert "function isBreak20GlobalFilterTab()" in html
    assert "return _activeTab === 'break20' || _activeTab === 'yin20' || _activeTab === 'lsbp';" in html
    assert "containerEl.classList.toggle('container-break20', isBreak20GlobalFilterTab());" in html
    assert "if (!isBreak20GlobalFilterTab()) return stocks || [];" in html
    assert "if (isBreak20GlobalFilterTab()) {" in html
    assert "break20Layout.appendChild(renderBreak20GlobalFilterRail());" in html
    assert html.index("label: '阴线累加'") < html.index("label: '拉升且不碰'", first_yin_count_col)
    assert "20日涨停累计次数排序" not in html
    assert "按20日涨停累计次数由大到小分组显示" not in html
    assert "连续非阴非跌停天数排序" in html
    assert "参考日涨停" in html
    assert "连续非阴非跌停天数由大到小分组显示" in html
    assert "仅显示参考日最高涨幅>=6.85%的股票，按连续非阴非跌停天数由大到小分组显示" in html
    assert "全部股票按不是阴线，或者是阴线但 low >= 当天5日均价的天数由大到小分组显示" in html
    assert "仅显示参考日涨停股票，按连续满足条件天数由大到小分组显示" in html
    assert "仅显示参考日光脚阳线股票，按连续非阴非跌停天数由大到小分组显示" in html
    assert "仅显示参考日最高涨幅>=6.85%的股票，按连续满足条件天数由大到小分组显示" in html
    assert "全部股票按最近10日强不碰次数由大到小分组显示" in html
    assert "仅显示参考日涨停股票，按最近10日强不碰次数由大到小分组显示" in html
    assert "仅显示参考日光脚阳线股票，按连续满足条件天数由大到小分组显示" in html
    assert "仅显示参考日最高涨幅>=6.85%的股票，按最近10日强不碰次数由大到小分组显示" in html
    assert "仅显示参考日光脚阳线股票，按最近10日强不碰次数由大到小分组显示" in html
    assert "headerLabel += '（共' + ((col.source || []).length) + '只）';" in html
    assert "最高涨幅>=6.85%" in html
    assert "几乎光脚阳线且最高涨幅>=7%" in html
    assert "function buildBareFootSubgroups(groupStocks)" in html
    assert "function buildBareFootMainGroups(stocks)" in html
    assert "renderBareFootColumn(colDiv, col, showDualCounts);" in html
    assert "renderBareFootMainGroup(colDiv, col, bigGroup, showDualCounts);" in html
    assert "groupFn(sorted, col.key)" in html
    assert ".bare-main-group {" in html
    assert "mainGroupBox.className = 'bare-main-group';" in html
    assert "function formatGroupLabelText(col, groupLabel, stockCount, group)" in html
    assert "if (!labelText) labelText = '未分组';" in html
    assert "return (getGroupMetricLabel(col, group) || '') + '：' + labelText + '  (' + stockCount + ')';" in html
    assert "Number(s.is_bare_foot_yang || 0) === 1" in html
    assert "Number(s.is_bare_foot || 0) === 1 && Number(s.is_bare_foot_yang || 0) !== 1" in html
    assert "mainGroupLabel.className = 'group-sub-label';" in html
    assert "smallGroupLabel.className = 'group-label ' + col.cls;" in html
    assert "buildSubgroupEmptyState('该大组暂无数据')" in html
    assert "buildSubgroupEmptyState('该小组暂无数据')" in html
    assert 'high-gain-title' in html
    assert 'high_gain_10d_gte_5' in html
    assert 'high_gain_10d_count' in html
    assert 'high_gain5_and_yin_pull' in html
    assert "rowHeader.className = 'stock-row-header' + ((showDualCounts || showCustomMetrics) ? ' dual-counts' : '');" in html
    assert "rowHeader.className = 'high-gain-row-header dual-counts';" in html
    assert 'columns-grid-wide-counts' in html
    assert 'grid-template-columns: 1.02fr 1.02fr 1.12fr 1.12fr 0.82fr;' in html
    assert '.columns-grid.columns-grid-wide-counts .task-section {' in html
    assert 'grid-template-columns: repeat(5, 280px);' in html
    assert '@media (max-width: 768px)' in html
    assert 'mobile no-scroll baseline' in html
    assert 'table-layout: fixed' in html
    assert 'overflow-wrap: anywhere' in html
    assert 'overflow-x: hidden !important' in html
    assert 'mobile density tuning' in html
    assert 'padding: 7px 5px;' in html
    assert 'font-size: 11px;' in html
    assert '/* mobile access layout */' in html
    assert 'body { padding: 8px; overflow-x: hidden; }' in html
    assert '.nav-buttons {\n                flex-wrap: wrap;\n                overflow-x: visible;' in html
    assert '.nav-left,\n            .nav-right {\n                flex: 1 1 100%;' in html
    assert '.tab-switcher {\n                width: 100%;\n                justify-content: flex-start;\n                flex-wrap: wrap;' in html
    assert 'overflow-x: visible;' in html
    assert '.columns-grid,\n            .columns-grid.columns-grid-wide-counts,\n            .columns-grid.columns-grid-yin20-focus,\n            .columns-grid.columns-grid-break20,\n            .columns-grid.columns-grid-lsbp {' in html
    assert 'grid-auto-columns: minmax(286px, 86vw);' in html
    assert 'overflow-x: auto !important;' in html
    assert 'scroll-snap-type: x proximity;' in html
    assert '.break20-layout {\n                display: block;' in html
    assert '.break20-filter-rail {\n                position: static;' in html
    assert '.break20-filter-rail {\n                position: static;\n                left: auto;\n                top: auto;\n                width: 100%;\n                display: flex;\n                flex-direction: row;\n                flex-wrap: wrap;' in html
    assert '.break20-filter-rail .col-filter-btn { flex: 0 1 auto; width: auto; min-width: 88px;' in html
    assert '.focus-alert-panels.focus-alert-side {\n                min-width: 286px;' in html
    assert '.stock-tooltip {\n                display: none !important;' not in html
    assert 'function isMobileKlineMode()' in html
    assert 'function attachKlineEvents(row, code, name)' in html
    assert "row.addEventListener('click', function(e) {" in html
    assert "if (!isMobileKlineMode()) return;" in html
    assert 'let _lastKlineOpenAt = 0;' in html
    assert '_lastKlineOpenAt = Date.now();' in html
    assert 'if (Date.now() - _lastKlineOpenAt < 350) return;' in html
    assert 'window.showKline = showKline;' in html
    assert 'window.hideKline = hideKline;' in html
    assert 'scanInfoText' in html
    assert '扫描耗时--s，扫描完成时间:--，Redis更新:--' in html
    assert 'updateScanInfoDisplay' in html
    assert 'formatScanDuration' in html
    assert 'formatScanEndTime' in html
    assert 'formatSnapshotDurationMs' in html
    assert 'buildSnapshotStatusText' in html
    assert 'let _lastStableSnapshotVersion = null;' in html
    assert 'const nextSnapshotVersion = String(result.snapshot_version || \'\').trim();' in html
    assert 'const isSnapshotStable = Boolean(result.is_snapshot_stable);' in html
    assert 'if (!isSnapshotStable || !nextSnapshotVersion || nextSnapshotVersion === _lastStableSnapshotVersion) {' in html
    assert "console.log('[ztorgj][refreshFromRedis] 跳过重渲染'" in html
    assert 'DEBUG_STOCK_CODES = new Set([\'688268\'])' in html
    assert 'function shouldDebugStock(stock)' in html
    assert 'function logDebugStockSnapshot(stage, datasets)' in html
    assert 'function logDebugRenderedStockRows()' in html
    assert 'function installDebugStockDomObserver()' in html
    assert 'function getDisplayChangeValue(stock, context)' in html
    assert "[ztorgj][debug][display-change]" in html
    assert "[ztorgj][debug][display-change-text]" in html
    assert "[ztorgj][debug][refreshFromRedis]" in html
    assert "[ztorgj][debug][after-fetch-text]" in html
    assert "[ztorgj][debug][render-row]" in html
    assert "[ztorgj][debug][rendered-dom]" in html
    assert "[ztorgj][debug][rendered-dom-text]" in html
    assert "[ztorgj][debug][dom-mutation]" in html
    assert "[ztorgj][debug][dom-mutation-text]" in html
    assert "row.dataset.stockCode = s.code || '';" in html
    assert "row.dataset.columnLabel = col && col.label || '';" in html
    assert 'snapshot_info' in html
    assert '快照成功，写入' in html
    assert '该列表快照失败，已保留旧数据' in html
    assert '本轮快照中断，这个列表还没来得及更新' in html
    assert '本列表快照成功，但同轮其他列表有失败' in html
    assert 'async function refreshFromRedis(showLoading)' in html
    assert 'function startPollingRefresh()' in html
    assert '_checkTimer = setInterval(function() {' in html
    assert '/api/ztorgj/check_update' not in html
    assert 'function getShanghaiNowParts()' in html
    assert 'function shouldUseRealtimeStocks()' in html
    assert 'async function refreshRealtime()' in html
    assert "_realtimeTimer = setInterval(refreshRealtime, 3000);" in html
    assert "/api/ztorgj/realtime" in html
    assert "stock.today_change = realtime.change;" in html
    assert 'function updateRealtimeGainNodes(realtimeData)' in html
    assert 'updateRealtimeGainNodes(result.data);' in html
    realtime_body = html[html.index('async function refreshRealtime()'):html.index('function startRealtimeRefresh()')]
    assert 'applyRealtimeToManualTasks(result.data);\n        updateRealtimeGainNodes(result.data);' in realtime_body
    assert 'applyRealtimeToManualTasks(result.data);\n        renderColumns(_datasets);' not in realtime_body
    assert 'await refreshFromRedis(false);' in html
    assert '今日任务列表' in html
    assert 'task-context-menu' in html
    assert 'task-ios-overlay' in html
    assert 'YHY_TASK_API_BASE = \'/api/yhy/tasks\'' in html
    assert '/api/yhy/tasks/add' in html
    assert '/api/yhy/tasks/remove' in html
    assert 'bodyEl.onclick = clickHandler;' in html
    assert 'function getTaskSourcePageName()' in html
    assert 'getVisibleTaskStocksForCurrentPage' in html
    assert "return `涨停或光脚:${tabConfig.label}`;" in html
    assert 'grid.appendChild(buildTaskSection());' in html
    assert 'function cloneDatasets(datasets)' in html
    assert 'var gain = getDisplayChangeValue(s);' in html
    assert '.stock-row-header span { font-size: 10px; font-weight: 600; color: #86868b; text-align: center;' in html
    assert '.s-name { font-size: 13px; font-weight: 500; color: #1d1d1f; text-align: center; display: flex;' in html
    assert 'display: -webkit-box;' in html
    assert '-webkit-line-clamp: 2;' in html
    assert '.high-gain-name {' in html
    assert '.high-gain-row-header span {' in html
    assert 'text-align: center;' in html
    assert '.stock-row-header.dual-counts,\n        .stock-row.dual-counts {\n            grid-template-columns: 1.6fr 0.8fr 1fr 1fr;' in html
    assert '.high-gain-row-header.dual-counts,\n        .high-gain-row.dual-counts {\n            grid-template-columns: 1.45fr 0.72fr 1.02fr 0.95fr 0.95fr;' in html
    assert "var base = isHighGain ? ['1.42fr', '0.72fr'] : ['1.58fr', '0.8fr'];" in html
    assert '.stock-row > span,\n        .high-gain-row > span {\n            min-width: 0;\n        }' in html
    assert '.s-gain, .s-metric, .high-gain-count, .high-gain-metric {\n            white-space: nowrap;\n        }' in html
    assert '.s-gain { font-size: 13px; font-weight: 700; text-align: center; }' in html
    assert '.s-metric { font-size: 12px; font-weight: 700; text-align: center; color: #374151; }' in html
    assert '.stock-row-header.dual-counts,\n            .stock-row.dual-counts {\n                grid-template-columns: 1.5fr 0.85fr 1fr 1fr;' in html
    assert '.high-gain-row-header.dual-counts,\n            .high-gain-row.dual-counts {\n                grid-template-columns: 1.35fr 0.85fr 1.05fr 0.95fr 0.95fr;' in html
    assert '/5拉升20阴少' not in html
    assert '/5拉升小于2阴' not in html
    assert '/5拉升3光脚' not in html
    assert '/极品关注' in html
    assert '/涨停前连续强' not in html


def test_ztorgj_template_contains_lsbp_tab_between_yin20_and_break20(client):
    resp = client.get('/涨停或光脚')
    html = resp.get_data(as_text=True)

    yin20_pos = html.index("{ key: 'yin20', label: '20日阴线次数'")
    lsbp_pos = html.index("{ key: 'lsbp', label: '拉升且不碰'")
    break20_pos = html.index("{ key: 'break20', label: '20日突破'")

    assert yin20_pos < lsbp_pos < break20_pos
    assert "datasetKey: 'lsbp'" in html
    assert "'lsbp': []" in html
    assert "const DATASET_KEYS = ['ztorgj', 'yxcs', 'ruoyinxian', 'ztgjcs', '20tupo', 'lsbp'" in html
    assert "label: '按涨跌幅排序'" in html
    assert "sortFn: compareDisplayGainDesc" in html
    assert "label: '距MA5排序'" in html
    assert "metricKey: 'ref_low_ma5_distance_pct'" in html
    assert "sortFn: compareRefLowMa5DistanceDesc" in html
    assert "label: '下引线排序'" in html
    assert "metricKey: 'ref_lower_shadow_pct'" in html
    assert "sortFn: compareRefLowerShadowAsc" in html
    assert "label: '20日阴线次数分组'" in html
    assert "groupFn: groupLsbpYin20Asc" in html
    assert "label: '10日阴线次数分组'" in html
    assert "metricKey: 'yin_count_10d'" in html
    assert "groupFn: groupLsbpYin10Asc" in html



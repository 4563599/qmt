import base64
import inspect
import json
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def client():
    import app as flask_app
    flask_app.app.config['TESTING'] = True
    with flask_app.app.test_client() as c:
        yield c


def _mock_ruoyinxian_redis(payloads):
    mock_r = MagicMock()
    mock_r.exists.side_effect = lambda key: key == 'ruoyinxian:20260421'
    mock_r.type.side_effect = lambda key: 'hash' if key == 'ruoyinxian:20260421' else None
    mock_r.hscan.return_value = (0, payloads)
    return mock_r


def _mock_qmt_kline_redis(payloads):
    mock_r = MagicMock()
    mock_r.hget.side_effect = lambda key, field: payloads.get((key, field))
    return mock_r


def test_ztorgj_kline_api_returns_chronological_ohlcv(client):
    payload = {
        'code': '688628',
        'ts_code': '688628.SH',
        'name': '优利德',
        'rows': [
            {'ts_code': '688628.SH', 'trade_date': '20260427', 'open': 101.5, 'high': 108.08, 'low': 100.12, 'close': 105.10, 'pre_close': 98.04, 'pct_chg': 7.2011, 'vol': 89260.24, 'amount': 929795.354},
            {'ts_code': '688628.SH', 'trade_date': '20260428', 'open': 104.0, 'high': 107.90, 'low': 99.50, 'close': 101.07, 'pre_close': 105.10, 'pct_chg': -3.8344, 'vol': 72060.22, 'amount': 736685.881},
            {'ts_code': '688628.SH', 'trade_date': '20260429', 'open': 99.0, 'high': 103.87, 'low': 97.87, 'close': 98.04, 'pre_close': 101.07, 'pct_chg': -2.9979, 'vol': 55119.13, 'amount': 551880.32},
        ],
    }
    mock_r = _mock_qmt_kline_redis({
        ('qmt:kline:20260429', '688628'): json.dumps(payload, ensure_ascii=False),
    })

    with patch('app.get_redis_client', return_value=mock_r), patch('app.ts.pro_api') as pro_api:
        resp = client.get('/api/ztorgj/kline?code=688628.SH&days=2&end_date=2026-04-29')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['stock']['code'] == '688628.SH'
    assert data['stock']['name'] == '优利德'
    pro_api.assert_not_called()
    assert len(data['data']) == 2
    assert [item['trade_date'] for item in data['data']] == ['20260428', '20260429']
    assert data['data'][0]['open'] == pytest.approx(104.0)
    assert data['data'][1]['close'] == pytest.approx(98.04)
    assert data['data'][1]['pct_chg'] == pytest.approx(-2.9979)


def test_ztorgj_kline_api_reports_missing_qmt_redis_data(client):
    mock_r = _mock_qmt_kline_redis({})

    with patch('app.get_redis_client', return_value=mock_r), patch('app.ts.pro_api') as pro_api:
        resp = client.get('/api/ztorgj/kline?code=000066.SZ&days=3&end_date=2026-05-13')
        data = json.loads(resp.data)

    assert resp.status_code == 404
    assert data['success'] is False
    assert data['key'] == 'qmt:kline:20260513'
    assert data['field'] == '000066'
    assert 'Redis' in data['error']
    pro_api.assert_not_called()


def test_api_kline_loader_merges_history_and_realtime_rows():
    history = {
        'code': '000001',
        'name': '平安银行',
        'updated_at': '2026-07-30 09:25:08',
        'rows': [
            {'trade_date': '20260728', 'open': 10.0, 'high': 10.4, 'low': 9.9, 'close': 10.2},
            {'trade_date': '20260729', 'open': 10.2, 'high': 10.6, 'low': 10.1, 'close': 10.5},
        ],
    }
    realtime = {
        'code': '000001',
        'name': '平安银行',
        'stock_code_with_market': '000001.SZ',
        'snapshot_at': '2026-07-30 10:30:10',
        'published_at': '2026-07-30 10:30:11',
        'rows': [{
            'trade_date': '20260730',
            'open': 10.5,
            'high': 10.9,
            'low': 10.4,
            'close': 10.8,
            'pre_close': 10.5,
        }],
    }
    mock_r = _mock_qmt_kline_redis({
        ('qmt:kline:api:history:20260730', '000001'): json.dumps(
            history, ensure_ascii=False
        ),
        ('qmt:kline:api:realtime:20260730', '000001'): json.dumps(
            realtime, ensure_ascii=False
        ),
    })

    with patch('app.get_redis_client', return_value=mock_r):
        import app
        payload, redis_key, redis_field = app._ztorgj_load_api_kline_payload(
            '000001.SZ', '20260730', days=60
        )

    assert redis_key == 'qmt:kline:api:realtime:20260730'
    assert redis_field == '000001'
    assert [row['trade_date'] for row in payload['data']] == [
        '20260728', '20260729', '20260730'
    ]
    assert payload['snapshot_at'] == '2026-07-30 10:30:10'
    assert payload['published_at'] == '2026-07-30 10:30:11'
    assert payload['updated_at'] == '2026-07-30 10:30:11'
    assert payload['source'] == 'redis:qmt:kline:api'


def test_api_kline_loader_realtime_row_overrides_same_history_date():
    history = {
        'code': '000001',
        'name': '平安银行',
        'rows': [
            {'trade_date': '20260730', 'open': 10.5, 'high': 10.6, 'low': 10.4, 'close': 10.5},
        ],
    }
    realtime = {
        'snapshot_at': '2026-07-30 10:30:10',
        'published_at': '2026-07-30 10:30:11',
        'row': {
            'trade_date': '20260730',
            'open': 10.5,
            'high': 10.9,
            'low': 10.4,
            'close': 10.8,
        },
    }
    mock_r = _mock_qmt_kline_redis({
        ('qmt:kline:api:history:20260730', '000001'): json.dumps(history),
        ('qmt:kline:api:realtime:20260730', '000001'): json.dumps(realtime),
    })

    with patch('app.get_redis_client', return_value=mock_r):
        import app
        payload, _, _ = app._ztorgj_load_api_kline_payload(
            '000001', '20260730', days=60
        )

    assert len(payload['data']) == 1
    assert payload['data'][0]['close'] == pytest.approx(10.8)


def test_api_kline_loader_falls_back_to_legacy_key():
    legacy = {
        'code': '000001',
        'name': '平安银行',
        'updated_at': '2026-07-30 10:20:00',
        'rows': [
            {'trade_date': '20260730', 'open': 10.5, 'high': 10.8, 'low': 10.4, 'close': 10.7},
        ],
    }
    mock_r = _mock_qmt_kline_redis({
        ('qmt:kline:20260730', '000001'): json.dumps(
            legacy, ensure_ascii=False
        ),
    })

    with patch('app.get_redis_client', return_value=mock_r):
        import app
        payload, redis_key, _ = app._ztorgj_load_api_kline_payload(
            '000001', '20260730', days=60
        )

    assert redis_key == 'qmt:kline:20260730'
    assert payload['source'] == 'redis:qmt:kline'
    assert payload['data'][0]['close'] == pytest.approx(10.7)


def test_kline_api_stale_count_uses_fifteen_second_threshold():
    import app

    now = datetime(2026, 7, 30, 10, 30, 20)
    assert app._kline_api_stale_count([
        '2026-07-30 10:30:10',
        '2026-07-30 10:30:04',
        '',
    ], now=now) == 1


def test_10above5_klines_api_returns_names_and_strict_ohlc(client):
    encryption_key = bytes(range(32))
    encryption_key_b64 = base64.b64encode(encryption_key).decode('ascii')
    stock_payloads = {
        b'000001': json.dumps({
            'code': '000001',
            'name': '平安银行',
            'close_above_ma5_count_10d': 10,
            'low_above_ma5_count': 7,
            'today_change': 2.5,
            'ref_high_gain_ok': True,
            'ref_is_limit_up': False,
        }, ensure_ascii=False),
        b'000002': json.dumps({
            'code': '000002',
            'name': '万科A',
            'close_above_ma5_count_10d': 9,
            'today_change': 1.2,
            'ref_high_gain_ok': False,
            'is_limit_up': 1,
        }, ensure_ascii=False),
        b'000003': json.dumps({
            'code': '000003',
            'name': '国华网安',
            'close_above_ma5_count_10d': 8,
        }, ensure_ascii=False),
    }
    kline_payloads = {
        ('qmt:kline:api:history:20260710', '000001'): json.dumps({
            'code': '000001',
            'name': '平安银行',
            'rows': [
                {'trade_date': '20260709', 'open': 10.2, 'high': 10.7, 'low': 10.1, 'close': 10.6},
            ],
        }, ensure_ascii=False),
        ('qmt:kline:api:realtime:20260710', '000001'): json.dumps({
            'code': '000001',
            'name': '平安银行',
            'snapshot_at': '2026-07-10 14:35:20',
            'published_at': '2026-07-10 14:35:21',
            'row': {
                'trade_date': '20260710',
                'open': 10.6,
                'high': 10.9,
                'low': 10.4,
                'close': 10.8,
            },
        }, ensure_ascii=False),
        ('qmt:kline:api:history:20260710', '000002'): json.dumps({
            'code': '000002',
            'name': '万科A',
            'rows': [],
        }, ensure_ascii=False),
    }
    mock_r = MagicMock()
    mock_r.exists.side_effect = lambda key: key == '10above5:20260710'
    mock_r.type.side_effect = lambda key: 'hash' if key == '10above5:20260710' else None
    mock_r.hscan.return_value = (0, stock_payloads)
    mock_r.hget.side_effect = lambda key, field: kline_payloads.get((key, field))

    with patch('app.get_redis_client', return_value=mock_r), patch(
        'app._kline_api_stale_count', return_value=0
    ), patch.dict(
        os.environ, {'KLINE_API_AES_KEY_B64': encryption_key_b64}
    ):
        resp = client.get('/api/10above5/klines?date=2026-07-10&days=48')
        encrypted_payload = json.loads(resp.data)

    assert resp.status_code == 200
    assert encrypted_payload == {
        'success': True,
        'encrypted': True,
        'algorithm': 'AES-256-GCM',
        'version': 1,
        'nonce': encrypted_payload['nonce'],
        'ciphertext': encrypted_payload['ciphertext'],
    }
    nonce = base64.b64decode(encrypted_payload['nonce'], validate=True)
    ciphertext = base64.b64decode(encrypted_payload['ciphertext'], validate=True)
    plaintext = AESGCM(encryption_key).decrypt(
        nonce, ciphertext, b'10above5-klines:v1'
    )
    payload = json.loads(plaintext.decode('utf-8'))

    assert payload['success'] is True
    assert payload['date'] == '2026-07-10'
    assert payload['days'] == 48
    assert payload['count'] == 1
    assert payload['kline_updated_at'] == '2026-07-10 14:35:21'
    assert payload['kline_redis_key'] == 'qmt:kline:api:realtime:20260710'
    assert payload['kline_oldest_snapshot_at'] == '2026-07-10 14:35:20'
    assert payload['kline_newest_snapshot_at'] == '2026-07-10 14:35:20'
    assert payload['kline_stale_count'] == 0
    assert payload['missing_codes'] == ['000002']
    assert payload['data'][0]['code'] == '000001'
    assert payload['data'][0]['name'] == '平安银行'
    assert payload['data'][0]['updated_at'] == '2026-07-10 14:35:21'
    assert payload['data'][0]['kline_snapshot_at'] == '2026-07-10 14:35:20'
    assert payload['data'][0]['kline_published_at'] == '2026-07-10 14:35:21'
    assert payload['data'][0]['close_above_ma5_count_10d'] == 10
    assert payload['data'][0]['low_above_ma5_count'] == 7
    assert payload['data'][0]['ref_high_gain_ok'] is True
    assert payload['data'][0]['ref_is_limit_up'] is False
    assert payload['data'][0]['data'] == [
        {'trade_date': '20260709', 'open': 10.2, 'high': 10.7, 'low': 10.1, 'close': 10.6},
        {'trade_date': '20260710', 'open': 10.6, 'high': 10.9, 'low': 10.4, 'close': 10.8},
    ]


def test_20xiasha_dates_returns_only_strict_stock_dates(client):
    mock_r = MagicMock()
    mock_r.keys.return_value = [
        b'20xiasha:20260718',
        b'20xiasha:20260717',
        b'20xiasha:20260718:tmp:1',
        b'20xiasha:latest',
        b'yxcs:20260718',
    ]

    with patch('app.get_redis_client', return_value=mock_r):
        response = client.get('/api/20xiasha/dates')

    assert response.status_code == 200
    assert response.get_json() == {
        'success': True,
        'dates': ['2026-07-18', '2026-07-17'],
    }


def test_20xiasha_klines_encrypts_stock_fields_with_targeted_kline_data(client):
    encryption_key = bytes(range(32))
    encryption_key_b64 = base64.b64encode(encryption_key).decode('ascii')
    stock_payloads = {
        b'000001': json.dumps({
            'code': '000001',
            'name': '平安银行',
            'stock_code_with_market': '000001.SZ',
            'is_high_gain': 1,
            'ref_gain': 3.21,
            'close_below_ma5_abs_pct_sum_20d': 1.25,
            'low_above_ma5_count_20d': 7,
            'low_above_ma5_count_10d': 6,
            'purple_match': 1,
            'group_color': 'purple',
            'group_name': '紫色组',
            'purple_reasons': ['low_above_ma5_count_20d_gte_7'],
            'custom_field': {'kept': True},
        }, ensure_ascii=False),
        b'000002': json.dumps({
            'code': '000002',
            'name': '万科A',
            'stock_code_with_market': '000002.SZ',
            'is_high_gain': False,
            'ref_gain': -1.2,
            'close_below_ma5_abs_pct_sum_20d': 0.75,
        }, ensure_ascii=False),
    }
    mock_r = MagicMock()
    mock_r.hgetall.return_value = stock_payloads
    mock_r.hscan.side_effect = AssertionError('20xiasha列表接口应一次读取Hash')
    mock_r.hget.side_effect = lambda key, field: {
        ('qmt:kline:api:history:20260718', '000001'): json.dumps({
            'code': '000001',
            'name': '平安银行',
            'rows': [
                {'trade_date': '20260717', 'open': 10.2, 'high': 10.7, 'low': 10.1, 'close': 10.6},
            ],
        }, ensure_ascii=False),
        ('qmt:kline:api:realtime:20260718', '000001'): json.dumps({
            'code': '000001',
            'name': '平安银行',
            'snapshot_at': '2026-07-18 14:35:20',
            'published_at': '2026-07-18 14:35:21',
            'row': {
                'trade_date': '20260718',
                'open': 10.5,
                'high': 10.9,
                'low': 10.4,
                'close': 10.8,
            },
        }, ensure_ascii=False),
    }.get((key, field))

    with patch('app.get_redis_client', return_value=mock_r), patch(
        'app._kline_api_stale_count', return_value=0
    ), patch.dict(
        os.environ, {'KLINE_API_AES_KEY_B64': encryption_key_b64}
    ):
        response = client.get('/api/20xiasha/klines?date=2026-07-18&days=60')
        envelope = response.get_json()

    assert response.status_code == 200
    assert envelope['success'] is True
    assert envelope['encrypted'] is True
    plaintext = AESGCM(encryption_key).decrypt(
        base64.b64decode(envelope['nonce'], validate=True),
        base64.b64decode(envelope['ciphertext'], validate=True),
        b'20xiasha-klines:v1',
    )
    payload = json.loads(plaintext.decode('utf-8'))

    assert payload['redis_key'] == '20xiasha:20260718'
    assert payload['kline_redis_key'] == 'qmt:kline:api:realtime:20260718'
    assert payload['count'] == 2
    assert payload['kline_count'] == 1
    assert payload['missing_codes'] == ['000002']
    assert payload['kline_updated_at'] == '2026-07-18 14:35:21'
    assert payload['kline_oldest_snapshot_at'] == '2026-07-18 14:35:20'
    assert payload['kline_newest_snapshot_at'] == '2026-07-18 14:35:20'
    assert payload['kline_stale_count'] == 0
    by_code = {item['code']: item for item in payload['data']}
    assert set(by_code['000001']) == {
        'code',
        'name',
        'stock_code_with_market',
        'ref_gain',
        'today_change',
        'is_high_gain',
        'close_below_ma5_abs_pct_sum_20d',
        'low_above_ma5_count_20d',
        'low_above_ma5_count_10d',
        'purple_match',
        'group_color',
        'group_name',
        'purple_reasons',
        'kline_data',
        'kline_snapshot_at',
        'kline_published_at',
    }
    assert by_code['000001']['close_below_ma5_abs_pct_sum_20d'] == 1.25
    assert by_code['000001']['is_high_gain'] is True
    assert by_code['000001']['low_above_ma5_count_20d'] == 7
    assert by_code['000001']['low_above_ma5_count_10d'] == 6
    assert by_code['000001']['purple_match'] == 1
    assert by_code['000001']['group_color'] == 'purple'
    assert by_code['000001']['group_name'] == '紫色组'
    assert by_code['000001']['purple_reasons'] == [
        'low_above_ma5_count_20d_gte_7'
    ]
    assert by_code['000001']['kline_data'][-1] == {
        'trade_date': '20260718',
        'open': 10.5,
        'high': 10.9,
        'low': 10.4,
        'close': 10.8,
    }
    assert by_code['000001']['kline_snapshot_at'] == '2026-07-18 14:35:20'
    assert by_code['000001']['kline_published_at'] == '2026-07-18 14:35:21'
    assert by_code['000002']['is_high_gain'] is False
    assert by_code['000002']['kline_data'] == []
    mock_r.hscan.assert_not_called()


def test_yxcs_klines_api_encrypts_all_stock_fields_and_corresponding_ohlc(client):
    encryption_key = bytes(range(32))
    encryption_key_b64 = base64.b64encode(encryption_key).decode('ascii')
    first_stock = {
        'code': '000001',
        'name': '平安银行',
        'stock_code_with_market': '000001.SZ',
        'yin_count_20': 2,
        'ref_gain': 3.21,
        'custom_redis_field': {'kept': True},
        'ref_high_gain_ok': True,
        'ref_is_limit_up': False,
    }
    second_stock = {
        'code': '000002',
        'name': '万科A',
        'stock_code_with_market': '000002.SZ',
        'yin_count_20': 1,
        'ref_gain': -1.2,
        'custom_list': [1, 2, 3],
        'ref_high_gain_ok': False,
        'is_limit_up': 1,
    }
    stock_payloads = {
        b'000001': json.dumps(first_stock, ensure_ascii=False),
        b'000002': json.dumps(second_stock, ensure_ascii=False),
    }
    kline_payloads = {
        ('qmt:kline:api:history:20260710', '000001'): json.dumps({
            'code': '000001',
            'name': '平安银行',
            'rows': [
                {'trade_date': '20260709', 'open': 10.2, 'high': 10.7, 'low': 10.1, 'close': 10.6},
            ],
        }, ensure_ascii=False),
        ('qmt:kline:api:realtime:20260710', '000001'): json.dumps({
            'code': '000001',
            'name': '平安银行',
            'snapshot_at': '2026-07-10 14:35:20',
            'published_at': '2026-07-10 14:35:21',
            'row': {
                'trade_date': '20260710',
                'open': 10.6,
                'high': 10.9,
                'low': 10.4,
                'close': 10.8,
            },
        }, ensure_ascii=False),
    }
    mock_r = MagicMock()
    mock_r.exists.side_effect = lambda key: key == 'yxcs:20260710'
    mock_r.type.side_effect = lambda key: 'hash' if key == 'yxcs:20260710' else None
    mock_r.hscan.return_value = (0, stock_payloads)
    mock_r.hget.side_effect = lambda key, field: kline_payloads.get((key, field))

    with patch('app.get_redis_client', return_value=mock_r), patch(
        'app._kline_api_stale_count', return_value=0
    ), patch.dict(
        os.environ, {'KLINE_API_AES_KEY_B64': encryption_key_b64}
    ):
        resp = client.get('/api/yxcs/klines?date=2026-07-10&days=48')
        envelope = json.loads(resp.data)
        plain_resp = client.get('/api/yxcs/klines/plain?date=2026-07-10&days=48')
        plain_payload = json.loads(plain_resp.data)

    assert resp.status_code == 200
    assert set(envelope) == {
        'success', 'encrypted', 'algorithm', 'version', 'nonce', 'ciphertext'
    }
    assert envelope['success'] is True
    assert envelope['encrypted'] is True
    plaintext = AESGCM(encryption_key).decrypt(
        base64.b64decode(envelope['nonce'], validate=True),
        base64.b64decode(envelope['ciphertext'], validate=True),
        b'yxcs-klines:v1',
    )
    payload = json.loads(plaintext.decode('utf-8'))

    assert plain_resp.status_code == 200
    assert plain_payload == payload
    assert plain_payload['success'] is True
    assert 'encrypted' not in plain_payload

    assert payload['success'] is True
    assert payload['date'] == '2026-07-10'
    assert payload['days'] == 48
    assert payload['count'] == 2
    assert payload['kline_count'] == 1
    assert payload['missing_codes'] == ['000002']
    assert payload['kline_updated_at'] == '2026-07-10 14:35:21'
    assert payload['kline_redis_key'] == 'qmt:kline:api:realtime:20260710'
    assert payload['kline_oldest_snapshot_at'] == '2026-07-10 14:35:20'
    assert payload['kline_newest_snapshot_at'] == '2026-07-10 14:35:20'
    assert payload['kline_stale_count'] == 0
    by_code = {item['code']: item for item in payload['data']}
    assert by_code['000001']['stock'] == first_stock
    assert by_code['000001']['ref_high_gain_ok'] is True
    assert by_code['000001']['ref_is_limit_up'] is False
    assert by_code['000001']['kline_updated_at'] == '2026-07-10 14:35:21'
    assert by_code['000001']['kline_snapshot_at'] == '2026-07-10 14:35:20'
    assert by_code['000001']['kline_published_at'] == '2026-07-10 14:35:21'
    assert by_code['000001']['kline_data'] == [
        {'trade_date': '20260709', 'open': 10.2, 'high': 10.7, 'low': 10.1, 'close': 10.6},
        {'trade_date': '20260710', 'open': 10.6, 'high': 10.9, 'low': 10.4, 'close': 10.8},
    ]
    assert by_code['000002']['stock'] == second_stock
    assert by_code['000002']['ref_high_gain_ok'] is False
    assert by_code['000002']['ref_is_limit_up'] is True
    assert by_code['000002']['kline_updated_at'] == ''
    assert by_code['000002']['kline_data'] == []


def test_yxcs_dates_api_returns_valid_dates_newest_first(client):
    mock_r = MagicMock()
    mock_r.keys.return_value = [
        b'yxcs:20260709',
        b'yxcs:20260711',
        b'yxcs:invalid',
        b'yxcs:20260710',
        b'yxcs:20260710',
    ]

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/yxcs/dates')
        payload = json.loads(resp.data)

    assert resp.status_code == 200
    assert payload == {
        'success': True,
        'dates': ['2026-07-11', '2026-07-10', '2026-07-09'],
    }
    mock_r.keys.assert_called_once_with('yxcs:*')


def test_special5_dates_api_returns_valid_dates_newest_first(client):
    mock_r = MagicMock()
    mock_r.keys.side_effect = lambda pattern: {
        'special5:*': [
            b'special5:20260718',
            b'special5:invalid',
            b'special5:20260719',
            b'special5:20260718',
        ],
        '4above5:*': [
            b'4above5:20260720',
            b'4above5:invalid',
            b'4above5:20260719',
        ],
    }[pattern]

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/special5/dates')
        payload = json.loads(resp.data)

    assert resp.status_code == 200
    assert payload == {
        'success': True,
        'dates': ['2026-07-20', '2026-07-19', '2026-07-18'],
    }
    mock_r.keys.assert_any_call('special5:*')
    mock_r.keys.assert_any_call('4above5:*')
    assert mock_r.keys.call_count == 2


def test_special5_merge_source_rows_preserves_membership_and_merges_overlap_fields():
    import app as flask_app

    merged = flask_app._special5_merge_source_rows(
        [
            ('000001', {'code': '000001', 'name': '甲', 'yin_count_20': 4}),
            ('000002', {'code': '000002', 'name': '乙', 'yin_count_20': 6}),
        ],
        [
            ('000002', {
                'code': '000002',
                'name': '乙',
                'yin_count_20': 5,
                'low_above_ma5_20d_count': 4,
            }),
            ('000003', {
                'code': '000003',
                'name': '丙',
                'yin_count_20': 2,
                'low_above_ma5_20d_count': 7,
            }),
        ],
    )

    by_code = {row['code']: row for row in merged}
    assert set(by_code) == {'000001', '000002', '000003'}
    assert by_code['000001']['special5_member'] == 1
    assert by_code['000001']['fourabove5_member'] == 0
    assert by_code['000002']['special5_member'] == 1
    assert by_code['000002']['fourabove5_member'] == 1
    assert by_code['000002']['info']['low_above_ma5_20d_count'] == 4
    assert by_code['000003']['special5_member'] == 0
    assert by_code['000003']['fourabove5_member'] == 1


def test_special5_klines_api_encrypts_full_market_match_and_ohlc(client):
    encryption_key = bytes(range(32))
    encryption_key_b64 = base64.b64encode(encryption_key).decode('ascii')
    stock = {
        'code': '000001',
        'name': '平安银行',
        'stock_code_with_market': '000001.SZ',
        'yin_count_20': 9,
        'ref_gain': 4.2,
        'max_gain': 7.1,
        'is_high_gain': 1,
        'ref_is_limit_up': False,
        'low_above_ma5_count_10d': 6,
        'low_above_ma5_count_20d': 11,
        'custom_redis_field': {'kept': True},
    }
    mock_r = MagicMock()
    mock_r.exists.side_effect = lambda key: key == 'special5:20260719'
    mock_r.type.side_effect = lambda key: 'hash' if key == 'special5:20260719' else None
    mock_r.hscan.return_value = (0, {b'000001': json.dumps(stock, ensure_ascii=False)})
    mock_r.hget.side_effect = lambda key, field: {
        ('qmt:kline:api:history:20260719', '000001'): json.dumps({
            'code': '000001',
            'rows': [
                {'trade_date': '20260718', 'open': 10.0, 'high': 10.5, 'low': 9.9, 'close': 10.4},
            ],
        }, ensure_ascii=False),
        ('qmt:kline:api:realtime:20260719', '000001'): json.dumps({
            'code': '000001',
            'snapshot_at': '2026-07-19 14:50:00',
            'published_at': '2026-07-19 14:50:01',
            'row': {
                'trade_date': '20260719',
                'open': 10.4,
                'high': 11.1,
                'low': 10.2,
                'close': 10.8,
            },
        }, ensure_ascii=False),
    }.get((key, field))

    with patch('app.get_redis_client', return_value=mock_r), patch(
        'app._kline_api_stale_count', return_value=0
    ), patch.dict(
        os.environ, {'KLINE_API_AES_KEY_B64': encryption_key_b64}
    ):
        resp = client.get('/api/special5/klines?date=2026-07-19&days=60')
        envelope = json.loads(resp.data)

    assert resp.status_code == 200
    plaintext = AESGCM(encryption_key).decrypt(
        base64.b64decode(envelope['nonce'], validate=True),
        base64.b64decode(envelope['ciphertext'], validate=True),
        b'special5-klines:v1',
    )
    payload = json.loads(plaintext.decode('utf-8'))
    assert payload['redis_key'] == 'special5:20260719'
    assert payload['fourabove5_redis_key'] == '4above5:20260719'
    assert payload['kline_redis_key'] == 'qmt:kline:api:realtime:20260719'
    assert payload['count'] == 1
    assert payload['kline_count'] == 1
    assert payload['kline_oldest_snapshot_at'] == '2026-07-19 14:50:00'
    assert payload['kline_stale_count'] == 0
    assert payload['data'][0]['stock'] == stock
    assert payload['data'][0]['special5_member'] == 1
    assert payload['data'][0]['fourabove5_member'] == 0
    assert payload['data'][0]['kline_updated_at'] == '2026-07-19 14:50:01'
    assert payload['data'][0]['kline_snapshot_at'] == '2026-07-19 14:50:00'
    assert [row['trade_date'] for row in payload['data'][0]['kline_data']] == [
        '20260718', '20260719'
    ]


def test_5lsabove10_dates_api_returns_plain_json_newest_first(client):
    mock_r = MagicMock()
    mock_r.keys.return_value = [
        b'5lsabove10:20260708',
        b'5lsabove10:20260710',
        b'5lsabove10:invalid',
        b'5lsabove10:20261340',
        b'5lsabove10:20260709',
        b'5lsabove10:20260710',
    ]

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/5lsabove10/dates')
        payload = json.loads(resp.data)

    assert resp.status_code == 200
    assert resp.mimetype == 'application/json'
    assert payload == {
        'success': True,
        'dates': ['2026-07-10', '2026-07-09', '2026-07-08'],
    }
    assert payload['success'] is True
    assert payload['dates']
    mock_r.keys.assert_called_once_with('5lsabove10:*')


def test_5lsabove10_klines_api_encrypts_all_stock_fields_and_kline_data(client):
    encryption_key = bytes(range(32))
    encryption_key_b64 = base64.b64encode(encryption_key).decode('ascii')
    stock_info = {
        'code': '000001',
        'name': '平安银行',
        'stock_code_with_market': '000001.SZ',
        'ref_gain': 3.21,
        'ref_high_gain_ok': True,
        'is_ref_yang': 1,
        'ref_is_limit_up': False,
        'high_gain_10d_count': 5,
        'yin_count_10d': 2,
        'all_yin_low_gte_ma10_10d': 1,
        'yin_low_ma10_details': [
            {'date': '20260709', 'low': 10.2, 'ma10': 10.1, 'passed': 1},
        ],
    }
    mock_r = MagicMock()
    mock_r.exists.side_effect = lambda key: key == '5lsabove10:20260710'
    mock_r.type.side_effect = lambda key: 'hash' if key == '5lsabove10:20260710' else None
    mock_r.hscan.return_value = (
        0,
        {b'000001': json.dumps(stock_info, ensure_ascii=False)},
    )
    mock_r.hget.side_effect = lambda key, field: (
        json.dumps({
            'code': '000001',
            'name': '平安银行',
            'rows': [
                {'trade_date': '20260709', 'open': 10.2, 'high': 10.7, 'low': 10.1, 'close': 10.6},
            ],
        }, ensure_ascii=False)
        if (key, field) == ('qmt:kline:api:history:20260710', '000001')
        else json.dumps({
            'code': '000001',
            'name': '平安银行',
            'snapshot_at': '2026-07-10 14:49:59',
            'published_at': '2026-07-10 14:50:00',
            'row': {
                'trade_date': '20260710',
                'open': 10.6,
                'high': 10.9,
                'low': 10.4,
                'close': 10.8,
            },
        }, ensure_ascii=False)
        if (key, field) == ('qmt:kline:api:realtime:20260710', '000001')
        else None
    )

    with patch('app.get_redis_client', return_value=mock_r), patch(
        'app._kline_api_stale_count', return_value=0
    ), patch.dict(
        os.environ, {'KLINE_API_AES_KEY_B64': encryption_key_b64}
    ):
        resp = client.get('/api/5lsabove10/klines?date=2026-07-10&days=48')
        envelope = json.loads(resp.data)

    assert resp.status_code == 200
    assert set(envelope) == {
        'success', 'encrypted', 'algorithm', 'version', 'nonce', 'ciphertext'
    }
    assert 'data' not in envelope
    plaintext = AESGCM(encryption_key).decrypt(
        base64.b64decode(envelope['nonce'], validate=True),
        base64.b64decode(envelope['ciphertext'], validate=True),
        b'5lsabove10-klines:v1',
    )
    payload = json.loads(plaintext.decode('utf-8'))

    assert payload['success'] is True
    assert payload['redis_key'] == '5lsabove10:20260710'
    assert payload['kline_redis_key'] == 'qmt:kline:api:realtime:20260710'
    assert payload['count'] == 1
    assert payload['kline_count'] == 1
    assert payload['missing_codes'] == []
    assert payload['kline_updated_at'] == '2026-07-10 14:50:00'
    assert payload['kline_oldest_snapshot_at'] == '2026-07-10 14:49:59'
    assert payload['kline_stale_count'] == 0
    item = payload['data'][0]
    assert item['code'] == '000001'
    assert item['name'] == '平安银行'
    assert item['stock'] == stock_info
    assert item['ref_high_gain_ok'] is True
    assert item['ref_is_limit_up'] is False
    assert item['kline_updated_at'] == '2026-07-10 14:50:00'
    assert item['kline_snapshot_at'] == '2026-07-10 14:49:59'
    assert item['kline_published_at'] == '2026-07-10 14:50:00'
    assert item['kline_data'] == [
        {'trade_date': '20260709', 'open': 10.2, 'high': 10.7, 'low': 10.1, 'close': 10.6},
        {'trade_date': '20260710', 'open': 10.6, 'high': 10.9, 'low': 10.4, 'close': 10.8},
    ]


def test_ths_dates_api_returns_plain_valid_dates_newest_first(client):
    mock_r = MagicMock()
    mock_r.get.return_value = None
    mock_r.keys.return_value = [
        b'ths:20260710',
        b'ths:20260712:timestamp',
        b'ths:latest',
        b'ths:20260712',
        b'ths:20261340',
        b'ths:invalid',
        b'ths:20260711',
    ]

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ths/dates')
        payload = json.loads(resp.data)

    assert resp.status_code == 200
    assert resp.mimetype == 'application/json'
    assert payload == {
        'success': True,
        'dates': ['2026-07-12', '2026-07-11', '2026-07-10'],
        'latest_date': '',
    }
    mock_r.keys.assert_called_once_with('ths:*')


def test_ths_klines_api_uses_requested_date_kline_instead_of_stale_snapshot(client):
    encryption_key = bytes(range(32))
    encryption_key_b64 = base64.b64encode(encryption_key).decode('ascii')
    scan_info = {
        'source': 'qmt_ths_cleaning',
        'source_key': 'ths:20260713',
        'selected_count': 1,
        'purple_count': 1,
        'red_count': 0,
        'updated_at': '2026-07-13 15:06:19',
    }
    cleaning_stock = {
        'code': '300039',
        'name': '上海凯宝',
        'stock_code_with_market': '300039.SZ',
        'board_names': ['中药', '创新药'],
        'ths_change_percent': 15.81,
        'group_color': 'purple',
        'group_name': '紫色组',
        'purple_match': 1,
        'red_match': 0,
        'match_reasons': ['all_yin_no_touch'],
    }
    kline_payload = {
        'code': '300039',
        'name': '上海凯宝',
        'updated_at': '2026-07-13 17:29:33',
        'rows': [
            {
                'trade_date': '20260713',
                'open': 4.51,
                'high': 5.27,
                'low': 4.50,
                'close': 5.20,
            },
        ],
    }
    mock_r = MagicMock()
    mock_r.get.return_value = None
    mock_r.hgetall.return_value = {
        '_scan_info': json.dumps(scan_info, ensure_ascii=False),
        '_temporary_meta': json.dumps({'ignored': True}),
        '300039': json.dumps(cleaning_stock, ensure_ascii=False),
    }
    mock_r.hget.side_effect = lambda key, field: (
        json.dumps(kline_payload, ensure_ascii=False)
        if (key, field) == ('qmt:kline:20260713', '300039') else None
    )

    with patch('app.get_redis_client', return_value=mock_r), patch.dict(
        os.environ, {'KLINE_API_AES_KEY_B64': encryption_key_b64}
    ):
        resp = client.get('/api/ths/klines?date=2026-07-13&days=30')
        envelope = json.loads(resp.data)
        plain_resp = client.get('/api/ths/klines/plain?date=2026-07-13&days=30')
        plain_payload = json.loads(plain_resp.data)

    assert resp.status_code == 200
    assert set(envelope) == {
        'success', 'encrypted', 'algorithm', 'version', 'nonce', 'ciphertext'
    }
    plaintext = AESGCM(encryption_key).decrypt(
        base64.b64decode(envelope['nonce'], validate=True),
        base64.b64decode(envelope['ciphertext'], validate=True),
        b'ths-klines:v1',
    )
    payload = json.loads(plaintext.decode('utf-8'))

    assert plain_resp.status_code == 200
    assert plain_payload == payload
    assert plain_payload['success'] is True
    assert 'encrypted' not in plain_payload

    assert payload['success'] is True
    assert payload['date'] == '2026-07-13'
    assert payload['days'] == 30
    assert payload['redis_key'] == 'thscleaning:20260713'
    assert payload['kline_redis_key'] == 'qmt:kline:20260713'
    assert payload['kline_ref_date'] == '2026-07-13'
    assert payload['board_count'] == 2
    assert payload['stock_count'] == 2
    assert payload['unique_stock_count'] == 1
    assert payload['kline_count'] == 1
    assert payload['missing_codes'] == []
    assert payload['meta'] == scan_info
    assert [board['board_name'] for board in payload['boards']] == ['中药', '创新药']
    for board in payload['boards']:
        first_stock = board['stocks'][0]
        assert first_stock['code'] == '300039'
        assert first_stock['ths_change_percent'] == 15.81
        assert first_stock['group_color'] == 'purple'
        assert first_stock['group_name'] == '紫色组'
        assert first_stock['purple_match'] == 1
        assert first_stock['match_reasons'] == ['all_yin_no_touch']
        assert first_stock['kline_updated_at'] == '2026-07-13 17:29:33'
        assert first_stock['kline_data'] == [
            {
                'trade_date': '20260713',
                'open': 4.51,
                'high': 5.27,
                'low': 4.5,
                'close': 5.2,
            },
        ]
    assert mock_r.hgetall.call_count == 2
    mock_r.hgetall.assert_called_with('thscleaning:20260713')
    assert mock_r.get.call_count == 2
    mock_r.get.assert_called_with('ths:20260713')
    mock_r.hget.assert_any_call('qmt:kline:20260713', '300039')
    assert all(
        call.args[0] != 'qmt:kline:20260710'
        for call in mock_r.hget.call_args_list
    )


def test_ths_klines_api_sorts_boards_and_stocks_by_gain_descending(client):
    encryption_key = bytes(range(32))
    encryption_key_b64 = base64.b64encode(encryption_key).decode('ascii')
    scan_info = {
        'source': 'qmt_ths_cleaning',
        'source_key': 'ths:20260715',
        'selected_count': 5,
    }
    source_payload = {
        'boards': [
            {'board_name': '低涨板块', 'change_percent': 1.2, 'limit_up_count': 2},
            {'board_name': '高涨板块', 'change_percent': 5.2, 'limit_up_count': 6},
        ],
    }
    mock_r = MagicMock()
    mock_r.hgetall.return_value = {
        '_scan_info': json.dumps(scan_info, ensure_ascii=False),
        '000004': json.dumps({
            'code': '000004',
            'name': '低涨股票',
            'board_names': ['低涨板块'],
            'ref_gain': 8.0,
        }, ensure_ascii=False),
        '000005': json.dumps({
            'code': '000005',
            'name': '未知股票',
            'board_names': ['未知板块'],
            'board_limit_up_counts': {'未知板块': 4},
            'ref_gain': 9.0,
        }, ensure_ascii=False),
        '000003': json.dumps({
            'code': '000003',
            'name': '并列股票乙',
            'board_names': ['高涨板块'],
            'ref_gain': 5.0,
        }, ensure_ascii=False),
        '000001': json.dumps({
            'code': '000001',
            'name': '并列股票甲',
            'board_names': ['高涨板块'],
            'ref_gain': 5.0,
        }, ensure_ascii=False),
        '000002': json.dumps({
            'code': '000002',
            'name': '实时涨幅股票',
            'board_names': ['高涨板块'],
            'today_change': 7.0,
            'ref_gain': -2.0,
        }, ensure_ascii=False),
    }
    mock_r.get.side_effect = lambda key: (
        json.dumps(source_payload, ensure_ascii=False)
        if key == 'ths:20260715' else None
    )
    mock_r.hget.return_value = None

    with patch('app.get_redis_client', return_value=mock_r), patch.dict(
        os.environ, {'KLINE_API_AES_KEY_B64': encryption_key_b64}
    ):
        response = client.get('/api/ths/klines?date=2026-07-15&days=30')
        envelope = json.loads(response.data)

    plaintext = AESGCM(encryption_key).decrypt(
        base64.b64decode(envelope['nonce'], validate=True),
        base64.b64decode(envelope['ciphertext'], validate=True),
        b'ths-klines:v1',
    )
    payload = json.loads(plaintext.decode('utf-8'))

    assert response.status_code == 200
    assert [board['board_name'] for board in payload['boards']] == [
        '高涨板块', '低涨板块', '未知板块',
    ]
    assert [board['board_change_percent'] for board in payload['boards']] == [
        5.2, 1.2, None,
    ]
    assert [board['limit_up_count'] for board in payload['boards']] == [
        6, 2, 4,
    ]
    assert [stock['code'] for stock in payload['boards'][0]['stocks']] == [
        '000002', '000001', '000003',
    ]


def test_ths_klines_api_puts_stock_without_board_names_in_unclassified(client):
    encryption_key = bytes(range(32))
    encryption_key_b64 = base64.b64encode(encryption_key).decode('ascii')
    mock_r = MagicMock()
    mock_r.hgetall.return_value = {
        '_scan_info': json.dumps({'selected_count': 1}),
        '000001': json.dumps({
            'code': '000001',
            'name': '平安银行',
            'group_color': 'red',
            'group_name': '红色组',
            'red_match': 1,
        }, ensure_ascii=False),
    }
    mock_r.hget.return_value = None

    with patch('app.get_redis_client', return_value=mock_r), patch.dict(
        os.environ, {'KLINE_API_AES_KEY_B64': encryption_key_b64}
    ):
        response = client.get('/api/ths/klines?date=2026-07-13&days=30')
        envelope = json.loads(response.data)

    plaintext = AESGCM(encryption_key).decrypt(
        base64.b64decode(envelope['nonce'], validate=True),
        base64.b64decode(envelope['ciphertext'], validate=True),
        b'ths-klines:v1',
    )
    payload = json.loads(plaintext.decode('utf-8'))
    assert response.status_code == 200
    assert payload['board_count'] == 1
    assert payload['boards'][0]['board_name'] == '未分类'
    stock = payload['boards'][0]['stocks'][0]
    assert stock['group_color'] == 'red'
    assert stock['group_name'] == '红色组'


def test_ths_klines_api_returns_404_when_cleaning_snapshot_is_missing(client):
    mock_r = MagicMock()
    mock_r.hgetall.return_value = {}

    with patch('app.get_redis_client', return_value=mock_r):
        response = client.get('/api/ths/klines?date=2026-07-13&days=30')
        payload = json.loads(response.data)

    assert response.status_code == 404
    assert payload['success'] is False
    assert 'thscleaning:20260713' in payload['error']


def test_ztorgj_weak20_kline_stocks_api_filters_new_high_and_sorts(client):
    payloads = {
        '000001': json.dumps({'code': '000001', 'name': '平安银行', 'weak_yin_20d': 2, 'yin_count_20': 3, 'is_20d_new_high': 1}, ensure_ascii=False),
        '000002': json.dumps({'code': '000002', 'name': '万科A', 'weak_yin_20d': 1, 'yin_count_20': 4, 'is_20d_new_high': 0}, ensure_ascii=False),
        '000003': json.dumps({'code': '000003', 'name': '国华网安', 'weak_yin_20d': 1, 'yin_count_20': 2, 'is_20d_new_high': 1}, ensure_ascii=False),
    }
    mock_r = _mock_ruoyinxian_redis(payloads)

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ztorgj/weak20/kline-stocks?date=2026-04-21&new_high_only=1')
        data = json.loads(resp.data)

    assert resp.status_code == 200
    assert data['success'] is True
    assert data['date'] == '2026-04-21'
    assert data['new_high_only'] is True
    assert data['count'] == 2
    assert [item['name'] for item in data['data']] == ['国华网安', '平安银行']
    assert data['data'][0]['stock_code_with_market'] == '000003.SZ'
    assert data['data'][0]['weak_yin_20d'] == 1
    assert data['data'][0]['yin_count_20'] == 2
    assert data['data'][0]['is_20d_new_high'] == 1


def test_ztorgj_weak20_kline_stocks_api_can_limit_weak_yin_20d(client):
    payloads = {
        '000001': json.dumps({'code': '000001', 'name': '平安银行', 'weak_yin_20d': 2, 'yin_count_20': 3, 'is_20d_new_high': 1}, ensure_ascii=False),
        '000002': json.dumps({'code': '000002', 'name': '万科A', 'weak_yin_20d': 4, 'yin_count_20': 1, 'is_20d_new_high': 1}, ensure_ascii=False),
        '000003': json.dumps({'code': '000003', 'name': '国华网安', 'weak_yin_20d': 3, 'yin_count_20': 2, 'is_20d_new_high': 0}, ensure_ascii=False),
    }
    mock_r = _mock_ruoyinxian_redis(payloads)

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ztorgj/weak20/kline-stocks?date=2026-04-21&max_weak_yin_20d=3')
        data = json.loads(resp.data)

    assert resp.status_code == 200
    assert data['success'] is True
    assert data['max_weak_yin_20d'] == 3
    assert [item['name'] for item in data['data']] == ['平安银行', '国华网安']
    assert all(item['weak_yin_20d'] <= 3 for item in data['data'])


def test_ztorgj_weak20_kline_stocks_api_matches_visible_group_order_by_gain(client):
    payloads = {
        '000001': json.dumps({'code': '000001', 'name': '平安银行', 'weak_yin_20d': 1, 'yin_count_20': 1, 'ref_gain': 2.0}, ensure_ascii=False),
        '000002': json.dumps({'code': '000002', 'name': '万科A', 'weak_yin_20d': 1, 'yin_count_20': 9, 'ref_gain': 8.0}, ensure_ascii=False),
        '000003': json.dumps({'code': '000003', 'name': '国华网安', 'weak_yin_20d': 2, 'yin_count_20': 1, 'today_change': 20.0}, ensure_ascii=False),
        '000004': json.dumps({'code': '000004', 'name': '北方国际', 'weak_yin_20d': 1, 'yin_count_20': 2, 'today_change': 8.0}, ensure_ascii=False),
    }
    mock_r = _mock_ruoyinxian_redis(payloads)

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ztorgj/weak20/kline-stocks?date=2026-04-21&max_weak_yin_20d=3')
        data = json.loads(resp.data)

    assert resp.status_code == 200
    assert [item['code'] for item in data['data']] == ['000002', '000004', '000001', '000003']


def test_ztorgj_weak20_kline_page_contains_stock_list_and_kline_renderer(client):
    resp = client.get('/涨停或光脚/弱阴线K线?date=2026-04-21&new_high_only=1&max_weak_yin_20d=3')
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert '弱阴线K线图' in html
    assert 'echarts.min.js' in html
    assert 'weak20KlineRowList' in html
    assert 'kline-name-cell' in html
    assert 'kline-chart-cell' in html
    assert 'kline-chart-small' in html
    assert 'weak20-kline-group-title' in html
    assert 'buildWeak20KlineGroupedRows' in html
    assert '20日弱阴线次数：' in html
    assert 'WEAK20_KLINE_COLLAPSED_GROUP_VALUES' in html
    assert "new Set(['3', '4', '5', '>5'])" in html
    assert 'function isWeak20KlineGroupCollapsed(groupValue)' in html
    assert 'function toggleWeak20KlineGroup(groupValue)' in html
    assert 'weak20-kline-group-toggle' in html
    assert '显示折叠的K线图' in html
    assert 'data-kline-group-value="' in html
    assert 'if (isWeak20KlineStockCollapsed(index)) continue;' in html
    assert 'await loadWeak20KlineGroupCharts(groupValue);' in html
    assert 'kline-two-column-grid' in html
    assert 'grid-template-columns: repeat(3, 620px)' in html
    assert 'width: 600px' in html
    assert 'min-height: 392px' in html
    assert 'height: 320px' in html
    assert '{ left: 42, right: 52, top: 60, height: 230 }' in html
    assert 'volumeData' not in html
    assert "'成交量'" not in html
    assert 'xAxisIndex: 1' not in html
    assert "type: 'slider'" not in html
    assert 'renderAllWeak20KlineCharts' in html
    assert 'renderWeak20KlineChartCards' in html
    assert 'loadWeak20KlineChartBatch' in html
    assert 'WEAK20_KLINE_CONCURRENCY' in html
    assert 'scrollWeak20KlineChartIntoView' not in html
    assert 'left-panel' not in html
    assert '/api/ztorgj/weak20/kline-stocks' in html
    assert '/api/ztorgj/kline' in html
    assert 'KLINE_THEME_VARIANTS' in html
    assert 'calcKlineMA' in html
    assert 'buildKlineOption' in html
    assert "days: '30'" in html
    assert "end_date" in html
    assert "new_high_only: '1'" in html
    assert "max_weak_yin_20d: '3'" in html
    assert "order_token: '" in html
    assert 'applyWeak20KlineStoredOrder' in html
    assert 'localStorage.getItem' in html
    assert 'Array.isArray(saved.stocks)' in html
    assert 'return saved.stocks.filter(function(stock)' in html
    assert '[ztorgj][weak20-kline][fetch-start]' in html
    assert '[ztorgj][weak20-kline][fetch-http]' in html
    assert '[ztorgj][weak20-kline][fetch-payload]' in html
    assert '[ztorgj][weak20-kline][render-fail]' in html


def test_ztorgj_kline_api_has_diagnostic_logs(client):
    import app as flask_app
    source = inspect.getsource(flask_app.get_ztorgj_kline)

    assert '[ztorgj][kline][redis-request]' in source
    assert '[ztorgj][kline][redis-miss]' in source
    assert '[ztorgj][kline][redis-hit]' in source
    assert '[ztorgj][kline][success]' in source
    assert '[ztorgj][kline][fail]' in source
    assert 'ts.pro_api' not in source
    assert 'pro.daily' not in source
    assert 'realtime_quote' not in source
    assert 'traceback.format_exc()' in source


def test_ztorgj_template_contains_kline_screening_workbench(client):
    resp = client.get('/涨停或光脚')
    html = resp.get_data(as_text=True)

    assert 'echarts.min.js' in html
    assert "key: 'klineTest'" in html
    assert "label: 'K线测试'" in html
    assert 'renderKlineTestPanel' in html
    assert 'renderAllKlineCharts' in html
    assert 'KLINE_THEME_VARIANTS' in html
    assert '方案A 通达信白底' in html
    assert '方案B 通达信黑底' in html
    assert '方案C Web简洁版' not in html
    assert '第一轮筛选开始！请筛选' not in html
    assert '添加到一字特征' not in html
    assert '添加到厂子特征' not in html
    assert '添加到厂厂字特征' not in html
    assert '添加到横盘调整特征待观察' not in html
    assert '无特征' not in html
    assert '极品关注' in html
    assert '下一只' in html
    assert '上一只' in html
    assert 'goPreviousKlineStock' in html
    assert '现在筛选阴线都是十字星且不碰的股票。' in html
    assert '现在筛选阴线都不碰的股票。' in html
    assert '现在筛选阴线都向上的股票' in html
    assert '下面进入阳线 且 最高涨幅&gt;=6.50%的股票进行筛选' in html or '下面进入阳线 且 最高涨幅>=6.50%的股票进行筛选' in html
    assert 'runKlineScreeningStage' in html
    assert 'saveScreeningTask' not in html
    assert 'saveJipinFocus' in html
    assert 'all_yin_no_touch' in html
    assert 'all_yin_cross_star' in html
    assert 'all_yin_up' in html
    assert 'layang' in html
    assert '/api/ztorgj/kline' in html
    assert "days: '30'" in html
    assert '/api/ztorgj/kline-screening/state' in html
    assert 'loadKlineScreeningState' in html
    assert 'persistKlineScreeningState' in html
    assert 'restartKlineScreening' in html
    assert '重新开始' in html
    assert '筛选完成' in html
    assert 'pctColor' in html
    assert '涨幅' in html
    assert '显示突破' not in html
    assert '筛选跌破天数' in html
    assert 'showHuiche6BelowTopDayFilterDialog' in html
    assert 'top_day_close_below_count' in html
    assert '跌破天数小于等于1' in html
    assert '跌破天数小于等于2' in html
    assert '设置' not in html
    assert '隐藏第一轮' not in html
    assert 'KLINE_SCREENING_SETTINGS' not in html
    assert 'showKlineSettings' not in html
    assert 'loadSecondRoundSourceFromTasks' not in html
    assert 'startDirectSecondRoundScreening' in html
    assert 'loadSecondRoundStageSourceFromFields' in html
    assert 'getSecondRoundFieldSourceStocks' in html


def test_ztorgj_stocks_api_loads_lstz_dataset(client):
    mock_r = MagicMock()

    def exists_side_effect(key):
        return key == 'lstz:20260421'

    def type_side_effect(key):
        return 'hash' if key == 'lstz:20260421' else None

    def hscan_side_effect(key, cursor, count=100):
        if key != 'lstz:20260421':
            return 0, {}
        return 0, {
            '000001': json.dumps({
                'code': '000001',
                'name': '平安银行',
                'ref_gain': 5.25,
                'max_gain': 7.12,
                'yin_below_ma5_count': 1,
                'lstz_new_high_pct': 3.45,
                'max_rise_top_day': '20260417',
                'top_day_close': 12.34,
                'is_limit_up': 0,
                'is_nearly_bare_foot_yang': 1,
                'is_high_gain': 1,
                'ref_shape_ok': 1,
            }, ensure_ascii=False),
        }

    mock_r.exists.side_effect = exists_side_effect
    mock_r.type.side_effect = type_side_effect
    mock_r.hscan.side_effect = hscan_side_effect

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ztorgj/stocks?date=2026-04-21')
        data = json.loads(resp.data)

    assert resp.status_code == 200
    assert data['success'] is True
    assert len(data['datasets']['lstz']) == 1
    stock = data['datasets']['lstz'][0]
    assert stock['code'] == '000001'
    assert stock['yin_below_ma5_count'] == 1
    assert stock['lstz_new_high_pct'] == pytest.approx(3.45)
    assert stock['max_rise_top_day'] == '20260417'
    assert stock['top_day_close'] == pytest.approx(12.34)
    assert stock['is_nearly_bare_foot_yang'] == 1
    assert stock['ref_shape_ok'] == 1


def test_ztorgj_stocks_api_loads_lsbp_dataset(client):
    mock_r = MagicMock()

    def exists_side_effect(key):
        return key == 'lsbp:20260421'

    def type_side_effect(key):
        return 'hash' if key == 'lsbp:20260421' else None

    def hscan_side_effect(key, cursor, count=100):
        if key != 'lsbp:20260421':
            return 0, {}
        return 0, {
            '000001': json.dumps({
                'code': '000001',
                'name': '平安银行',
                'ref_gain': 4.25,
                'max_gain': 7.12,
                'ref_low_ma5_distance_pct': 2.34,
                'ref_lower_shadow_pct': 0.56,
                'high_gain_10d_count': 6,
                'high_gain_10d_gte_5': 1,
                'yin_count_20': 3,
                'yin_count_10d': 1,
                'yin_count_10': 1,
                'ref_high_gain_ok': True,
                'ref_low_above_ma5': 1,
                'ref_high_gain_gte_6_5_and_yang': True,
                'ref_high_gain_gte_6_5_yang_low_above_ma5': False,
                'yin_close_below_ma5_count_10d': 1,
                'yin_close_below_ma5_lte_2_10d': 1,
            }, ensure_ascii=False),
        }

    mock_r.exists.side_effect = exists_side_effect
    mock_r.type.side_effect = type_side_effect
    mock_r.hscan.side_effect = hscan_side_effect

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ztorgj/stocks?date=2026-04-21')
        data = json.loads(resp.data)

    assert resp.status_code == 200
    assert data['success'] is True
    assert len(data['datasets']['lsbp']) == 1
    stock = data['datasets']['lsbp'][0]
    assert stock['code'] == '000001'
    assert stock['ref_low_ma5_distance_pct'] == pytest.approx(2.34)
    assert stock['ref_lower_shadow_pct'] == pytest.approx(0.56)
    assert stock['high_gain_10d_gte_5'] == 1
    assert stock['yin_count_20'] == 3
    assert stock['yin_count_10d'] == 1
    assert stock['ref_high_gain_gte_6_5_and_yang'] is True
    assert stock['ref_high_gain_ok'] is True
    assert stock['ref_low_above_ma5'] == 1
    assert stock['ref_high_gain_gte_6_5_yang_low_above_ma5'] == 1
    assert stock['yin_close_below_ma5_count_10d'] == 1
    assert stock['yin_close_below_ma5_lte_2_10d'] == 1


def test_yinbreak5_normalize_high_gain_no_touch_no_longer_requires_yang():
    import app as flask_app

    stock = flask_app._yinbreak5_normalize_stock('000001', {
        'code': '000001',
        'name': '平安银行',
        'ref_high_gain_ok': 1,
        'ref_low_above_ma5': 1,
        'ref_high_gain_gte_6_5_and_yang': 0,
        'ref_high_gain_gte_6_5_yang_low_above_ma5': 0,
    })

    assert stock['ref_high_gain_gte_6_5_and_yang'] == 0
    assert stock['ref_high_gain_ok'] == 1
    assert stock['ref_low_above_ma5'] == 1
    assert stock['ref_high_gain_gte_6_5_yang_low_above_ma5'] == 1


def test_ztorgj_template_contains_adjust_break_tab_between_huiche6_and_kline(client):
    resp = client.get('/涨停或光脚')
    html = resp.get_data(as_text=True)

    huiche6_pos = html.index("key: 'huiche6'")
    adjust_pos = html.index("key: 'adjustBreak'")
    kline_pos = html.index("key: 'klineTest'")

    assert huiche6_pos < adjust_pos < kline_pos
    assert "label: '调整突破'" in html
    assert "datasetKey: 'lstz'" in html
    assert "metricKey: 'yin_below_ma5_count'" in html
    assert 'compareYinBelowMa5AscThenGainDesc' in html
    assert "metricLabel: '阴线跌破MA5次数'" in html
    assert "label: '高涨调整>=4天'" in html
    assert "Number(s.is_high_gain || 0) === 1 && Number(s.days_since_top_day || 0) >= 4" in html


def test_ztorgj_template_orders_weak20_columns_and_filters_default_yang_new_high(client):
    resp = client.get('/涨停或光脚')
    html = resp.get_data(as_text=True)

    weak20_pos = html.index("key: 'weak20'")
    next_tab_pos = html.index("key: 'limit20'")
    weak20_block = html[weak20_pos:next_tab_pos]

    all_pos = weak20_block.index("label: '20日弱阴线次数排序'")
    high_pos = weak20_block.index("label: '最高涨幅>=6.85%'")
    limit_pos = weak20_block.index("label: '当日涨停'")
    low_ma5_pos = weak20_block.index('createLowAboveMa5WeakSection()')

    assert all_pos < high_pos < limit_pos < low_ma5_pos
    assert 'createFiveRiseWeakSection()' not in weak20_block
    assert "label: '不碰5日线列表'" in html
    assert ".col-header.metric-low-ma5 { background: linear-gradient(135deg, #be123c, #9f1239); }" in html
    assert ".group-label.metric-low-ma5 { background: rgba(190, 18, 60, 0.9); }" in html
    assert "metricKey: 'weak_yin_20d'" in html
    assert 'compareWeak20AscThenLowAboveMa5Desc' in html
    assert "metricLabel: '20日弱阴线'" in html
    assert "isTrueFlag(s && s.is_ref_yang) && Number(s.low_above_ma5_20d_count || 0) > 0" in html
    assert "isWeak20YangAndNewHighStock(s) && getRefHighGainValue(s) >= 6.85" in html
    assert "isWeak20YangAndNewHighStock(s) && Number(s.is_limit_up || 0) === 1" in html
    assert "默认：阳线+20日新高" in html
    assert "metric.key === 'cum_gain_20d_pct'" in html
    assert "stack.appendChild(buildTaskSection())" in html



def test_ztorgj_kline_screening_state_api_persists_and_loads_progress(client):
    mock_r = MagicMock()
    mock_r.get.return_value = None

    progress = {
        'round': 'second',
        'stageIndex': 2,
        'index': 5,
        'completed': False,
        'firstRoundSelected': [{'code': '688628', 'name': '优利德'}],
        'secondScreenedCodes': ['688628'],
    }
    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.post('/api/ztorgj/kline-screening/state', json={
            'date': '2026-04-30',
            'state': progress,
        })
        data = json.loads(resp.data)

    assert data['success'] is True
    mock_r.set.assert_called_once()
    assert mock_r.set.call_args.args[0] == 'myStock:klineScreening:20260430'
    stored_payload = json.loads(base64.b64decode(mock_r.set.call_args.args[1]).decode('utf-8'))
    assert stored_payload['round'] == 'second'
    assert stored_payload['stageIndex'] == 2
    assert stored_payload['index'] == 5
    assert stored_payload['completed'] is False
    mock_r.expire.assert_called_once_with('myStock:klineScreening:20260430', 30 * 24 * 3600)

    mock_r = MagicMock()
    mock_r.get.return_value = base64.b64encode(json.dumps(progress, ensure_ascii=False).encode('utf-8')).decode('utf-8')
    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/ztorgj/kline-screening/state?date=2026-04-30')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['exists'] is True
    assert data['data']['round'] == 'second'
    assert data['data']['firstRoundSelected'][0]['name'] == '优利德'


def test_ztorgj_kline_screening_state_api_can_reset_progress(client):
    mock_r = MagicMock()
    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.delete('/api/ztorgj/kline-screening/state?date=2026-04-30')
        data = json.loads(resp.data)

    assert data['success'] is True
    mock_r.delete.assert_called_once_with('myStock:klineScreening:20260430')


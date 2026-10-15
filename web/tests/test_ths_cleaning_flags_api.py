"""The existing plain THS API must preserve the scanner's new flags."""

import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def encode(value):
    return base64.b64encode(json.dumps(value).encode("utf-8")).decode("ascii")


def test_plain_ths_api_passes_through_panzhong_flags():
    import app as flask_app

    fields = {
        "is_limit_up": 1, "ref_is_limit_up": True,
        "qiangchi": 1, "yi_yin_yi_yang": 1,
        "yin_count_20": 3, "yin_count_10d": 1, "is_quanyang_10d": 0,
        "ref_high_gain_ok": True,
    }
    stock = {
        "code": "600000", "name": "Sample", "ref_gain": 10.0,
        "board_names": ["Sample Board"], **fields,
    }
    client = MagicMock()
    client.hgetall.return_value = {"600000": encode(stock)}
    client.hget.return_value = encode({
        "ts_code": "600000.SH", "updated_at": "2026-10-15 10:00:00",
        "rows": [{
            "trade_date": "20261015", "open": 10.0, "high": 11.0,
            "low": 10.0, "close": 11.0, "pre_close": 10.0,
            "vol": 1000.0, "amount": 10000.0,
        }],
    })
    with patch("app.get_redis_client", return_value=client):
        with flask_app.app.test_client() as web:
            response = web.get("/api/ths/klines/plain?date=2026-10-15&days=60")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["redis_key"] == "thscleaning:20261015"
    returned = payload["boards"][0]["stocks"][0]
    for key, value in fields.items():
        assert returned[key] == value
        assert type(returned[key]) is type(value)

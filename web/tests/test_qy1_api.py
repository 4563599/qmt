import base64
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def client():
    import app as flask_app

    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as test_client:
        yield test_client


def encode_payload(value):
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def test_qy1_stocks_returns_plain_dynamic_payload_sorted_by_gain(client):
    mock_r = MagicMock()
    mock_r.exists.return_value = True
    mock_r.type.return_value = "hash"
    scan_info = {
        "published_at": "2026-09-14 04:10:44",
        "selection_rule": "gain_gte_5_and_all_yin_no_touch_or_eaten_10d",
    }
    mock_r.hget.return_value = encode_payload(scan_info)
    mock_r.hscan.return_value = (
        0,
        {
            "000001": encode_payload(
                {
                    "code": "000001",
                    "name": "平安银行",
                    "ref_gain": 5.2,
                    "strong_k_count_10d": 6,
                    "custom_future_field": "kept",
                    "updated_at": "2026-09-14 04:10:40",
                }
            ),
            "000002": json.dumps(
                {
                    "code": "000002",
                    "name": "万科A",
                    "today_change": 7.1,
                    "strong_k_count_10d": 4,
                    "updated_at": "2026-09-14 04:10:42",
                },
                ensure_ascii=False,
            ),
            "_scan_info": encode_payload(scan_info),
        },
    )

    with patch("app.get_redis_client", return_value=mock_r):
        response = client.get("/api/qy1/stocks?date=2026-09-11")
        payload = response.get_json()

    assert response.status_code == 200
    assert response.content_type.startswith("application/json")
    assert payload["success"] is True
    assert payload["date"] == "2026-09-11"
    assert payload["redis_key"] == "qy1:20260911"
    assert payload["count"] == 2
    assert payload["scan_info"] == scan_info
    assert payload["updated_at"] == "2026-09-14 04:10:44"
    assert [item["code"] for item in payload["data"]] == ["000002", "000001"]
    assert payload["data"][1]["custom_future_field"] == "kept"
    assert "encrypted" not in payload
    assert "ciphertext" not in payload


def test_qy1_stocks_without_date_uses_latest_strict_qy1_key(client):
    mock_r = MagicMock()
    mock_r.keys.return_value = [
        "qy1:20260910",
        "qy1:20260911",
        "qy1:invalid",
        "qy1:20260912:tmp:abc",
    ]
    mock_r.exists.return_value = True
    mock_r.type.return_value = "hash"
    mock_r.hget.return_value = None
    mock_r.hscan.return_value = (0, {})

    with patch("app.get_redis_client", return_value=mock_r):
        response = client.get("/api/qy1/stocks")
        payload = response.get_json()

    assert response.status_code == 200
    assert payload["date"] == "2026-09-11"
    assert payload["redis_key"] == "qy1:20260911"
    assert payload["data"] == []


def test_qy1_stocks_rejects_invalid_date(client):
    with patch("app.get_redis_client", return_value=MagicMock()):
        response = client.get("/api/qy1/stocks?date=2026/09/11")
        payload = response.get_json()

    assert response.status_code == 400
    assert payload["success"] is False


def test_qy1_klines_returns_plain_stock_and_deduplicated_rows(client):
    mock_r = MagicMock()
    mock_r.exists.return_value = True
    mock_r.type.return_value = "hash"
    stock = {
        "code": "000001",
        "name": "平安银行",
        "ref_gain": 6.2,
        "today_change": 6.2,
        "strong_k_count_10d": 5,
        "is_quanyang_10d": 1,
    }
    scan_info = {"published_at": "2026-09-11 14:58:10"}
    history_payload = {
        "code": "000001",
        "updated_at": "2026-09-11 14:58:00",
        "rows": [
            {"trade_date": "20260909", "open": 10.0, "high": 10.3, "low": 9.9, "close": 10.2},
            {"trade_date": "20260910", "open": 10.2, "high": 10.5, "low": 10.1, "close": 10.4},
        ],
    }
    realtime_payload = {
        "code": "000001",
        "snapshot_at": "2026-09-11 14:58:08",
        "published_at": "2026-09-11 14:58:09",
        "row": {"trade_date": "20260911", "open": 10.4, "high": 11.1, "low": 10.3, "close": 11.0},
    }

    def hget_side_effect(key, field):
        values = {
            ("qy1:20260911", "_scan_info"): encode_payload(scan_info),
            ("qy1:kline:history:20260911", "000001"): encode_payload(history_payload),
            ("qy1:kline:realtime:20260911", "000001"): encode_payload(realtime_payload),
        }
        return values.get((key, field))

    mock_r.hget.side_effect = hget_side_effect
    mock_r.hscan.return_value = (0, {"000001": encode_payload(stock)})

    with patch("app.get_redis_client", return_value=mock_r):
        response = client.get("/api/qy1/klines?date=2026-09-11&days=2")
        payload = response.get_json()

    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["encrypted"] is False
    assert payload["date"] == "2026-09-11"
    assert payload["days"] == 2
    assert payload["count"] == 1
    assert payload["kline_count"] == 1
    assert payload["missing_codes"] == []
    assert payload["data"][0]["stock"]["strong_k_count_10d"] == 5
    assert payload["data"][0]["stock"]["is_quanyang_10d"] == 1
    assert [row["trade_date"] for row in payload["data"][0]["kline_data"]] == [
        "20260910",
        "20260911",
    ]

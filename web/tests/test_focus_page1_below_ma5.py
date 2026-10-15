import base64
import json
import os
import re
import shutil
import subprocess
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


def encoded(record):
    return base64.b64encode(json.dumps(record).encode("utf-8")).decode("ascii")


def test_first_page_below_ma5_group_uses_only_yin7_yang_pz1(client):
    rows = {
        b"000001": encoded({"code": "000001", "yin_count_20": 2,
                             "is_ref_yang": 1, "close_below_ma5_count_20d": 1,
                             "ref_gain": 5.0}),
        b"000002": encoded({"code": "000002", "yin_count_20": 1,
                             "is_ref_yang": 1, "close_below_ma5_count_20d": 0,
                             "ref_gain": 3.0}),
        b"000003": encoded({"code": "000003", "yin_count_20": 7,
                             "is_ref_yang": 1, "close_below_ma5_count_20d": 1,
                             "ref_gain": 8.0}),
        b"000004": encoded({"code": "000004", "yin_count_20": 8,
                             "is_ref_yang": 1, "close_below_ma5_count_20d": 0}),
        b"000005": encoded({"code": "000005", "yin_count_20": 2,
                             "is_ref_yang": 0, "close_below_ma5_count_20d": 0}),
    }
    redis_client = MagicMock()
    redis_client.exists.return_value = True
    redis_client.type.return_value = "hash"
    redis_client.hscan.return_value = (0, rows)

    with patch("app.get_redis_client", return_value=redis_client):
        response = client.get("/api/pz1/focus?date=2026-09-29&page=1")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["redis_key"] == "pz1:20260929"
    assert [(item["code"], item["close_below_ma5_count_20d"])
            for item in payload["groups"]["pz_all"]] == [
        ("000002", 0), ("000001", 1), ("000003", 1),
    ]


def test_first_page_has_second_group_section_and_valid_script(client):
    html = client.get("/重点关注").get_data(as_text=True)

    assert html.index('id="focusPage1Groups"') < html.index('id="focusPage1BelowGroups"')
    assert "阴线跌破5日线分组" in html
    assert 'data-below-count="${escapeHtml(key)}"' in html
    assert "state.page1GroupMode = 'below'" in html
    assert "state.page1GroupMode = 'yin'" in html
    assert "const belowCount = closeBelowMa5Count20d(stock);" in html
    assert "if (belowCount !== null && belowCount <= 3)" in html
    assert "const prioritySection = priorityRows" in html
    assert "const belowKeys = sortedKeys(groupedByBelow);" in html
    assert "focusStockGain(right) - focusStockGain(left)" in html

    if shutil.which("node"):
        script = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)[-1]
        result = subprocess.run(["node", "--check", "-"], input=script,
                                text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr

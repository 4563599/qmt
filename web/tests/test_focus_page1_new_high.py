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


def test_first_page_new_high_checkbox_follows_quanyang_and_is_page_scoped(client):
    html = client.get("/重点关注").get_data(as_text=True)

    assert html.index('id="focusQuanyangFilterLabel"') < html.index(
        'id="focusNewHighFilterLabel"'
    )
    assert 'id="focusNewHighOnly"' in html
    assert "if (newHighFilterLabel) newHighFilterLabel.hidden = !firstPage;" in html
    assert "state.focusPage === 1 && state.newHighOnly" in html
    assert "state.newHighOnly = newHighFilter.checked" in html
    assert "state.newHighOnly ? '新高' : ''" in html

    if shutil.which("node"):
        script = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)[-1]
        result = subprocess.run(
            ["node", "--check", "-"], input=script,
            text=True, capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr


def test_first_page_api_keeps_new_high_field_as_stored(client):
    redis_client = MagicMock()
    redis_client.exists.return_value = True
    redis_client.type.return_value = "hash"
    redis_client.hscan.return_value = (0, {
        b"000001": encoded({
            "code": "000001", "yin_count_20": 2,
            "is_ref_yang": 1, "is_ref_price_20d_high": 1,
        }),
        b"000002": encoded({
            "code": "000002", "yin_count_20": 2,
            "is_ref_yang": 1, "is_ref_price_20d_high": False,
        }),
    })
    with patch("app.get_redis_client", return_value=redis_client):
        response = client.get("/api/pz1/focus?date=2026-09-30&page=1")

    assert response.status_code == 200
    records = response.get_json()["groups"]["pz_all"]
    assert [(item["code"], item["is_ref_price_20d_high"]) for item in records] == [
        ("000001", 1), ("000002", False),
    ]

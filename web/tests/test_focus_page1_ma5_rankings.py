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

FIELDS = (
    "yin_close_below_ma5_max_abs_pct_20d",
    "yin_close_below_ma5_drop_pct_sum_20d",
    "yin_close_below_ma5_body_pct_sum_20d",
)


@pytest.fixture
def client():
    import app as flask_app

    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as test_client:
        yield test_client


def encoded(record):
    return base64.b64encode(json.dumps(record).encode("utf-8")).decode("ascii")


def test_focus_api_preserves_three_pz1_metrics(client):
    redis_client = MagicMock()
    redis_client.exists.return_value = True
    redis_client.type.return_value = "hash"
    redis_client.hscan.return_value = (0, {
        b"000001": encoded({
            "code": "000001", "yin_count_20": 3, "is_ref_yang": 1,
            FIELDS[0]: 1.25, FIELDS[1]: 2.5, FIELDS[2]: 3.75,
        }),
        b"000002": encoded({
            "code": "000002", "yin_count_20": 8, "is_ref_yang": 1,
            FIELDS[0]: 0.1, FIELDS[1]: 0.2, FIELDS[2]: 0.3,
        }),
    })
    with patch("app.get_redis_client", return_value=redis_client):
        response = client.get("/api/pz1/focus?date=2026-09-30&page=1")

    assert response.status_code == 200
    rows = response.get_json()["groups"]["pz_all"]
    assert len(rows) == 1
    assert [rows[0][field] for field in FIELDS] == [1.25, 2.5, 3.75]


def test_first_page_three_metric_rankings_and_missing_values(client):
    html = client.get("/重点关注").get_data(as_text=True)
    assert html.index('id="focusPage1BelowGroups"') < html.index('id="focusPage1MetricGroups"')
    for description in (
        "20天阴线的收盘价距离 MA5 最大的百分比",
        "20天跌破 MA5 的阴线累计跌幅",
        "20天跌破 MA5 的阴线累计实体大小",
    ):
        assert description in html
    if not shutil.which("node"):
        pytest.skip("Node.js is unavailable")

    script = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)[-1]
    source = script[script.index("function focusStockGain("):script.index("const BOARD_REASON_LABELS")]
    harness = r"""
const fields = [
  'yin_close_below_ma5_max_abs_pct_20d',
  'yin_close_below_ma5_drop_pct_sum_20d',
  'yin_close_below_ma5_body_pct_sum_20d'
];
const stocks = [
  {code:'000001', name:'A', yin_count_20:2, ref_gain:3, [fields[0]]:5, [fields[1]]:1, [fields[2]]:4},
  {code:'000002', name:'B', yin_count_20:2, ref_gain:2, [fields[0]]:1, [fields[1]]:4, [fields[2]]:2},
  {code:'000003', name:'C', yin_count_20:2, ref_gain:8, [fields[0]]:1, [fields[1]]:3, [fields[2]]:3},
  {code:'000004', name:'D', yin_count_20:2, ref_gain:9}
];
const state = {page1GroupMode:'yin', page1YinGroup:'', page1BelowGroup:''};
const focusPage1Groups = {innerHTML:''};
const focusPage1BelowGroups = {innerHTML:''};
const focusPage1MetricGroups = {innerHTML:''};
const focusPage1Rows = {innerHTML:''};
const focusPage1Heading = {textContent:''};
const focusPage1Count = {textContent:''};
const focusPage1Description = {hidden:true, textContent:''};
const focusPage1ValueHeading = {textContent:''};
const escapeHtml = value => String(value);
"""
    checks = r"""
for (const field of fields) {
  state.page1GroupMode = field;
  const rows = renderFocusPage1(stocks);
  const expected = field === fields[0] ? 'C,B,A' : field === fields[1] ? 'A,C,B' : 'B,C,A';
  if (rows.map(item => item.code === '000001' ? 'A' : item.code === '000002' ? 'B' : 'C').join(',') !== expected) {
    throw new Error('Wrong order for ' + field);
  }
  if (focusPage1Description.hidden || !focusPage1Rows.innerHTML.includes('%')) {
    throw new Error('Missing percentage display for ' + field);
  }
}
state.page1GroupMode = fields[0];
renderFocusPage1([{code:'000004', name:'D', yin_count_20:2, ref_gain:9}]);
if (!focusPage1Rows.innerHTML.includes('当前日期暂无该指标数据')) {
  throw new Error('Missing legacy-data message');
}
"""
    result = subprocess.run(
        ["node", "-"], input=harness + source + checks,
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr

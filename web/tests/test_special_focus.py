import os
import json
import subprocess
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def client():
    import app as flask_app

    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as test_client:
        yield test_client


def test_special_focus_routes_select_expected_renderer(client, monkeypatch):
    monkeypatch.setenv("KLINE_API_AES_KEY_B64", "test-runtime-key")

    cases = (
        ("/特别关注", "canvas", "特别关注1", False),
        ("/特别关注1", "canvas", "特别关注1", False),
        ("/特别关注2", "echarts", "特别关注2", True),
        ("/特别关注3", "svg", "特别关注3", True),
    )

    for path, renderer, title, encrypted in cases:
        response = client.get(path)
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        assert f'data-renderer="{renderer}"' in html
        assert title in html
        assert ("test-runtime-key" in html) is encrypted


def test_special_focus_board_page_uses_ths_api_without_changing_original_pages(
    client, monkeypatch
):
    monkeypatch.setenv("KLINE_API_AES_KEY_B64", "test-runtime-key")

    board_response = client.get("/特别板块")
    board_html = board_response.get_data(as_text=True)
    assert board_response.status_code == 200
    assert (
        'class="special-focus-shell is-split-mode is-board-mode"' in board_html
    )
    assert 'data-renderer="canvas"' in board_html
    assert "特别板块" in board_html
    assert 'groupMode: "board"' in board_html
    assert 'datesUrl: "/api/ths/dates"' in board_html
    assert 'klinesUrl: "/api/ths/klines/plain"' in board_html
    assert 'encryptedPayload: false' in board_html
    assert 'aad: "ths-klines:v1"' in board_html
    assert '<a href="/特别板块" class="active">特别板块</a>' in board_html
    assert '/static/special-focus.css?v=20260813_6' in board_html
    assert '/static/special-focus-core.js?v=20260806_1' in board_html
    assert '/static/special-focus.js?v=20260813_2' in board_html

    legacy_response = client.get("/特别关注板块")
    assert legacy_response.status_code == 302
    assert legacy_response.headers["Location"].endswith(
        "/%E7%89%B9%E5%88%AB%E6%9D%BF%E5%9D%97"
    )

    for path in ("/特别关注1", "/特别关注2", "/特别关注3"):
        html = client.get(path).get_data(as_text=True)
        assert 'groupMode: "yin"' in html
        if path == "/特别关注1":
            assert 'datesUrl: "/api/pz/dates"' in html
            assert 'klinesUrl: "/api/pz/klines"' in html
            assert 'encryptedPayload: false' in html
        else:
            assert 'datesUrl: "/api/yxcs/dates"' in html
            assert 'klinesUrl: "/api/yxcs/klines"' in html
            assert 'aad: "yxcs-klines:v1"' in html
            assert 'encryptedPayload: true' in html


def test_pz_five_pull_pages_use_plain_condition_apis_and_ordered_navigation(client):
    pages = (
        ("/5拉收盘大于10日", 3),
        ("/5拉阴线都向上", 5),
        ("/5拉阴被吃", 8),
        ("/5拉阴后拉或吃", 7),
        ("/5拉阴线都是十字星", 6),
        ("/5拉阴都不碰", 9),
    )
    expected_links = [
        f'<a href="{path}"' for path, _condition_no in pages
    ]

    for active_path, condition_no in pages:
        response = client.get(active_path)
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        assert f'datesUrl: "/api/pz/dates"' in html
        assert f'klinesUrl: "/api/pz/condition{condition_no}/klines"' in html
        assert 'groupMode: "yin"' in html
        assert 'yinGroupField: "yin_count_10d"' in html
        assert r'yinGroupLabel: "10\u65e5"' in html
        assert 'encryptedPayload: false' in html
        assert f'<a href="{active_path}" class="active">' in html
        assert '<a href="/特别5拉升">' not in html
        assert '<a href="/特别关注4">' not in html
        assert '<a href="/特别关注5">' not in html

        positions = [html.index(link) for link in expected_links]
        assert positions == sorted(positions)


def test_pz_five_pull_pages_show_centered_bottom_hint(client):
    hint = '最近4，5天 几乎光头 几乎光脚 短十字'
    pz_html = client.get("/5拉收盘大于10日").get_data(as_text=True)
    focus_html = client.get("/特别关注1").get_data(as_text=True)

    assert f'<div class="special-focus-bottom-hint">{hint}</div>' in pz_html
    assert hint not in focus_html

    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "static", "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()
    assert ".special-focus-bottom-hint {" in styles
    assert "text-align: center;" in styles
    assert "font-weight: 700;" in styles


def test_removed_special_focus_pages_and_navigation_are_not_available(client):
    removed_paths = ("/特别5拉升", "/特别关注4", "/特别关注5")
    for path in removed_paths:
        assert client.get(path).status_code == 404

    for path in ("/特别关注1", "/特别关注2", "/特别关注3", "/特别板块"):
        html = client.get(path).get_data(as_text=True)
        for removed_path in removed_paths:
            assert f'<a href="{removed_path}">' not in html


def test_special_focus_yxcs5_script_and_style_contract():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "static", "special-focus.js"), encoding="utf-8") as handle:
        script = handle.read()
    with open(os.path.join(root, "static", "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()

    assert "'yxcs5'" in script
    assert "core.groupYxcsFocusStocks" in script
    assert "group.metricField" in script
    assert "group.metricLabel" in script
    assert "special-focus-yxcs5-metric" in script
    assert "is-yxcs5-mode" in script
    assert ".special-focus-board-layout.is-yxcs5-mode" in styles


def test_special_focus_xiasha_page_script_and_style_contract():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "static", "special-focus.js"), encoding="utf-8") as handle:
        script = handle.read()
    with open(os.path.join(root, "static", "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()

    assert "groupMode === 'xiasha'" in script
    assert "core.groupXiashaStocks" in script
    assert "20日下穿累计" in script
    assert "special-focus-xiasha-sum" in script
    assert "groupMode === 'board' || groupMode === 'xiasha'" in script
    assert (
        "<span>股票</span><span>代码</span><span>涨跌幅</span>"
        "<span>极品</span><span>20日下穿累计</span>"
    ) in script
    assert ".is-xiasha-mode" in styles
    assert (
        "grid-template-columns: minmax(120px, 1fr) 72px 90px "
        "minmax(170px, 1.4fr) 128px;"
    ) in styles
    assert (
        ".special-focus-board-layout.is-xiasha-mode "
        ".special-focus-excellent"
    ) in styles
    assert "font-size: 12px;" in styles
    assert (
        "grid-template-columns: minmax(58px, 1fr) 56px "
        "minmax(84px, 1.2fr) 64px;"
    ) in styles
    assert "font-size: 11px;" in styles


def test_special_focus_primary_pages_link_to_each_other_and_use_image_only_tooltip(
    client,
):
    focus_html = client.get("/特别关注1").get_data(as_text=True)
    board_html = client.get("/特别板块").get_data(as_text=True)

    assert '<a href="/特别板块">特别板块</a>' in focus_html
    assert '<a href="/特别关注1">特别关注1</a>' in board_html
    for html in (focus_html, board_html):
        assert 'id="specialFocusKlineImage"' in html
        assert 'id="specialFocusKlineTitle"' not in html
        assert 'class="special-focus-kline-tooltip-header"' not in html
        assert 'aria-label="股票日K线图"' in html


def test_special_focus_board_page_keeps_purple_filter_button(client):
    board_html = client.get("/特别板块").get_data(as_text=True)
    assert board_html.count('id="specialFocusPurpleButton"') == 1
    assert 'aria-pressed="false"' in board_html
    assert ">紫色组<" in board_html

    for path in (
        "/特别关注1",
        "/特别关注2",
        "/特别关注3",
        "/5拉收盘大于10日",
    ):
        html = client.get(path).get_data(as_text=True)
        assert 'id="specialFocusPurpleButton"' not in html


def test_special_focus_script_toggles_purple_filter_without_unnecessary_board_jump():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    assert "var boardFilterMode = 'all';" in script
    assert "function visibleBoardGroups()" in script
    assert "function handlePurpleFilter()" in script
    assert "core.filterPurpleBoardGroups(boardGroups)" in script
    assert "groupMode === 'yin10' || groupMode === 'xiasha'" in script
    assert "core.filterPurpleGroups(groups)" in script
    assert "selectedGroup();" in script
    assert "selectedBoardValue" not in script
    assert "button.setAttribute('aria-pressed', String(pressed));" in script
    assert "byId('specialFocusPurpleButton')" in script


def test_special_focus_script_live_refresh_contract_preserves_page_state():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    assert "var REFRESH_INTERVAL_MS = 3000;" in script
    assert "var DATE_CHECK_EVERY_CYCLES = 20;" in script
    assert "var refreshInFlight = false;" in script
    assert "var latestDate = '';" in script
    assert "async function loadEncryptedPayload(date, days)" in script
    assert "function mergeRefreshPayload(payload, fullReload)" in script
    assert "function renderPreservingPosition()" in script
    assert "async function refreshLatestData()" in script
    assert "if (document.hidden || refreshInFlight) return;" in script
    assert "loadEncryptedPayload(latestDate, 1)" in script
    assert "config.requestDays || DEFAULTS.requestDays" in script
    assert "core.mergeRefreshedItem(" in script
    refresh_body = script.split("function mergeRefreshPayload", 1)[1].split(
        "function renderPreservingPosition", 1
    )[0]
    assert "selectedGroup();" in refresh_body
    assert "selectedGroupValue = '';" not in refresh_body
    latest_refresh_body = script.split("async function refreshLatestData()", 1)[1].split(
        "function startRefreshTimer()", 1
    )[0]
    assert "var refreshedOpenItem = findItemByCode(openKlineCode);" in latest_refresh_body
    assert "renderImageKline(refreshedOpenItem);" in latest_refresh_body
    assert "closeFloatingKline();" in latest_refresh_body
    assert "var pageScrollTop = window.scrollY;" in script
    assert "window.scrollTo(0, pageScrollTop);" in script
    assert "document.addEventListener('visibilitychange'" in script
    assert "window.addEventListener('beforeunload'" in script
    assert "refreshInFlight = false;" in script
    assert "setInterval(function()" in script


def test_special_focus_template_does_not_hardcode_production_key():
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "templates",
        "special_focus.html",
    )

    with open(template_path, encoding="utf-8") as handle:
        template = handle.read()

    assert 'aesKeyB64: {{ special_focus_key_b64|tojson }}' in template


def _run_special_focus_core(expression):
    core_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus-core.js",
    )
    script = (
        f"const core = require({json.dumps(core_path)});"
        f"const result = ({expression});"
        "process.stdout.write(JSON.stringify(result));"
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def test_special_focus_core_groups_yin_count_and_sorts_gain_descending():
    result = _run_special_focus_core(
        "core.groupStocks(["
        "{code:'000001',stock:{yin_count_20:4,ref_gain:2}},"
        "{code:'000002',stock:{yin_count_20:3,ref_gain:1}},"
        "{code:'000003',stock:{yin_count_20:4,ref_gain:-2,today_change:5}}"
        "]).map(g=>({value:g.value,codes:g.items.map(x=>x.code)}))"
    )

    assert result == [
        {"value": 3, "codes": ["000002"]},
        {"value": 4, "codes": ["000003", "000001"]},
    ]


def test_special_focus_core_yin10_group_sorts_groups_and_gain():
    result = _run_special_focus_core(
        "core.groupStocksByField(["
        "{code:'000001',stock:{yin_count_10d:1,ref_gain:2}},"
        "{code:'000002',stock:{yin_count_10d:1,today_change:5}},"
        "{code:'000003',stock:{yin_count_10d:0,ref_gain:-1}},"
        "{code:'000004',stock:{yin_count_10d:2,ref_gain:9}}"
        "],'yin_count_10d').map(g=>({value:g.value,codes:g.items.map(x=>x.code)}))"
    )

    assert result == [
        {"value": 0, "codes": ["000003"]},
        {"value": 1, "codes": ["000002", "000001"]},
        {"value": 2, "codes": ["000004"]},
    ]


def test_special_focus_xiasha_core_uses_fixed_groups_and_distance_sorting():
    result = _run_special_focus_core(
        "core.groupXiashaStocks(["
        "{code:'000001',stock:{is_high_gain:1,close_below_ma5_abs_pct_sum_20d:2,ref_gain:9}},"
        "{code:'000002',stock:{is_high_gain:true,close_below_ma5_abs_pct_sum_20d:1,ref_gain:1}},"
        "{code:'000003',stock:{is_high_gain:false,close_below_ma5_abs_pct_sum_20d:0.5,ref_gain:3}},"
        "{code:'000004',stock:{is_high_gain:'true',close_below_ma5_abs_pct_sum_20d:1,today_change:5}}"
        "]).map(g=>({value:g.value,codes:g.items.map(x=>x.code)}))"
    )

    assert result == [
        {"value": "拉升", "codes": ["000004", "000002", "000001"]},
        {"value": "不拉升", "codes": ["000003"]},
    ]


def test_special_focus_xiasha_core_keeps_both_groups_when_empty():
    result = _run_special_focus_core(
        "core.groupXiashaStocks([]).map(g=>({value:g.value,count:g.items.length}))"
    )

    assert result == [
        {"value": "拉升", "count": 0},
        {"value": "不拉升", "count": 0},
    ]


def test_special_focus_core_groups_boards_and_sorts_gain_descending():
    result = _run_special_focus_core(
        "core.groupBoardStocks(["
        "{board_name:'中药',board_change_percent:1.2,stocks:[{code:'2',ref_gain:-1},{code:'1',ref_gain:3}]},"
        "{board_name:'未知板块',board_change_percent:null,stocks:[{code:'4',today_change:9}]},"
        "{board_name:'白酒',board_change_percent:5.2,stocks:[{code:'3',today_change:2}]}"
        "]).map(g=>({value:g.value,boardChangePercent:g.boardChangePercent,codes:g.items.map(x=>x.code)}))"
    )

    assert result == [
        {"value": "白酒", "boardChangePercent": 5.2, "codes": ["3"]},
        {"value": "中药", "boardChangePercent": 1.2, "codes": ["1", "2"]},
        {"value": "未知板块", "boardChangePercent": None, "codes": ["4"]},
    ]


def test_special_focus_core_finds_group_for_six_digit_codes():
    result = _run_special_focus_core(
        "(() => { const groups=["
        "{value:2,items:[{code:'000001.SZ'}]},"
        "{value:3,items:[{stock:{stock_code_with_market:'600000.SH'}}]},"
        "{value:4,items:[{stock:{code:'300001.BJ'}}]}"
        "]; return ["
        "core.findGroupValueByCode(groups,'000001'),"
        "core.findGroupValueByCode(groups,'600000'),"
        "core.findGroupValueByCode(groups,'300001'),"
        "core.findGroupValueByCode([{value:'中药',items:[{code:'000001'}]}],'000001.SZ'),"
        "core.findGroupValueByCode(groups,'688001')"
        "]; })()"
    )

    assert result == ["2", "3", "4", "中药", ""]


def test_special_focus_core_builds_same_eastmoney_kline_url_as_ztorgj():
    result = _run_special_focus_core(
        "[core.eastmoneyKlineUrl('600000.SH'),"
        "core.eastmoneyKlineUrl('000001.SZ'),"
        "core.eastmoneyKlineUrl('bad-code')]"
    )

    base = "https://webquoteklinepic.eastmoney.com/GetPic.aspx"
    suffix = "&imageType=KXL&token=44c9d251add88e27b65ed86506f6e5da&at=1"
    assert result == [
        f"{base}?nid=1.600000{suffix}",
        f"{base}?nid=0.000001{suffix}",
        "",
    ]


def test_special_focus_core_positions_floating_panel():
    result = _run_special_focus_core(
        "["
        "core.floatingPanelPosition(200,100,680,380,1440,900,10,15),"
        "core.floatingPanelPosition(1300,800,680,380,1440,900,10,15),"
        "core.floatingPanelPosition(2,2,355,300,375,667,10,15),"
        "core.floatingPanelPosition(200.6,100.6,680.5,380.5,1440,900,10,15),"
        "core.floatingPanelPosition(200,-20,100,100,500,400,10,15),"
        "core.floatingPanelPosition(1000,1000,100,100,500,400,10,15)"
        "]"
    )

    assert result == [
        {"left": 215, "top": 115},
        {"left": 605, "top": 405},
        {"left": 10, "top": 17},
        {"left": 216, "top": 116},
        {"left": 215, "top": 10},
        {"left": 390, "top": 290},
    ]


def test_special_focus_yxcs5_core_builds_ten_overlapping_sorted_groups():
    result = _run_special_focus_core(
        "core.groupYxcsFocusStocks(["
        "{code:'000001',stock:{ref_is_limit_up:true,yin_count_20:5,low_above_ma5_count_20d:8,low_above_ma5_count_10d:5,ref_gain:3}},"
        "{code:'000002',stock:{is_limit_up:1,yin_count_20:3,low_above_ma5_count_20d:9,low_above_ma5_count_10d:4,ref_gain:2}},"
        "{code:'000003',stock:{ref_is_limit_up:false,is_limit_up:1,is_high_gain:1,yin_count_20:6,low_above_ma5_count_20d:7,low_above_ma5_count_10d:6,ref_gain:5}},"
        "{code:'000004',stock:{ref_is_limit_up:false,is_bare_foot_yang:1,ref_low_above_ma5:1,low_above_ma5_count_20d:5,low_above_ma5_count_10d:8,ref_gain:4}},"
        "{code:'000005',fourabove5_member:1,stock:{ref_is_limit_up:false,is_high_gain:true,is_bare_foot_yang:true,ref_low_above_ma5:true,yin_count_20:4,low_above_ma5_count_20d:7,low_above_ma5_count_10d:8,ref_gain:6}},"
        "{code:'000006',stock:{ref_is_limit_up:false,is_high_gain:false,is_bare_foot_yang:false,ref_low_above_ma5:false,low_above_ma5_count_20d:6,low_above_ma5_count_10d:6,ref_gain:1}},"
        "{code:'000007',special5_member:0,fourabove5_member:1,stock:{ref_is_limit_up:true,is_high_gain:1,yin_count_20:2,low_above_ma5_count_20d:4,low_above_ma5_count_10d:7,ref_gain:7}},"
        "{code:'000008',special5_member:0,fourabove5_member:1,stock:{ref_is_limit_up:false,is_high_gain:1,yin_count_20:5,low_above_ma5_count_20d:5,low_above_ma5_count_10d:8,ref_gain:8}}"
        "]).map(g=>({value:g.value,metricField:g.metricField,metricLabel:g.metricLabel,codes:g.items.map(x=>x.code)}))"
    )

    assert result == [
        {
            "value": "涨停20天阴线",
            "metricField": "yin_count_20",
            "metricLabel": "20日阴线",
            "codes": ["000002", "000001"],
        },
        {
            "value": "涨停20日不碰",
            "metricField": "low_above_ma5_count_20d",
            "metricLabel": "20日不碰",
            "codes": ["000002", "000001"],
        },
        {
            "value": "涨停10日不碰",
            "metricField": "low_above_ma5_count_10d",
            "metricLabel": "10日不碰",
            "codes": ["000001", "000002"],
        },
        {
            "value": "非涨停20天阴线",
            "metricField": "yin_count_20",
            "metricLabel": "20日阴线",
            "codes": ["000005", "000003"],
        },
        {
            "value": "非涨停拉升20日不碰",
            "metricField": "low_above_ma5_count_20d",
            "metricLabel": "20日不碰",
            "codes": ["000005", "000003"],
        },
        {
            "value": "非涨停拉升10日不碰",
            "metricField": "low_above_ma5_count_10d",
            "metricLabel": "10日不碰",
            "codes": ["000005", "000003"],
        },
        {
            "value": "光脚拉升不碰20日",
            "metricField": "low_above_ma5_count_20d",
            "metricLabel": "20日不碰",
            "codes": ["000005", "000004"],
        },
        {
            "value": "光脚拉升不碰10日",
            "metricField": "low_above_ma5_count_10d",
            "metricLabel": "10日不碰",
            "codes": ["000005", "000004"],
        },
        {
            "value": "10日低不碰>=6",
            "metricField": "low_above_ma5_count_10d",
            "metricLabel": "10日不碰",
            "codes": ["000005", "000004", "000003", "000006"],
        },
        {
            "value": "4不碰20天阴线",
            "metricField": "yin_count_20",
            "metricLabel": "20日阴线",
            "codes": ["000007", "000005", "000008"],
        },
    ]


def test_special_focus_yxcs5_core_keeps_all_ten_groups_when_empty():
    result = _run_special_focus_core(
        "core.groupYxcsFocusStocks([]).map(g=>({value:g.value,count:g.items.length}))"
    )

    assert result == [
        {"value": "涨停20天阴线", "count": 0},
        {"value": "涨停20日不碰", "count": 0},
        {"value": "涨停10日不碰", "count": 0},
        {"value": "非涨停20天阴线", "count": 0},
        {"value": "非涨停拉升20日不碰", "count": 0},
        {"value": "非涨停拉升10日不碰", "count": 0},
        {"value": "光脚拉升不碰20日", "count": 0},
        {"value": "光脚拉升不碰10日", "count": 0},
        {"value": "10日低不碰>=6", "count": 0},
        {"value": "4不碰20天阴线", "count": 0},
    ]


def test_special_focus_core_builds_excellent_labels_for_purple_and_red_groups():
    result = _run_special_focus_core(
        "["
        "core.excellentDetails({group_color:'purple',purple_reasons:["
        "'all_yin_no_touch','high_gain5_and_yin_pull'],match_reasons:["
        "'ths_change_percent_gte_4_5','all_yin_no_touch']}),"
        "core.excellentDetails({group_color:'red',red_reasons:["
        "'high_gain_10d_gte_5','all_yin_close_above_ma5',"
        "'low_above_ma5_count_gte_4','lower_shadow_high_gain_low_above_ma5']}),"
        "core.excellentDetails({group_color:'',match_reasons:["
        "'ths_change_percent_gte_4_5']})"
        "]"
    )

    assert result == [
        {
            "tone": "purple",
            "labels": [
                "20天阴线次数≤4",
                "10日所有阴线低不碰MA5",
                "5拉升阴前后拉",
            ],
        },
        {
            "tone": "default",
            "labels": [
                "20天阴线次数5-6",
                "最近10日5拉升",
                "阴线收盘都大于MA5",
                "10日低不碰次数≥4",
                "光脚拉升不碰",
            ],
        },
        {"tone": "default", "labels": []},
    ]


def test_special_focus_core_builds_exact_xiasha_excellent_reason_labels():
    result = _run_special_focus_core(
        "["
        "core.excellentDetails({purple_match:1,purple_reasons:["
        "'yin_count_20_lte_4']}),"
        "core.excellentDetails({purple_match:1,purple_reasons:["
        "'low_above_ma5_count_20d_gte_7']}),"
        "core.excellentDetails({purple_match:1,purple_reasons:["
        "'yin_count_20_lte_4','low_above_ma5_count_20d_gte_7']})"
        "]"
    )

    assert result == [
        {"tone": "purple", "labels": ["20天阴线次数≤4"]},
        {"tone": "purple", "labels": ["20日低不碰次数≥7"]},
        {
            "tone": "purple",
            "labels": ["20天阴线次数≤4", "20日低不碰次数≥7"],
        },
    ]


def test_special_focus_core_builds_exact_yin10_excellent_reason_labels():
    result = _run_special_focus_core(
        "core.excellentDetails({purple_match:1,purple_reasons:["
        "'high_gain5_and_yin_pull','all_yin_up','all_yin_cross_star',"
        "'is_quanyang_10d']},'yin10')"
    )

    assert result == {
        "tone": "purple",
        "labels": [
            "5拉升阴前后拉",
            "阴线都向上",
            "阴线都是十字星",
            "最近10日全阳",
        ],
    }


def test_special_focus_yin10_page_has_vertical_excellent_column_contract():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "static", "special-focus.js"), encoding="utf-8") as handle:
        script = handle.read()
    with open(os.path.join(root, "static", "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()

    assert "groupMode === 'yin10'" in script
    assert "excellentMarkup(item)" in script
    assert (
        "<span>股票</span><span>代码</span><span>涨跌幅</span><span>极品</span>"
    ) in script
    assert "is-yin10-mode" in script
    assert ".special-focus-board-layout.is-yin10-mode" in styles


def test_special_focus_core_aligned_gain_hides_positive_sign_but_keeps_slot():
    result = _run_special_focus_core(
        "[core.alignedGain(5.52),core.alignedGain(-2.31),core.alignedGain(0)]"
    )

    assert result == [
        {"negative": False, "sign": "+", "hideSign": True, "value": "5.52%"},
        {"negative": True, "sign": "-", "hideSign": False, "value": "2.31%"},
        {"negative": False, "sign": "+", "hideSign": True, "value": "0.00%"},
    ]


def test_special_focus_core_merge_kline_rows_replaces_appends_and_limits():
    result = _run_special_focus_core(
        "(() => {"
        "const previous=[{trade_date:'20260714',close:10},{trade_date:'20260715',close:11}];"
        "const many=Array.from({length:61},(_,i)=>({trade_date:String(20260000+i+1),close:i+1}));"
        "return {"
        "replace:core.mergeKlineRows(previous,[{trade_date:'20260715',close:12}],60),"
        "append:core.mergeKlineRows(previous,[{trade_date:'20260716',close:13}],60),"
        "preserve:core.mergeKlineRows(previous,[],60),"
        "limited:core.mergeKlineRows([],many,60)"
        "};})()"
    )

    assert result["replace"] == [
        {"trade_date": "20260714", "close": 10},
        {"trade_date": "20260715", "close": 12},
    ]
    assert [row["trade_date"] for row in result["append"]] == [
        "20260714", "20260715", "20260716",
    ]
    assert result["preserve"] == [
        {"trade_date": "20260714", "close": 10},
        {"trade_date": "20260715", "close": 11},
    ]
    assert len(result["limited"]) == 60
    assert result["limited"][0]["trade_date"] == "20260002"
    assert result["limited"][-1]["trade_date"] == "20260061"


def test_special_focus_core_merge_refreshed_item_preserves_nested_fields_and_history():
    result = _run_special_focus_core(
        "core.mergeRefreshedItem("
        "{code:'000001',name:'旧名称',stock:{yin_count_20:4,ref_gain:1},"
        "kline_data:[{trade_date:'20260714',close:10},{trade_date:'20260715',close:11}]},"
        "{code:'000001',name:'新名称',stock:{ref_gain:2},"
        "kline_data:[{trade_date:'20260715',close:12}]},60)"
    )

    assert result["name"] == "新名称"
    assert result["stock"] == {"yin_count_20": 4, "ref_gain": 2}
    assert result["kline_data"] == [
        {"trade_date": "20260714", "close": 10},
        {"trade_date": "20260715", "close": 12},
    ]


def test_special_focus_core_filters_empty_non_purple_boards():
    result = _run_special_focus_core(
        "core.filterPurpleBoardGroups(["
        "{value:'A',boardChangePercent:5,items:["
        "{code:'1',group_color:'purple'},{code:'2',group_color:'red'}]},"
        "{value:'B',boardChangePercent:4,items:[{code:'3',purple_match:1}]},"
        "{value:'C',boardChangePercent:3,items:[{code:'4',red_match:1}]}"
        "]).map(g=>({value:g.value,boardChangePercent:g.boardChangePercent,"
        "codes:g.items.map(x=>x.code)}))"
    )

    assert result == [
        {"value": "A", "boardChangePercent": 5, "codes": ["1"]},
        {"value": "B", "boardChangePercent": 4, "codes": ["3"]},
    ]


def test_special_focus_core_generic_purple_filter_keeps_only_purple_stocks():
    result = _run_special_focus_core(
        "core.filterPurpleGroups(["
        "{value:0,items:[{code:'000001',stock:{purple_match:1}},"
        "{code:'000002',stock:{purple_match:0}}]},"
        "{value:1,items:[{code:'000003',stock:{group_color:'purple'}}]},"
        "{value:2,items:[{code:'000004',stock:{group_color:'red'}}]}"
        "]).map(g=>({value:g.value,codes:g.items.map(x=>x.code)}))"
    )

    assert result == [
        {"value": 0, "codes": ["000001"]},
        {"value": 1, "codes": ["000003"]},
    ]


def test_special_focus_core_deduplicates_board_stocks_for_search():
    result = _run_special_focus_core(
        "core.uniqueBoardStocks(["
        "{value:'A',items:[{code:'000001',name:'平安银行'}]},"
        "{value:'B',items:[{code:'000001',name:'平安银行'},{code:'000002',name:'万科A'}]}"
        "]).map(x=>x.code)"
    )

    assert result == ["000001", "000002"]


def test_special_focus_core_calculates_ma5_without_early_values():
    result = _run_special_focus_core(
        "core.calculateMA5([1,2,3,4,5,6].map(close=>({close})))"
    )

    assert result == [None, None, None, None, 3, 4]


def test_special_focus_ohlc_glyph_splits_wicks_at_body_edges():
    result = _run_special_focus_core(
        "core.createOhlcGlyph(20,30,40,10,55,6,true)"
    )

    assert result["upperWick"] == {"x1": 20, "x2": 20, "y1": 10, "y2": 30}
    assert result["lowerWick"] == {"x1": 20, "x2": 20, "y1": 40, "y2": 55}
    assert result["open"] == {"x1": 14, "x2": 26, "y1": 30, "y2": 30}
    assert result["close"] == {"x1": 14, "x2": 26, "y1": 40, "y2": 40}
    assert result["left"] == {"x1": 14, "x2": 14, "y1": 30, "y2": 40}
    assert result["right"] == {"x1": 26, "x2": 26, "y1": 30, "y2": 40}
    assert "wick" not in result
    assert set(result) == {
        "upperWick",
        "lowerWick",
        "open",
        "close",
        "left",
        "right",
        "tone",
    }
    assert result["tone"] == "light"


def test_special_focus_core_searches_name_initials_and_code():
    result = _run_special_focus_core(
        "(() => { const items=["
        "{code:'000779',name:'甘咨询',stock:{code:'000779',name:'甘咨询'}},"
        "{code:'600657',name:'信达地产',stock:{code:'600657.SH',name:'信达地产'}}"
        "]; return {"
        "initials:core.pinyinInitials('甘咨询'),"
        "name:core.searchStocks(items,'甘咨询').map(x=>x.code),"
        "abbr:core.searchStocks(items,'gzx').map(x=>x.code),"
        "code:core.searchStocks(items,'600657').map(x=>x.code),"
        "suffix:core.searchStocks(items,'600657.SH').map(x=>x.code)"
        "}; })()"
    )

    assert result == {
        "initials": "gzx",
        "name": ["000779"],
        "abbr": ["000779"],
        "code": ["600657"],
        "suffix": ["600657"],
    }


def test_special_focus_core_search_prefers_exact_name():
    result = _run_special_focus_core(
        "core.searchStocks(["
        "{code:'1',name:'上海凯宝股份'},"
        "{code:'2',name:'上海凯宝'}"
        "],'上海凯宝').map(x=>x.code)"
    )

    assert result == ["2", "1"]


def test_special_focus_1_uses_plain_pz_api_while_other_renderers_keep_encryption(client, monkeypatch):
    monkeypatch.setenv("KLINE_API_AES_KEY_B64", "test-runtime-key")

    canvas_html = client.get("/特别关注1").get_data(as_text=True)
    echarts_html = client.get("/特别关注2").get_data(as_text=True)
    svg_html = client.get("/特别关注3").get_data(as_text=True)

    assert "/api/pz/dates" in canvas_html
    assert "/api/pz/klines" in canvas_html
    assert "/api/yxcs/klines/plain" not in canvas_html
    assert 'encryptedPayload: false' in canvas_html
    assert "forge.min.js" not in canvas_html
    assert "test-runtime-key" not in canvas_html

    for html in (echarts_html, svg_html):
        assert "/api/yxcs/dates" in html
        assert "/api/yxcs/klines" in html
        assert "yxcs-klines:v1" in html
        assert 'encryptedPayload: true' in html
        assert "forge.min.js" in html
        assert "special-focus-core.js" in html
        assert "special-focus.js" in html
        assert "special-focus.css" in html

    assert "echarts.min.js" not in canvas_html
    assert "echarts.min.js" in echarts_html
    assert "echarts.min.js" not in svg_html


def test_special_board_page_uses_plain_api_without_aes_assets(client, monkeypatch):
    monkeypatch.setenv("KLINE_API_AES_KEY_B64", "test-runtime-key")

    html = client.get("/特别板块").get_data(as_text=True)

    assert "/api/ths/dates" in html
    assert "/api/ths/klines/plain" in html
    assert 'encryptedPayload: false' in html
    assert "forge.min.js" not in html
    assert "test-runtime-key" not in html


def test_special_focus_uses_local_forge_asset(client):
    html = client.get("/特别关注2").get_data(as_text=True)

    assert '/static/vendor/forge.min.js?v=1.3.1' in html
    assert 'cdn.jsdelivr.net/npm/node-forge' not in html


def test_special_focus_page_skips_global_theme_assets(client):
    html = client.get("/特别关注1").get_data(as_text=True)

    assert "monochrome-theme.css" not in html
    assert "monochrome-theme.js" not in html
    assert "mobile-single-list.css" not in html
    assert "mobile-single-list.js" not in html
    assert "切换简约样式" not in html


def test_special_focus_pages_use_image_only_tooltip_without_day_controls(client):
    for path in (
        "/特别关注",
        "/特别关注1",
        "/特别关注2",
        "/特别关注3",
        "/特别板块",
    ):
        response = client.get(path)
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        assert 'class="special-focus-shell is-split-mode' in html
        assert 'id="specialFocusSearch"' in html
        assert (
            '<button id="specialFocusSearchButton" type="button">查</button>' in html
        )
        assert 'id="specialFocusShowButton"' not in html
        assert (
            '<div id="specialFocusKlineTooltip" '
            'class="special-focus-kline-tooltip" hidden aria-live="polite" '
            'role="region" aria-label="股票日K线图">' in html
        )
        assert 'id="specialFocusKlineTitle"' not in html
        assert 'class="special-focus-kline-tooltip-header"' not in html
        assert 'id="specialFocusKlineImage"' in html
        assert 'class="special-focus-kline-image"' in html
        assert 'id="specialFocusKlineChart"' not in html
        assert 'id="specialFocusDays30"' not in html
        assert 'id="specialFocusDays60"' not in html
        assert "passwordRequired: false" in html
        assert 'id="specialFocusPasswordDialog"' in html
        assert 'type="password"' in html


def test_special_focus_core_uses_hidden_history_for_visible_moving_averages():
    result = _run_special_focus_core(
        "core.buildChartData(Array.from({length:35},(_,i)=>({"
        "trade_date:String(i+1).padStart(2,'0'),open:i+1,high:i+2,low:i+0.5,close:i+1"
        "})),30,[5,10,20,30])"
    )

    assert len(result["rows"]) == 30
    assert result["rows"][0]["trade_date"] == "06"
    assert result["movingAverages"]["5"][0] == 4
    assert result["movingAverages"]["10"][4] == 5.5
    assert result["movingAverages"]["30"][24] == 15.5


def test_special_focus_assets_keep_monochrome_kline_palette():
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    with open(os.path.join(static_dir, "special-focus.js"), encoding="utf-8") as handle:
        script = handle.read()
    with open(os.path.join(static_dir, "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()

    assert "background: '#ffffff'" in script
    assert "grid: '#f0f0f0'" in script
    assert "yang: '#8b8b8b'" in script
    assert "yin: '#202020'" in script
    assert "ma5: '#929292'" in script
    assert "var MA_PERIODS = [5]" in script
    assert "requestDays: 60" in script
    assert "displayDays: 60" in script
    assert "background: #ffffff" in styles


def test_special_focus_script_retains_single_kline_drawing_primitives():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    assert (
        "[glyph.upperWick, glyph.lowerWick, glyph.open, "
        "glyph.close, glyph.left, glyph.right]"
    ) in script
    assert "lineCanvas(ctx, glyph.upperWick)" in script
    assert "lineCanvas(ctx, glyph.lowerWick)" in script
    assert "lineCanvas(ctx, glyph.left)" in script
    assert "lineCanvas(ctx, glyph.right)" in script
    assert "svgLine(svg, glyph.upperWick" in script
    assert "svgLine(svg, glyph.lowerWick" in script
    assert "svgLine(svg, glyph.left" in script
    assert "svgLine(svg, glyph.right" in script


def test_special_focus_split_shell_uses_desktop_yin_columns():
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    with open(os.path.join(static_dir, "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()

    assert (
        ".special-focus-shell.is-split-mode {\n"
        "    width: min(980px, calc(100% - 32px));\n"
        "}" in styles
    )
    assert (
        ".special-focus-board-layout.is-yin-mode .special-focus-board-columns,\n"
        ".special-focus-board-layout.is-yin-mode .special-focus-stock,\n"
        ".special-focus-board-layout.is-yin-mode "
        ".special-focus-row.is-locked .special-focus-stock {\n"
        "    grid-template-columns: minmax(150px, 1fr) 90px 100px;\n"
        "}" in styles
    )


def test_special_focus_tools_match_available_controls():
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    with open(os.path.join(static_dir, "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()

    assert (
        "grid-template-columns: minmax(120px, 1fr) 44px "
        "minmax(0, 0.8fr);" in styles
    )
    assert (
        "grid-template-columns: minmax(120px, 1fr) 44px 64px "
        "minmax(0, 0.7fr);" in styles
    )
    assert "grid-template-columns: minmax(0, 1fr) 40px;" in styles
    assert (
        "grid-template-columns: minmax(68px, 1fr) 40px 58px;" in styles
    )


def test_special_focus_floating_kline_matches_fixed_image_tooltip_contract():
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    with open(os.path.join(static_dir, "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()

    tooltip = styles.split(".special-focus-kline-tooltip {", 1)[1].split("}", 1)[0]
    image = styles.split(".special-focus-kline-image {", 1)[1].split("}", 1)[0]

    assert "position: fixed;" in tooltip
    assert "z-index: 10000;" in tooltip
    assert "width: min(680px, calc(100vw - 20px));" in tooltip
    assert "padding: 10px;" in tooltip
    assert "border: 1px solid #e1e1e1;" in tooltip
    assert "border-radius: 8px;" in tooltip
    assert "background: #ffffff;" in tooltip
    assert "box-shadow: 0 8px 24px rgba(0,0,0,.12);" in tooltip
    assert "[hidden] {" in styles
    assert "display: none !important;" in styles
    assert ".special-focus-kline-tooltip-header {" not in styles
    assert "width: 100%;" in image
    assert "aspect-ratio: 680 / 380;" in image
    assert "object-fit: contain;" in image
    assert "background: #f5f5f7;" in image


def test_special_focus_mobile_keeps_split_list_and_fixed_tooltip():
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    with open(os.path.join(static_dir, "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()

    mobile = styles.split("@media (max-width: 700px) {", 1)[1]

    assert "grid-template-columns: 116px minmax(0, 1fr);" in mobile
    assert (
        ".special-focus-board-layout.is-yin-mode .special-focus-board-columns,\n"
        "    .special-focus-board-layout.is-yin-mode .special-focus-stock,\n"
        "    .special-focus-board-layout.is-yin-mode "
        ".special-focus-row.is-locked .special-focus-stock {\n"
        "        grid-template-columns: minmax(0, 1fr) 82px;\n"
        "    }" in mobile
    )
    assert ".special-focus-board-layout .special-focus-code {" in mobile
    assert "display: none;" in mobile
    assert ".special-focus-kline-tooltip {" in mobile


def test_special_focus_mobile_navigation_wraps_to_multiple_rows():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "static", "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()

    mobile = styles.split("@media (max-width: 700px) {", 1)[1]
    nav_block = mobile.split(".special-focus-nav {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: wrap;" in nav_block
    assert "overflow-x: visible;" in nav_block
    assert "flex-wrap: nowrap;" not in nav_block


def test_special_focus_mobile_navigation_starts_five_pull_links_on_new_row(client):
    html = client.get("/特别关注1").get_data(as_text=True)
    marker = '<span class="special-focus-nav-break" aria-hidden="true"></span>'
    first_five_pull = '<a href="/5拉收盘大于10日"'
    assert marker in html
    assert html.index(marker) < html.index(first_five_pull)

    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "static", "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()
    mobile = styles.split("@media (max-width: 700px) {", 1)[1]
    assert ".special-focus-nav-break {" in mobile
    assert "flex-basis: 100%;" in mobile


def test_special_focus_mobile_kline_uses_near_full_width_image():
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    with open(os.path.join(static_dir, "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()

    mobile = styles.split("@media (max-width: 700px) {", 1)[1]
    assert "width: calc(100vw - 8px);" in mobile
    assert "max-height: calc(100vh - 8px);" in mobile
    assert "padding: 0;" in mobile
    assert "aspect-ratio: auto;" in mobile

    with open(os.path.join(static_dir, "special-focus.js"), encoding="utf-8") as handle:
        script = handle.read()
    position_body = script.split("function positionFloatingKline()", 1)[1].split(
        "function renderImageKline(item)", 1
    )[0]
    assert "var edgeMargin = isMobileKlineMode() ? 4 : 10;" in position_body
    assert "var cursorGap = isMobileKlineMode() ? 8 : 15;" in position_body


def test_special_focus_mobile_list_uses_larger_text_and_rows():
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    with open(os.path.join(static_dir, "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()
    mobile = styles.split("@media (max-width: 700px) {", 1)[1]

    assert "min-height: 48px;" in mobile
    assert ".special-focus-board-name {\n        font-size: 11px;" in mobile
    assert (
        ".special-focus-board-button .special-focus-board-gain {\n"
        "        justify-content: flex-start;\n"
        "        min-width: 0;\n"
        "        font-size: 10px;" in mobile
    )
    assert "padding: 13px 0;" in mobile
    assert (
        ".special-focus-board-main .special-focus-name {\n"
        "        font-size: 12px;" in mobile
    )
    assert (
        ".special-focus-board-main .special-focus-gain {\n"
        "        font-size: 12px;" in mobile
    )
    assert "font-size: 9.5px;" in mobile


def test_special_focus_board_layout_css_is_scoped_and_mobile_safe():
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    with open(os.path.join(static_dir, "special-focus.css"), encoding="utf-8") as handle:
        styles = handle.read()

    assert ".special-focus-board-layout {" in styles
    assert ".special-focus-shell.is-board-mode {" in styles
    assert ".special-focus-board-sidebar {" in styles
    assert ".special-focus-board-main {" in styles
    assert ".special-focus-excellent {" in styles
    assert ".special-focus-excellent.is-purple {" in styles
    assert ".special-focus-gain-sign.is-placeholder {" in styles
    assert "grid-template-columns: 188px minmax(0, 1fr);" in styles
    assert "width: min(980px, calc(100% - 32px));" in styles
    assert "grid-template-columns: 116px minmax(0, 1fr);" in styles
    assert ".special-focus-board-layout .special-focus-code {" in styles
    assert "visibility: hidden;" in styles
    assert "font-size: 16px" in styles
    assert "font-size: 11px" in styles


def test_special_focus_script_controls_one_password_free_image_kline():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    assert "var openKlineCode = '';" in script
    assert "var openKlineAnchor = null;" in script
    assert "function closeFloatingKline()" in script
    assert "function positionFloatingKline()" in script
    assert "function renderImageKline(item)" in script
    assert "function showFloatingKline(item, anchor)" in script
    assert "core.eastmoneyKlineUrl(itemCode(item))" in script
    assert "imageNode.removeAttribute('src');" in script
    assert "imageNode.src = imageUrl;" in script
    assert "core.floatingPanelPosition(" in script
    assert "openKlineCode = '';" in script
    assert "openKlineAnchor = null;" in script
    assert "specialFocusKlineChart" not in script
    assert "specialFocusDays30" not in script
    assert "specialFocusDays60" not in script
    assert 'role="button" tabindex="0"' in script
    assert "groupsHost.addEventListener('click'" in script
    assert "groupsHost.addEventListener('keydown'" in script
    assert "document.addEventListener('click'" in script
    assert "event.key === 'Escape'" in script
    assert "event.stopPropagation();" in script
    assert "getBoundingClientRect()" in script
    assert "target.closest('.special-focus-tools')" in script
    assert "target.closest('#specialFocusKlineTooltip')" in script
    assert "target.closest('[data-group-value]')" in script
    assert "target.closest('[data-stock-code]')" in script
    purple_body = script.split("function handlePurpleFilter()", 1)[1].split(
        "async function handleSearch()", 1
    )[0]
    assert "closeFloatingKline();" in purple_body
    bind_body = script.split("function bindControls()", 1)[1].split(
        "var resizeTimer", 1
    )[0]
    assert "showFloatingKline(item, { x: event.clientX, y: event.clientY });" in bind_body
    assert "specialFocusShowButton" not in script
    assert "handleToggleCharts" not in script
    assert "await requestUnlock()" not in script
    assert "function chartMarkup" not in script
    assert "IntersectionObserver" not in script
    assert "installLazyRendering" not in script
    assert "data-chart-index" not in script
    assert "chartRecords" not in script


def test_special_focus_group_click_closes_kline_before_active_group_noop():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    group_click = script.split(
        "var groupValue = button.getAttribute('data-group-value');", 1
    )[1].split("groupsHost.addEventListener('keydown'", 1)[0]
    close_index = group_click.index("closeFloatingKline();")
    active_noop_index = group_click.index(
        "if (groupValue === selectedGroupValue) return;"
    )
    selection_index = group_click.index("selectedGroupValue = groupValue;")
    render_index = group_click.index("renderPreservingPosition();")

    assert close_index < active_noop_index < selection_index < render_index


def test_special_focus_script_uses_desktop_hover_and_mobile_click_for_kline():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    assert "function isMobileKlineMode()" in script
    assert "window.innerWidth <= 768" in script
    assert "window.matchMedia('(hover: none), (pointer: coarse)').matches" in script
    assert "groupsHost.addEventListener('mouseover'" in script
    assert "groupsHost.addEventListener('mousemove'" in script
    assert "groupsHost.addEventListener('mouseout'" in script
    assert "document.addEventListener('mousemove'" in script
    assert "row.contains(event.relatedTarget)" in script
    bind_body = script.split("function bindControls()", 1)[1].split(
        "var resizeTimer", 1
    )[0]
    click_body = bind_body.split("groupsHost.addEventListener('click'", 1)[1].split(
        "groupsHost.addEventListener('keydown'", 1
    )[0]
    assert "if (!isMobileKlineMode()) return;" in click_body
    hover_body = bind_body.split("groupsHost.addEventListener('mouseover'", 1)[1].split(
        "groupsHost.addEventListener('mousemove'", 1
    )[0]
    assert "if (isMobileKlineMode()) return;" in hover_body


def test_special_focus_mobile_click_toggles_current_stock_image():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    assert "specialFocusKlineTitle" not in script
    click_body = script.split("groupsHost.addEventListener('click'", 1)[1].split(
        "groupsHost.addEventListener('keydown'", 1
    )[0]
    assert "if (itemCode(item) === openKlineCode)" in click_body
    assert "closeFloatingKline();" in click_body
    assert "showFloatingKline(item, {" in click_body
    assert "rowBottom: rowRect.bottom" in click_body


def test_special_focus_mobile_click_anchors_kline_below_selected_row():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    click_body = script.split("groupsHost.addEventListener('click'", 1)[1].split(
        "groupsHost.addEventListener('keydown'", 1
    )[0]
    assert "var rowRect = row.getBoundingClientRect();" in click_body
    assert "rowTop: rowRect.top" in click_body
    assert "y: rowRect.bottom" in click_body
    assert "rowBottom: rowRect.bottom" in click_body

    hover_body = script.split("groupsHost.addEventListener('mouseover'", 1)[1].split(
        "groupsHost.addEventListener('mousemove'", 1
    )[0]
    assert "y: event.clientY" in hover_body

    position_body = script.split("function positionFloatingKline()", 1)[1].split(
        "function renderImageKline(item)", 1
    )[0]
    assert "var belowTop = openKlineAnchor.rowBottom + cursorGap;" in position_body
    assert "var aboveTop = openKlineAnchor.rowTop - cursorGap - rect.height;" in position_body
    assert "belowTop + rect.height <= window.innerHeight - edgeMargin" in position_body


def test_special_focus_script_preserves_group_and_stock_focus_across_renders():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    capture_body = script.split("function captureListFocus(host)", 1)[1].split(
        "function restoreListFocus(host, focusState)", 1
    )[0]
    restore_body = script.split(
        "function restoreListFocus(host, focusState)", 1
    )[1].split("function renderSplitLayout()", 1)[0]
    render_body = script.split("function renderSplitLayout()", 1)[1].split(
        "function chartLayout", 1
    )[0]

    assert "document.activeElement" in capture_body
    assert "host.contains(active)" in capture_body
    assert "data-group-value" in capture_body
    assert "data-stock-code" in capture_body
    assert "focus({ preventScroll: true });" in restore_body
    capture_index = render_body.index("var focusState = captureListFocus(host);")
    first_replace_index = render_body.index("host.innerHTML =")
    last_replace_index = render_body.rindex("host.innerHTML =")
    restore_indexes = [
        index
        for index in range(len(render_body))
        if render_body.startswith("restoreListFocus(host, focusState);", index)
    ]
    assert capture_index < first_replace_index
    assert len(restore_indexes) == 2
    assert first_replace_index < restore_indexes[0] < last_replace_index
    assert last_replace_index < restore_indexes[1]


def test_special_focus_script_disposes_failed_echarts_before_fallback_markup():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    render_chart_body = script.split("function renderChart(node, record)", 1)[1].split(
        "function closeFloatingKline()", 1
    )[0]
    dispose_index = render_chart_body.index("record.instance.dispose();")
    clear_index = render_chart_body.index("record.instance = null;")
    fallback_index = render_chart_body.index(
        "node.innerHTML = '<div class=\"special-focus-chart-placeholder\">K线绘制失败"
    )

    assert dispose_index < clear_index < fallback_index


def test_special_focus_script_supports_board_payload_without_changing_yin_mode():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    assert "['board', 'yin10', 'xiasha', 'yxcs5'].indexOf(config.groupMode) >= 0" in script
    assert "? config.groupMode" in script
    assert ": 'yin';" in script
    assert "core.groupBoardStocks(mergedBoards)" in script
    assert "core.uniqueBoardStocks(boardGroups)" in script
    assert "groupMode === 'board'" in script
    assert "core.groupStocksByField(allItems || [], yinGroupField)" in script
    assert "(isYin10Mode ? '10日' : yinGroupLabel) + '阴线次数 = '" in script


def test_special_focus_script_renders_all_modes_with_one_split_layout():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    assert "var selectedGroupValue = '';" in script
    assert "function visibleGroups()" in script
    assert "function groupKey(group)" in script
    assert "function selectedGroup()" in script
    assert "function renderSplitLayout()" in script
    assert 'data-group-value="' in script
    assert 'data-stock-code="' in script
    assert "aria-pressed" in script
    assert "is-yin-mode" in script
    assert "(isYin10Mode ? '10日' : yinGroupLabel) + '阴线次数 = '" in script
    assert "板块排行" in script
    assert "renderGroups(allItems" not in script
    assert "function renderSingleStock" not in script
    assert "renderCurrentView();" in script
    assert "core.excellentDetails(item, groupMode)" in script
    assert "core.alignedGain(value)" in script


def test_special_focus_script_search_selects_underlying_stock_group():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    search_body = script.split("async function handleSearch()", 1)[1].split(
        "function bindControls()", 1
    )[0]
    assert "var searchGroups = groupMode === 'board'" in search_body
    assert "boardGroups" in search_body
    assert "groupedYinStocks()" in search_body
    assert "core.findGroupValueByCode(searchGroups" in search_body
    assert "boardFilterMode = 'all';" in search_body
    assert "selectedGroupValue = matchedGroupValue;" in search_body
    assert "closeFloatingKline();" in search_body
    assert "var buttonRect = byId('specialFocusSearchButton').getBoundingClientRect();" in search_body
    assert "showFloatingKline(item, {" in search_body
    assert "matches.length > 1" in search_body


def test_special_focus_script_retains_self_drawing_functions_unwired():
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    assert "function drawCanvas(" in script
    assert "function drawEcharts(" in script
    assert "function drawSvg(" in script
    assert "function renderChart(" in script
    active_kline_body = script.split("function renderImageKline(item)", 1)[1].split(
        "function syncPurpleFilterButton()", 1
    )[0]
    assert "renderChart(" not in active_kline_body
    assert "core.buildChartData(" not in active_kline_body


def test_special_focus_css_aligns_yin_columns_and_interactive_stock_rows():
    css_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.css",
    )
    with open(css_path, encoding="utf-8") as handle:
        styles = handle.read()

    assert styles.count(
        ".special-focus-board-layout.is-yin-mode .special-focus-board-columns,"
    ) >= 2
    assert styles.count(
        ".special-focus-board-layout.is-yin-mode .special-focus-stock"
    ) >= 2
    assert "grid-template-columns: minmax(150px, 1fr) 90px 100px;" in styles
    assert "grid-template-columns: minmax(0, 1fr) 82px;" in styles
    assert ".special-focus-row[data-stock-code] {" in styles
    assert ".special-focus-row[data-stock-code]:hover" in styles
    assert ".special-focus-row[data-stock-code]:focus-visible" in styles


def test_special_focus_page_defaults_to_sixty_visible_kline_bars(client):
    html = client.get("/特别关注1").get_data(as_text=True)
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "special-focus.js",
    )
    with open(script_path, encoding="utf-8") as handle:
        script = handle.read()

    assert "requestDays: 60" in html
    assert "displayDays: 60" in html
    assert "var DEFAULTS = { requestDays: 60, displayDays: 60 }" in script
    assert "60根K线" in script
    assert "30日K线" not in script

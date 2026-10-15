"""Pure THS condition selection and focus-page integration contracts."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "static" / "ths-condition-core.js"
TEMPLATE = ROOT / "templates" / "pz_push_log.html"


def run_core(script):
    assert CORE.exists(), "THS condition filtering helper is missing"
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend logic tests")
    prelude = "const assert = require('node:assert/strict');\nconst core = require(" + json.dumps(str(CORE)) + ");\n"
    result = subprocess.run([node, "-e", prelude + script], text=True,
                            encoding="utf-8", capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_conditions_have_the_requested_order_and_fields():
    run_core("""
assert.deepEqual(core.conditions.map(item => item.key), [
    'limit_up', 'qiangchi', 'yi_yin_yi_yang', 'is_quanyang_10d',
    'yin_count_10d', 'yin_count_20'
]);
assert.deepEqual(core.conditions.map(item => item.label), [
    '涨停', '5拉强吃', '5拉一阴一阳', '10日无阴线', '10日阴线次数', '20日阴线次数'
]);
""")


@pytest.mark.parametrize("key", ["qiangchi", "yi_yin_yi_yang", "is_quanyang_10d"])
def test_boolean_conditions_require_a_true_flag(key):
    run_core("""
const key = KEY;
const values = [1, true, '1', ' TRUE ', 'yes', 'y', 0, false, 'false', '0', '', null, undefined, 2];
const stocks = values.map((value, index) => ({code:String(index).padStart(6, '0'), [key]:value}));
const result = core.build([{board_name:'A', stocks}], key, 'A');
assert.deepEqual(result.stocks.map(stock => stock.code), ['000000','000001','000002','000003','000004','000005']);
""".replace("KEY", json.dumps(key)))


def test_reference_limit_up_false_takes_precedence_and_legacy_flag_works():
    run_core("""
const stocks = [
    {code:'000001', ref_is_limit_up:false, is_limit_up:1},
    {code:'000002', ref_is_limit_up:true, is_limit_up:0},
    {code:'000003', is_limit_up:'1'},
    {code:'000004', ref_is_limit_up:null, is_limit_up:1},
    {code:'000005', ref_is_limit_up:'false', is_limit_up:1},
    {code:'000006'},
];
assert.deepEqual(core.build([{board_name:'A',stocks}], 'limit_up').stocks.map(stock => stock.code),
    ['000002','000003','000004']);
""")


@pytest.mark.parametrize("key", ["yin_count_10d", "yin_count_20"])
def test_counts_group_ascending_and_stocks_sort_gain_descending(key):
    run_core("""
const key = KEY;
const stocks = [
    {code:'000004', [key]:2, today_change:1},
    {code:'000003', [key]:'2', ref_gain:5},
    {code:'000002', [key]:0, ref_gain:3},
    {code:'000001', [key]:2, ref_gain:5},
    {code:'000005', [key]:5, ref_gain:-2},
];
const result = core.build([{board_name:'A',stocks}], key);
assert.deepEqual(result.groups.map(group => group.value), [0, 2, 5]);
assert.equal(result.groupValue, 0);
assert.deepEqual(result.stocks.map(stock => stock.code), ['000002']);
assert.deepEqual(core.build([{board_name:'A',stocks}], key, '2').stocks.map(stock => stock.code),
    ['000001','000003','000004']);
""".replace("KEY", json.dumps(key)))


def test_missing_invalid_counts_are_not_silently_grouped_as_zero():
    run_core("""
const values = [undefined, null, '', ' ', false, true, -1, 1.5, 'NaN', 'Infinity', 0, '3'];
const stocks = values.map((value,index) => ({code:String(index).padStart(6,'0'), yin_count_10d:value}));
const result = core.build([{board_name:'A',stocks}], 'yin_count_10d');
assert.deepEqual(result.groups.map(group => group.value), [0,3]);
assert.equal(result.uniqueCount,2);
assert.equal(result.stocks.length,1);
""")


@pytest.mark.parametrize("key", ["yin_count_10d", "yin_count_20"])
def test_numeric_sidebar_groups_all_boards_and_lists_stock_board_names(key):
    run_core("""
const key = KEY;
const shared = Object.freeze({code:'000001', [key]:2, ref_gain:3});
const boards = Object.freeze([
    Object.freeze({board_name:'B', stocks:Object.freeze([
        shared, Object.freeze({code:'000003', [key]:2, ref_gain:8}),
        Object.freeze({code:'000002', [key]:0, ref_gain:1})
    ])}),
    Object.freeze({board_name:'A', stocks:Object.freeze([
        shared, Object.freeze({code:'000004', [key]:2, ref_gain:10}),
        Object.freeze({code:'000005', [key]:5, ref_gain:4})
    ])}),
]);
const before = JSON.stringify(boards);
const result = core.build(boards, key, '2');
assert.deepEqual(result.groups.map(group => [group.value, group.stocks.length]), [[0,1],[2,3],[5,1]]);
assert.equal(result.groupValue,2);
assert.equal(result.boardName,'');
assert.equal(result.uniqueCount,5);
assert.deepEqual(result.stocks.map(stock => stock.code), ['000003','000001','000004']);
assert.deepEqual(result.stocks[1].ths_board_names, ['B','A']);
assert.deepEqual(result.stocks[2].ths_board_names, ['A']);
assert.equal(JSON.stringify(boards),before);
assert.equal(Object.hasOwn(shared,'ths_board_names'),false);
assert.equal(core.build(boards,key,'missing').groupValue,0);
assert.equal(core.build(boards,key,0).groupValue,0);
assert.equal(core.build(boards,key,'5').groupValue,5);
assert.equal(core.build([],key,'2').groupValue,null);
assert.deepEqual(core.build([],key,'2').stocks,[]);
""".replace("KEY", json.dumps(key)))


def test_numeric_groups_use_current_filtered_payload_and_gain_callback():
    run_core("""
const boards = [{board_name:'A',stocks:[
    {code:'000002',yin_count_10d:3,ref_gain:10,custom_gain:1},
    {code:'000001',yin_count_10d:3,ref_gain:1,custom_gain:8},
]}];
const result=core.build(boards,'yin_count_10d','3',stock=>stock.custom_gain);
assert.deepEqual(result.stocks.map(stock=>stock.code),['000001','000002']);
assert.equal(result.groupValue,3);
const filtered=boards.map(board=>({...board,stocks:board.stocks.filter(stock=>stock.code==='000002')}));
assert.equal(core.build(filtered,'yin_count_10d','3').uniqueCount,1);
assert.deepEqual(core.build(filtered,'yin_count_10d','3').stocks.map(stock=>stock.code),['000002']);
""")


def test_board_order_selection_empty_results_and_unique_counts():
    run_core("""
const shared = {code:'000001', qiangchi:1};
const boards = [
    {board_name:'B', board_change_percent:-3, stocks:[shared]},
    {board_name:'Empty', board_change_percent:9, stocks:[{code:'000003',qiangchi:0}]},
    {board_name:'A', board_change_percent:8, stocks:[shared,{code:'000002',qiangchi:1}]},
];
const result = core.build(boards, 'qiangchi', 'A');
assert.deepEqual(result.boards.map(board => board.board_name), ['B','A']);
assert.equal(result.boardName, 'A');
assert.equal(result.uniqueCount,2);
assert.equal(core.build(boards, 'qiangchi', 'missing').boardName, 'B');
const empty = core.build(boards, 'is_quanyang_10d', 'A');
assert.deepEqual(empty.boards, []);
assert.deepEqual(empty.stocks, []);
assert.equal(empty.boardName, '');
assert.equal(core.build([], 'missing').condition.key, 'limit_up');
""")


def test_source_payload_is_not_mutated_and_gain_callback_is_supported():
    run_core("""
const stocks = Object.freeze([
    Object.freeze({code:'000002', qiangchi:1, custom_gain:8}),
    Object.freeze({code:'000001', qiangchi:1, custom_gain:10}),
]);
const boards = Object.freeze([Object.freeze({board_name:'A',stocks})]);
const before = JSON.stringify(boards);
const result = core.build(boards, 'qiangchi', 'A', stock => stock.custom_gain);
assert.deepEqual(result.stocks.map(stock => stock.code), ['000001','000002']);
assert.equal(JSON.stringify(boards),before);
""")


def test_template_embeds_lower_list_and_reuses_existing_data_and_kline_handlers():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="focusPage6ConditionPanel"' in html
    assert html.index('id="focusPage6Rows"') < html.index('id="focusPage6ConditionPanel"') < html.index('id="focusRedisPage"')
    assert 'id="focusPage6Conditions"' in html
    assert 'id="focusPage6ConditionBoards"' in html
    assert 'id="focusPage6ConditionRows"' in html
    assert '/static/ths-condition-core.js?v=' in html
    assert 'renderFocusPage6Conditions(visibleBoards)' in html
    assert 'bindStockInteractions(focusPage6ConditionRows)' in html
    assert "'page6-conditions'" in html
    assert 'focusPage6ConditionRows?.contains(state.openKlineAnchor)' in html
    assert 'grid-template-columns: 98px 104px minmax(0, 1fr)' in html
    assert 'id="focusPage6ConditionSidebarTitle"' in html
    assert 'id="focusPage6ConditionSidebar"' in html
    assert 'id="focusPage6ConditionColumns"' in html
    assert 'data-ths-yin-count=' in html
    assert "'data-ths-yin-count', 'page6ConditionGroup'" in html
    assert 'stock.ths_board_names' in html
    assert 'focus-page6-condition-board' in html


def test_unchanged_polling_preserves_controls_and_open_kline_elements():
    html = TEMPLATE.read_text(encoding="utf-8")
    marker = '        function renderThsConditionMarkup('
    assert marker in html, "Unchanged condition lists should preserve their DOM"
    function = marker + html.split(marker, 1)[1].split('        function renderFocusPage6Conditions', 1)[0]
    run_core("const thsConditionMarkup = new WeakMap();\n" + function + """
let updates = 0;
const element = {set innerHTML(value) { updates += 1; }};
renderThsConditionMarkup(element, 'stocks');
renderThsConditionMarkup(element, 'stocks');
assert.equal(updates,1);
renderThsConditionMarkup(element, 'loading');
renderThsConditionMarkup(element, 'stocks');
assert.equal(updates,3);
""")

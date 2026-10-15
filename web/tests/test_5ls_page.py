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


def test_5ls_stocks_exposes_boolean_groups_and_scan_info(client):
    """5拉升接口应返回页面分组所需字段，并单独透传扫描元信息"""
    mock_r = MagicMock()
    mock_r.exists.return_value = True
    mock_r.type.return_value = 'hash'
    payload = {
        'code': '600488',
        'name': '津药药业',
        'stock_code_with_market': '600488.SH',
        'ref_gain': 6.23,
        'ref_max_gain': 9.88,
        'today_change': 7.12,
        'current_price': 12.34,
        'ref_limit_up': 1,
        'high_gain_count': 3,
        'close_below_ma5_10d_count': 5,
        'yin_close_below_ma5_10d_count': 2,
        'gains_10': 12.5,
        'lsyin_sum': 4.5,
        'lsyin_avg': 1.5,
        'yin_body_pct_sum_10d': 2.3456,
        'max_drawdown_10d': 4.56,
        'yin_count': 1,
        'all_yin_up': 1,
        'all_yin_no_touch': 0,
        'all_yin_cross_star': 1,
        'all_yin_after_strong_10d': 1,
        'almost_bare_foot_yang_count': 4,
        'bare_foot_yang_count_10d': 3,
        'almost_bare_foot_yang_gte3': 1,
        'yin_count_lt1': 1,
        'all_yin_pull_prev_or_next': 1,
        'non_special_limit_up': 1,
        'ref_high_gain_ok': 1,
        'ref_is_yang': 1,
        'ref_bare_foot_yang_high_gain_gte_6_5_low_gt_ma5': 1,
        'is_20d_new_high': 1,
        'first_found_time': '2026-04-23 09:35:01',
    }
    scan_info = {
        'last_scan_duration_sec': 3.18,
        'last_scan_end_time': '2026-04-23 15:20:30',
    }
    mock_r.hgetall.return_value = {
        '600488': base64.b64encode(json.dumps(payload, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
        '_scan_info': base64.b64encode(json.dumps(scan_info, ensure_ascii=False).encode('utf-8')).decode('utf-8'),
    }

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/5ls/stocks?date=2026-04-23')
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['date'] == '20260423'
    assert data['data'][0]['code'] == '600488'
    assert data['data'][0]['ref_gain'] == pytest.approx(6.23)
    assert data['data'][0]['ref_max_gain'] == pytest.approx(9.88)
    assert data['data'][0]['today_change'] == pytest.approx(7.12)
    assert data['data'][0]['current_price'] == pytest.approx(12.34)
    assert data['data'][0]['close_below_ma5_10d_count'] == 5
    assert data['data'][0]['yin_close_below_ma5_10d_count'] == 2
    assert data['data'][0]['lsyin_sum'] == pytest.approx(4.5)
    assert data['data'][0]['lsyin_avg'] == pytest.approx(1.5)
    assert data['data'][0]['yin_body_pct_sum_10d'] == pytest.approx(2.3456)
    assert data['data'][0]['ref_limit_up'] is True
    assert data['data'][0]['all_yin_up'] is True
    assert data['data'][0]['all_yin_no_touch'] is False
    assert data['data'][0]['all_yin_cross_star'] is True
    assert data['data'][0]['all_yin_after_strong_10d'] is True
    assert data['data'][0]['max_drawdown_10d'] == pytest.approx(4.56)
    assert data['data'][0]['bare_foot_yang_count_10d'] == 3
    assert data['data'][0]['almost_bare_foot_yang_gte3'] is True
    assert data['data'][0]['yin_count_lt1'] is True
    assert data['data'][0]['all_yin_pull_prev_or_next'] is True
    assert data['data'][0]['non_special_limit_up'] is True
    assert data['data'][0]['ref_high_gain_ok'] is True
    assert data['data'][0]['ref_is_yang'] is True
    assert data['data'][0]['ref_bare_foot_yang_high_gain_gte_6_5_low_gt_ma5'] == 1
    assert data['data'][0]['is_20d_new_high'] is True
    assert data['scan_info']['last_scan_duration_sec'] == pytest.approx(3.18)
    assert data['scan_info']['last_scan_end_time'] == '2026-04-23 15:20:30'


def test_5ls_dates_formats_redis_dates(client):
    """5拉升日期接口应把 Redis key 日期格式化为 YYYY-MM-DD"""
    mock_r = MagicMock()
    mock_r.keys.return_value = ['5ls:20260423', '5ls:20260422']

    with patch('app.get_redis_client', return_value=mock_r):
        resp = client.get('/api/5ls/dates')
        data = json.loads(resp.data)

    assert data == {
        'success': True,
        'dates': ['2026-04-23', '2026-04-22'],
    }


def test_5ls_realtime_returns_latest_change(client):
    """5拉升实时接口应返回按代码映射的最新涨跌幅"""
    mock_response = MagicMock()
    mock_response.text = 'v_sz300123="51~测试股票~300123~12.30~10.00~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0";'
    mock_response.encoding = 'gbk'

    with patch('app.requests.get', return_value=mock_response):
        resp = client.post('/api/5ls/realtime', json={'codes': ['300123']})
        data = json.loads(resp.data)

    assert data['success'] is True
    assert data['data']['300123']['change'] == pytest.approx(23.0)


def test_5ls_realtime_logs_tencent_payload_and_parse_summary(client):
    """5拉升实时接口应打印腾讯原始返回与解析摘要，便于排查刷新滞后"""
    mock_response = MagicMock()
    mock_response.text = 'v_sz300123="51~测试股票~300123~12.30~10.00~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0";'
    mock_response.encoding = 'gbk'

    with patch('app.requests.get', return_value=mock_response), patch('builtins.print') as mock_print:
        resp = client.post('/api/5ls/realtime', json={'codes': ['300123']})
        data = json.loads(resp.data)

    assert data['success'] is True
    logged = '\n'.join(' '.join(str(arg) for arg in call.args) for call in mock_print.call_args_list)
    assert '[5ls][realtime] start' in logged
    assert 'codes=1' in logged
    assert 'raw=' in logged
    assert 'parsed code=300123' in logged
    assert 'change=23.0' in logged
    assert '[5ls][realtime] done' in logged


def test_5ls_template_and_yhy_nav_include_new_page(client):
    """5拉升页面应使用连续5不碰风格分组，并在阴后强导航中出现入口"""
    resp = client.get('/5拉升')
    html = resp.get_data(as_text=True)

    assert '5拉升' in html
    assert 'Redis: 5ls' in html
    assert 'columns-grid' in html
    assert 'grid-template-columns: repeat(7, minmax(0, 1fr));' in html
    assert '.container { max-width: 1320px; width: 100%; }' in html
    assert 'justify-content: stretch;' in html
    assert 'gap: 8px;' in html
    assert 'width: auto;' in html
    assert 'metric-column' in html
    assert '.nav-buttons { margin-bottom: 10px; display: flex; justify-content: flex-start; align-items: center; gap: 15px; flex-wrap: wrap; }' in html
    assert '.nav-right { display: flex; gap: 5px; align-items: center; flex-wrap: wrap; justify-content: flex-start; }' in html
    assert 'stock-row-header' in html
    assert 'grid-template-columns: 2.2fr 1fr 1fr 1fr 1fr;' in html
    assert 'grid-template-columns: 2.25fr 0.72fr 0.78fr 0.9fr 0.85fr;' in html
    assert '.stock-name { display: flex; flex-direction: column; align-items: center; justify-content: center; min-width: 0; text-align: center; }' in html
    assert 'grid-template-columns: minmax(0, 1.6fr) minmax(54px, 0.82fr);' in html
    assert '.stock-row-header.metric-row-header,\n        .stock-row.metric-row {\n            grid-template-columns: minmax(0, 1.35fr) minmax(42px, 0.72fr) minmax(46px, 0.78fr);\n        }' in html
    assert '.stock-row-header span {\n            white-space: normal;\n            overflow-wrap: anywhere;\n            word-break: keep-all;\n            line-height: 1.15;\n            font-size: 9px;\n        }' in html
    assert '.stock-row .s-name {\n            font-family: "Microsoft YaHei", "微软雅黑", Arial, sans-serif;\n            font-size: 13px;\n            font-weight: 400;\n            line-height: 1.2;\n            color: #333;\n            padding: 0 2px;\n        }' in html
    assert '.s-code { font-size: 10px; color: #86868b; margin-top: 1px; text-align: center; }' in html
    assert '.stock-row .s-gain {\n            font-family: "Microsoft YaHei", "微软雅黑", Arial, sans-serif;\n            font-size: 13px;\n            font-weight: 400;\n            line-height: 1.2;\n            white-space: nowrap;\n            overflow-wrap: normal;\n            word-break: keep-all;\n        }' in html
    assert '.positive { color: #db4a39; font-weight: 400; }' in html
    assert '.negative { color: #27ae60; font-weight: 400; }' in html
    assert '.neutral  { color: #333; font-weight: 400; }' in html
    assert '.stock-row.metric-row .s-metric {\n            font-size: 10px;\n            line-height: 1.15;\n            white-space: normal;\n            overflow-wrap: anywhere;\n            word-break: break-word;\n        }' in html
    assert '.task-row-header span:first-child {' in html
    assert '.good     { color: #f43f5e; }' in html
    assert '.low-good { color: #27ae60; }' in html
    assert '.mid-good { color: #f59e0b; }' in html
    assert '.extra    { color: #f97316; font-weight: 700; }' in html
    assert '.group-block + .group-block { border-top: 1px solid #e5e7eb; }' in html
    assert '.group-block.all-stocks-divider' in html
    assert '.group-label.all-stocks-divider { background: #f0f9ff; color: #0369a1; }' in html
    assert '.group-label.yin-up { background: #eef2ff; color: #4338ca; }' in html
    assert '.group-label.no-touch { background: #fff7ed; color: #c2410c; }' in html
    assert '.group-label.yin-body { background: #fdf2f8; color: #be123c; }' in html
    assert '.group-label.cross-star { background: #f5f3ff; color: #6d28d9; }' in html
    assert '.group-label.limit-up-pull { color: #dc2626; }' in html
    assert '.group-label.non-limit-pull { background: #fff7ed; color: #c2410c; }' in html
    assert '.group-label.lsyin-avg-rank { background: #eef2ff; color: #4338ca; }' in html
    assert '.group-label.drawdown-low { background: #ecfdf5; color: #15803d; }' in html
    assert '.group-label.drawdown-mid { background: #fffbeb; color: #b45309; }' in html
    assert '.group-label.drawdown-high { background: #fef2f2; color: #b91c1c; }' in html
    assert '.s-found { font-size: 10px; color: #86868b; margin-top: 1px; text-align: center; }' in html
    assert '.col-header {\n            height: 86px;\n            box-sizing: border-box;\n            display: flex;\n            flex-direction: column;\n            align-items: center;\n            justify-content: center;\n        }' in html
    assert '.badge-group { display: flex; flex-wrap: nowrap; justify-content: center; gap: 2px; margin-bottom: 2px; overflow: hidden; }' in html
    assert '.yhq-badge {' in html
    assert '.stock-badge-line {\n            display: flex;\n            flex-wrap: nowrap;' in html
    assert 'overflow: hidden;\n            white-space: nowrap;' in html
    assert '.stock-row.metric-row .stock-inline-badge {\n            font-size: 8px;\n            padding: 1px 3px;\n            line-height: 1.15;\n            letter-spacing: 0;\n        }' in html
    assert '.stock-row.metric-row .stock-badge-line {\n            gap: 2px;\n            min-height: 13px;\n            margin-bottom: 2px;\n        }' in html
    assert '.yhq-badge-up      { background: linear-gradient(135deg, #7c3aed, #a855f7); }' in html
    assert 'function formatFoundTime(value)' in html
    assert 'function formatDrawdown(value)' in html
    assert 'function cloneStocks(stocks)' in html
    assert 'function getShanghaiNowParts()' in html
    assert 'function isTradingSessionNow()' in html
    assert 'function shouldUseRealtimeStocks()' in html
    assert 'function buildPullBadge(s)' in html
    assert '前后拉升' in html
    assert '回撤' in html
    assert 'deriveLatestFoundTime' in html
    assert ".map(s => String(s.first_found_time || ''))" in html
    assert '阴线都向上' in html
    assert '寻找一字，厂子，然后添加！' in html
    assert '涨幅≥7.5%' in html
    assert '从第一列筛选，再按回撤幅度分组' in html
    assert '参考日最高涨幅≥6.5%' in html
    assert '读取 ref_high_gain_ok，且当前涨幅&lt;7.5%' in html or '读取 ref_high_gain_ok，且当前涨幅<7.5%' in html
    assert '回撤幅度&lt;5%' in html or '回撤幅度<5%' in html
    assert '回撤幅度5%-10%' in html
    assert '回撤幅度&gt;10%' in html or '回撤幅度>10%' in html
    assert '一字横盘示例' in html
    assert '厂子横盘示例' in html
    assert '一字横盘01' in html
    assert '一字横盘02' in html
    assert '厂子横盘01' in html
    assert '5ls-hints' in html
    assert "header.innerHTML = '阴线跌破5日线等于' + count" in html
    assert "header.innerHTML = '跌破5日线等于' + count" not in html
    assert "stock.yin_close_below_ma5_10d_count" in html
    assert "stock.close_below_ma5_10d_count" not in html
    assert '先显示20新高，再显示全部股票；组内按涨跌幅由高到低排序' in html
    assert '5拉升' in html
    assert '显示全部股票，按涨跌幅由高到低排序' in html
    assert '按光脚高涨、高涨阳线、阳线三组显示' not in html
    assert '几乎光脚阳线且最高涨幅 >= 6.5%' not in html
    assert '最高涨幅 >= 6.5%并且是阳线' not in html
    assert '20新高且拉升' in html
    assert '20新高' in html
    assert '全部股票' in html
    assert 'isTrueFlag(stock.is_20d_new_high) && isTrueFlag(stock.ref_high_gain_ok) && isTrueFlag(stock.ref_is_yang)' in html
    assert 'isTrueFlag(stock.is_20d_new_high) && isTrueFlag(stock.ref_is_yang) && !isTrueFlag(stock.ref_high_gain_ok)' in html
    assert "const allStocks = stocks.slice().sort((a, b) => getDisplayChangeValue(b) - getDisplayChangeValue(a));" in html
    assert "grid.appendChild(buildStackedColumn([buildAllYinUpColumn(stocks), buildAllYinNoTouchColumn(stocks), buildColumn(mainColumn, mainColumn.stocks)]));" in html
    assert "grid.appendChild(buildYinBodyPctSumColumn(stocks));" in html
    assert "grid.appendChild(buildYinCountColumn(stocks));" in html
    assert "grid.appendChild(buildStackedColumn([buildAllYinUpColumn(stocks), buildYinCountColumn(stocks)]));" not in html
    assert "grid.appendChild(buildStackedColumn([buildAllYinUpColumn(stocks), buildColumn(mainColumn, mainColumn.stocks)]));" not in html
    assert "grid.appendChild(buildColumn(mainColumn, mainColumn.stocks));" not in html
    assert "function buildAllYinUpColumn(sourceStocks)" in html
    assert "return isTrueFlag(stock && stock.all_yin_up);" in html
    assert "header.innerHTML = '阴线都向上<div class=\"col-header-sub\">筛选 all_yin_up=true，按涨跌幅由高到低排序</div>';" in html
    assert "buildGroup(card, '阴线都向上', 'yin-up', matchedStocks);" in html
    assert "function getYinCount(stock)" in html
    assert "function buildYinCountColumn(sourceStocks)" in html
    assert "header.innerHTML = '阴线次数分组<div class=\"col-header-sub\">按 redis yin_count 由小到大分组，同组按涨跌幅由高到低排序</div>';" in html
    assert "按 redis yin_count 由小到大分组，同组按涨跌幅由高到低排序" in html
    assert "按 redis yin_count 由小到大分组，同组按 yin_body_pct_sum_10d 由小到大排序" not in html
    assert "groups.sort(function(a, b) { return a.count - b.count; });" in html
    assert "stocks: entry[1].slice().sort(function(a, b) {\n                return getDisplayChangeValue(b) - getDisplayChangeValue(a);\n            })" in html
    assert "buildGroup(card, group.count + '阴', 'yin-count', group.stocks);" in html
    assert ".col-header.yin-count { background: linear-gradient(135deg, #f9a8d4, #ec4899); color: #ffffff; }" in html
    assert ".group-label.yin-count { background: rgba(236, 72, 153, 0.88); color: #ffffff !important; }" in html
    assert ".group-label.yin-count { background: #fce7f3 !important; color: #9f1239 !important; }" not in html
    assert ".group-label.no-touch { background: rgba(249, 115, 22, 0.88); color: #ffffff !important; }" in html
    assert ".group-label.cross-star { background: rgba(124, 58, 237, 0.88); color: #ffffff !important; }" in html
    assert "function getYinBodyPctSum(stock)" in html
    assert "function buildYinBodyPctSumColumn(sourceStocks)" in html
    assert "let _yinBodyShowHighGainYangOnly" not in html
    assert "function setYinBodyHighGainYangFilter(enabled)" not in html
    assert "header.innerHTML = '累计阴线实体<div class=\"col-header-sub\">按 yin_body_pct_sum_10d 由小到大排序</div>';" in html
    assert "const crossStarStocks = sortedStocks.filter(function(stock) { return isTrueFlag(stock && stock.all_yin_cross_star); });" in html
    assert "buildGroup(card, '阴线都是十字星', 'cross-star', crossStarStocks);" in html
    assert html.index("buildGroup(card, '阴线都是十字星', 'cross-star', crossStarStocks);") < html.index("labelDiv.textContent = '全部股票  (' + sortedStocks.length + ')';")
    assert "actions.className = 'col-toggle-actions';" not in html
    assert "rowHeader.className = 'stock-row-header metric-row-header';" in html
    assert "rowHeader.innerHTML = '<span>名称</span><span>涨跌幅</span><span>阴线实体</span>';" in html
    assert "appendYinBodyPctRows(body, sortedStocks);" in html
    assert "function appendYinBodyPctRows(body, stocks)" in html
    assert "row.className = 'stock-row metric-row';" in html
    assert "return getCloseBelowMa5Count(stock) === count;" in html
    assert "return getDisplayChangeValue(b) - getDisplayChangeValue(a);" in html
    assert "ungrouped: true" in html
    assert "grid.appendChild(buildStackedColumn([buildAllYinNoTouchColumn(stocks), buildCloseBelowMa5Column(stocks, 0)]));" not in html
    assert "grid.appendChild(buildCloseBelowMa5Column(stocks, 0));" in html
    assert "grid.appendChild(buildCloseBelowMa5Column(stocks, 1));" in html
    assert "grid.appendChild(buildCloseBelowMa5Column(stocks, 2));" in html
    assert "function buildAllYinNoTouchColumn(sourceStocks)" in html
    assert "return isTrueFlag(stock && stock.all_yin_no_touch);" in html
    assert "header.innerHTML = '阴线都不碰<div class=\"col-header-sub\">筛选 all_yin_no_touch=true，按涨跌幅由高到低排序</div>';" in html
    assert "buildGroup(card, '阴线都不碰', 'no-touch', matchedStocks);" in html
    assert "grid.appendChild(buildStackedColumn([buildAlmostBareFootYangColumn(stocks), buildTaskSection()]));" in html
    assert "function getAlmostBareFootYangCount(stock)" in html
    assert "function getBareFootYangCount10d(stock)" in html
    assert "function getActiveBareFootYangCount(stock)" in html
    assert "function buildAlmostBareFootYangColumn(sourceStocks)" in html
    assert "let _bareFootYangCountMode = 'exact';" in html
    assert "const isAlmostMode = _bareFootYangCountMode === 'almost';" in html
    assert "header.innerHTML = (isAlmostMode ? '几乎光脚阳线次数' : '10天光脚阳线次数')" in html
    assert "按 bare_foot_yang_count_10d 由高到低分组" in html
    assert "按 almost_bare_foot_yang_count 由高到低分组" in html
    assert "data-bare-foot-count-mode=\"exact\"" in html
    assert "data-bare-foot-count-mode=\"almost\"" in html
    assert "setBareFootYangCountMode(modeBtn.dataset.bareFootCountMode);" in html
    assert "return _bareFootYangCountMode === 'almost' ? getAlmostBareFootYangCount(stock) : getBareFootYangCount10d(stock);" in html
    assert ".group-label.bare-foot { background: #be185d; color: #ffffff; }" in html
    assert "const yangStocks = (sourceStocks || []).filter(function(stock) { return isTrueFlag(stock.ref_is_yang); });" in html
    assert "yangStocks.forEach(function(stock)" in html
    assert "groups.sort(function(a, b) { return b.count - a.count; });" in html
    assert "buildGroup(card, group.count + '次', 'bare-foot', group.stocks);" in html
    assert "function getLsyinAvg(stock)" in html
    assert "function buildCloseBelowMa5LsyinAvgGroup(card, sourceStocks)" in html
    assert "buildCloseBelowMa5LsyinAvgGroup(card, newHighPullStocks.concat(newHighStocks));" in html
    assert "return avg !== null && avg >= 0;" in html
    assert "return getLsyinAvg(b) - getLsyinAvg(a);" in html
    assert "阴线均值排行" in html
    assert "labelDiv.className = 'group-label lsyin-avg-rank';" in html
    assert "从20新高且拉升和20新高筛选，lsyin_avg >= 0" in html
    assert "function buildLsyinAvgColumn(sourceStocks)" not in html
    assert "getCloseBelowMa5Count(stock) === 0 || getCloseBelowMa5Count(stock) === 1 || getCloseBelowMa5Count(stock) === 2" not in html
    assert "if (col.ungrouped)" in html
    assert "buildUngroupedList(card, stocks);" in html
    assert "buildGroup(card, '全部股票', 'non-limit-no-pull all-stocks-divider', matchedStocks);" in html
    assert "block.classList.add('all-stocks-divider');" in html
    assert "rowHeader.innerHTML = '<span>名称</span><span>涨跌幅</span>';" in html
    assert "10天累计" not in html
    assert "'<span class=\"s-metric ' + gains10Class + '\">'" not in html
    assert "appendStockRows(body, sortedStocks);" in html
    assert "formatDrawdown(stock.max_drawdown_10d)" in html
    assert '3秒刷新 · 涨跌幅从接口获取' not in html
    assert 'async function refreshRealtime()' in html
    assert 'if (!shouldUseRealtimeStocks()) {' in html
    assert '_stocks = cloneStocks(_redisStocks);' in html
    assert '_hasRealtimeOverlay = false;' in html
    assert "const resp = await fetch('/api/5ls/realtime'," in html
    assert "const codes = _redisStocks.map(s => s.code);" in html
    assert "await refreshRealtime();" in html
    assert '_realtimeTimer = setInterval(refreshRealtime, 3000);' in html
    assert '涨停&前后拉升' in html
    assert '涨停&非前后拉升' in html
    assert '非涨停组&前后拉升' in html
    assert '非涨停组&非前后拉升' in html
    assert '/api/5ls/dates' in html
    assert '/api/5ls/stocks' in html
    assert '/api/5ls/realtime' in html
    assert 'scanInfoText' in html
    assert '扫描耗时' in html
    assert '扫描完成时间:' in html
    assert 'displayModeFilters' in html
    assert 'main-content-layout' in html
    assert '.main-content-layout {\n            position: relative;' in html
    assert 'background: #ffffff; min-height: 100vh; padding: 20px 20px 20px 144px; margin: 0;' in html
    assert 'position: absolute;\n            left: -100px;\n            top: 0;' in html
    assert 'position: fixed;\n            left: 12px;\n            top: 120px;' not in html
    assert '@media (max-width: 1430px) {\n            body { padding-left: 136px; }' in html
    assert 'left: -100px;\n                top: 0;' in html
    assert 'position: sticky;\n            top: 10px;' not in html
    assert '/* mobile access layout */' in html
    assert '.main-content-layout {\n                flex-direction: column;' in html
    assert '.nav-buttons {\n                flex-wrap: wrap;\n                overflow-x: visible;' in html
    assert '.nav-left,\n            .nav-right {\n                flex: 1 1 100%;' in html
    assert '.display-mode-filters {\n                position: static;\n                left: auto;' in html
    assert 'flex-wrap: wrap;\n                overflow-x: visible;' in html
    assert '.mobile-list-tabs {' in html
    assert '.mobile-list-tabs {\n                position: sticky;\n                top: 0;\n                z-index: 80;\n                display: flex;\n                flex-wrap: wrap;' in html
    assert '.mobile-list-tabs' in html and 'overflow-x: visible;' in html
    assert '.mobile-list-tab.active {' in html
    assert '.columns-grid.mobile-single-list {' in html
    assert '.columns-grid.mobile-single-list .metric-column,' in html
    assert '.columns-grid.mobile-single-list .mobile-active-list {' in html
    assert 'function getMobileListPanels(grid)' in html
    assert 'function setupMobileSingleListMode(layout, grid)' in html
    assert 'function setMobileListIndex(grid, index)' in html
    assert "grid.addEventListener('touchstart', function(e) {" in html
    assert "grid.addEventListener('touchend', function(e) {" in html
    assert 'if (Math.abs(deltaX) <= Math.abs(deltaY) || Math.abs(deltaX) < 60) return;' in html
    assert 'if (deltaX < -60) switchMobileList(grid, 1);' in html
    assert 'if (deltaX > 60) switchMobileList(grid, -1);' in html
    assert 'if (deltaY < -60) switchMobileList(grid, 1);' not in html
    assert 'if (deltaY > 60) switchMobileList(grid, -1);' not in html
    assert '.display-mode-btn { flex: 0 1 auto; width: auto; min-width: 74px;' in html
    assert '.mobile-list-tab {\n                flex: 0 1 auto;' in html
    assert '.stock-tooltip {\n                display: none !important;' not in html
    assert 'function isMobileKlineMode()' in html
    assert 'function attachKlineEvents(row, code, name)' in html
    assert "row.addEventListener('click', function(e) {" in html
    assert "if (!isMobileKlineMode()) return;" in html
    assert 'left: -84px;\n                top: 0;' in html
    assert 'buildDisplayModeFilterRail' in html
    assert 'displayModeBareFootHighGainLowGtMa5Btn' in html
    assert 'displayModeBareFootLimitUpBtn' in html
    assert 'displayModeHighGainYangBtn' in html
    assert 'displayModeRefYangBtn' in html
    assert 'displayModeBareFootHighGainBtn' in html
    assert "label: '极品光脚拉升不碰'" in html
    assert "label: '光脚涨停'" in html
    assert "label: '光脚拉升'" in html
    assert "label: '显示阳线'" in html
    assert html.index("label: '极品光脚拉升不碰'") < html.index("label: '光脚拉升'")
    assert html.index("label: '极品光脚拉升不碰'") < html.index("label: '光脚涨停'")
    assert html.index("label: '光脚涨停'") < html.index("label: '光脚拉升'")
    assert html.index("label: '光脚拉升'") < html.index("label: '显示拉升阳线'")
    assert html.index("label: '显示拉升阳线'") < html.index("label: '显示阳线'")
    assert html.index("label: '显示阳线'") < html.index("label: '显示全部'")
    assert "variant: 'purple'" in html
    assert ".display-mode-btn-purple" in html
    assert "background: #f3e8ff;" in html
    assert "background: linear-gradient(135deg, #4c1d95, #7c3aed);" in html
    assert "box-shadow: 0 0 0 2px rgba(196, 181, 253, 0.85), 0 6px 14px rgba(76, 29, 149, 0.28);" in html
    assert 'displayModeAllBtn' in html
    assert 'displayModePullBtn' in html
    assert 'displayModeLimitBtn' in html
    assert 'displayModeYinAfterStrongBtn' in html
    assert "btn.dataset.displayMode = item.mode;" in html
    assert "let _displayHighGainYangOnly = true;" in html
    assert "let _displayRefYangOnly = false;" in html
    assert "let _displayBareFootHighGainOnly = false;" in html
    assert "let _displayBareFootHighGainLowGtMa5Only = false;" in html
    assert "let _displayBareFootLimitUpOnly = false;" in html
    assert "let _displayMode = 'all';" in html
    assert "let _displayLimitOnly = false;" in html
    assert "let _displayYinAfterStrongOnly = false;" in html
    assert "function getVisibleStocksForCurrentMode(stocks)" in html
    assert "if (_displayBareFootHighGainLowGtMa5Only)" in html
    assert "list = list.filter(s => isTrueFlag(s.ref_bare_foot_yang_high_gain_gte_6_5_low_gt_ma5));" in html
    assert "if (_displayBareFootLimitUpOnly)" in html
    assert "list = list.filter(s => isTrueFlag(s.ref_bare_foot_yang_high_gain_gte_6_5) && isTrueFlag(s.ref_limit_up));" in html
    assert "if (_displayHighGainYangOnly)" in html
    assert "list = list.filter(s => isTrueFlag(s.ref_high_gain_gte_6_5_and_yang));" in html
    assert "if (_displayRefYangOnly)" in html
    assert "list = list.filter(s => isTrueFlag(s.ref_is_yang));" in html
    assert "if (_displayBareFootHighGainOnly)" in html
    assert "list = list.filter(s => isTrueFlag(s.ref_bare_foot_yang_high_gain_gte_6_5));" in html
    assert "if (_displayMode === 'pull')" in html
    assert "list = list.filter(s => isTrueFlag(s.all_yin_pull_prev_or_next));" in html
    assert "if (_displayLimitOnly)" in html
    assert "list = list.filter(s => isTrueFlag(s.ref_limit_up));" in html
    assert "if (_displayYinAfterStrongOnly)" in html
    assert "list = list.filter(s => isTrueFlag(s.all_yin_after_strong_10d));" in html
    assert "function updateDisplayModeButtons()" in html
    assert "if (bareFootHighGainLowGtMa5Btn) bareFootHighGainLowGtMa5Btn.classList.toggle('active', _displayBareFootHighGainLowGtMa5Only);" in html
    assert "if (bareFootLimitUpBtn) bareFootLimitUpBtn.classList.toggle('active', _displayBareFootLimitUpOnly);" in html
    assert "if (refYangBtn) refYangBtn.classList.toggle('active', _displayRefYangOnly);" in html
    assert "if (bareFootHighGainBtn) bareFootHighGainBtn.classList.toggle('active', _displayBareFootHighGainOnly);" in html
    assert "if (yinAfterStrongBtn) yinAfterStrongBtn.classList.toggle('active', _displayYinAfterStrongOnly);" in html
    assert "if (allBtn) allBtn.classList.toggle('active', _displayMode === 'all' && !_displayHighGainYangOnly && !_displayRefYangOnly && !_displayBareFootHighGainOnly && !_displayBareFootHighGainLowGtMa5Only && !_displayBareFootLimitUpOnly && !_displayYinAfterStrongOnly);" in html
    assert "function setDisplayMode(mode)" in html
    assert "if (mode === 'bare_foot_high_gain_low_gt_ma5')" in html
    assert "_displayBareFootHighGainLowGtMa5Only = !_displayBareFootHighGainLowGtMa5Only;" in html
    assert "if (_displayBareFootHighGainLowGtMa5Only) {" in html
    assert "else if (mode === 'bare_foot_limit_up')" in html
    assert "_displayBareFootLimitUpOnly = !_displayBareFootLimitUpOnly;" in html
    assert "if (_displayBareFootLimitUpOnly) {" in html
    assert "if (mode === 'high_gain_yang')" in html
    assert "_displayHighGainYangOnly = !_displayHighGainYangOnly;" in html
    assert "else if (mode === 'ref_yang')" in html
    assert "_displayRefYangOnly = !_displayRefYangOnly;" in html
    assert "else if (mode === 'bare_foot_high_gain')" in html
    assert "_displayBareFootHighGainOnly = !_displayBareFootHighGainOnly;" in html
    assert "if (_displayBareFootHighGainOnly) {" in html
    assert "_displayBareFootHighGainLowGtMa5Only = false;" in html
    assert "else if (mode === 'limit')" in html
    assert "_displayLimitOnly = !_displayLimitOnly;" in html
    assert "else if (mode === 'yin_after_strong')" in html
    assert "_displayYinAfterStrongOnly = !_displayYinAfterStrongOnly;" in html
    assert "else if (mode === 'yin_after_strong') {\n        _displayYinAfterStrongOnly = !_displayYinAfterStrongOnly;\n    } else if (mode === 'all')" in html
    assert "            _displayYinAfterStrongOnly = false;" not in html
    assert "label: '10天阴后强'" in html
    assert "variant: 'red'" in html
    assert html.index("mode: 'limit', label: '显示涨停'") < html.index("mode: 'yin_after_strong', label: '10天阴后强'")
    assert "else if (mode === 'all')" in html
    assert "_displayHighGainYangOnly = false;" in html
    assert "_displayBareFootHighGainOnly = false;" in html
    assert "_displayBareFootHighGainLowGtMa5Only = false;" in html
    assert "_displayBareFootLimitUpOnly = false;" in html
    assert "else if (mode === 'pull')" in html
    assert "renderColumns(visibleStocks);" in html
    assert "const displayBtn = e.target.closest('[data-display-mode]');" in html
    assert "setDisplayMode(displayBtn.dataset.displayMode);" in html
    assert 'deriveLatestFoundTime' in html
    assert 'first_found_time' in html
    assert "timeZone: 'Asia/Shanghai'" in html
    assert "const isTradingTime = (totalMinutes >= 570 && totalMinutes < 690) || (totalMinutes >= 780 && totalMinutes < 900);" in html
    assert 's.all_yin_pull_prev_or_next' in html
    assert "if (!s || !s.all_yin_pull_prev_or_next) return '';" in html
    assert "'<div class=\"badge-group\"><span class=\"yhq-badge yhq-badge-up\">前后拉升</span></div>'" in html
    assert 'if (gains10 > 50)' not in html
    assert 'else if (gains10 >= 40)' not in html
    assert '今日任务列表' in html
    assert 'task-context-menu' in html
    assert 'task-ios-overlay' in html
    assert 'YHY_TASK_API_BASE = \'/api/yhy/tasks\'' in html
    assert '/api/yhy/tasks/add' in html
    assert '/api/yhy/tasks/remove' in html
    assert 'bodyEl.onclick = clickHandler;' in html
    assert "source_page: getTaskSourcePageName()" in html
    assert "return '5拉升:阴线都向上';" in html
    assert "getVisibleTaskStocksForCurrentPage" in html
    assert 'buildImageHintSection()' in html
    assert '来源:' in html
    assert '筛出 all_yin_up=true' not in html
    assert '筛选 all_yin_no_touch=true' in html
    assert '阴线都是十字星' in html
    assert '筛出 almost_bare_foot_yang_gte3=true' not in html
    assert '筛出 yin_count_lt1=true' not in html
    assert '筛出 non_special_limit_up=true' not in html
    assert 's.ref_limit_up' in html

    yhy_resp = client.get('/阴后强')
    yhy_html = yhy_resp.get_data(as_text=True)
    assert '<a href="/阴后强" class="nav-btn nav-btn-yhy" style="font-weight:700;">阴后强</a>' in yhy_html
    assert '<a href="/5拉升" class="nav-btn nav-btn-5ls-core">5拉升</a>' in yhy_html

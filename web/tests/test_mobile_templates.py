import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def client():
    import app as flask_app
    flask_app.app.config['TESTING'] = True
    with flask_app.app.test_client() as c:
        yield c


def test_index_template_contains_mobile_nav_rules(client):
    resp = client.get('/', follow_redirects=True)
    html = resp.get_data(as_text=True)

    assert resp.request.path == '/5拉升'
    assert '5拉升' in html
    assert '@media (max-width: 768px)' in html
    assert 'flex-direction: column' in html
    assert 'calc(50% - 4px)' in html


def test_shared_theme_defaults_to_simple_mode(client):
    resp = client.get('/5拉升')
    html = resp.get_data(as_text=True)
    js = client.get('/static/monochrome-theme.js').get_data(as_text=True)

    assert 'stock-web-simple-theme' in html
    assert 'stock_web_simple_theme' in html
    assert 'var on=s!=="0";' in html
    assert 'monochrome-theme.css?v=20260621_1' in html
    assert "button.textContent = enabled ? '彩色模式' : '简约';" in js
    assert "return true;" in js
    assert '同花顺主题' not in html
    assert '同花顺主题' not in js


def test_shared_theme_styles_filter_secondary_and_purple_active_states(client):
    css = client.get('/static/monochrome-theme.css').get_data(as_text=True)

    assert '.monochrome-theme .col-filter-btn.secondary-active' in css
    assert '.monochrome-theme .col-filter-btn.purple-active' in css
    assert 'outline: 2px solid rgba(219, 74, 57, 0.32) !important;' in css


def test_yx205_template_contains_mobile_table_rules(client):
    resp = client.get('/20阴少跌少横盘')
    html = resp.get_data(as_text=True)

    assert '@media (max-width: 768px)' in html
    assert 'justify-content: flex-start' in html
    assert 'grid-template-columns: 1fr' in html
    assert 'table-layout: fixed' in html
    assert 'white-space: normal !important' in html
    assert 'overflow-wrap: anywhere' in html
    assert 'mobile density tuning' in html
    assert 'padding: 7px 5px;' in html
    assert 'font-size: 11px;' in html


def test_doji_star_template_contains_mobile_table_rules(client):
    resp = client.get('/阴线都是十字星')
    html = resp.get_data(as_text=True)

    assert '@media (max-width: 768px)' in html
    assert 'justify-content: flex-start' in html
    assert 'table-layout: fixed' in html
    assert 'white-space: normal !important' in html
    assert 'overflow-wrap: anywhere' in html
    assert 'mobile density tuning' in html
    assert 'padding: 7px 5px;' in html
    assert 'font-size: 11px;' in html


def test_dailystock_template_contains_mobile_table_rules(client):
    resp = client.get('/dailystock')
    html = resp.get_data(as_text=True)

    assert '@media (max-width: 768px)' in html
    assert 'overflow-x: hidden' in html
    assert 'table-layout: fixed' in html
    assert 'overflow-wrap: anywhere' in html
    assert 'min-width: 900px' not in html


@pytest.mark.parametrize(
    ('route', 'title'),
    [
        ('/涨停前横盘', '涨停前横盘'),
        ('/阴后强', '阴后强'),
        ('/5拉升', '5拉升'),
        ('/20阴少跌少横盘', '20阴少跌少横盘'),
        ('/涨停或光脚', '涨停或光脚'),
        ('/连续5不碰', '连续5不碰'),
        ('/连续涨停', '连续涨停'),
        ('/极品关注', '极品关注'),
        ('/同花顺板块', '同花顺板块'),
    ],
)
def test_core_pages_include_shared_mobile_compaction_rules(client, route, title):
    resp = client.get(route, follow_redirects=True)
    html = resp.get_data(as_text=True)

    assert title in html
    assert '@media (max-width: 768px)' in html
    assert (
        'body { padding: 10px; overflow-x: hidden; }' in html
        or 'body { padding: 8px; overflow-x: hidden; }' in html
    )
    assert '.nav-buttons { gap: 10px; }' in html
    assert '.date-selector { width: 100%; justify-content: center; flex-wrap: wrap; }' in html
    assert '#datePicker' in html
    assert 'min-width: 0 !important;' in html
    assert 'max-width: 100%;' in html


def test_jpgz_nav_links_to_ths_bankuai_next_to_jpgz(client):
    resp = client.get('/极品关注')
    html = resp.get_data(as_text=True)

    jpgz_index = html.index('href="/极品关注"')
    ths_index = html.index('href="/同花顺板块"')

    assert ths_index > jpgz_index
    assert '同花顺板块' in html


def test_ztorgj_template_contains_mobile_tab_scroller_rules(client):
    resp = client.get('/涨停或光脚')
    html = resp.get_data(as_text=True)

    assert '.tab-switcher {' in html
    assert 'justify-content: flex-start;' in html
    assert 'flex-wrap: wrap;' in html
    assert 'overflow-x: visible;' in html
    assert 'scrollbar-width: none;' in html
    assert '.tab-switcher::-webkit-scrollbar {' in html
    assert 'display: none;' in html
    assert '.tab-btn {\n                flex: 0 1 auto;' in html
    assert 'white-space: normal;' in html
    assert 'padding: 8px 10px;' in html
    assert 'border-radius: 999px;' in html
    assert 'grid-template-columns: 1.45fr 0.82fr 0.73fr;' in html
    assert 'grid-template-columns: 1.02fr 0.7fr 0.88fr 0.88fr;' in html


@pytest.mark.parametrize(
    'route',
    [
        '/涨停前横盘',
        '/阴后强',
        '/5拉升',
        '/20阴少跌少横盘',
        '/涨停或光脚',
        '/连续5不碰',
        '/连续涨停',
        '/首板涨停',
        '/极品关注',
        '/同花顺板块',
        '/连续4天拉升',
        '/阴线都向上',
        '/阴线都是十字星',
        '/5拉升20阴少',
        '/5拉升小于2阴',
        '/5拉升阴少3不下杀',
        '/5拉升3光脚',
        '/通达信5拉升十字星',
        '/通达信5拉升阴向上',
        '/dailystock',
    ],
)
def test_stock_pages_load_shared_mobile_single_list_assets(client, route):
    resp = client.get(route, follow_redirects=True)
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert '/static/mobile-single-list.css' in html
    assert '/static/mobile-single-list.js' in html
    assert 'mobile-single-list.css?v=' in html
    assert 'mobile-single-list.js?v=' in html


def test_shared_mobile_single_list_assets_support_swipe_and_list_containers(client):
    css = client.get('/static/mobile-single-list.css').get_data(as_text=True)
    js = client.get('/static/mobile-single-list.js').get_data(as_text=True)

    assert '.mobile-list-tabs[data-mobile-single-list-generated="1"]' in css
    assert css.index('.mobile-list-tabs[data-mobile-single-list-generated="1"]') < css.index('@media (max-width: 768px)')
    assert 'display: none;' in css[:css.index('@media (max-width: 768px)')]
    assert 'html.mobile-single-list-enabled .mobile-list-tabs' in css
    assert 'html.mobile-single-list-enabled .columns-grid.mobile-single-list' in css
    assert '.mobile-single-list.mobile-single-list-managed > .metric-column' in css
    assert '.mobile-single-list.mobile-single-list-managed > .table-section' in css
    assert '.mobile-single-list.mobile-single-list-managed > .ths-panel' in css
    assert '.mobile-single-list.mobile-single-list-managed > .group-container' in css
    assert '.mobile-active-list' in css
    assert '.mobile-single-list.mobile-single-list-managed > .stacked-column' in css

    assert "'.columns-grid'" in js
    assert "'.tables-layout'" in js
    assert "'.ths-layout'" in js
    assert "'.group-container'" in js
    assert "container.classList.add('mobile-single-list-managed');" in js
    assert "touchstart" in js
    assert "touchend" in js
    assert "deltaX < -60" in js
    assert "deltaX > 60" in js
    assert "function setActive(container, index, options)" in js
    assert "options = options || {};" in js
    assert "if (options.hideKline && typeof window.hideKline === 'function')" in js
    assert "setActive(container, state.index + direction, { hideKline: true, scrollTab: true });" in js
    assert "setActive(container, index, { hideKline: true, scrollTab: true });" in js
    assert "setActive(container, Math.min(state.index, panels.length - 1));" in js


def test_mobile_single_list_script_injects_after_inline_body_literals(client):
    resp = client.get('/阴后强')
    html = resp.get_data(as_text=True)

    mobile_idx = html.index('/static/mobile-single-list.js')

    assert mobile_idx > html.index('function computeTableRenderState')
    assert mobile_idx > html.index('loadDates();')

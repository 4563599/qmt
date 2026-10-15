from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_FILE = BASE_DIR / "static" / "async-market-cap.js"


TEMPLATES_WITH_ASYNC_MARKET_CAP = [
    "5las3guangjiao.html",
    "5ls.html",
    "5ls20ys.html",
    "5lsy2.html",
    "5lsyqhq.html",
    "5lsys3bxs.html",
    "5lsys3bxs_temp.html",
    "doji_star.html",
    "jpgz.html",
    "tdx5lssxs.html",
    "tdx5lsszx.html",
    "yhy.html",
    "yx205.html",
    "yx205_new.html",
    "yxdxs.html",
    "ztorgj.html",
]


def test_async_market_cap_helper_exists():
    assert STATIC_FILE.exists()
    content = STATIC_FILE.read_text(encoding="utf-8")
    assert "window.initAsyncMarketCapRefresh" in content
    assert "/api/market_cap/" in content
    assert "[data-market-cap-code]" in content
    assert "dataStores" in content
    assert "async-market-cap:updated" in content


def test_templates_include_async_market_cap_refresh():
    for template_name in TEMPLATES_WITH_ASYNC_MARKET_CAP:
        content = (TEMPLATE_DIR / template_name).read_text(encoding="utf-8")
        assert "async-market-cap.js" in content, template_name
        assert "window.initAsyncMarketCapRefresh({" in content, template_name
        assert "dataStores:" in content, template_name
        assert "data-market-cap-code" in content, template_name

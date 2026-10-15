"""Check the standalone export without connecting to production services."""

import ast
import importlib
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", ["app.py", "config.py"])
def test_tushare_token_has_no_embedded_default(filename):
    source = (ROOT / filename).read_text(encoding="utf-8")
    assignments = [node for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.Assign) and any(
                       isinstance(target, ast.Name) and target.id == "TUSHARE_TOKEN"
                       for target in node.targets)]
    assert assignments
    for assignment in assignments:
        value = assignment.value
        assert isinstance(value, ast.Call)
        assert ast.unparse(value.func) == "os.environ.get"
        assert value.args[0].value == "TUSHARE_TOKEN"
        assert value.args[1].value == ""


def test_review_builder_is_bundled_without_publishing_credentials():
    path = ROOT / "feishu_daily_review.py"
    assert path.exists()
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert {"build_sheet_content", "build_qy2_group_rows", "style_payload",
            "_style_template_data", "calculate_column_widths"} <= functions
    assert not ({"main", "load_feishu_helper", "organize_daily_sheets"} & functions)
    assert "STOCKER_FOLDER_TOKEN" not in source
    assert "DAILY_TEMPLATE_TOKEN" not in source


def test_redis_config_uses_environment(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "redis.test.invalid")
    monkeypatch.setenv("REDIS_PORT", "6381")
    monkeypatch.setenv("REDIS_PASSWORD", "test-only")
    monkeypatch.setenv("REDIS_DB", "2")
    import redis_config
    with patch.dict(redis_config.__dict__):
        config = importlib.reload(redis_config)
        assert (config.REDIS_HOST, config.REDIS_PORT, config.REDIS_PASSWORD, config.REDIS_DB) == (
            "redis.test.invalid", 6381, "test-only", 2,
        )


def test_windows_launcher_imports_all_routes_before_serving():
    source = (ROOT / "start.bat").read_text(encoding="utf-8")
    assert "python -m flask --app app run" in source
    assert "python app.py" not in source


def test_direct_runtime_dependencies_are_declared():
    source = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = {line.split("==")[0].split(">=")[0].split(";")[0].strip().lower()
             for line in source.splitlines() if line.strip() and not line.startswith("#")}
    assert {"pandas", "requests", "flask", "redis", "tushare", "cryptography"} <= names

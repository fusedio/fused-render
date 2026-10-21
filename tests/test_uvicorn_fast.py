"""The local uvicorn server uses httptools when it is installed."""
import importlib.util
import tomllib
from pathlib import Path

import pytest

from fused_render import logs

ROOT = Path(__file__).resolve().parent.parent


def test_kwargs_follow_httptools_availability(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda n, *a, **k: object() if n == "httptools" else real(n, *a, **k))
    assert logs.uvicorn_fast_kwargs() == {"loop": "asyncio", "http": "httptools"}
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda n, *a, **k: None if n == "httptools" else real(n, *a, **k))
    assert logs.uvicorn_fast_kwargs() == {"loop": "asyncio", "http": "auto"}


def test_both_launchers_pass_the_kwargs():
    for rel in ("fused_render/cli.py", "fused_render/app.py"):
        assert "**uvicorn_fast_kwargs()" in (ROOT / rel).read_text(encoding="utf-8"), rel


def test_httptools_is_in_cli_and_all_extras_only():
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = proj["optional-dependencies"]
    for name in ("cli", "all"):
        assert any(d.startswith("httptools") for d in extras[name]), name
    assert not any(d.startswith("httptools") for d in proj["dependencies"])


def test_config_selects_the_httptools_protocol():
    pytest.importorskip("httptools")
    import uvicorn

    cfg = uvicorn.Config(lambda *a: None, **logs.uvicorn_fast_kwargs())
    cfg.load()
    assert cfg.http_protocol_class.__name__ == "HttpToolsProtocol"

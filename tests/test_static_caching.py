"""Content-versioned static URLs: `?v=<mtime-size>` is immutable, nothing else is."""
import os

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.server.asset_version import asset_version

IMMUTABLE = "public, max-age=31536000, immutable"


@pytest.fixture()
def staged(tmp_path, monkeypatch):
    from fused_render.server import templates as _t

    fake = tmp_path / "staged-templates"
    (fake / "vendor").mkdir(parents=True)
    (fake / "shared").mkdir(parents=True)
    (fake / "vendor" / "marked.js").write_text("// vendor", encoding="utf-8")
    (fake / "shared" / "sciviz.js").write_text("// shared", encoding="utf-8")
    monkeypatch.setattr(_t, "TEMPLATES_DIR", str(fake))
    client = TestClient(create_app(start_dir=str(tmp_path)))
    return client, fake, tmp_path


PATHS = ["/static/runtime.js", "/template-assets/marked.js", "/template-shared/sciviz.js"]


def test_unversioned_runtime_and_assets_stay_no_cache(staged):
    client, _, _ = staged
    for p in PATHS:
        assert client.get(p).headers["cache-control"] == "no-cache", p


def test_matching_v_is_immutable(staged):
    client, _, _ = staged
    for p in PATHS:
        v = asset_version(p)
        assert v, p
        res = client.get(p, params={"v": v})
        assert res.status_code == 200
        assert res.headers["cache-control"] == IMMUTABLE, p


def test_wrong_v_is_no_cache(staged):
    client, _, _ = staged
    for p in PATHS:
        assert client.get(p, params={"v": "deadbeef"}).headers["cache-control"] == "no-cache"


def test_edit_invalidates_version(staged):
    client, fake, _ = staged
    p = "/template-assets/marked.js"
    v1 = asset_version(p)
    f = fake / "vendor" / "marked.js"
    f.write_text("// vendor, edited and longer", encoding="utf-8")
    st = os.stat(f)
    os.utime(f, (st.st_atime + 2, st.st_mtime + 2))
    assert asset_version(p) != v1
    assert client.get(p, params={"v": v1}).headers["cache-control"] == "no-cache"


def test_asset_version_rejects_traversal(staged):
    (staged[1] / "vendor" / "sub").mkdir()
    for p in ("/template-assets/../shared/sciviz.js", "/template-assets/",
              "/template-assets/sub", "/template-assets/./marked.js",
              "/template-assets/a\\b", "/elsewhere/marked.js", "/static/other.js"):
        assert asset_version(p) is None, p


def test_render_injects_versioned_runtime_and_assets(staged):
    client, _, tmp_path = staged
    page = tmp_path / "p.html"
    page.write_text(
        '<html><head></head><body><script src="/template-shared/sciviz.js"></script>'
        '<script src="/template-assets/missing.js"></script>'
        '<a href="/template-shared/sciviz.js?x=1"></a></body></html>',
        encoding="utf-8")
    body = client.get("/render", params={"path": str(page)}).text
    assert f'/static/runtime.js?v={asset_version("/static/runtime.js")}' in body
    token = asset_version("/template-shared/sciviz.js")
    assert f'src="/template-shared/sciviz.js?v={token}"' in body
    assert 'src="/template-assets/missing.js"' in body
    assert 'href="/template-shared/sciviz.js?x=1"' in body

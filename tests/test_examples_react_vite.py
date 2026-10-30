"""examples/react-vite — the React + Vite app docs/FRAMEWORKS.md walks through.

Nothing here runs npm: the example's build output is gitignored and CI's
Python job has no Node. What IS pinned is everything the guide's recipe rests
on, so an edit to the example cannot quietly break the guide:

- the page is an app (the `fused-app` marker, in the first 4 KiB),
- the page loads the bundle through `fused.rawUrl`, never a bare `src`,
- the Vite config emits one fixed-name file with no chunks,
- `main.py` is a runnable target the page calls, and the `/render` route
  serves the page with the runtime injected ahead of the loader.
"""
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from fused_render.app_listing import text_has_fused_meta
from fused_render.server import create_app

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "react-vite"


def test_the_example_page_is_an_app():
    html = (EXAMPLE / "index.html").read_text(encoding="utf-8")
    assert text_has_fused_meta(html)
    assert '<meta name="fused-app" />' in html[:4096]


def test_the_page_loads_the_bundle_through_raw_url():
    html = (EXAMPLE / "index.html").read_text(encoding="utf-8")
    assert 'fused.rawUrl("dist/app.js")' in html
    assert 'fused.rawUrl("dist/app.css")' in html
    # A bare relative src/href 404s on /render (SPEC RH-1): the whole reason
    # the loader exists.
    assert not re.search(r'<(script|link)[^>]+(src|href)="\.?/?dist/', html)


def test_the_build_emits_one_fixed_name_bundle():
    config = (EXAMPLE / "vite.config.ts").read_text(encoding="utf-8")
    assert 'entryFileNames: "app.js"' in config
    assert 'assetFileNames: "app.[ext]"' in config
    assert "inlineDynamicImports: true" in config
    assert 'input: "src/main.tsx"' in config


def test_the_example_types_fused_from_the_shipped_declarations():
    tsconfig = json.loads((EXAMPLE / "tsconfig.json").read_text(encoding="utf-8"))
    assert "../../fused_render/static/fused.d.ts" in tsconfig["include"]
    pkg = json.loads((EXAMPLE / "package.json").read_text(encoding="utf-8"))
    assert pkg["scripts"]["build"] == "vite build"
    assert "react" in pkg["dependencies"]


def test_main_py_runs_through_the_executor(tmp_path):
    client = TestClient(create_app(start_dir=str(tmp_path)))
    res = client.post(
        "/api/run",
        json={"py": "./main.py", "html": str(EXAMPLE / "index.html"), "params": {"limit": "10"}},
        headers={"X-Fused": "1"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"], body
    assert body["result"] == {"limit": 10, "primes": [2, 3, 5, 7]}


def test_render_injects_the_runtime_ahead_of_the_loader(tmp_path):
    client = TestClient(create_app(start_dir=str(tmp_path)))
    res = client.get("/render", params={"path": str(EXAMPLE / "index.html")})
    assert res.status_code == 200
    runtime_at = res.text.find("/static/runtime.js")
    loader_at = res.text.find("fused.rawUrl(")
    assert 0 <= runtime_at < loader_at

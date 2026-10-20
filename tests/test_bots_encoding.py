"""bot.json and friends are read/written as UTF-8 regardless of the process
locale. The macOS app's embedded Python launches from the Dock with no
LANG/LC_*, so an encoding-less open() decodes as ASCII there and a bot whose
bot.json holds a raw non-ASCII byte (hand edit, sync, restore) vanishes from
the registry. Dev/test runs default to UTF-8, which is why nothing caught it."""
import ast
import os
import subprocess
import sys

import pytest

import fused_render.bots as _bots_pkg

_CHILD = r"""
import json, locale, os, sys
enc = locale.getpreferredencoding(False)
if enc.lower().replace("-", "").replace("_", "") in ("utf8", "utf8mode"):
    print("SKIP locale=" + enc); sys.exit(3)
from fused_render.bots import store
meta = {"id": "b1", "name": "Bot \U0001F916", "rules": [{"label": "Loading…"}]}
os.makedirs(store.bot_dir("b1"))
with open(store.meta_path("b1"), "wb") as f:   # raw UTF-8, not \u-escaped
    f.write(json.dumps(meta, ensure_ascii=False).encode("utf-8"))
assert store.read_meta("b1") == meta
store.write_meta("b1", {**meta, "name": "Renamed …"})
assert store.read_meta("b1")["name"] == "Renamed …"
print("OK locale=" + enc)
"""


def test_read_meta_with_raw_utf8_under_non_utf8_locale(tmp_path):
    # Non-obvious: monkeypatching can't fake the locale for a bare open(), so
    # run a child. LC_ALL=C + PYTHONUTF8=0 + PYTHONCOERCECLOCALE=0 yields an
    # ASCII preferred encoding on macOS and Linux (no UTF-8 mode, no coercion).
    env = {k: v for k, v in os.environ.items() if not k.startswith("LC_") and k != "LANG"}
    env.update(LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0",
               FUSED_RENDER_HOME=str(tmp_path / "home"), HOME=str(tmp_path / "home"))
    os.makedirs(env["HOME"])
    p = subprocess.run([sys.executable, "-c", _CHILD], env=env, capture_output=True,
                       text=True, timeout=60, cwd=os.path.dirname(os.path.dirname(_bots_pkg.__file__)))
    if p.returncode == 3:
        pytest.skip("platform cannot produce a non-UTF-8 locale encoding: " + p.stdout.strip())
    assert p.returncode == 0, p.stdout + p.stderr


def _text_opens_without_encoding():
    root = os.path.dirname(_bots_pkg.__file__)
    bad = []
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dp, fn)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            for n in ast.walk(tree):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open"):
                    continue
                mode = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == "mode"), None)
                if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
                    continue
                if len(n.args) > 3 or any(k.arg == "encoding" for k in n.keywords):
                    continue
                bad.append(f"{os.path.relpath(path, root)}:{n.lineno}")
    return bad


def test_every_text_mode_open_names_its_encoding():
    assert _text_opens_without_encoding() == []

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

# ASCII-only source (escapes, not the characters): `-c` code reaches the child
# through argv, which a C-locale Linux interpreter decodes as ASCII before it
# ever runs — a raw "…" there dies as "Unable to decode the command from the
# command line" (macOS always decodes argv as UTF-8, so it hid there).
_CHILD = r"""
import json, locale, os, sys
enc = locale.getpreferredencoding(False)
if enc.lower().replace("-", "").replace("_", "") in ("utf8", "utf8mode"):
    print("SKIP locale=" + enc); sys.exit(3)
from fused_render.bots import store
meta = {"id": "b1", "name": "Bot \U0001F916", "rules": [{"label": "Loading\u2026"}]}
os.makedirs(store.bot_dir("b1"))
with open(store.meta_path("b1"), "wb") as f:   # raw UTF-8, not \u-escaped
    f.write(json.dumps(meta, ensure_ascii=False).encode("utf-8"))
assert store.read_meta("b1") == meta
store.write_meta("b1", {**meta, "name": "Renamed \u2026"})
assert store.read_meta("b1")["name"] == "Renamed \u2026"
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
                if not isinstance(n, ast.Call):
                    continue
                f = n.func
                has_enc_kw = any(k.arg == "encoding" for k in n.keywords)
                if isinstance(f, ast.Attribute) and f.attr in ("read_text", "write_text"):
                    # Path.read_text(encoding) / Path.write_text(data, encoding)
                    enc_pos = 1 if f.attr == "read_text" else 2
                    if len(n.args) >= enc_pos or has_enc_kw:
                        continue
                elif (isinstance(f, ast.Name) and f.id == "open") or (
                    isinstance(f, ast.Attribute) and f.attr == "open"
                    and isinstance(f.value, ast.Name) and f.value.id in ("io", "codecs")
                ):
                    mode = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == "mode"), None)
                    if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
                        continue
                    # open(file, mode, buffering, encoding): encoding is the 4th positional
                    if len(n.args) >= 4 or has_enc_kw:
                        continue
                else:
                    continue
                bad.append(f"{os.path.relpath(path, root)}:{n.lineno}")
    return bad


def test_every_text_mode_open_names_its_encoding():
    assert _text_opens_without_encoding() == []

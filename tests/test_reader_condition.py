"""Tests for the `reader` conditional preview template gate (SPEC CT-12).

Unlike the `canvas` gate (a per-file content sniff), the `reader` gate is a
GLOBAL feature switch driven by the `reader_enabled` preference — an
accessibility opt-in, default off. So the two surfaces are:

  * stat marks the `reader` entry `conditional` (never runs the gate), the same
    deferred-evaluation contract every conditional template uses.
  * `/api/fs/conditions` resolves the verdict: False by default (the mode is
    hidden), True once `~/.fused-render/prefs.json` has `reader_enabled: true`.

FUSED_RENDER_HOME is redirected to a tmp dir so no test reads the real prefs.
"""
import importlib.util
import json
import os

import pytest

from fused_render import server


TEMPLATES_DIR = server.TEMPLATES_DIR


def reader_verdict(path):
    # The deferred half of CT-12: the background /api/fs/conditions payload.
    payload = server._conditions_payload(path)
    return payload["conditions"].get("reader"), payload.get("error")


def _load_condition():
    path = os.path.join(TEMPLATES_DIR, "reader", "condition.py")
    spec = importlib.util.spec_from_file_location("reader_condition", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(d))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)  # baseline home
    return d


def _enable_reader(home):
    (home / "prefs.json").write_text(json.dumps({"reader_enabled": True}), encoding="utf-8")


# ------------------------------------------------------- condition gate (CT-12)

def test_reader_entry_is_marked_conditional(home, tmp_path):
    # reader is bound to .csv (among many); stat lists it, marked conditional,
    # WITHOUT running the gate (deferred CT-12).
    f = tmp_path / "a.csv"
    f.write_text("x\n")
    entries, error = server._templates_for(str(f), False)
    assert error is None
    reader = next(e for e in entries if e["mode"] == "reader")
    assert reader.get("conditional") is True
    assert reader["path"].endswith(os.path.join("reader", "template.html"))


def test_reader_denied_by_default(home, tmp_path):
    # Accessibility opt-in: off until turned on, so the verdict is False and it
    # is NOT an error (a disabled feature is a normal state, not a broken gate).
    f = tmp_path / "a.csv"
    f.write_text("x\n")
    allowed, err = reader_verdict(str(f))
    assert allowed is False
    assert err is None


def test_reader_allowed_when_pref_enabled(home, tmp_path):
    _enable_reader(home)
    f = tmp_path / "a.csv"
    f.write_text("x\n")
    allowed, err = reader_verdict(str(f))
    assert allowed is True
    assert err is None


def test_condition_main_ignores_path_and_never_raises(home):
    # Global switch: the gate reads the pref and ignores target_path entirely,
    # and must never raise on odd input (CT-12 fail-closed).
    cond = _load_condition()
    assert cond.main("/anything.csv") is False
    assert cond.main("") is False
    assert cond.main(None) is False
    _enable_reader(home)
    # Same odd inputs, now the pref is on: path still ignored, verdict True.
    assert cond.main("/anything.csv") is True
    assert cond.main(None) is True


def test_condition_main_false_for_non_bool_pref(home):
    # Only a literal True enables it — a truthy-but-not-True stored value reads
    # as off (mirrors prefs.reader_enabled's `is True` check).
    (home / "prefs.json").write_text(json.dumps({"reader_enabled": "yes"}), encoding="utf-8")
    assert _load_condition().main("/x.csv") is False


# --------------------------------------------------------------- template files

def test_reader_template_ships_condition():
    d = os.path.join(TEMPLATES_DIR, "reader")
    files = os.listdir(d)
    assert "condition.py" in files
    assert "template.html" in files

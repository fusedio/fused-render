"""Tests for the entry ordering of GET /api/fs/list (fused_render/server.py).

Entries are grouped dirs-first and ordered case-insensitively by name. The
comparator carries an exact-name tiebreak so names that fold together under a
case-insensitive comparison (case- or accent-only differences) still get a
stable, deterministic order instead of falling back to arbitrary os.listdir()
arrival order — otherwise the displayed order could change between refreshes.
"""

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app


def _case_insensitive_fs(tmp_path):
    (tmp_path / "CaseProbe").write_text("x", encoding="utf-8")
    collides = (tmp_path / "caseprobe").exists()
    (tmp_path / "CaseProbe").unlink()
    return collides


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _names(tmp_path):
    data = _client(tmp_path).get("/api/fs/list", params={"path": str(tmp_path)}).json()
    return [e["name"] for e in data["entries"]]


def test_dirs_group_before_files_then_alpha(tmp_path):
    (tmp_path / "beta").mkdir()
    (tmp_path / "Data").mkdir()
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "zeta.txt").write_text("z", encoding="utf-8")
    assert _names(tmp_path) == ["beta", "Data", "alpha.txt", "zeta.txt"]


def test_case_colliding_names_have_deterministic_order(tmp_path):
    # "README" and "readme" collide under the case-insensitive primary key
    # (name.lower()); without the exact-name tiebreak the backend would fall
    # back to arbitrary os.listdir() order for the pair. Only meaningful on a
    # case-sensitive filesystem, where both names can coexist.
    if _case_insensitive_fs(tmp_path):
        pytest.skip("case-insensitive filesystem cannot hold README + readme")
    (tmp_path / "readme").write_text("x", encoding="utf-8")
    (tmp_path / "README").write_text("x", encoding="utf-8")
    # Uppercase 'R' (0x52) sorts before lowercase 'r' (0x72) via the tiebreak.
    assert _names(tmp_path) == ["README", "readme"]


def test_order_is_stable_across_repeated_calls(tmp_path):
    for n in ["café.txt", "cafe.txt", "résumé", "resume", "alpha.txt"]:
        (tmp_path / n).write_text("x", encoding="utf-8")
    for n in ["Data", "beta"]:
        (tmp_path / n).mkdir()
    orders = [_names(tmp_path) for _ in range(5)]
    assert all(o == orders[0] for o in orders)

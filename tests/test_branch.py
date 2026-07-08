import pytest

from fused_render import _branch


@pytest.fixture(autouse=True)
def _reset_cache():
    _branch._CACHED_REF = None
    yield
    _branch._CACHED_REF = None


def test_sanitize_baseline():
    assert _branch.sanitize("main") == ""
    assert _branch.sanitize("master") == ""
    assert _branch.sanitize("") == ""
    assert _branch.sanitize("HEAD") == ""
    assert _branch.sanitize("head") == ""


def test_sanitize_normalizes_and_truncates():
    result = _branch.sanitize("Feature/Foo Bar!!")
    assert result == result.lower()
    assert "/" not in result
    assert " " not in result
    assert "!" not in result
    assert not result.startswith("-")
    assert not result.endswith("-")
    assert len(result) <= 12
    assert "--" not in result


def test_sanitize_truncates_long_input():
    long_ref = "a" * 30
    result = _branch.sanitize(long_ref)
    assert len(result) <= 12


def test_sanitize_collapses_multiple_specials():
    result = _branch.sanitize("foo---bar___baz")
    assert "--" not in result


def test_resolve_env_wins(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "Feat/X")
    assert _branch._resolve_ref() == "feat-x"


def test_resolve_env_empty_string_is_baseline(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "")
    assert _branch._resolve_ref() == ""


def test_resolve_baked_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    monkeypatch.setattr(_branch, "_baked_ref", lambda: "baked-branch")
    assert _branch._resolve_ref() == "baked-branch"


def test_resolve_all_absent(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    monkeypatch.setattr(_branch, "_baked_ref", lambda: "")
    assert _branch._resolve_ref() == ""


def test_resolve_no_git_detection(monkeypatch):
    """Being on a feature branch does nothing on its own — with no env var
    and no baked ref, resolution is baseline. Git is never consulted.
    """
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    monkeypatch.setattr(_branch, "_baked_ref", lambda: "")
    assert _branch._resolve_ref() == ""
    assert not hasattr(_branch, "_git_ref")


def test_resolve_baked_main_is_baseline(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    monkeypatch.setattr(_branch, "_baked_ref", lambda: "main")
    assert _branch._resolve_ref() == ""
    _branch._CACHED_REF = None
    assert _branch.branch_port() == 8765
    assert _branch.branch_suffix() == ""


def test_branch_port_baseline():
    assert _branch.branch_port("") == 8765


def test_branch_port_deterministic_and_stable():
    p1 = _branch.branch_port("foo")
    p2 = _branch.branch_port("foo")
    assert p1 == p2
    assert 8776 <= p1 <= 9775
    assert p1 not in range(8765, 8776)


def test_branch_suffix():
    assert _branch.branch_suffix("") == ""
    assert _branch.branch_suffix("foo") == "-foo"


def test_cached_no_arg_path_non_empty(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "Feat/Y")
    r1 = _branch.branch_ref()
    r2 = _branch.branch_ref()
    assert r1 == r2 == "feat-y"
    assert _branch.branch_port() == _branch.branch_port("feat-y")
    assert _branch.branch_suffix() == "-feat-y"

    # cache is stable even if the underlying env changes afterward
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "other")
    assert _branch.branch_ref() == "feat-y"


def test_cached_no_arg_path_empty_baseline(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "")
    r1 = _branch.branch_ref()
    r2 = _branch.branch_ref()
    assert r1 == r2 == ""
    assert _branch.branch_port() == 8765
    assert _branch.branch_suffix() == ""

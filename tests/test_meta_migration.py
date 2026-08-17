"""The one-time `<meta name="fused-app">` stamping migration.

Exercised directly (`stamp_entry` / `migrate_workspace` / `run_once`) — the
startup wiring is one background-thread call, and everything worth testing is
a filesystem condition: where the tag lands, what is skipped, and that a
second run finds nothing to do.
"""
import os
import subprocess

import pytest

from fused_render import app_listing, meta_migration


STARTER = ('<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8" />\n'
           '<title>t</title>\n</head>\n<body>hi</body>\n</html>\n')


def _mkapp(root, tag, name, body=STARTER, entry="index.html"):
    d = root / tag / name
    d.mkdir(parents=True)
    p = d / entry
    p.write_text(body, encoding="utf-8")
    return p


def test_the_tag_lands_right_after_the_charset_meta(tmp_path):
    p = _mkapp(tmp_path, "local", "app")
    assert meta_migration.stamp_entry(str(p))
    text = p.read_text(encoding="utf-8")
    assert app_listing.has_fused_meta(str(p))
    # After the charset, before the title — the top of head, where detection's
    # bounded read is guaranteed to see it.
    assert text.index('charset') < text.index('fused-app') < text.index('<title')


def test_a_page_without_a_charset_gets_it_after_head(tmp_path):
    p = _mkapp(tmp_path, "local", "app", body="<html><head><title>t</title></head></html>")
    assert meta_migration.stamp_entry(str(p))
    text = p.read_text(encoding="utf-8")
    assert text.index("<head>") < text.index("fused-app") < text.index("<title")


def test_a_page_with_no_anchor_is_left_exactly_as_it_was(tmp_path):
    body = "<div>not really a page</div>"
    p = _mkapp(tmp_path, "local", "app", body=body)
    assert not meta_migration.stamp_entry(str(p))
    assert p.read_text(encoding="utf-8") == body


def test_a_non_utf8_page_is_left_exactly_as_it_was(tmp_path):
    p = _mkapp(tmp_path, "local", "app")
    raw = b"<html><head>\xff\xfe</head></html>"
    p.write_bytes(raw)
    assert not meta_migration.stamp_entry(str(p))
    assert p.read_bytes() == raw


def test_an_already_tagged_page_is_not_stamped_twice(tmp_path):
    p = _mkapp(tmp_path, "local", "app")
    assert meta_migration.stamp_entry(str(p))
    once = p.read_text(encoding="utf-8")
    assert not meta_migration.stamp_entry(str(p))
    assert p.read_text(encoding="utf-8") == once


def test_migrate_workspace_stamps_every_listed_app(tmp_path):
    _mkapp(tmp_path, "local", "a")
    _mkapp(tmp_path, "local", "b", entry="dash.html")
    assert meta_migration.migrate_workspace(str(tmp_path)) == 2
    assert meta_migration.migrate_workspace(str(tmp_path)) == 0


def test_run_once_runs_once_per_machine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    ws = tmp_path / "ws"
    p = _mkapp(ws, "local", "a")
    meta_migration.run_once(str(ws))
    assert app_listing.has_fused_meta(str(p))
    assert os.path.exists(home / "fused_meta_migration.json")
    # A later app is NOT stamped — the stamp file says the migration ran, and
    # new apps carry the tag from the starter.
    q = _mkapp(ws, "local", "later")
    meta_migration.run_once(str(ws))
    assert not app_listing.has_fused_meta(str(q))


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def test_a_repo_with_a_remote_is_never_touched_or_descended(tmp_path):
    """An externally synced tree (showcase clone, deeplink clone, the user's
    own checkout) must not be stamped — a modified tracked file breaks its
    `--ff-only` pull forever — and nothing INSIDE it is stamped either: an app
    deeper in a synced repo is that repo's file too."""
    p = _mkapp(tmp_path, "local", "cloned")
    sub = p.parent / "sub"
    sub.mkdir()
    inner = sub / "index.html"
    inner.write_text(STARTER, encoding="utf-8")
    repo = str(p.parent)
    if _git("init", "-q", cwd=repo).returncode != 0:
        pytest.skip("git unavailable")
    _git("remote", "add", "origin", "https://example.com/x.git", cwd=repo)

    assert meta_migration.migrate_workspace(str(tmp_path)) == 0
    assert p.read_text(encoding="utf-8") == STARTER
    assert inner.read_text(encoding="utf-8") == STARTER


def test_a_clean_app_repo_gets_the_stamp_as_a_commit_and_a_dirty_one_stays_dirty(
        tmp_path):
    p_clean = _mkapp(tmp_path, "local", "clean")
    p_dirty = _mkapp(tmp_path, "local", "dirty")
    for p in (p_clean, p_dirty):
        d = str(p.parent)
        if _git("init", "-q", cwd=d).returncode != 0:
            pytest.skip("git unavailable")
        _git("-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-m", "seed", cwd=d)
        _git("add", "-A", cwd=d)
        _git("-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "content", cwd=d)
    (p_dirty.parent / "wip.txt").write_text("in progress", encoding="utf-8")

    assert meta_migration.migrate_workspace(str(tmp_path)) == 2
    # Clean repo: stamped AND committed — nothing left dirty.
    assert not _git("status", "--porcelain", cwd=str(p_clean.parent)).stdout.strip()
    # Dirty repo: stamped but NOT committed — the user's wip must never be
    # swept into a migration commit.
    status = _git("status", "--porcelain", cwd=str(p_dirty.parent)).stdout
    assert "wip.txt" in status and "index.html" in status

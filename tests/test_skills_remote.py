"""skills.sh search + GitHub-backed remote_preview/remote_add + marketplace preview.

No real network: `urllib.request.urlopen` or `skills._http_get` is faked.
"""
import io
import json

import pytest

from fused_render.claude_config import lib, skills


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "claude-home"
    root.mkdir()
    monkeypatch.setattr(lib, "CLAUDE_DIR", str(root))
    monkeypatch.setattr(lib, "SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setattr(lib, "INSTALLED_PLUGINS_PATH", str(root / "plugins" / "installed_plugins.json"))
    monkeypatch.setattr(lib, "KNOWN_MARKETPLACES_PATH", str(root / "plugins" / "known_marketplaces.json"))
    monkeypatch.setattr(lib, "MARKETPLACES_DIR", str(root / "plugins" / "marketplaces"))
    monkeypatch.setattr(lib, "_LOCK_PATH", str(root / ".config-ui.lock"))
    monkeypatch.setattr(skills, "SKILLS_DIR", str(root / "skills"))
    monkeypatch.setattr(skills, "_enabled_clis", lambda: ["claude"])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(skills, "_trees", {})
    return root


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _fake_urlopen(payload, calls):
    def urlopen(req, timeout=None):
        calls.append((req.full_url, req.get_header("User-agent"), timeout))
        if isinstance(payload, Exception):
            raise payload
        return _Resp(json.dumps(payload).encode())
    return urlopen


def _row(source="vercel-labs/agent-skills", skill_id="react", installs=5):
    return {"id": f"{source}/{skill_id}", "source": source, "skillId": skill_id,
            "name": skill_id, "installs": installs}


# -- search --------------------------------------------------------------------


def test_search_short_query_makes_no_request(home, monkeypatch):
    calls = []
    monkeypatch.setattr(skills.urllib.request, "urlopen", _fake_urlopen({}, calls))
    assert skills.main(action="search", query="a") == {"ok": True, "skills": []}
    assert skills.main(action="search", query="  ") == {"ok": True, "skills": []}
    assert calls == []


def test_search_proxies_with_ua_timeout_and_drops_invalid_rows(home, monkeypatch):
    (home / "skills" / "react").mkdir(parents=True)
    (home / "skills" / "react" / "SKILL.md").write_text("---\nname: react\n---\n")
    calls = []
    payload = {"skills": [
        _row(skill_id="React", installs=10),
        _row(skill_id="vue"),
        _row(source="../evil", skill_id="x1"),
        _row(source="a/b/c", skill_id="x2"),
        _row(skill_id="bad slug"),
        _row(skill_id="../up"),
        "junk",
    ]}
    monkeypatch.setattr(skills.urllib.request, "urlopen", _fake_urlopen(payload, calls))
    res = skills.main(action="search", query="react & co")
    assert res["ok"] is True
    assert [(s["skillId"], s["added"], s["installs"]) for s in res["skills"]] == [
        ("react", True, 10), ("vue", False, 5)]
    url, ua, timeout = calls[0]
    assert url.startswith("https://skills.sh/api/search?q=react+%26+co&limit=30")
    assert ua and timeout == 10


def test_search_network_error_is_a_result(home, monkeypatch):
    import urllib.error
    monkeypatch.setattr(skills.urllib.request, "urlopen",
                        _fake_urlopen(urllib.error.URLError("offline"), []))
    res = skills.main(action="search", query="react")
    assert res["ok"] is False and "offline" in res["error"]


def test_search_bad_json_is_a_result(home, monkeypatch):
    monkeypatch.setattr(skills.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(b"<html>"))
    assert skills.main(action="search", query="react")["ok"] is False


# -- locating + remote_preview / remote_add -------------------------------------


def _repo(monkeypatch, tree, files, calls=None):
    """Fake GitHub: `tree` is [(path, size)], `files` maps path -> text."""
    def http_get(url):
        if calls is not None:
            calls.append(url)
        if "/git/trees/" in url:
            return json.dumps({"tree": [
                {"path": p, "type": "blob", "size": n} for p, n in tree]}).encode()
        path = url.split("/HEAD/", 1)[1]
        return files[path].encode()
    monkeypatch.setattr(skills, "_http_get", http_get)


TREE = [("README.md", 5), ("skills/foo/SKILL.md", 40), ("skills/foo/refs/a.md", 3),
        ("skills/foo/.git/x", 1), ("skills/foo/node_modules/y", 1), ("skills/foobar/SKILL.md", 9)]
FILES = {"skills/foo/SKILL.md": "---\nname: foo\n---\nbody", "skills/foo/refs/a.md": "ref",
         "skills/foobar/SKILL.md": "---\nname: foobar\n---\n"}


def test_locate_by_dir_name_skips_junk_dirs(home, monkeypatch):
    _repo(monkeypatch, TREE, FILES)
    res = skills.main(action="remote_preview", source="o/r", skill_id="foo")
    assert res["ok"] and res["text"].endswith("body")
    assert res["files"] == ["SKILL.md", "refs/a.md"]


def test_locate_falls_back_to_frontmatter_name(home, monkeypatch):
    tree = [("pkg/a/SKILL.md", 10), ("pkg/b/SKILL.md", 10)]
    files = {"pkg/a/SKILL.md": "---\nname: alpha\n---\n", "pkg/b/SKILL.md": "---\nname: beta\n---\nB"}
    _repo(monkeypatch, tree, files)
    res = skills.main(action="remote_preview", source="o/r", skill_id="beta")
    assert res["ok"] and res["text"].endswith("B") and res["files"] == ["SKILL.md"]


def test_locate_not_found(home, monkeypatch):
    _repo(monkeypatch, TREE, FILES)
    res = skills.main(action="remote_preview", source="o/r", skill_id="nope")
    assert res == {"ok": False, "error": "no skill nope in o/r"}


def test_tree_is_cached_per_source(home, monkeypatch):
    calls = []
    _repo(monkeypatch, TREE, FILES, calls)
    skills.main(action="remote_preview", source="o/r", skill_id="foo")
    skills.main(action="remote_preview", source="o/r", skill_id="foo")
    assert sum("/git/trees/" in c for c in calls) == 1


def test_bad_source_or_id_is_rejected_without_a_request(home, monkeypatch):
    calls = []
    _repo(monkeypatch, TREE, FILES, calls)
    for src, sid in (("../x", "foo"), ("o/r", "../foo"), ("o", "foo"), ("o/r", "")):
        assert skills.main(action="remote_add", source=src, skill_id=sid)["ok"] is False
    assert calls == []


def test_size_cap_refuses(home, monkeypatch):
    monkeypatch.setattr(skills, "_COPY_MAX_BYTES", 10)
    _repo(monkeypatch, TREE, FILES)
    res = skills.main(action="remote_add", source="o/r", skill_id="foo")
    assert res["ok"] is False and "too large" in res["error"]
    assert not (home / "skills" / "foo").exists()


def test_file_count_cap_refuses(home, monkeypatch):
    monkeypatch.setattr(skills, "_COPY_MAX_FILES", 1)
    _repo(monkeypatch, TREE, FILES)
    assert skills.main(action="remote_add", source="o/r", skill_id="foo")["ok"] is False


def test_dotdot_paths_are_never_fetched_or_written(home, monkeypatch, tmp_path):
    tree = [("s/foo/SKILL.md", 5), ("s/foo/../evil.md", 1), ("s/foo/a/../../b.md", 1)]
    got = []
    _repo(monkeypatch, tree, {"s/foo/SKILL.md": "---\nname: foo\n---\n"}, got)
    res = skills.main(action="remote_add", source="o/r", skill_id="foo")
    assert res["ok"] is True
    assert not any("evil" in u or "b.md" in u for u in got)
    assert sorted(p.name for p in (home / "skills" / "foo").rglob("*")) == ["SKILL.md"]


def test_remote_add_places_files_via_place(home, monkeypatch):
    _repo(monkeypatch, TREE, FILES)
    res = skills.main(action="remote_add", source="o/r", skill_id="foo")
    assert res["ok"] and res["slug"] == "foo"
    dest = home / "skills" / "foo"
    assert (dest / "SKILL.md").read_text().endswith("body")
    assert (dest / "refs" / "a.md").read_text() == "ref"
    assert not (dest / ".git").exists() and not (dest / "node_modules").exists()
    again = skills.main(action="remote_add", source="o/r", skill_id="foo")
    assert again["ok"] is False and "already" in again["error"]


def test_network_failure_in_remote_add_is_a_result(home, monkeypatch):
    def boom(url):
        raise OSError("down")
    monkeypatch.setattr(skills, "_http_get", boom)
    res = skills.main(action="remote_add", source="o/r", skill_id="foo")
    assert res["ok"] is False and "down" in res["error"]


# -- marketplace preview -------------------------------------------------------


def _mkt(home):
    mkt = home / "plugins" / "marketplaces" / "acme"
    (mkt / ".claude-plugin").mkdir(parents=True)
    (mkt / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
        {"name": "acme", "plugins": [{"name": "kit", "source": "./kit"}]}))
    sk = mkt / "kit" / "skills" / "brew"
    (sk / "refs").mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: brew\n---\nhello")
    (sk / "refs" / "a.md").write_text("r")
    (home / "settings.json").write_text(json.dumps(
        {"extraKnownMarketplaces": {"acme": {"source": {"source": "github", "repo": "a/b"}}}}))
    return mkt


def test_marketplace_preview(home):
    _mkt(home)
    res = skills.main(action="preview", marketplace="acme", plugin="kit", slug="brew")
    assert res["ok"] and res["text"].endswith("hello") and res["files"] == ["SKILL.md", "refs/a.md"]


def test_marketplace_preview_unknown(home):
    _mkt(home)
    res = skills.main(action="preview", marketplace="acme", plugin="kit", slug="../x")
    assert res["ok"] is False


# -- review fixes ----------------------------------------------------------------


def test_root_skill_takes_skill_md_and_its_resource_dirs_only(home, monkeypatch):
    tree = [("SKILL.md", 10), ("README.md", 5), ("src/app.py", 5), ("package.json", 5),
            ("references/a.md", 3), ("scripts/run.sh", 3), ("assets/x.png", 3), ("templates/t.md", 3),
            ("docs/guide.md", 3)]
    files = {"SKILL.md": "---\nname: rootskill\n---\nbody"}
    _repo(monkeypatch, tree, files)
    res = skills.main(action="remote_preview", source="o/r", skill_id="rootskill")
    assert res["ok"] and res["files"] == [
        "SKILL.md", "assets/x.png", "references/a.md", "scripts/run.sh", "templates/t.md"]


def test_root_skill_in_a_big_repo_is_not_too_large(home, monkeypatch):
    monkeypatch.setattr(skills, "_COPY_MAX_FILES", 3)
    tree = [("SKILL.md", 10)] + [(f"src/f{i}.py", 1) for i in range(50)]
    _repo(monkeypatch, tree, {"SKILL.md": "---\nname: big\n---\n"})
    assert skills.main(action="remote_preview", source="o/r", skill_id="big")["files"] == ["SKILL.md"]


def test_truncated_tree_is_refused_and_not_cached(home, monkeypatch):
    calls = []

    def http_get(url):
        calls.append(url)
        return json.dumps({"truncated": True, "tree": []}).encode()
    monkeypatch.setattr(skills, "_http_get", http_get)
    for _ in range(2):
        res = skills.main(action="remote_preview", source="o/r", skill_id="foo")
        assert res == {"ok": False, "error": "o/r is too large to read on GitHub"}
    assert len(calls) == 2


def test_frontmatter_search_that_stops_early_says_so(home, monkeypatch):
    monkeypatch.setattr(skills, "_FRONTMATTER_PROBES", 2)
    tree = [(f"pkg/s{i}/SKILL.md", 10) for i in range(5)]
    files = {p: f"---\nname: n{i}\n---\n" for i, (p, _n) in enumerate(tree)}
    _repo(monkeypatch, tree, files)
    res = skills.main(action="remote_preview", source="o/r", skill_id="zzz")
    assert res["ok"] is False and "stopped after 2 of 5" in res["error"]


def test_tree_cache_drops_expired_sources_on_write(home, monkeypatch):
    skills._trees["old/one"] = (0, [])
    _repo(monkeypatch, TREE, FILES)
    skills.main(action="remote_preview", source="o/r", skill_id="foo")
    assert "old/one" not in skills._trees and "o/r" in skills._trees


def test_remote_add_downloads_before_taking_the_config_lock(home, monkeypatch):
    held = []
    real = lib.config_lock

    class Spy:
        def __enter__(self):
            held.append(True)
            self.cm = real()
            return self.cm.__enter__()

        def __exit__(self, *a):
            held.pop()
            return self.cm.__exit__(*a)
    monkeypatch.setattr(lib, "config_lock", lambda: Spy())
    seen = []

    def http_get(url):
        seen.append(bool(held))
        if "/git/trees/" in url:
            return json.dumps({"tree": [{"path": "s/foo/SKILL.md", "type": "blob", "size": 5}]}).encode()
        return b"---\nname: foo\n---\n"
    monkeypatch.setattr(skills, "_http_get", http_get)
    assert skills.main(action="remote_add", source="o/r", skill_id="foo")["ok"]
    assert seen and not any(seen)


def test_marketplace_preview_hides_symlinked_files(home):
    mkt = _mkt(home)
    sk = mkt / "kit" / "skills" / "brew"
    (sk / "link.md").symlink_to(sk / "refs" / "a.md")
    (sk / "linkdir").symlink_to(sk / "refs", target_is_directory=True)
    res = skills.main(action="preview", marketplace="acme", plugin="kit", slug="brew")
    assert res["files"] == ["SKILL.md", "refs/a.md"]

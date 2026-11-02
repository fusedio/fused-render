"""skills.sh search + GitHub-backed remote_preview/remote_add + marketplace preview.

No real network: `urllib.request.urlopen` is faked (GitHub is a tarball).
"""
import io
import json
import tarfile
import urllib.error
import urllib.parse

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


def _tarball(files, prefix="repo-abc123", extra=()):
    """A codeload-style .tar.gz: `files` maps path -> text/bytes under `prefix/`;
    `extra` is [(TarInfo, fileobj|None)] added as they are."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        top = tarfile.TarInfo(prefix)
        top.type = tarfile.DIRTYPE
        tf.addfile(top)
        for path, data in files.items():
            data = data.encode() if isinstance(data, str) else data
            ti = tarfile.TarInfo(f"{prefix}/{path}")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for ti, fobj in extra:
            tf.addfile(ti, fobj)
    return buf.getvalue()


def _repo(monkeypatch, files, calls=None, extra=(), tree=None):
    """Fake GitHub. codeload serves one tarball of `files`; raw serves a file
    of `files`; the tree API is rate limited (403, so the tarball fallback runs)
    unless `tree` is "ok" (a full listing) or "truncated"."""
    blob = _tarball(files, extra=extra)

    def urlopen(req, timeout=None):
        url = req.full_url
        if calls is not None:
            calls.append(url)
        if url.startswith("https://api.github.com/"):
            if tree is None:
                raise urllib.error.HTTPError(url, 403, "rate limited", {}, None)
            return _Resp(json.dumps({"truncated": tree == "truncated", "tree": [
                {"path": p, "type": "blob", "mode": "100644",
                 "size": len(d.encode() if isinstance(d, str) else d)} for p, d in files.items()
            ] + [{"path": "link", "type": "blob", "mode": "120000", "size": 3},
                 {"path": "sub", "type": "commit", "mode": "160000"},
                 {"path": "a/../b", "type": "blob", "mode": "100644", "size": 1}]}).encode())
        if url.startswith("https://raw.githubusercontent.com/o/r/HEAD/"):
            d = files[urllib.parse.unquote(url.split("/HEAD/", 1)[1])]
            return _Resp(d.encode() if isinstance(d, str) else d)
        return _Resp(blob)
    monkeypatch.setattr(skills.urllib.request, "urlopen", urlopen)


FILES = {"README.md": "readme", "skills/foo/SKILL.md": "---\nname: foo\n---\nbody",
         "skills/foo/refs/a.md": "ref", "skills/foo/.git/x": "x",
         "skills/foo/node_modules/y": "y", "skills/foobar/SKILL.md": "---\nname: foobar\n---\n"}


def test_locate_by_dir_name_skips_junk_dirs(home, monkeypatch):
    _repo(monkeypatch, FILES)
    root, files = skills._locate("o/r", "foo")
    assert root == "skills/foo" and list(files) == ["SKILL.md", "refs/a.md"]
    res = skills.main(action="remote_preview", source="o/r", skill_id="foo")
    assert res["ok"] and res["text"].endswith("body")
    assert res["fileCount"] == 2 and res["totalBytes"] == len(FILES["skills/foo/SKILL.md"]) + 3
    assert "files" not in res


def test_locate_falls_back_to_frontmatter_name(home, monkeypatch):
    _repo(monkeypatch, {"pkg/a/SKILL.md": "---\nname: alpha\n---\n",
                        "pkg/b/SKILL.md": "---\nname: beta\n---\nB"})
    res = skills.main(action="remote_preview", source="o/r", skill_id="beta")
    assert res["ok"] and res["text"].endswith("B") and res["fileCount"] == 1


def test_locate_not_found(home, monkeypatch):
    _repo(monkeypatch, FILES)
    res = skills.main(action="remote_preview", source="o/r", skill_id="nope")
    assert res == {"ok": False, "error": "no skill nope in o/r"}


def test_archive_is_cached_per_source_across_preview_and_add(home, monkeypatch):
    calls = []
    _repo(monkeypatch, FILES, calls)
    skills.main(action="remote_preview", source="o/r", skill_id="foo")
    skills.main(action="remote_preview", source="o/r", skill_id="foo")
    assert skills.main(action="remote_add", source="o/r", skill_id="foo")["ok"]
    assert [c for c in calls if "codeload" in c] == ["https://codeload.github.com/o/r/tar.gz/HEAD"]
    assert len(calls) == 2  # the 403 tree try, then one tarball


def test_bad_source_or_id_is_rejected_without_a_request(home, monkeypatch):
    calls = []
    _repo(monkeypatch, FILES, calls)
    for src, sid in (("../x", "foo"), ("o/r", "../foo"), ("o", "foo"), ("o/r", "")):
        assert skills.main(action="remote_add", source=src, skill_id=sid)["ok"] is False
    assert calls == []


def test_size_cap_refuses(home, monkeypatch):
    monkeypatch.setattr(skills, "_COPY_MAX_BYTES", 10)
    _repo(monkeypatch, FILES)
    res = skills.main(action="remote_add", source="o/r", skill_id="foo")
    assert res["ok"] is False and res["tooLarge"] is True and "too large" in res["error"]
    assert not (home / "skills" / "foo").exists()


def test_file_count_cap_refuses(home, monkeypatch):
    monkeypatch.setattr(skills, "_COPY_MAX_FILES", 1)
    _repo(monkeypatch, FILES)
    assert skills.main(action="remote_add", source="o/r", skill_id="foo")["ok"] is False


def test_archive_over_the_byte_cap_is_refused(home, monkeypatch):
    monkeypatch.setattr(skills, "_ARCHIVE_STREAM_MAX_BYTES", 100)
    _repo(monkeypatch, {"SKILL.md": "---\nname: foo\n---\n", "big.bin": bytes(range(256)) * 40})
    res = skills.main(action="remote_add", source="o/r", skill_id="foo")
    assert res["ok"] is False and "larger than" in res["error"]
    assert "o/r" not in skills._trees


def test_preview_of_an_oversized_repo_is_flagged_too_large(home, monkeypatch):
    monkeypatch.setattr(skills, "_ARCHIVE_STREAM_MAX_BYTES", 100)
    _repo(monkeypatch, {"SKILL.md": "---\nname: foo\n---\n", "big.bin": bytes(range(256)) * 40})
    res = skills.main(action="remote_preview", source="o/r", skill_id="foo")
    assert res["ok"] is False and res["tooLarge"] is True and "larger than" in res["error"]


def test_symlink_and_odd_members_are_skipped(home, monkeypatch):
    link = tarfile.TarInfo("repo-abc123/s/foo/link.md")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    hard = tarfile.TarInfo("repo-abc123/s/foo/hard.md")
    hard.type = tarfile.LNKTYPE
    hard.linkname = "repo-abc123/s/foo/SKILL.md"
    dots = tarfile.TarInfo("repo-abc123/s/foo/../evil.md")
    dots.size = 1
    absolute = tarfile.TarInfo("/abs.md")
    absolute.size = 1
    _repo(monkeypatch, {"s/foo/SKILL.md": "---\nname: foo\n---\n"},
          extra=[(link, None), (hard, None), (dots, io.BytesIO(b"x")), (absolute, io.BytesIO(b"x"))])
    assert skills.main(action="remote_add", source="o/r", skill_id="foo")["ok"] is True
    assert sorted(p.name for p in (home / "skills" / "foo").rglob("*")) == ["SKILL.md"]
    assert not (home / "skills" / "evil.md").exists()


def test_remote_add_places_files_via_place(home, monkeypatch):
    _repo(monkeypatch, FILES)
    res = skills.main(action="remote_add", source="o/r", skill_id="foo")
    assert res["ok"] and res["slug"] == "foo"
    dest = home / "skills" / "foo"
    assert (dest / "SKILL.md").read_text().endswith("body")
    assert (dest / "refs" / "a.md").read_text() == "ref"
    assert not (dest / ".git").exists() and not (dest / "node_modules").exists()
    again = skills.main(action="remote_add", source="o/r", skill_id="foo")
    assert again["ok"] is False and "already" in again["error"]


def test_network_failure_in_remote_add_is_a_result(home, monkeypatch):
    def boom(req, timeout=None):
        raise OSError("down")
    monkeypatch.setattr(skills.urllib.request, "urlopen", boom)
    res = skills.main(action="remote_add", source="o/r", skill_id="foo")
    assert res["ok"] is False and "down" in res["error"]


def test_corrupt_archive_is_a_result(home, monkeypatch):
    monkeypatch.setattr(skills.urllib.request, "urlopen", lambda req, timeout=None: _Resp(b"<html>"))
    res = skills.main(action="remote_preview", source="o/r", skill_id="foo")
    assert res["ok"] is False and "could not read o/r" in res["error"]


def test_preview_returns_root_readme_with_absolute_urls(home, monkeypatch):
    readme = ("# T\n![shot](docs/a.png) [guide](docs/g.md) [abs](https://x.io/a) "
              "[top](#top) [m](mailto:a@b.c)\n<img src=\"img/b.png\" width=3> "
              "<img src='https://x.io/c.png'>\n")
    _repo(monkeypatch, {"README.md": readme, "skills/foo/README.md": "inner",
                        "skills/foo/SKILL.md": "---\nname: foo\n---\nbody"})
    res = skills.main(action="remote_preview", source="o/r", skill_id="foo")
    assert res["readmeIsSkill"] is False
    r = res["readme"]
    assert "![shot](https://raw.githubusercontent.com/o/r/HEAD/docs/a.png)" in r
    assert "[guide](https://github.com/o/r/blob/HEAD/docs/g.md)" in r
    assert "[abs](https://x.io/a)" in r and "[top](#top)" in r and "[m](mailto:a@b.c)" in r
    assert '<img src="https://raw.githubusercontent.com/o/r/HEAD/img/b.png" width=3>' in r
    assert "<img src='https://x.io/c.png'>" in r


def test_readme_prefers_md_and_matches_case_insensitively(home, monkeypatch):
    _repo(monkeypatch, {"README": "plain", "readme.md": "md one", "s/foo/SKILL.md": "---\nname: foo\n---\n"})
    assert skills.main(action="remote_preview", source="o/r", skill_id="foo")["readme"] == "md one"


def test_no_readme_falls_back_to_skill_md(home, monkeypatch):
    _repo(monkeypatch, {"s/foo/SKILL.md": "---\nname: foo\n---\nbody"})
    res = skills.main(action="remote_preview", source="o/r", skill_id="foo")
    assert res["readmeIsSkill"] is True and res["readme"].endswith("body")


def test_archive_cache_keeps_at_most_eight_sources(home, monkeypatch):
    _repo(monkeypatch, FILES)
    for i in range(10):
        skills._repo_view(f"o/r{i}")
    assert len(skills._trees) == 8 and "o/r0" not in skills._trees and "o/r9" in skills._trees


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
    _repo(monkeypatch, {"SKILL.md": "---\nname: rootskill\n---\nbody", "README.md": "r",
                        "src/app.py": "x", "package.json": "x", "references/a.md": "x",
                        "scripts/run.sh": "x", "assets/x.png": "x", "templates/t.md": "x",
                        "docs/guide.md": "x"})
    _root, files = skills._locate("o/r", "rootskill")
    assert list(files) == ["SKILL.md", "assets/x.png", "references/a.md", "scripts/run.sh", "templates/t.md"]


def test_root_skill_in_a_big_repo_is_not_too_large(home, monkeypatch):
    monkeypatch.setattr(skills, "_COPY_MAX_FILES", 3)
    _repo(monkeypatch, {"SKILL.md": "---\nname: big\n---\n", **{f"src/f{i}.py": "x" for i in range(50)}})
    assert skills.main(action="remote_preview", source="o/r", skill_id="big")["fileCount"] == 1


def test_archive_cache_drops_expired_sources_on_write(home, monkeypatch):
    skills._trees["old/one"] = (0, skills._Repo("old/one", {}))
    _repo(monkeypatch, FILES)
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
    blob = _tarball({"s/foo/SKILL.md": "---\nname: foo\n---\n"})

    def urlopen(req, timeout=None):
        seen.append(bool(held))
        if "api.github.com" in req.full_url:
            raise OSError("no api")
        return _Resp(blob)
    monkeypatch.setattr(skills.urllib.request, "urlopen", urlopen)
    assert skills.main(action="remote_add", source="o/r", skill_id="foo")["ok"]
    assert seen and not any(seen)


def test_marketplace_preview_hides_symlinked_files(home):
    mkt = _mkt(home)
    sk = mkt / "kit" / "skills" / "brew"
    (sk / "link.md").symlink_to(sk / "refs" / "a.md")
    (sk / "linkdir").symlink_to(sk / "refs", target_is_directory=True)
    res = skills.main(action="preview", marketplace="acme", plugin="kit", slug="brew")
    assert res["files"] == ["SKILL.md", "refs/a.md"]


# -- tree API: only the skill's folder is downloaded --------------------------------

BIG = {**FILES, "big/huge.bin": b"x" * 5000, "src/app.py": "x"}


def _raws(calls):
    return [urllib.parse.unquote(c.split("/HEAD/", 1)[1]) for c in calls if "raw.githubusercontent" in c]


def test_tree_api_fetches_only_the_skill_folder(home, monkeypatch):
    calls = []
    _repo(monkeypatch, BIG, calls, tree="ok")
    root, files = skills._locate("o/r", "foo")
    assert root == "skills/foo" and list(files) == ["SKILL.md", "refs/a.md"]
    assert not any("codeload" in c for c in calls)
    assert sorted(_raws(calls)) == ["skills/foo/SKILL.md", "skills/foo/refs/a.md"]
    api = [c for c in calls if "api.github.com" in c]
    assert api == ["https://api.github.com/repos/o/r/git/trees/HEAD?recursive=1"]


def test_tree_api_request_headers(home, monkeypatch):
    seen = []

    def urlopen(req, timeout=None):
        seen.append((req.get_header("Accept"), req.get_header("User-agent"), timeout))
        return _Resp(json.dumps({"tree": []}).encode())
    monkeypatch.setattr(skills.urllib.request, "urlopen", urlopen)
    skills._tree_listing("o/r")
    assert seen == [("application/vnd.github+json", "fused-render", 10)]


def test_tree_api_skips_symlinks_submodules_and_odd_paths(home, monkeypatch):
    _repo(monkeypatch, BIG, tree="ok")
    assert sorted(skills._repo_view("o/r").paths()) == sorted(BIG)


def test_tree_api_preview_warms_the_add(home, monkeypatch):
    calls = []
    _repo(monkeypatch, BIG, calls, tree="ok")
    skills.main(action="remote_preview", source="o/r", skill_id="foo")
    n = len(calls)
    assert skills.main(action="remote_add", source="o/r", skill_id="foo")["ok"]
    assert len(calls) == n
    assert (home / "skills" / "foo" / "refs" / "a.md").read_text() == "ref"


def test_size_limit_is_enforced_from_listed_sizes_before_any_fetch(home, monkeypatch):
    monkeypatch.setattr(skills, "_COPY_MAX_BYTES", 10)
    calls = []
    _repo(monkeypatch, BIG, calls, tree="ok")
    res = skills.main(action="remote_preview", source="o/r", skill_id="foo")
    assert res["ok"] is False and res["tooLarge"] is True
    assert _raws(calls) == [] and not any("codeload" in c for c in calls)


def test_truncated_tree_falls_back_to_the_tarball(home, monkeypatch):
    calls = []
    _repo(monkeypatch, BIG, calls, tree="truncated")
    root, files = skills._locate("o/r", "foo")
    assert root == "skills/foo" and list(files) == ["SKILL.md", "refs/a.md"]
    assert any("codeload" in c for c in calls)


def test_rate_limited_tree_falls_back_to_the_tarball(home, monkeypatch):
    calls = []
    _repo(monkeypatch, BIG, calls)  # tree API answers 403
    assert skills.main(action="remote_preview", source="o/r", skill_id="foo")["ok"]
    assert any("codeload" in c for c in calls)


def test_tarball_fallback_fetches_large_skill_files_raw(home, monkeypatch):
    monkeypatch.setattr(skills, "_ARCHIVE_KEEP_FILE_BYTES", 4)
    calls = []
    _repo(monkeypatch, FILES, calls)
    repo = skills._repo_view("o/r")
    assert set(repo._bytes) == {"README.md", "skills/foo/SKILL.md", "skills/foobar/SKILL.md"}
    _root, files = skills._locate("o/r", "foo")
    assert files["refs/a.md"] == b"ref"
    assert _raws(calls) == ["skills/foo/refs/a.md"]


def test_tarball_fallback_keeps_bytes_within_budget(home, monkeypatch):
    monkeypatch.setattr(skills, "_ARCHIVE_KEEP_BUDGET", 30)
    _repo(monkeypatch, FILES)
    repo = skills._repo_view("o/r")
    assert sum(len(b) for b in repo._bytes.values()) <= 30
    assert set(repo.paths()) >= set(FILES)  # the listing stays complete


def test_tree_api_frontmatter_name_fallback_reads_candidates(home, monkeypatch):
    calls = []
    _repo(monkeypatch, {"pkg/a/SKILL.md": "---\nname: alpha\n---\n",
                        "pkg/b/SKILL.md": "---\nname: beta\n---\nB", "pkg/b/x.md": "x"}, calls, tree="ok")
    root, files = skills._locate("o/r", "beta")
    assert root == "pkg/b" and list(files) == ["SKILL.md", "x.md"]


def test_tree_api_root_skill_takes_only_resource_dirs(home, monkeypatch):
    calls = []
    _repo(monkeypatch, {"SKILL.md": "---\nname: rootskill\n---\nbody", "README.md": "r",
                        "src/app.py": "x", "references/a.md": "x", "docs/g.md": "x"}, calls, tree="ok")
    _root, files = skills._locate("o/r", "rootskill")
    assert list(files) == ["SKILL.md", "references/a.md"]
    assert sorted(_raws(calls)) == ["SKILL.md", "references/a.md"]


def test_tree_api_preview_reads_the_root_readme(home, monkeypatch):
    calls = []
    _repo(monkeypatch, {"README.md": "see [g](docs/g.md)", "s/foo/SKILL.md": "---\nname: foo\n---\n"},
          calls, tree="ok")
    res = skills.main(action="remote_preview", source="o/r", skill_id="foo")
    assert res["readmeIsSkill"] is False
    assert res["readme"] == "see [g](https://github.com/o/r/blob/HEAD/docs/g.md)"
    assert sorted(_raws(calls)) == ["README.md", "s/foo/SKILL.md"]

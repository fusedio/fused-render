"""Bot package export (`.fusedbot`, docs/bots.md §17, D1459): the public bot.json member never carries routine run
state, the skills/memory text budget is split so memory can be left out, the no-hard-link save never overwrites,
and a starter or marketplace copy with local edits is bundled (with a `fallback`) instead of travelling as a
reference that would drop them. _bots_conftest points FUSED_RENDER_HOME and FUSED_RENDER_DIR at tmp dirs; nothing
here touches the network."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import errno
import json
import os
import shutil

import pytest

from fused_render import appfile_container as container
from fused_render.bots import paths as bpaths
from fused_render.bots import store

BID = "b0b0b0b0"
CAPS = {"max_entries": 100, "max_entry_bytes": 1 << 24, "max_total_bytes": 1 << 26}
APP_HTML = '<!doctype html><html><head><meta name="fused-app"><title>{t}</title></head><body>hi</body></html>'


def _bot(meta: dict | None = None, *, skills: dict | None = None, memory: str | None = None) -> str:
    """A bot folder with bot.json written AS GIVEN (not through write_meta), plus skills/ and .fused/memory.md."""
    folder = bpaths.place(BID, "Tester")
    with open(store.meta_path(BID), "w", encoding="utf-8") as f:
        json.dump({"id": BID, "name": "Tester", **(meta or {})}, f)
    for name, text in (skills or {}).items():
        os.makedirs(os.path.join(folder, "skills"), exist_ok=True)
        with open(os.path.join(folder, "skills", f"{name}.md"), "w", encoding="utf-8") as f:
            f.write(text)
    if memory is not None:
        with open(os.path.join(bpaths.bot_private(BID), "memory.md"), "w", encoding="utf-8") as f:
            f.write(memory)
    return folder


def _member(path: str, rel: str) -> bytes:
    index = container.read_index(path, **CAPS, kind="bot")
    return container.read_member(path, index, rel, 1 << 20)


# ---------------------------------------------------------------- bot.json ---
def test_id_less_routine_ships_only_its_public_keys(tmp_path):
    from fused_render.bots import package

    run_state = {"enabled": True, "last": 1700000000, "last_result": "ok", "next": 1700000300, "fails": 2}
    _bot({"routines": [{"task": "water plants", "kind": "every", "minutes": 5, **run_state},
                       {"id": "r1", "task": "feed cat", "kind": "every", "minutes": 60}]})
    out = package.write(BID, memory=False, apps=[], file_name=None, dest_dir=str(tmp_path / "out"))
    rows = json.loads(_member(out, "bot.json"))["routines"]
    assert rows[0] == {"task": "water plants", "kind": "every", "minutes": 5}
    assert rows[1] == {"id": "r1", "task": "feed cat", "kind": "every", "minutes": 60}  # a row with an id: unchanged
    assert all(set(r) <= store.ROUTINE_PUBLIC_KEYS for r in rows)
    assert all(set(r) <= store.ROUTINE_PUBLIC_KEYS for r in package.plan(BID)["public"]["routines"])


# ------------------------------------------------------------- text budget ---
def test_large_memory_blocks_export_only_when_memory_is_ticked(tmp_path, monkeypatch):
    from fused_render.bots import package

    monkeypatch.setattr(package, "TEXT_MAX_BYTES", 100)
    _bot(skills={"a": "# A\nsmall"}, memory="m" * 200)
    p = package.plan(BID)
    assert p["text_problem"] is None
    assert "memory" in p["memory_text_problem"]
    assert os.path.isfile(package.write(BID, memory=False, apps=[], file_name="x", dest_dir=str(tmp_path)))
    with pytest.raises(ValueError, match="skills and memory are too large"):
        package.write(BID, memory=True, apps=[], file_name="y", dest_dir=str(tmp_path))


def test_large_skills_block_export_and_never_mention_memory(tmp_path, monkeypatch):
    from fused_render.bots import package

    monkeypatch.setattr(package, "TEXT_MAX_BYTES", 100)
    _bot(skills={"a": "s" * 200})
    p = package.plan(BID)
    assert p["text_problem"] and "memory" not in p["text_problem"]
    assert p["memory_text_problem"] is None  # no memory.md: ticking it changes nothing
    with pytest.raises(ValueError, match=r"^skills are too large"):
        package.write(BID, memory=False, apps=[], file_name=None, dest_dir=str(tmp_path))


# ------------------------------------------------------------------ _place ---
def _no_hard_links(monkeypatch):
    def link(*_a, **_k):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", link)


def test_place_without_hard_links_never_overwrites_a_name_taken_meanwhile(tmp_path, monkeypatch):
    from fused_render.bots import package

    tmp = tmp_path / "package.fusedbot"
    tmp.write_bytes(b"PKG")
    dest = tmp_path / "dest"
    dest.mkdir()
    taken = dest / "bot.fusedbot"
    taken.write_bytes(b"SOMEONE ELSE'S")  # a concurrent export took the name after unique() looked
    names = iter([str(taken), str(dest / "bot (1).fusedbot")])
    _no_hard_links(monkeypatch)
    final = package._place(str(tmp), str(dest), "bot.fusedbot", lambda *_a: next(names))
    assert final == str(dest / "bot (1).fusedbot")
    assert taken.read_bytes() == b"SOMEONE ELSE'S"
    assert (dest / "bot (1).fusedbot").read_bytes() == b"PKG"


def test_place_without_hard_links_leaves_no_partial_file_when_the_copy_fails(tmp_path, monkeypatch):
    from fused_render.bots import package

    tmp = tmp_path / "package.fusedbot"
    tmp.write_bytes(b"PKG")
    dest = tmp_path / "dest"
    dest.mkdir()
    _no_hard_links(monkeypatch)

    def broken(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copyfileobj", broken)
    with pytest.raises(ValueError, match="could not save the package"):
        package._place(str(tmp), str(dest), "bot.fusedbot", lambda d, n: os.path.join(d, n))
    assert os.listdir(dest) == []


def _copy_that_checks_final_is_absent(monkeypatch, final):
    """Wrap shutil.copyfileobj: record whether `final` existed while the bytes were being written."""
    real = shutil.copyfileobj
    seen = []

    def copy(*a, **k):
        seen.append(os.path.lexists(final))
        real(*a, **k)
        seen.append(os.path.lexists(final))

    monkeypatch.setattr(shutil, "copyfileobj", copy)
    return seen


@pytest.mark.parametrize("links_in_dest", [True, False])
def test_place_copy_publishes_the_final_name_only_once_complete(tmp_path, monkeypatch, links_in_dest):
    """The source cannot be hard-linked (first os.link fails): the copy goes to a hidden temp in dest and the final
    `.fusedbot` name appears only after the bytes are in — by a hard link from the temp, or (no links at all) by a
    no-replace rename. No temp is left behind."""
    from fused_render.bots import package

    tmp = tmp_path / "package.fusedbot"
    tmp.write_bytes(b"PKG" * 1000)
    dest = tmp_path / "dest"
    dest.mkdir()
    final = dest / "bot.fusedbot"
    real_link = os.link
    calls = []

    def link(src, dst, *a, **k):
        calls.append((src, dst))
        if len(calls) == 1 or not links_in_dest:
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", link)
    seen = _copy_that_checks_final_is_absent(monkeypatch, str(final))
    got = package._place(str(tmp), str(dest), "bot.fusedbot", lambda d, n: os.path.join(d, n))
    assert got == str(final)
    assert seen == [False, False]  # the final name did not exist at any point of the copy
    assert final.read_bytes() == b"PKG" * 1000
    assert os.listdir(dest) == ["bot.fusedbot"]
    assert calls[0] == (str(tmp), str(final))
    if links_in_dest:
        assert os.path.basename(calls[1][0]).startswith(".bot.fusedbot.part-") and calls[1][1] == str(final)


def _noreplace_or_skip(src, dst):
    from fused_render.bots import package

    try:
        package._rename_noreplace(src, dst)
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno in package._NO_NOREPLACE:
            pytest.skip(f"no no-replace rename here: {e}")
        raise


def test_publish_without_hard_links_refuses_an_existing_name_and_keeps_its_bytes(tmp_path, monkeypatch):
    from fused_render.bots import package

    probe_a, probe_b = tmp_path / "probe-a", tmp_path / "probe-b"
    probe_a.write_bytes(b"x")
    _noreplace_or_skip(str(probe_a), str(probe_b))  # the platform can do it (renamex_np / renameat2 / os.rename)
    part, dst = tmp_path / ".bot.part", tmp_path / "bot.fusedbot"
    part.write_bytes(b"PKG")
    dst.write_bytes(b"SOMEONE ELSE'S")
    _no_hard_links(monkeypatch)
    with pytest.raises(FileExistsError):
        package._publish_exclusive(str(part), str(dst))
    assert dst.read_bytes() == b"SOMEONE ELSE'S"
    assert part.read_bytes() == b"PKG"
    dst.unlink()
    package._publish_exclusive(str(part), str(dst))
    assert dst.read_bytes() == b"PKG" and not part.exists()


def test_publish_falls_back_to_claim_and_replace_when_the_filesystem_rejects_noreplace(tmp_path, monkeypatch):
    from fused_render.bots import package

    def unsupported(*_a, **_k):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    replaced = []
    real_replace = os.replace

    def replace(src, dst, *a, **k):
        replaced.append((src, dst, os.path.getsize(dst)))  # dst is the empty exclusive claim at this point
        return real_replace(src, dst, *a, **k)

    _no_hard_links(monkeypatch)
    monkeypatch.setattr(package, "_rename_noreplace", unsupported)
    monkeypatch.setattr(os, "replace", replace)
    part, dst = tmp_path / ".bot.part", tmp_path / "bot.fusedbot"
    part.write_bytes(b"PKG")
    package._publish_exclusive(str(part), str(dst))
    assert replaced == [(str(part), str(dst), 0)]
    assert dst.read_bytes() == b"PKG" and not part.exists()
    part.write_bytes(b"NEW")
    with pytest.raises(FileExistsError):  # the claim still refuses a taken name
        package._publish_exclusive(str(part), str(dst))
    assert dst.read_bytes() == b"PKG"


# ---------------------------------------------------------- edited starter ---
@pytest.fixture
def starter_app(tmp_path, monkeypatch):
    """A one-starter catalog (`demo`, version 1.0) installed under apps_root() the way the Apps panel installs it."""
    from fused_render.bots import starters

    src = tmp_path / "catalog" / "demo"
    src.mkdir(parents=True)
    (src / "index.html").write_text(APP_HTML.format(t="Demo"), encoding="utf-8")
    (src / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.0"\n', encoding="utf-8")
    (src / "tool.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(starters, "STARTERS_DIR", str(tmp_path / "catalog"))
    return starters.install("demo", bpaths.apps_root())["dir"]


def test_pristine_starter_travels_as_a_reference_whatever_it_generated(starter_app):
    from fused_render.bots import package

    assert package.resolve_source(starter_app) == {"kind": "starter", "key": "demo"}
    # Derived files are not edits: uv's lock, the app's private data, caches.
    with open(os.path.join(starter_app, "uv.lock"), "w") as f:
        f.write("lock")
    os.makedirs(os.path.join(starter_app, ".fused", "data"))
    with open(os.path.join(starter_app, ".fused", "data", "token.json"), "w") as f:
        f.write("{}")
    os.makedirs(os.path.join(starter_app, "__pycache__"))
    with open(os.path.join(starter_app, "__pycache__", "tool.cpython-312.pyc"), "wb") as f:
        f.write(b"x")
    assert package.resolve_source(starter_app) == {"kind": "starter", "key": "demo"}


@pytest.mark.parametrize("edit", ["change", "add", "remove"])
def test_edited_starter_is_bundled_with_a_starter_fallback(starter_app, edit):
    from fused_render.bots import package

    if edit == "change":
        with open(os.path.join(starter_app, "tool.py"), "w") as f:
            f.write("def main():\n    return 2\n")  # same size: the hash, not the size, catches it
    elif edit == "add":
        with open(os.path.join(starter_app, "mine.py"), "w") as f:
            f.write("x = 1\n")
    else:
        os.remove(os.path.join(starter_app, "tool.py"))
    want = {"kind": "bundled", "fallback": {"kind": "starter", "code": "dirty", "key": "demo"}}
    assert package.resolve_source(starter_app) == want
    row = package._app_row("demo", {"folder": "demo", "name": "Demo", "app_id": None, "source": {"kind": "starter"}})
    assert row["source"] == {"kind": "bundled"} and row["fallback"] == want["fallback"] and row["bytes"] > 0


def test_starter_from_another_version_keeps_its_reference(starter_app):
    from fused_render.bots import package

    rec_path = os.path.join(starter_app, ".fused", "starter.json")
    with open(rec_path, encoding="utf-8") as f:
        rec = json.load(f)
    with open(rec_path, "w", encoding="utf-8") as f:
        json.dump({**rec, "version": "0.9"}, f)
    with open(os.path.join(starter_app, "tool.py"), "a") as f:
        f.write("# differs, but so would any older release\n")
    assert package.resolve_source(starter_app) == {"kind": "starter", "key": "demo"}


# ------------------------------------------------------ edited marketplace ---
@pytest.mark.parametrize("state,want_dirty", [
    ({"ok": True, "code": "contributor", "changes": 2}, True),
    ({"ok": True, "code": "contributor", "changes": 0}, False),
    ({"ok": False, "code": "dirty", "repo": "a/b"}, False),   # not the copy's own count: keep the reference
    (RuntimeError("git is gone"), False),                       # any failure keeps the reference
])
def test_marketplace_copy_with_own_edits_is_bundled(monkeypatch, state, want_dirty):
    from fused_render import share_app
    from fused_render.bots import package
    from fused_render.contrib import store as cstore

    app = os.path.join(bpaths.apps_root(), "cool")
    os.makedirs(app)
    with open(os.path.join(app, "index.html"), "w", encoding="utf-8") as f:
        f.write(APP_HTML.format(t="Cool"))
    monkeypatch.setattr(cstore, "get_raw", lambda _k: {"kind": "marketplace", "slug": "cool", "repo": "a/b",
                                                       "approved": {"commit": "abc123"}})

    def link_state(*_a, **_k):
        if isinstance(state, Exception):
            raise state
        return dict(state)

    monkeypatch.setattr(share_app, "github_link_state", link_state)
    got = package.resolve_source(app)
    if want_dirty:
        assert got == {"kind": "bundled", "fallback": {"kind": "marketplace", "code": "dirty", "slug": "cool"}}
    else:
        assert got == {"kind": "marketplace", "slug": "cool", "repo": "a/b", "commit": "abc123"}


def _git(cwd, *args):
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"})


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_marketplace_copy_whose_only_edit_is_a_new_file_is_bundled(monkeypatch):
    """The share sheet's count leaves untracked files out (a proposal makes them opt-in), but a slug reference would
    lose them: a real clone with one new, non-ignored file is bundled; fused's own `.fused/` state and ignored files
    are not edits."""
    from fused_render import share_app
    from fused_render.bots import package
    from fused_render.contrib import store as cstore

    app = os.path.join(bpaths.apps_root(), "cool")
    os.makedirs(app)
    with open(os.path.join(app, "index.html"), "w", encoding="utf-8") as f:
        f.write(APP_HTML.format(t="Cool"))
    with open(os.path.join(app, ".gitignore"), "w", encoding="utf-8") as f:
        f.write("secret.txt\n")
    _git(app, "init", "-q")
    _git(app, "add", "-A")
    _git(app, "commit", "-q", "-m", "init")
    monkeypatch.setattr(cstore, "get_raw", lambda _k: {"kind": "marketplace", "slug": "cool", "repo": "a/b"})
    monkeypatch.setattr(share_app, "github_link_state", lambda *_a, **_k: {"ok": True, "code": "contributor",
                                                                           "changes": 0})
    os.makedirs(os.path.join(app, ".fused"))
    with open(os.path.join(app, ".fused", "state.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    with open(os.path.join(app, "secret.txt"), "w", encoding="utf-8") as f:
        f.write("ignored")
    assert package.resolve_source(app) == {"kind": "marketplace", "slug": "cool", "repo": "a/b"}

    os.makedirs(os.path.join(app, "data"))
    with open(os.path.join(app, "data", "notes.csv"), "w", encoding="utf-8") as f:
        f.write("a,b\n")
    assert package.resolve_source(app) == {"kind": "bundled",
                                           "fallback": {"kind": "marketplace", "code": "dirty", "slug": "cool"}}

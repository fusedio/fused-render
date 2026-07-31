"""Tests for annotate.py's revert actions — the seam between the annotate view
and the Claude-file-history reader (SPEC §33, D193).

`file_history.py` owns the store and the write; annotate.py owns two things it
cannot: turning every failure into an `{"error": ...}` DICT (a raised exception
would become the red traceback overlay, which is not an acceptable answer to
"this file has no history"), and stashing the pre-restore content into the
`<file>.json` sidecar it already read-merge-writes.

The stash is the answer to the sharpest hazard in the feature: current on-disk
content is frequently in NO checkpoint, so a restore can vaporize work that
exists nowhere else. The UI's confirm step is the first line of defence; this is
the second.
"""
import importlib.util
import json
import os

import pytest

from _claude_history import (  # noqa: F401  (claude_home is a fixture)
    claude_home, delta_record, path_hash, write_transcript, write_version)

skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


def _load_annotate():
    path = os.path.join("fused_render", "templates", "annotate", "annotate.py")
    spec = importlib.util.spec_from_file_location("annotate_target", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _target(tmp_path, content="a\nb\nc\n", name="page.html"):
    f = tmp_path / name
    f.write_text(content)
    return str(f)


def _sidecar(target):
    return target + ".json"


# ------------------------------------------------------------- history action

def test_history_returns_the_timeline(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "old\n", mtime=1000)
    out = ann.main(action="history", file=f)
    assert out["available"] is True
    assert [v["id"] for v in out["versions"]] == ["s@v1"]
    assert out["revert"] == "s@v1"
    assert out["writable"] is True


def test_history_with_no_store_is_an_empty_state_not_an_error(claude_home,
                                                             tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    out = ann.main(action="history", file=f)
    assert out["available"] is False
    assert out["versions"] == []
    assert out["revert"] is None
    assert out["note"]
    assert "error" not in out


def test_history_needs_a_file(claude_home):
    assert "error" in _load_annotate().main(action="history", file="")


def test_history_enrich_is_opt_in(claude_home, tmp_path):
    """The transcripts are the expensive half, so the boot-time call must not
    read them; only an expanded History panel asks."""
    ann = _load_annotate()
    f = _target(tmp_path, "made by claude\n")
    write_version(claude_home, "s", f, "made by claude\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z"),
    ])
    assert all(v["existed"] for v in ann.main(action="history", file=f)["versions"])
    rich = ann.main(action="history", file=f, enrich=True)["versions"]
    assert any(not v["existed"] for v in rich)


# ------------------------------------------------------------- plan action

def test_revert_plan_action_describes_the_write(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "unsaved\nwork\n")
    write_version(claude_home, "s", f, "old\n")
    plan = ann.main(action="revert_plan", file=f)
    assert plan["ok"] is True
    assert plan["action"] == "restore"
    assert plan["unique_current"] is True  # the confirm step must gate on this
    assert plan["removed"] == 2 and plan["added"] == 1


def test_revert_plan_with_nothing_to_revert(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    plan = ann.main(action="revert_plan", file=f)
    assert plan["ok"] is False and plan["error"]


# ------------------------------------------------------------- revert action

def test_revert_restores_and_stashes_the_previous_content(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "unsaved work\n")
    write_version(claude_home, "s", f, "wanted\n")

    out = ann.main(action="revert", file=f, version_id="s@v1")
    assert out["ok"] is True and out["action"] == "restore"
    assert out["stashed"] is True
    with open(f, encoding="utf-8") as h:
        assert h.read() == "wanted\n"

    stash = json.loads(open(_sidecar(f), encoding="utf-8").read())["revertStash"]
    assert len(stash) == 1
    assert stash[0]["content"] == "unsaved work\n"
    assert stash[0]["version_id"] == "s@v1"
    assert stash[0]["size"] == len("unsaved work\n")
    assert stash[0]["at"] > 0


def test_revert_defaults_to_the_last_change(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "older\n", mtime=1000)
    write_version(claude_home, "s", f, "disk\n", mtime=2000)  # a no-op version
    out = ann.main(action="revert", file=f)
    assert out["ok"] is True and out["id"] == "s@v1"
    with open(f, encoding="utf-8") as h:
        assert h.read() == "older\n"


def test_the_stash_preserves_every_key_it_does_not_own(claude_home, tmp_path):
    """Same read-merge-write contract as `_record`: the sidecar is shared with
    the claude chat template and the bookmark mirror, so a revert must round-trip
    their keys instead of clobbering them off disk."""
    ann = _load_annotate()
    f = _target(tmp_path, "before\n")
    write_version(claude_home, "s", f, "after\n")
    sess = [{"id": "s1", "preview": "hi", "created_at": 1, "last_used": 1,
             "cwd": "/x"}]
    with open(_sidecar(f), "w", encoding="utf-8") as h:
        json.dump({"claudeSessions": sess, "lastSession": "s1",
                   "comments": [{"id": "c1", "content": "keep me"}]}, h)

    ann.main(action="revert", file=f, version_id="s@v1")
    data = json.loads(open(_sidecar(f), encoding="utf-8").read())
    assert data["claudeSessions"] == sess
    assert data["lastSession"] == "s1"
    assert data["comments"][0]["content"] == "keep me"
    assert len(data["revertStash"]) == 1


def test_the_stash_is_bounded(claude_home, tmp_path):
    """A sidecar is a small JSON file the claude template rewrites constantly;
    an unbounded stash of file copies would grow it without limit."""
    ann = _load_annotate()
    f = _target(tmp_path, "gen0\n")
    for i in range(1, 7):
        write_version(claude_home, "s", f, "gen%d\n" % i)
    for i in range(1, 7):
        ann.main(action="revert", file=f, version_id="s@v%d" % i)
    stash = json.loads(open(_sidecar(f), encoding="utf-8").read())["revertStash"]
    assert len(stash) == ann.STASH_KEEP
    # Newest last: the entries kept are the most recent ones.
    assert stash[-1]["content"] == "gen5\n"


def test_a_large_file_is_reverted_without_being_copied_into_the_sidecar(
        claude_home, tmp_path, monkeypatch):
    """Better a revert with no stash than a multi-megabyte sidecar — and the
    caller is TOLD, so the UI can make the confirm step firmer."""
    ann = _load_annotate()
    monkeypatch.setattr(ann, "STASH_BYTE_CAP", 8)
    f = _target(tmp_path, "far too much content to stash\n")
    write_version(claude_home, "s", f, "small\n")
    out = ann.main(action="revert", file=f, version_id="s@v1")
    assert out["ok"] is True
    assert out["stashed"] is False
    assert out["stash_note"]
    assert not os.path.exists(_sidecar(f))  # nothing written at all
    with open(f, encoding="utf-8") as h:
        assert h.read() == "small\n"


def test_binary_content_is_not_stashed_but_is_still_reverted(claude_home,
                                                             tmp_path):
    ann = _load_annotate()
    f = str(tmp_path / "blob.bin")
    with open(f, "wb") as h:
        h.write(b"\xff\xfe\x00current")
    d = os.path.join(str(claude_home), "file-history", "s")
    os.makedirs(d)
    with open(os.path.join(d, path_hash(f) + "@v1"), "wb") as h:
        h.write(b"\xff\xfe\x00wanted")

    out = ann.main(action="revert", file=f, version_id="s@v1")
    assert out["ok"] is True and out["stashed"] is False
    assert out["stash_note"]
    with open(f, "rb") as h:
        assert h.read() == b"\xff\xfe\x00wanted"


def test_a_delete_revert_stashes_the_whole_file_first(claude_home, tmp_path):
    """Reverting across a did-not-exist boundary removes the file outright, so
    the stash is the ONLY copy of what was there."""
    ann = _load_annotate()
    f = _target(tmp_path, "claude wrote this\n")
    write_version(claude_home, "s", f, "claude wrote this\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z"),
    ])
    out = ann.main(action="revert", file=f, version_id="s@none0", enrich=True)
    assert out["ok"] is True and out["action"] == "delete"
    assert out["stashed"] is True
    assert not os.path.exists(f)
    stash = json.loads(open(_sidecar(f), encoding="utf-8").read())["revertStash"]
    assert stash[0]["content"] == "claude wrote this\n"


# ------------------------------------------------------- failures stay data

def test_an_unknown_version_is_an_error_dict_not_a_traceback(claude_home,
                                                             tmp_path):
    """Anything raised out of `main` becomes the red traceback overlay, which is
    never the right answer here — so every failure crosses the bridge as data."""
    ann = _load_annotate()
    f = _target(tmp_path, "keep\n")
    write_version(claude_home, "s", f, "v1\n")
    out = ann.main(action="revert", file=f, version_id="nope@v9")
    assert "error" in out and out.get("ok") is not True
    with open(f, encoding="utf-8") as h:
        assert h.read() == "keep\n"


def test_revert_needs_a_file(claude_home):
    assert "error" in _load_annotate().main(action="revert", file="")


def test_a_target_the_store_never_recorded_is_refused(claude_home, tmp_path):
    ann = _load_annotate()
    victim = _target(tmp_path, "precious\n", name="victim.txt")
    known = _target(tmp_path, "known\n", name="known.txt")
    write_version(claude_home, "s", known, "payload\n")
    out = ann.main(action="revert", file=victim, version_id="s@v1")
    assert "error" in out
    with open(victim, encoding="utf-8") as h:
        assert h.read() == "precious\n"


@skip_root
def test_a_read_only_target_is_refused_as_data(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "keep\n")
    write_version(claude_home, "s", f, "v1\n")
    os.chmod(f, 0o444)
    try:
        out = ann.main(action="revert", file=f, version_id="s@v1")
        assert "error" in out
        with open(f, encoding="utf-8") as h:
            assert h.read() == "keep\n"
    finally:
        os.chmod(f, 0o644)


@pytest.fixture
def ro_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    import fused_render.shell.mounts as mounts

    m = mounts.add_mount("pub", "pub-remote:bucket", read_only=True)
    mp = mounts.mountpoint(m)
    os.makedirs(mp)
    f = os.path.join(mp, "page.html")
    with open(f, "w") as fh:
        fh.write("keep\n")
    return f


def test_a_read_only_mount_refuses_the_revert(claude_home, ro_mount):
    ann = _load_annotate()
    write_version(claude_home, "s", ro_mount, "wanted\n")
    assert os.access(os.path.dirname(ro_mount), os.W_OK)  # the lie
    out = ann.main(action="revert", file=ro_mount, version_id="s@v1")
    assert "error" in out
    with open(ro_mount, encoding="utf-8") as h:
        assert h.read() == "keep\n"
    assert ann.main(action="history", file=ro_mount)["writable"] is False


def test_the_store_is_never_written_by_any_action(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "wanted\n")

    def snap():
        out = {}
        for root, _dirs, names in os.walk(str(claude_home)):
            for n in names:
                p = os.path.join(root, n)
                out[p] = open(p, "rb").read()
        return out

    before = snap()
    ann.main(action="history", file=f, enrich=True)
    ann.main(action="revert_plan", file=f)
    ann.main(action="revert", file=f, version_id="s@v1")
    assert snap() == before


def test_history_degrades_when_the_shared_helper_is_missing(claude_home,
                                                            tmp_path):
    """A copy of the annotate folder taken without its `shared/` sibling has no
    file_history at all — the same degradation appenv already has. Revert simply
    is not offered; nothing else in the view breaks."""
    import builtins
    import sys

    ann = _load_annotate()
    f = _target(tmp_path)
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "file_history":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    saved = sys.modules.pop("file_history", None)
    builtins.__import__ = blocked
    try:
        assert "error" in ann.main(action="history", file=f)
        assert "error" in ann.main(action="revert", file=f, version_id="s@v1")
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["file_history"] = saved


def test_the_comments_log_and_the_stash_coexist(claude_home, tmp_path):
    """Both writers go through the same read-merge-write, so recording a comment
    after a revert must not drop the stash, and vice versa."""
    ann = _load_annotate()
    f = _target(tmp_path, "before\n")
    write_version(claude_home, "s", f, "after\n")
    ann._record(f, [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    ann.main(action="revert", file=f, version_id="s@v1")
    ann._record(f, [{"id": "c2", "content": "there", "createdAt": 2}], [])

    data = json.loads(open(_sidecar(f), encoding="utf-8").read())
    assert sorted(c["id"] for c in data["comments"]) == ["c1", "c2"]
    assert len(data["revertStash"]) == 1

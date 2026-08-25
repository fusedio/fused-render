"""The Hugging Face cache inventory and its deletions behind the sidebar's
"AI Models" page (server/routers/ai_models.py): GET /api/ai-models,
/status, /revisions, and POST /api/ai-models/delete.

Four things here are easy to get quietly wrong, so each is pinned:

* **Where the cache is.** huggingface_hub resolves it through four env vars
  with a precedence order; reading only ``~/.cache/huggingface/hub`` would
  report "nothing cached" on every machine that sets ``HF_HOME`` (which is
  most machines with a shared model disk).
* **What a repo costs.** Every snapshot entry is a symlink back into the same
  repo's ``blobs/``, so a naive walk multiplies a repo's size by its revision
  count — a page whose entire job is disk footprint would then be wrong by
  hundreds of GB on a big cache.
* **What a revision deletion may take.** A blob two revisions share must
  survive the first one's deletion; getting this wrong corrupts the revision
  left behind, which is worse than any amount of wasted disk.
* **What a delete request may name.** The target is a cache FOLDER NAME, and
  every path is built server-side from it. A path taken from a request body
  would make this an arbitrary-rmtree endpoint.

The layout the fixtures build is huggingface_hub's own CACHE_STRUCTURE:
``<hub>/models--org--name/{blobs,snapshots/<commit>,refs/<ref>}``.
"""
import dataclasses
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.ai import catalog
from fused_render.ai import registry as _ai_registry
from fused_render.ai import hub_cache as ai_models_mod
from fused_render.ai import tasks as ai_tasks

# Windows makes symlinks a privileged operation, and huggingface_hub itself
# falls back to copies there — the dedup rule under test is a POSIX-cache one.
requires_symlinks = pytest.mark.skipif(
    os.name == "nt", reason="symlink creation needs privileges on Windows"
)


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    """An empty hub cache, pointed at by HF_HUB_CACHE.

    Every other HF var is cleared: a developer machine with a real HF_HOME must
    not leak its own cache into these assertions.
    """
    for var in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / "hub"
    d.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(d))
    return d


def _repo(hub, dirname, blobs=None, snapshots=None, refs=None):
    """One cache repo folder. `blobs` maps blob name -> byte length; `snapshots`
    maps commit -> {filename: blob name} (materialised as symlinks, exactly as
    huggingface_hub does); `refs` maps ref name -> commit."""
    repo = hub / dirname
    (repo / "blobs").mkdir(parents=True)
    for name, size in (blobs or {}).items():
        (repo / "blobs" / name).write_bytes(b"x" * size)
    for commit, files in (snapshots or {}).items():
        snap = repo / "snapshots" / commit
        snap.mkdir(parents=True)
        for filename, blob in files.items():
            os.symlink(repo / "blobs" / blob, snap / filename)
    for ref, commit in (refs or {}).items():
        path = repo / "refs" / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(commit)
    return repo


def _get(client):
    r = client.get("/api/ai-models")
    assert r.status_code == 200
    return r.json()


# -- what's in the cache -------------------------------------------------------


def test_decodes_repo_ids_and_kinds(client, hub):
    _repo(hub, "models--openai--whisper-small", blobs={"a": 10})
    _repo(hub, "datasets--squad", blobs={"a": 10})
    _repo(hub, "spaces--user--demo", blobs={"a": 10})
    data = _get(client)
    assert {(r["id"], r["kind"]) for r in data["repos"]} == {
        ("openai/whisper-small", "model"),
        ("squad", "dataset"),
        ("user/demo", "space"),
    }


def test_skips_everything_that_is_not_a_repo_folder(client, hub):
    _repo(hub, "models--gpt2", blobs={"a": 10})
    (hub / ".locks").mkdir()
    (hub / ".locks" / "models--gpt2").mkdir()
    (hub / "version.txt").write_text("1")
    (hub / "tmp7f3k").mkdir()  # a download in flight
    data = _get(client)
    assert [r["id"] for r in data["repos"]] == ["gpt2"]


def test_sorted_biggest_first_with_the_total(client, hub):
    _repo(hub, "models--small", blobs={"a": 100})
    _repo(hub, "models--big", blobs={"a": 5000})
    _repo(hub, "models--middle", blobs={"a": 900})
    data = _get(client)
    assert [r["id"] for r in data["repos"]] == ["big", "middle", "small"]
    assert data["totalSize"] == 6000 == sum(r["size"] for r in data["repos"])


def test_reports_revisions_and_refs(client, hub):
    _repo(
        hub,
        "models--org--m",
        blobs={"a": 10, "b": 20},
        snapshots={"c0ffee": {"model.bin": "a"}, "deadbeef": {"model.bin": "b"}},
        refs={"main": "deadbeef", "v1.0": "c0ffee"},
    )
    (repo,) = _get(client)["repos"]
    assert repo["revisions"] == 2
    assert repo["refs"] == ["main", "v1.0"]


@requires_symlinks
def test_a_repo_folder_symlinked_in_from_another_disk_is_listed(client, hub, tmp_path):
    # Moving a big model off the boot volume and symlinking it back is a normal
    # thing to do; the repo is still cached, and its bytes are still real.
    elsewhere = tmp_path / "big-disk"
    elsewhere.mkdir()
    real = _repo(elsewhere, "models--org--huge", blobs={"a": 4096})
    os.symlink(real, hub / "models--org--huge")
    (repo,) = _get(client)["repos"]
    assert repo["id"] == "org/huge"
    assert repo["size"] == 4096


def test_path_points_at_the_repo_folder(client, hub):
    _repo(hub, "models--gpt2", blobs={"a": 10})
    (repo,) = _get(client)["repos"]
    assert repo["path"] == str(hub / "models--gpt2").replace("\\", "/")


# -- footprint -----------------------------------------------------------------


@requires_symlinks
def test_snapshot_symlinks_are_not_counted_again(client, hub):
    # One 1000-byte blob, materialised into two revisions: on disk that is
    # 1000 bytes plus the two 8-byte refs files, NOT 3000.
    _repo(
        hub,
        "models--org--m",
        blobs={"blob1": 1000},
        snapshots={"c0ffee11": {"model.bin": "blob1"}, "deadbeef": {"model.bin": "blob1"}},
        refs={"main": "deadbeef"},
    )
    (repo,) = _get(client)["repos"]
    assert repo["size"] == 1000 + len("deadbeef")
    assert repo["files"] == 2  # the blob and the ref file; the links are not files


@requires_symlinks
def test_size_is_bytes_on_disk_not_apparent_size(client, hub):
    _repo(hub, "models--a", blobs={"b": 1000}, snapshots={"c1": {"f": "b"}})
    _repo(hub, "models--b", blobs={"b": 1000})
    a, b = sorted(_get(client)["repos"], key=lambda r: r["id"])
    assert a["size"] == b["size"] == 1000


def test_hardlinked_blob_counted_once(client, hub, tmp_path):
    repo = _repo(hub, "models--org--m", blobs={"blob1": 400})
    link = repo / "blobs" / "blob2"
    try:
        os.link(repo / "blobs" / "blob1", link)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support hard links")
    (out,) = _get(client)["repos"]
    assert out["size"] == 400


def test_mtime_is_null_for_a_repo_with_no_files(client, hub):
    (hub / "models--empty" / "blobs").mkdir(parents=True)
    (repo,) = _get(client)["repos"]
    assert repo["mtime"] is None
    assert repo["size"] == 0


def test_mtime_is_the_newest_entry(client, hub):
    repo = _repo(hub, "models--org--m", blobs={"a": 10, "b": 10})
    os.utime(repo / "blobs" / "a", (1000, 1000))
    os.utime(repo / "blobs" / "b", (2000, 2000))
    (out,) = _get(client)["repos"]
    assert out["mtime"] == 2000


# -- a cache being written under us ---------------------------------------------
# The scan runs against a directory other processes are actively downloading
# into, so both of its defensive paths are real: a folder it cannot read, and a
# file that exists in the listing but is gone by the time it is stat'd. Neither
# may fail the page — a partial answer beats a 500.


def test_an_unreadable_repo_folder_does_not_fail_the_page(client, hub, monkeypatch):
    _repo(hub, "models--ok", blobs={"a": 100})
    locked = _repo(hub, "models--locked", blobs={"a": 100})
    real_scandir = os.scandir

    def fake_scandir(path=".", *args, **kwargs):
        if str(path) == str(locked):
            raise PermissionError(13, "Permission denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(ai_models_mod.os, "scandir", fake_scandir)
    assert [(r["id"], r["size"]) for r in _get(client)["repos"]] == [("ok", 100), ("locked", 0)]


def test_a_file_that_vanishes_mid_scan_is_skipped(client, hub, monkeypatch):
    repo = _repo(hub, "models--org--m", blobs={"stays": 100})
    blobs = str(repo / "blobs")
    real_scandir = os.scandir

    class _Vanished:
        """A listing entry whose file was deleted before we could stat it —
        what a completed download's temp-blob rename looks like from here."""

        name = "vanishing"
        path = os.path.join(blobs, "vanishing")

        def stat(self, *, follow_symlinks=True):
            raise FileNotFoundError(2, "No such file or directory")

    def fake_scandir(path=".", *args, **kwargs):
        if str(path) == blobs:
            return [*real_scandir(path, *args, **kwargs), _Vanished()]
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(ai_models_mod.os, "scandir", fake_scandir)
    (out,) = _get(client)["repos"]
    assert out["size"] == 100
    assert out["files"] == 1


def test_a_file_that_vanishes_between_the_two_stats_is_skipped(
        client, hub, monkeypatch):
    """On win32 the scan takes a SECOND, uncached os.stat to get real
    st_nlink/st_ino (DirEntry.stat reports 0 for both there, which would
    silently disable hardlink dedup). That is another trip to the filesystem,
    so the same mid-download deletion the test above covers can land in this
    narrower window instead — and must be skipped like any other, not abort
    the whole listing.

    Skipped means counting for NOTHING — the timestamps too, not just
    size/files. They are asserted here because they used to be accumulated
    before the re-stat could rule the file out, so a vanished blob still dated
    the repo. `lastUsed` is the one with teeth: it drives prune selection in
    the client, so a deleted blob's atime leaking in marks a stale repo as
    recently used and shields it from the cleanup that removed the blob.
    """
    repo = _repo(hub, "models--org--m", blobs={"stays": 100, "vanishes": 50})
    gone = str(repo / "blobs" / "vanishes")

    # Distinctive, far-apart stamps so a leak cannot hide inside a max()/min():
    # the doomed blob is BOTH the newest and the most recently used AND the
    # oldest, so if any of the three accumulators still sees it, one of the
    # assertions below reads its number instead of the survivor's.
    os.utime(repo / "blobs" / "stays", (5_000_000, 5_000_000))
    os.utime(gone, (9_000_000, 1_000_000))
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if str(path) == gone:
            raise FileNotFoundError(2, "No such file or directory")
        return real_stat(path, *args, **kwargs)

    # The DirEntry.stat() in the loop still succeeds for both blobs — only the
    # win32-only re-stat finds this one gone.
    monkeypatch.setattr(ai_models_mod.sys, "platform", "win32")
    monkeypatch.setattr(ai_models_mod.os, "stat", fake_stat)
    (out,) = _get(client)["repos"]
    assert out["size"] == 100
    assert out["files"] == 1
    # Every field describes the surviving blob alone.
    assert out["mtime"] == 5_000_000       # not the vanished 1_000_000 mtime
    assert out["lastUsed"] == 5_000_000    # not its 9_000_000 atime
    assert out["added"] == 5_000_000       # not its 1_000_000 mtime as "oldest"


# -- no cache at all -----------------------------------------------------------


def test_missing_cache_dir_is_an_empty_answer_not_an_error(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "nope"))
    data = _get(client)
    assert data["exists"] is False
    assert data["repos"] == []
    assert data["totalSize"] == 0


def test_the_page_url_serves_the_shell(client):
    # The page is client-side, but a bookmark or a refresh is a real GET the
    # server has to answer with the shell (routers/shell.py) — otherwise the
    # route 404s for anyone who did not arrive by clicking the sidebar.
    r = client.get("/ai-models")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_no_status_probe(client):
    # The sidebar entry is unconditional (HF-8, D265), so the isdir() probe that
    # gated it is gone rather than left standing with no caller. `exists` on the
    # listing is the one remaining answer to "is there a cache here".
    assert client.get("/api/ai-models/status").status_code == 404


# -- where the cache is --------------------------------------------------------


@pytest.fixture()
def clean_env(monkeypatch):
    for var in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)


def test_cache_dir_defaults_under_the_home_cache(clean_env):
    assert ai_models_mod.hub_cache_dir() == os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface", "hub"
    )


def test_xdg_cache_home_moves_the_default(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert ai_models_mod.hub_cache_dir() == os.path.join(str(tmp_path), "huggingface", "hub")


def test_hf_home_wins_over_xdg(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert ai_models_mod.hub_cache_dir() == os.path.join(str(tmp_path / "hf"), "hub")


def test_hub_cache_vars_win_over_hf_home(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "legacy"))
    assert ai_models_mod.hub_cache_dir() == str(tmp_path / "legacy")
    # The current name outranks the deprecated one.
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "current"))
    assert ai_models_mod.hub_cache_dir() == str(tmp_path / "current")


def test_user_paths_are_expanded(clean_env, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", os.path.join("~", "models"))
    assert ai_models_mod.hub_cache_dir() == os.path.join(os.path.expanduser("~"), "models")


# -- revisions view ------------------------------------------------------------
# `size` per revision is what deleting it would FREE (blobs no sibling shares),
# not what it appears to contain. Two revisions of a 7GB model differing in a
# config file are 7GB shared and a few KB each; a row claiming 7GB apiece would
# be a lie in the one column this page exists for.


def _revisions(client, repo):
    r = client.get("/api/ai-models/revisions", params={"repo": repo})
    assert r.status_code == 200, r.text
    return {rev["commit"]: rev for rev in r.json()["revisions"]}


@requires_symlinks
def test_revision_sizes_split_exclusive_from_shared(client, hub):
    _repo(
        hub,
        "models--org--m",
        blobs={"shared": 4000, "only_a": 300, "only_b": 70},
        snapshots={
            "aaa": {"model.bin": "shared", "extra.json": "only_a"},
            "bbb": {"model.bin": "shared", "extra.json": "only_b"},
        },
        refs={"main": "bbb", "v1": "aaa"},
    )
    revs = _revisions(client, "models--org--m")
    assert revs["aaa"]["size"] == 300 and revs["aaa"]["shared"] == 4000
    assert revs["bbb"]["size"] == 70 and revs["bbb"]["shared"] == 4000
    assert revs["aaa"]["refs"] == ["v1"] and revs["bbb"]["refs"] == ["main"]
    assert revs["aaa"]["files"] == 2


@requires_symlinks
def test_a_lone_revision_owns_everything_it_references(client, hub):
    _repo(hub, "models--solo", blobs={"a": 900}, snapshots={"c1": {"m.bin": "a"}}, refs={"main": "c1"})
    (rev,) = _revisions(client, "models--solo").values()
    assert rev["size"] == 900 and rev["shared"] == 0


def test_revisions_of_an_unknown_repo_are_a_404(client, hub):
    assert client.get("/api/ai-models/revisions", params={"repo": "models--nope"}).status_code == 404


# -- deleting ------------------------------------------------------------------


def _delete(client, targets, headers=None):
    return client.post(
        "/api/ai-models/delete",
        json={"targets": targets},
        headers={"X-Fused": "1"} if headers is None else headers,
    )


def test_deleting_a_repo_frees_it_and_answers_with_the_fresh_listing(client, hub):
    _repo(hub, "models--keep", blobs={"a": 100})
    doomed = _repo(hub, "models--drop", blobs={"a": 5000})
    r = _delete(client, [{"dir": "models--drop"}])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["freed"] == 5000
    assert data["failures"] == []
    # The reply IS the listing, re-read from disk — not a patched copy of the
    # rows the page was showing.
    assert [repo["id"] for repo in data["repos"]] == ["keep"]
    assert data["totalSize"] == 100
    assert not doomed.exists()


def test_deleting_a_repo_takes_its_lock_folder_with_it(client, hub):
    _repo(hub, "models--m", blobs={"a": 10})
    locks = hub / ".locks" / "models--m"
    locks.mkdir(parents=True)
    (locks / "abc.lock").write_text("")
    assert _delete(client, [{"dir": "models--m"}]).status_code == 200
    assert not locks.exists()


@requires_symlinks
def test_deleting_a_revision_keeps_a_blob_its_sibling_shares(client, hub):
    repo = _repo(
        hub,
        "models--org--m",
        blobs={"shared": 4000, "only_a": 300},
        snapshots={
            "aaa": {"model.bin": "shared", "extra.json": "only_a"},
            "bbb": {"model.bin": "shared"},
        },
        refs={"main": "bbb", "v1": "aaa"},
    )
    data = _delete(client, [{"dir": "models--org--m", "revision": "aaa"}]).json()
    assert data["failures"] == []
    # NOT 4300: `shared` still backs revision bbb. The 3 extra bytes are the
    # refs/v1 file, which pointed at the revision that just went.
    assert data["freed"] == 300 + len("aaa")
    assert not (repo / "snapshots" / "aaa").exists()
    assert (repo / "blobs" / "shared").exists()
    assert not (repo / "blobs" / "only_a").exists()
    # The surviving revision still resolves through its link.
    assert (repo / "snapshots" / "bbb" / "model.bin").read_bytes() == b"x" * 4000


@requires_symlinks
def test_deleting_a_revision_drops_the_refs_that_pointed_at_it(client, hub):
    repo = _repo(
        hub,
        "models--org--m",
        blobs={"a": 100, "b": 100},
        snapshots={"aaa": {"m": "a"}, "bbb": {"m": "b"}},
        refs={"main": "bbb", "v1": "aaa", "tags/old": "aaa"},
    )
    _delete(client, [{"dir": "models--org--m", "revision": "aaa"}])
    assert not (repo / "refs" / "v1").exists()
    assert not (repo / "refs" / "tags" / "old").exists()
    assert (repo / "refs" / "main").read_text() == "bbb"


@requires_symlinks
def test_deleting_the_last_revision_removes_the_whole_repo(client, hub):
    repo = _repo(
        hub,
        "models--org--m",
        # `orphan` is referenced by no snapshot — exactly the litter that would
        # be left behind by removing only the revision's own bytes.
        blobs={"a": 100, "orphan": 900},
        snapshots={"aaa": {"m": "a"}},
        refs={"main": "aaa"},
    )
    data = _delete(client, [{"dir": "models--org--m", "revision": "aaa"}]).json()
    assert not repo.exists()
    assert data["freed"] == 1000 + len("aaa")  # the blob, the orphan, the ref
    assert data["repos"] == []


def test_targets_are_reported_one_by_one(client, hub):
    _repo(hub, "models--real", blobs={"a": 100})
    data = _delete(
        client, [{"dir": "models--real"}, {"dir": "models--ghost"}, {"dir": "models--real"}]
    ).json()
    # The one that existed is gone; the two that could not be found are named
    # rather than swallowed — a prune must not lose nine deletions to one
    # stale row.
    assert data["freed"] == 100
    assert [f["dir"] for f in data["failures"]] == ["models--ghost", "models--real"]
    assert data["repos"] == []


# -- what a delete request may name ---------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../../etc",
        "/etc/passwd",
        "..",
        ".",
        "models--a/../../../tmp",
        ".locks",
        "version.txt",
        "tmp7f3k",
        "",
        None,
        123,
    ],
)
def test_a_target_that_is_not_a_repo_folder_name_is_refused(client, hub, tmp_path, name):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("hi")
    (hub / ".locks").mkdir()
    (hub / "version.txt").write_text("1")
    (hub / "tmp7f3k").mkdir()
    data = _delete(client, [{"dir": name}]).json()
    assert data["freed"] == 0
    assert len(data["failures"]) == 1
    # Nothing outside the cache was touched, and the non-repo entries stay.
    assert (outside / "keep.txt").exists()
    assert (hub / ".locks").exists() and (hub / "version.txt").exists()


@pytest.mark.parametrize("revision", ["../../../../etc", "..", "/abs", "", "a/b", 0, False])
@requires_symlinks
def test_a_revision_that_is_not_a_plain_name_is_refused(client, hub, tmp_path, revision):
    # Including the falsy ones: a malformed revision must be an error, never a
    # fallback to "delete the whole repo" — the widest possible reading of the
    # narrowest possible request. Only an ABSENT revision means the repo.
    repo = _repo(hub, "models--m", blobs={"a": 10}, snapshots={"c1": {"m": "a"}}, refs={"main": "c1"})
    data = _delete(client, [{"dir": "models--m", "revision": revision}]).json()
    assert data["freed"] == 0 and len(data["failures"]) == 1
    assert (repo / "snapshots" / "c1").exists()
    assert repo.exists()


@requires_symlinks
def test_a_symlinked_repo_folder_is_refused_rather_than_followed(client, hub, tmp_path):
    elsewhere = tmp_path / "big-disk"
    elsewhere.mkdir()
    real = _repo(elsewhere, "models--org--huge", blobs={"a": 4096}, snapshots={"c1": {"m": "a"}})
    os.symlink(real, hub / "models--org--huge")
    for target in ({"dir": "models--org--huge"}, {"dir": "models--org--huge", "revision": "c1"}):
        data = _delete(client, [target]).json()
        assert data["freed"] == 0
        assert "symlink" in data["failures"][0]["error"]
    # Neither the link nor the files it points at were removed.
    assert (hub / "models--org--huge").is_symlink()
    assert (real / "blobs" / "a").exists()


def test_delete_requires_the_write_guard(client, hub):
    repo = _repo(hub, "models--m", blobs={"a": 100})
    r = _delete(client, [{"dir": "models--m"}], headers={})
    assert r.status_code == 403
    assert repo.exists()


def test_a_model_in_use_is_not_deleted(client, hub, monkeypatch):
    """The cache and the processes are two owners of the same files.

    `shutil.rmtree` over a repo a worker is mid-`from_pretrained` on removes the
    shards it is still reading, and the error arrives minutes later looking like
    a corrupt model. Deleting a RESIDENT one is quieter and worse: the weights
    are already mapped, so on POSIX the delete succeeds, the page says the model
    is gone, and it answers on until something unloads it.
    """
    from fused_render.ai import supervisor

    repo = _repo(hub, "models--org--live", blobs={"a": 100})
    monkeypatch.setattr(supervisor, "busy_reason",
                        lambda model: "in memory" if model == "org/live" else None)

    r = _delete(client, [{"dir": "models--org--live"}])
    assert r.status_code == 200
    (failure,) = r.json()["failures"]
    assert "in memory" in failure["error"] and "Unload it first" in failure["error"]
    assert repo.exists(), "the files were deleted out from under a live worker"
    assert r.json()["freed"] == 0


def test_a_revision_of_a_model_in_use_is_not_deleted_either(client, hub, monkeypatch):
    """"Just one revision" is not the safer request it looks like — it is the
    revision the resident worker has open."""
    from fused_render.ai import supervisor

    repo = _repo(hub, "models--org--live", blobs={"a": 100},
                 snapshots={"abc": {"model.bin": "a"}})
    monkeypatch.setattr(supervisor, "busy_reason", lambda model: "being downloaded")

    r = _delete(client, [{"dir": "models--org--live", "revision": "abc"}])
    (failure,) = r.json()["failures"]
    assert "being downloaded" in failure["error"]
    assert (repo / "snapshots" / "abc").exists()


def test_delete_needs_a_non_empty_target_list(client, hub):
    assert _delete(client, []).status_code == 400
    assert client.post("/api/ai-models/delete", json={}, headers={"X-Fused": "1"}).status_code == 400


# -- pruning by age --------------------------------------------------------------
# Prune is a client-side selection over `lastUsed` executed as a bulk delete of
# NAMED repos (D250), so what the server owes it is an honest read-time stamp.


def test_last_used_reads_the_newest_atime(client, hub):
    repo = _repo(hub, "models--m", blobs={"cold": 10, "warm": 10})
    os.utime(repo / "blobs" / "cold", (1_000_000, 5_000_000))
    os.utime(repo / "blobs" / "warm", (2_000_000, 4_000_000))
    (out,) = _get(client)["repos"]
    # atime, not mtime: a model pulled long ago and loaded this morning is in
    # use, and mtime cannot tell those two apart.
    assert out["lastUsed"] == 2_000_000
    assert out["mtime"] == 5_000_000


@requires_symlinks
def test_reading_the_page_does_not_mark_a_repo_as_freshly_used(client, hub):
    # The endpoints open ref files to resolve revisions, which bumps their
    # atime. Left alone, a trip through this page would mark every repo it
    # inspected as used today and quietly exclude it from the next prune — a
    # measuring instrument changing what it measures.
    repo = _repo(hub, "models--m", blobs={"a": 10}, snapshots={"c1": {"m": "a"}}, refs={"main": "c1"})
    old = 1_000_000
    for path in (repo / "blobs" / "a", repo / "refs" / "main"):
        os.utime(path, (old, old))
    assert _get(client)["repos"][0]["lastUsed"] == old
    client.get("/api/ai-models/revisions", params={"repo": "models--m"})
    assert _get(client)["repos"][0]["lastUsed"] == old


def test_a_prune_selection_deletes_exactly_the_named_repos(client, hub):
    stale_a = _repo(hub, "models--stale-a", blobs={"a": 400})
    stale_b = _repo(hub, "datasets--stale-b", blobs={"a": 300})
    fresh = _repo(hub, "models--fresh", blobs={"a": 200})
    for repo, atime in ((stale_a, 1_000_000), (stale_b, 1_000_000), (fresh, 9_000_000)):
        os.utime(repo / "blobs" / "a", (atime, atime))
    listing = _get(client)
    cutoff = 5_000_000
    stale = [r["dir"] for r in listing["repos"] if r["lastUsed"] and r["lastUsed"] < cutoff]
    data = _delete(client, [{"dir": d} for d in sorted(stale)]).json()
    assert data["freed"] == 700
    assert [r["id"] for r in data["repos"]] == ["fresh"]


# -- what a model is for, and how big --------------------------------------------
# Nothing in the cache states a model's purpose outright, so it is read from
# whatever evidence the download brought: the model card's pipeline_tag first
# (the Hub's own answer), then a diffusers/sentence-transformers marker, then
# the transformers architecture — with the SOURCE reported, because a
# pipeline_tag is a fact and an architecture is a reading of one.


def _snapshot_file(repo, commit, name, content):
    """A real file inside a snapshot (a model card / config, which arrive as
    ordinary files in the snapshot rather than as weight blobs)."""
    path = repo / "snapshots" / commit / name  # `name` may be "unet/model.safetensors"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content) if isinstance(content, str) else path.write_bytes(content)
    return path


def _safetensors(shapes, dtype="F16"):
    """A safetensors file that is nothing but its header — the parameter count
    is read from tensor SHAPES, so the weights themselves need not exist."""
    header = {
        name: {"dtype": dtype, "shape": list(shape), "data_offsets": [0, 0]}
        for name, shape in shapes.items()
    }
    header["__metadata__"] = {"format": "pt"}
    blob = json.dumps(header).encode()
    return len(blob).to_bytes(8, "little") + blob


def _repo_row(client, repo_id):
    return next(r for r in _get(client)["repos"] if r["id"] == repo_id)


@requires_symlinks
def test_pipeline_tag_from_the_model_card_wins(client, hub):
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"model.bin": "w"}},
                 refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md",
                   "---\nlibrary_name: diffusers\npipeline_tag: text-to-image\ntags:\n  - art\n---\n# Card\n")
    # A config that would infer something ELSE is present, so this also pins the
    # precedence rather than just the parse.
    _snapshot_file(repo, "c1", "config.json", json.dumps({"architectures": ["BertForMaskedLM"]}))
    row = _repo_row(client, "org/m")
    assert row["task"] == "text to image"
    assert row["taskSource"] == "the model card's pipeline_tag"
    assert row["library"] == "diffusers"


@requires_symlinks
@pytest.mark.parametrize(
    "architectures,model_type,expected",
    [
        (["LlamaForCausalLM"], "llama", "text generation"),
        (["BertForSequenceClassification"], "bert", "text classification"),
        (["BertForMaskedLM"], "bert", "fill mask"),
        (["ViTForImageClassification"], "vit", "image classification"),
        # An encoder-decoder is not the causal-LM path mlx-lm serves, and the
        # Hub retired its own `text2text-generation` tag — so what such a
        # checkpoint is USED for is the surviving honest answer, and it is not
        # served here either way.
        (["T5ForConditionalGeneration"], "t5", "translation"),
        # Same head, different job — the model type is what separates them.
        (["WhisperForConditionalGeneration"], "whisper", "speech recognition"),
        (["SomethingEntirelyNew"], "mystery", None),
    ],
)
def test_task_inferred_from_the_architecture(client, hub, architectures, model_type, expected):
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"model.bin": "w"}},
                 refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json",
                   json.dumps({"architectures": architectures, "model_type": model_type}))
    row = _repo_row(client, "org/m")
    assert row["task"] == expected
    # An inference says so; only the model card is reported as the Hub's answer.
    assert row["taskSource"] == ("the architecture in config.json" if expected else None)


@requires_symlinks
@pytest.mark.parametrize("architecture,model_type,extra", [
    # Qwen3.5 — a vision tower and no audio. Every MLX conversion of it, which
    # is the whole of this app's own Apple Silicon catalog since D3xx.
    ("Qwen3_5ForConditionalGeneration", "qwen3_5", {"vision_config": {}}),
    # gemma-4 — vision AND audio, and still a chat model to mlx-lm.
    ("Gemma4ForConditionalGeneration", "gemma4",
     {"vision_config": {}, "audio_config": {}}),
])
def test_a_multimodal_wrapper_is_a_TEXT_model_not_a_t5(client, hub, architecture,
                                                       model_type, extra):
    """`…ForConditionalGeneration` on a config with a `vision_config` is a
    vision-language model whose language tower is what mlx-lm loads — the case
    `_TASK_CAPABILITIES` already has a word for ("image + text to text"), and
    which it already maps to text generation.

    Read as T5's head instead, it came out "text-to-text generation" — a label
    in NO_RUNNER_YET — so the newest models in the app's own catalog arrived on
    this page with a wrong task and no Load button. The card path has always
    said "image + text to text" for exactly these repos (it is their
    pipeline_tag); this is the architecture path agreeing with it, which is the
    invariant `test_every_label_this_module_can_produce_is_explained` exists to
    protect.
    """
    repo = _repo(hub, "models--org--m", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json", json.dumps(
        {"architectures": [architecture], "model_type": model_type, **extra}))
    _snapshot_file(repo, "c1", "model.safetensors", _safetensors({"w": (8, 8)}))
    row = _repo_row(client, "org/m")
    assert row["task"] == "image + text to text"
    assert row["taskSource"] == "the architecture in config.json"
    assert row["capability"] == _ai_registry.TEXT_GENERATION


@requires_symlinks
def test_an_AUDIO_language_model_is_not_called_a_vision_one(client, hub):
    """An audio tower is not evidence of a vision tower.

    `Qwen2AudioForConditionalGeneration` carries an `audio_config` and no
    `vision_config`, and reading either key as "multimodal, therefore image +
    text to text" made it a VLM — which in this app means text generation,
    which means a Load button pointed at a runner that cannot use it: mlx-lm
    resolves a checkpoint by importing `mlx_lm.models.<model_type>` and ships
    no `qwen2_audio`. Nothing here loads an audio-language model, and the
    honest rendering of that is a label with no Load.
    """
    repo = _repo(hub, "models--Qwen--Qwen2-Audio-7B-Instruct", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json", json.dumps(
        {"architectures": ["Qwen2AudioForConditionalGeneration"],
         "model_type": "qwen2_audio", "audio_config": {}}))
    _snapshot_file(repo, "c1", "model.safetensors", _safetensors({"w": (8, 8)}))
    row = _repo_row(client, "Qwen/Qwen2-Audio-7B-Instruct")
    assert row["task"] == "audio + text to text"
    assert row["capability"] is None
    assert row["engine"] is None


@requires_symlinks
def test_a_text_only_encoder_decoder_is_still_text_to_text(client, hub):
    """The other half of the same branch: no vision and no audio is a T5, and
    reading THAT as a chat model would put a Load button on a translation
    model the text runner would then fail to use."""
    repo = _repo(hub, "models--org--t5", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json", json.dumps(
        {"architectures": ["T5ForConditionalGeneration"], "model_type": "t5"}))
    row = _repo_row(client, "org/t5")
    assert row["task"] == "translation"
    assert row["capability"] is None
    # …and it SAYS so, rather than leaving a null the card has to guess about.
    assert row["support"] == "no-runner"
    assert row["supportReason"]


@requires_symlinks
def test_diffusers_and_sentence_transformers_are_recognised(client, hub):
    a = _repo(hub, "models--org--sd", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(a, "c1", "model_index.json", json.dumps({"_class_name": "StableDiffusionXLPipeline"}))
    b = _repo(hub, "models--org--st", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(b, "c1", "modules.json", "[]")
    assert _repo_row(client, "org/sd")["task"] == "text to image"
    assert _repo_row(client, "org/sd")["library"] == "diffusers"
    assert _repo_row(client, "org/st")["task"] == "embeddings"


@requires_symlinks
def test_parameter_count_sums_the_safetensors_shapes(client, hub):
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    # Two shards, as a real multi-GB model is stored.
    _snapshot_file(repo, "c1", "model-00001-of-00002.safetensors",
                   _safetensors({"embed": (32000, 4096), "layer0": (4096, 4096)}))
    _snapshot_file(repo, "c1", "model-00002-of-00002.safetensors",
                   _safetensors({"layer1": (4096, 4096), "norm": (4096,)}))
    expected = 32000 * 4096 + 4096 * 4096 * 2 + 4096
    assert _repo_row(client, "org/m")["params"] == expected


@requires_symlinks
def test_no_parameter_count_is_reported_rather_than_a_guess(client, hub):
    # .bin pickles and .gguf carry no cheap header, so the count is absent —
    # never estimated from the file size.
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"pytorch_model.bin": "w"}},
                 refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json", json.dumps({"architectures": ["LlamaForCausalLM"]}))
    row = _repo_row(client, "org/m")
    assert row["params"] is None
    assert row["task"] == "text generation"


@requires_symlinks
def test_a_truncated_safetensors_header_is_survived(client, hub):
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "model.safetensors", _safetensors({"a": (8, 8)})[:12])
    assert _repo_row(client, "org/m")["params"] is None


@requires_symlinks
def test_the_default_revision_is_the_one_described(client, hub):
    repo = _repo(
        hub,
        "models--org--m",
        blobs={"w": 10},
        snapshots={"old": {"m": "w"}, "new": {"m": "w"}},
        refs={"main": "new"},
    )
    _snapshot_file(repo, "old", "config.json", json.dumps({"architectures": ["BertForMaskedLM"]}))
    _snapshot_file(repo, "new", "config.json", json.dumps({"architectures": ["LlamaForCausalLM"]}))
    assert _repo_row(client, "org/m")["task"] == "text generation"


@requires_symlinks
def test_reading_the_metadata_does_not_count_as_using_the_model(client, hub):
    # Same rule as the ref files: a model card and a safetensors header are
    # reached THROUGH the snapshot symlink, so reading them touches the blob's
    # atime — the signal prune is built on.
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: text-generation\n---\n")
    _snapshot_file(repo, "c1", "model.safetensors", _safetensors({"a": (4, 4)}))
    old = 1_000_000
    for path in (repo / "blobs" / "w", repo / "refs" / "main",
                 repo / "snapshots" / "c1" / "README.md",
                 repo / "snapshots" / "c1" / "model.safetensors"):
        os.utime(path, (old, old))
    assert _repo_row(client, "org/m")["task"] == "text generation"
    assert _repo_row(client, "org/m")["lastUsed"] == old


def test_added_is_the_oldest_file_not_the_newest(client, hub):
    repo = _repo(hub, "models--org--m", blobs={"first": 10, "later": 10})
    os.utime(repo / "blobs" / "first", (5_000_000, 1_000_000))
    os.utime(repo / "blobs" / "later", (5_000_000, 8_000_000))
    row = _repo_row(client, "org/m")
    # "added" is when the repo first landed here — deliberately not the Hub's
    # release date, which is not on this disk at all.
    assert row["added"] == 1_000_000
    assert row["mtime"] == 8_000_000


@requires_symlinks
def test_diffusers_weights_in_component_subfolders_are_counted(client, hub):
    # A pipeline keeps its weights per component, which is exactly the layout
    # behind the repos whose task we detect from model_index.json — a top-level
    # look would answer "no count" for the models people most want it for.
    repo = _repo(hub, "models--org--flux", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "model_index.json", json.dumps({"_class_name": "FluxPipeline"}))
    _snapshot_file(repo, "c1", "transformer/diffusion_pytorch_model.safetensors",
                   _safetensors({"blocks": (12000, 1_000_000)}))
    _snapshot_file(repo, "c1", "text_encoder/model.safetensors", _safetensors({"emb": (32000, 4096)}))
    _snapshot_file(repo, "c1", "vae/diffusion_pytorch_model.safetensors", _safetensors({"conv": (512, 512)}))
    row = _repo_row(client, "org/flux")
    assert row["task"] == "text to image"
    assert row["params"] == 12000 * 1_000_000 + 32000 * 4096 + 512 * 512


@requires_symlinks
def test_a_precision_variant_is_not_counted_twice(client, hub):
    # fp16 variants sit beside the file they are a variant of; both hold the
    # same tensors, so a repo that pulled both must not report double.
    repo = _repo(hub, "models--org--sd", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "unet/diffusion_pytorch_model.safetensors", _safetensors({"a": (1000, 1000)}))
    _snapshot_file(repo, "c1", "unet/diffusion_pytorch_model.fp16.safetensors", _safetensors({"a": (1000, 1000)}))
    assert _repo_row(client, "org/sd")["params"] == 1000 * 1000


@requires_symlinks
def test_a_lone_variant_still_counts(client, hub):
    # …but a repo that only ever pulled the fp16 build has no plain counterpart
    # to prefer, and must not report nothing.
    repo = _repo(hub, "models--org--sd", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "unet/diffusion_pytorch_model.fp16.safetensors", _safetensors({"a": (1000, 1000)}))
    assert _repo_row(client, "org/sd")["params"] == 1000 * 1000


def test_a_hardlinked_weight_alias_counts_once(client, hub):
    # The alias rule keys on the file's identity, not its resolved path: a
    # resolved path collapses a symlink onto its target but leaves two
    # HARDLINKS looking like two files — and a cache written where symlinks
    # were unavailable is exactly where the aliases are hardlinks.
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    snap = repo / "snapshots" / "c1"
    (snap / "model.safetensors").write_bytes(_safetensors({"a": (128, 128)}))
    try:
        os.link(snap / "model.safetensors", snap / "consolidated.safetensors")
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support hard links")
    assert _repo_row(client, "org/m")["params"] == 128 * 128


@requires_symlinks
def test_the_same_blob_under_two_names_counts_once(client, hub):
    repo = _repo(hub, "models--org--m", blobs={"shared": 4096}, snapshots={"c1": {"m": "shared"}},
                 refs={"main": "c1"})
    (repo / "blobs" / "shared").write_bytes(_safetensors({"a": (64, 64)}))
    snap = repo / "snapshots" / "c1"
    os.symlink(repo / "blobs" / "shared", snap / "model.safetensors")
    os.symlink(repo / "blobs" / "shared", snap / "consolidated.safetensors")
    assert _repo_row(client, "org/m")["params"] == 64 * 64


# -- quantized checkpoints -------------------------------------------------------
# A 4-bit checkpoint bit-packs eight weights into each U32, so summing shapes
# counts storage slots: mlx-community/gemma-3-12b-it-4bit reported 2.4B for a
# 12B model. That is not a small error but a different number.


def _quantized_safetensors(shapes_by_dtype):
    header = {}
    for dtype, shapes in shapes_by_dtype.items():
        for name, shape in shapes.items():
            header[name] = {"dtype": dtype, "shape": list(shape), "data_offsets": [0, 0]}
    header["__metadata__"] = {"format": "mlx"}
    blob = json.dumps(header).encode()
    return len(blob).to_bytes(8, "little") + blob


@requires_symlinks
def test_a_4bit_checkpoint_reports_weights_not_storage_slots(client, hub):
    repo = _repo(hub, "models--mlx-community--m-4bit", blobs={"w": 10},
                 snapshots={"c1": {"x": "w"}}, refs={"main": "c1"})
    # MLX's own shape: packed weights in U32, scales/biases in F16 beside them.
    _snapshot_file(repo, "c1", "config.json",
                   json.dumps({"architectures": ["Gemma3ForCausalLM"], "model_type": "gemma3",
                               "quantization": {"group_size": 64, "bits": 4}}))
    _snapshot_file(repo, "c1", "model.safetensors", _quantized_safetensors({
        "U32": {"layers.0.weight": (4096, 512)},   # 512 words × 8 weights = 4096 per row
        "F16": {"layers.0.scales": (4096, 64)},
    }))
    row = _repo_row(client, "org" if False else "mlx-community/m-4bit")
    # 4096*512 storage slots × 8 weights each, plus the unpacked scales.
    assert row["params"] == 4096 * 512 * 8 + 4096 * 64
    assert row["paramsEstimated"] is True
    assert row["quantization"] == "4-bit"


@requires_symlinks
def test_an_unquantized_checkpoint_is_not_marked_estimated(client, hub):
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"x": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json", json.dumps({"architectures": ["LlamaForCausalLM"]}))
    _snapshot_file(repo, "c1", "model.safetensors", _safetensors({"a": (4096, 4096)}))
    row = _repo_row(client, "org/m")
    assert row["params"] == 4096 * 4096
    assert row["paramsEstimated"] is False
    assert row["quantization"] is None


@requires_symlinks
@pytest.mark.parametrize(
    "config_block,expected_bits",
    [
        ({"quantization": {"bits": 8}}, "8-bit"),
        ({"quantization_config": {"bits": 4, "quant_method": "gptq"}}, "4-bit"),
        ({"quantization_config": {"load_in_4bit": True, "quant_method": "bitsandbytes"}}, "4-bit"),
        ({"quantization_config": {"load_in_8bit": True}}, "8-bit"),
        ({"quantization_config": {"w_bit": 4, "quant_method": "awq"}}, "4-bit"),
    ],
)
def test_the_declared_width_is_read_not_the_repo_name(client, hub, config_block, expected_bits):
    # `mlx-community/…-4bit` is a naming convention; a number this page prints
    # must rest on what the checkpoint says about itself, not on its name.
    repo = _repo(hub, "models--org--plain-name", blobs={"w": 10}, snapshots={"c1": {"x": "w"}},
                 refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json", json.dumps({"architectures": ["LlamaForCausalLM"], **config_block}))
    assert _repo_row(client, "org/plain-name")["quantization"] == expected_bits


@requires_symlinks
def test_quantization_is_read_even_when_the_card_named_the_task(client, hub):
    # The gemma case: the task came from the model card, so a config read that
    # only happened when the task was still unknown would miss the bit width.
    repo = _repo(hub, "models--org--vlm", blobs={"w": 10}, snapshots={"c1": {"x": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: image-text-to-text\n---\n")
    _snapshot_file(repo, "c1", "config.json", json.dumps({"quantization": {"bits": 4}}))
    row = _repo_row(client, "org/vlm")
    assert row["task"] == "image + text to text"  # not the unreadable "image text to text"
    assert row["quantization"] == "4-bit"


@requires_symlinks
def test_a_gguf_repo_is_named_but_not_given_a_task(client, hub):
    """No config.json, no card, no real GGUF header either — a fixture whose
    `.gguf` file is fake bytes, not a real one — and the library is all that
    can honestly be read off it.

    It used to say "text generation" here unconditionally, which is a guess
    about the MODALITY made from a CONTAINER, and it was wrong on the first
    image repo it met (`unsloth/FLUX.2-klein-4B-GGUF`, the quantized
    transformer the diffusers recipe fetches). Since SPEC AI-11 a GGUF whose
    OWN `general.architecture` metadata names a real causal-text model DOES
    get a task and a Load button (`llamacpp-text`) —
    `test_ai_runtime.py::test_a_cached_gguf_repo_now_loads_as_text_via_llamacpp`
    covers that with a real header. This fixture's file is not one (no magic
    bytes to read at all), which is why it still reads exactly as it did
    before that runner existed: nothing here can tell what it is.
    """
    repo = _repo(hub, "models--TheBloke--m-GGUF", blobs={"w": 10},
                 snapshots={"c1": {"model.Q4_K_M.gguf": "w"}}, refs={"main": "c1"})
    assert repo.exists()
    row = _repo_row(client, "TheBloke/m-GGUF")
    assert row["task"] is None
    assert row["taskSource"] is None
    assert row["library"] == "gguf"
    assert row["capability"] is None
    assert row["engine"] is None
    assert row["params"] is None  # no cheap header to read


@requires_symlinks
@pytest.mark.parametrize(
    "class_name,expected,tag",
    [
        ("StableVideoDiffusionPipeline", "video generation", "text-to-video"),
        ("MusicGenPipeline", "audio generation", "text-to-audio"),
        ("AudioLDM2Pipeline", "audio generation", "text-to-audio"),
        ("StableDiffusionPipeline", "text to image", "text-to-image"),
    ],
)
def test_a_diffusers_pipeline_names_its_medium(client, hub, class_name, expected, tag):
    """…in the Hub's own vocabulary. A pipeline read from `model_index.json` and
    a card that names the same thing must classify identically, which is why
    this path emits a TAG and the label comes from the one table."""
    repo = _repo(hub, "models--org--p", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "model_index.json", json.dumps({"_class_name": class_name}))
    row = _repo_row(client, "org/p")
    assert row["task"] == expected
    assert row["taskTag"] == tag


@requires_symlinks
def test_a_card_without_front_matter_falls_through_to_the_config(client, hub):
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "# Just a heading, no front matter\n")
    _snapshot_file(repo, "c1", "config.json", json.dumps({"architectures": ["LlamaForCausalLM"]}))
    row = _repo_row(client, "org/m")
    assert row["task"] == "text generation"
    assert row["taskSource"] == "the architecture in config.json"


@requires_symlinks
def test_without_a_main_ref_the_newest_snapshot_describes_the_repo(client, hub):
    repo = _repo(hub, "models--org--m", blobs={"w": 10},
                 snapshots={"old": {"m": "w"}, "new": {"m": "w"}}, refs={})
    _snapshot_file(repo, "old", "config.json", json.dumps({"architectures": ["BertForMaskedLM"]}))
    _snapshot_file(repo, "new", "config.json", json.dumps({"architectures": ["LlamaForCausalLM"]}))
    os.utime(repo / "snapshots" / "old", (1_000_000, 1_000_000))
    assert _repo_row(client, "org/m")["task"] == "text generation"


@requires_symlinks
def test_metadata_is_read_once_per_snapshot(client, hub):
    # The cache's promise ("a Refresh over forty repos re-reads nothing") and
    # its accepted cost, in one test: the key is the snapshot directory's own
    # mtime, because a snapshot is immutable once written — so an in-place edit
    # of a file inside it is deliberately NOT noticed.
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    config = repo / "snapshots" / "c1" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"architectures": ["LlamaForCausalLM"]}))
    assert _repo_row(client, "org/m")["task"] == "text generation"

    before = os.stat(repo / "snapshots" / "c1")
    config.write_text(json.dumps({"architectures": ["BertForMaskedLM"]}))
    os.utime(repo / "snapshots" / "c1", (before.st_atime, before.st_mtime))
    assert _repo_row(client, "org/m")["task"] == "text generation"  # served from the cache

    os.utime(repo / "snapshots" / "c1", (before.st_atime, before.st_mtime + 10))
    assert _repo_row(client, "org/m")["task"] == "fill mask"  # the directory moved, so re-read


def test_a_target_that_is_not_an_object_is_reported_not_crashed(client, hub):
    _repo(hub, "models--real", blobs={"a": 100})
    data = _delete(client, ["models--real", 42, None]).json()
    assert data["freed"] == 0
    assert [f["error"] for f in data["failures"]] == ["target must be an object"] * 3
    assert [r["id"] for r in data["repos"]] == ["real"]


def test_a_target_the_filesystem_refuses_does_not_lose_the_batch(client, hub, monkeypatch):
    _repo(hub, "models--ok", blobs={"a": 100})
    held = _repo(hub, "models--held", blobs={"a": 500})
    real_rmtree = local_rmtree = __import__("shutil").rmtree

    def fake_rmtree(path, *args, **kwargs):
        if str(path) == str(held):
            raise PermissionError(13, "Permission denied")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(ai_models_mod.shutil, "rmtree", fake_rmtree)
    data = _delete(client, [{"dir": "models--held"}, {"dir": "models--ok"}]).json()
    # The one that could go, went; the one that could not is named.
    assert data["freed"] == 100
    assert [f["dir"] for f in data["failures"]] == ["models--held"]
    assert "Permission denied" in data["failures"][0]["error"]
    assert [r["id"] for r in data["repos"]] == ["held"]


# -- a cache other processes are writing ------------------------------------------
# The listing must never 500 because a download finalised or another window
# deleted something between the scandir that listed an entry and the stat that
# asked about it. A row fewer is a better answer than an error page.


class _Vanished:
    """A listing entry whose directory was removed before we could stat it."""

    def __init__(self, name, path):
        self.name, self.path = name, path

    def is_dir(self, follow_symlinks=True):
        raise FileNotFoundError(2, "No such file or directory")

    def stat(self, *, follow_symlinks=True):
        raise FileNotFoundError(2, "No such file or directory")


class _ScandirResult:
    """A stand-in for os.scandir's return value.

    It has to be an ITERATOR as well as a context manager: os.walk keeps the
    original object, enters it for cleanup, and then calls next() on that same
    object — so returning a plain list (or even an iterable) is not enough.
    """

    def __init__(self, entries):
        self._entries = list(entries)

    def append(self, entry):
        self._entries.append(entry)

    def __iter__(self):
        return self

    def __next__(self):
        if not self._entries:
            raise StopIteration
        return self._entries.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        self._entries.clear()


def _with_vanished_entry(monkeypatch, inside: str, name: str):
    real_scandir = os.scandir

    def fake_scandir(path=".", *args, **kwargs):
        entries = _ScandirResult(real_scandir(path, *args, **kwargs))
        if str(path) == str(inside):
            entries.append(_Vanished(name, os.path.join(str(inside), name)))
        return entries

    monkeypatch.setattr(ai_models_mod.os, "scandir", fake_scandir)


def test_a_repo_that_vanishes_mid_listing_costs_one_row_not_the_page(client, hub, monkeypatch):
    _repo(hub, "models--survivor", blobs={"a": 100})
    _with_vanished_entry(monkeypatch, hub, "models--gone")
    data = _get(client)
    assert [r["id"] for r in data["repos"]] == ["survivor"]


@requires_symlinks
def test_a_revision_that_vanishes_mid_scan_costs_one_revision(client, hub, monkeypatch):
    repo = _repo(hub, "models--org--m", blobs={"a": 100}, snapshots={"c1": {"m": "a"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json", json.dumps({"architectures": ["LlamaForCausalLM"]}))
    _with_vanished_entry(monkeypatch, repo / "snapshots", "c2")
    row = _repo_row(client, "org/m")
    assert row["revisions"] == 1
    # …and the surviving revision still describes the repo.
    assert row["task"] == "text generation"


@requires_symlinks
def test_a_task_carries_a_sentence_explaining_what_it_means(client, hub):
    # "image + text to text" is the Hub's vocabulary, which is jargon until
    # someone says what goes in and what comes out.
    repo = _repo(hub, "models--org--vlm", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: image-text-to-text\n---\n")
    row = _repo_row(client, "org/vlm")
    assert row["task"] == "image + text to text"
    assert "image AND a prompt" in row["taskHelp"]


@requires_symlinks
def test_an_architecture_derived_task_is_explained_too(client, hub):
    # One glossary serves both paths: the label is the key, not the raw tag.
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json", json.dumps({"architectures": ["BertForMaskedLM"]}))
    row = _repo_row(client, "org/m")
    assert row["task"] == "fill mask"
    assert row["taskHelp"].startswith("Fills in blanked-out words")


@requires_symlinks
def test_a_tag_this_build_never_heard_of_still_shows_its_label_and_source(client, hub):
    """The Hub's vocabulary GROWS, and this table is a snapshot of it — so an
    unvendored tag degrades to label + provenance rather than to nothing.

    What it must not degrade to is a capability. `support` says `unknown`, which
    is a different answer from the ruled-out tag below and is what stops the
    format fallbacks from filing it under whichever runner reads the bytes."""
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: holographic-telepathy\n---\n")
    row = _repo_row(client, "org/m")
    assert row["task"] == "holographic telepathy"
    assert row["taskTag"] == "holographic-telepathy"
    assert row["taskSource"] == "the model card's pipeline_tag"
    assert row["taskHelp"] is None
    assert row["support"] == "unknown"
    assert row["capability"] is None


@requires_symlinks
def test_a_task_nothing_here_runs_says_which_and_why(client, hub):
    """The state the page could not draw before: not a missing button, a
    sentence. `graph-ml` is a task we recognise perfectly well and serve not at
    all, and telling that apart from "we have never heard of this" is the whole
    reason `support` has three values."""
    repo = _repo(hub, "models--org--g", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: graph-ml\n---\n")
    row = _repo_row(client, "org/g")
    assert row["task"] == "graph machine learning"
    assert row["support"] == "no-runner"
    assert row["supportReason"] == "Nothing on this machine runs graph machine learning models."
    assert row["capability"] is None
    # …and the glossary still explains the TASK, which is a different question
    # from whether we run it.
    assert row["taskHelp"]


def test_every_tag_this_module_can_produce_is_in_the_vocabulary():
    """The evidence paths here INVENT nothing: each one answers with a Hub tag,
    and every tag it can answer with is a row in `ai/tasks.py`.

    This replaces two tests that enumerated the prose labels the module could
    produce and checked each was explained and classified. Both were sound until
    the tag-to-label step grew a passthrough — after which the enumeration was a
    fiction, and `reinforcement-learning` walked through the hole and took a
    Load button with it. Keyed on the tag now, which is the value that actually
    travels: a producer emitting something unvendored fails HERE, where it is
    one line to classify, rather than on somebody's card.
    """
    produced = {task for _, task in ai_models_mod._ARCH_TASKS}
    produced |= {ai_models_mod._diffusers_task(name) for name in
                 ("StableDiffusionPipeline", "StableVideoDiffusionPipeline", "MusicGenPipeline")}
    # The branches that answer without a table: sentence-transformers, and the
    # decisive weight layouts.
    produced |= {"feature-extraction"}
    produced |= {found[0] for found in (
        ai_models_mod._format_task("mlx-community/FLUX.2-Klein-4B-4bit", set(),
                                   {"transformer", "text_encoder", "vae"}, {}),
        ai_models_mod._format_task("org/w", {"model.bin", "vocabulary.txt"}, set(), {}),
    ) if found}
    unvendored = sorted(tag for tag in produced if tag not in ai_tasks.TASKS)
    assert not unvendored, f"tags this module emits that nothing classifies: {unvendored}"


def test_a_vision_language_model_is_still_loadable_as_a_chat_model(client, hub):
    """The gemma-3 case, end to end.

    A multimodal checkpoint is labelled "image + text to text" because it CAN
    take a picture; it is still the causal LM the text runner loads when you
    only give it text. The card must offer Load, or the page is refusing a
    model the catalog recommends.
    """
    repo = _repo(hub, "models--mlx-community--gemma-3-12b-it-4bit", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: image-text-to-text\n---\n")
    row = _repo_row(client, "mlx-community/gemma-3-12b-it-4bit")
    assert row["task"] == "image + text to text"
    assert row["capability"] == "text-generation"


def test_every_suggested_model_could_be_loaded_by_the_page():
    """The catalog and the card must not disagree about the same model.

    Discover recommending a model that the Local tab then refuses to load is
    the app contradicting itself, and it is what a user actually hit.
    """
    from fused_render.ai import catalog

    for code, entries in catalog.SUGGESTIONS.items():
        runner = _ai_registry.by_code(code)
        assert runner is not None, f"{code!r} suggests models and is not a runner"
        assert runner.capability in {ai_tasks.TASKS[t].capability
                                     for t in ai_tasks.supported_tags()}, (
            f"nothing in the task vocabulary maps to {runner.capability!r}, so no "
            f"cached card will ever offer Load for the models suggested under it"
        )
        assert entries, f"{code} suggests nothing"


@requires_symlinks
def test_both_evidence_paths_agree_on_the_label(client, hub):
    # Same model, same concept: whichever evidence answers, the card reads the
    # same and the hover explains it.
    card = _repo(hub, "models--org--from-card", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(card, "c1", "README.md", "---\npipeline_tag: automatic-speech-recognition\n---\n")
    config = _repo(hub, "models--org--from-config", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(config, "c1", "config.json",
                   json.dumps({"architectures": ["WhisperForConditionalGeneration"], "model_type": "whisper"}))
    from_card, from_config = _repo_row(client, "org/from-card"), _repo_row(client, "org/from-config")
    assert from_card["task"] == from_config["task"] == "speech recognition"
    assert from_card["taskHelp"] == from_config["taskHelp"]
    # …while still reporting which evidence answered.
    assert from_card["taskSource"] == "the model card's pipeline_tag"
    assert from_config["taskSource"] == "the architecture in config.json"


# -- which capability could LOAD this (SPEC §40) ---------------------------------
# Answered HERE rather than in the page. The task vocabulary and the capability
# vocabulary both live on this side; a page deciding for itself would need a
# second copy of the mapping, and the first version of that page guessed
# "text-generation" for every cached repo — offering to load a dataset as a chat
# model, and a diffusion model as one too.


@requires_symlinks
def test_a_repo_reports_the_capability_that_could_load_it(client, hub):
    text = _repo(hub, "models--org--chat", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(text, "c1", "config.json",
                   json.dumps({"architectures": ["LlamaForCausalLM"], "model_type": "llama"}))
    image = _repo(hub, "models--org--sd", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(image, "c1", "model_index.json", json.dumps({"_class_name": "StableDiffusionXLPipeline"}))
    assert _repo_row(client, "org/chat")["capability"] == "text-generation"
    assert _repo_row(client, "org/sd")["capability"] == "text-to-image"


@requires_symlinks
def test_a_repo_no_runner_serves_reports_no_capability(client, hub):
    """None is the honest answer, and the page turns it into "no Load button" —
    rather than a button that is offered and always fails.

    **`image-feature-extraction`, and the tag had to change twice.** This test
    first used the module's own `modules.json` detection, which sets `meta.task`
    to the bare string "embeddings" — a label the embedding runners now serve, so
    that stopped being an unserved task. It then used `sentence-similarity`,
    which was unserved for as long as the capability meant DUAL ENCODERS only.
    SPEC §40's widening claimed that one too: both engines load a prose encoder
    now, so a sentence-transformers checkpoint is genuinely loadable and
    reporting `None` for it would be the lie this test exists to prevent, in
    reverse.

    `image-feature-extraction` is the neighbour that did NOT move, and it is
    unserved for a structural reason rather than a scheduling one: it wears an
    IMAGE-ONLY encoder (DINOv2/v3), which has no text tower at all — the dual
    load path wants both towers and the prose path wants a tokenizer, so neither
    can open one. That makes it a stable choice here rather than the next tag to
    be claimed.
    """
    embed = _repo(hub, "models--org--st", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(embed, "c1", "README.md", "---\npipeline_tag: image-feature-extraction\n---\n")
    assert _repo_row(client, "org/st")["task"] == "image embeddings"
    assert _repo_row(client, "org/st")["capability"] is None


@requires_symlinks
def test_a_downloaded_repo_absent_from_the_suggestion_catalog_still_appears_in_the_listing(
        client, hub):
    """The Discover tab's Hub search can download ANY repo, curated or not.

    This listing is the only place a user could then see it, and it is what
    `cached_models()` — and through it `/api/ai/catalog` — reads to put the
    same repo in a page's model picker (D323). So the listing must not quietly
    restrict itself to the curation: pinned here because a "show only what we
    recommend" filter looks like a tidy-up and is the whole bug.
    """
    repo_id = "some-org/a-model-nobody-curated"
    assert repo_id not in catalog.all_suggested_ids()
    repo = _repo(hub, "models--some-org--a-model-nobody-curated", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json",
                   json.dumps({"architectures": ["LlamaForCausalLM"], "model_type": "llama"}))
    row = _repo_row(client, repo_id)
    assert row["kind"] == "model"
    assert row["capability"] == _ai_registry.TEXT_GENERATION


@requires_symlinks
def test_a_dataset_is_never_loadable(client, hub):
    # A dataset folder can carry a config.json that reads like a model's. The
    # kind is what settles it: nothing here loads a dataset into a text runner.
    data = _repo(hub, "datasets--org--corpus", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(data, "c1", "config.json",
                   json.dumps({"architectures": ["LlamaForCausalLM"], "model_type": "llama"}))
    assert _repo_row(client, "org/corpus")["capability"] is None


# -- which ENGINE could load this ------------------------------------------------
# The card's most useful fact, and the one it did not carry: in this app a repo
# belongs to a BACKEND, not to a capability, and the three mutually unloadable
# formats of Whisper are the standing proof. The detection reads the same
# evidence each worker's own `load()` checks (`ai/runners/formats.py`), so a
# card cannot offer a Load the runner then refuses.


def _engine(client, repo_id):
    return _repo_row(client, repo_id)["engine"]


@requires_symlinks
def test_a_ctranslate2_whisper_repo_is_recognised_and_loadable(client, hub, monkeypatch):
    """`deepdml/faster-whisper-large-v3-turbo-ct2` — this app's own recommended
    speech model everywhere but Apple Silicon — showed no task line and no Load
    button, because a CT2 conversion carries no pipeline_tag and no
    `architectures`. Its LAYOUT is the evidence, and it is the same layout
    `faster_whisper/worker.py` checks before it loads."""
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "x86_64")
    repo = _repo(hub, "models--deepdml--faster-whisper-large-v3-turbo-ct2",
                 blobs={"w": 10}, snapshots={"c1": {"model.bin": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json",
                   json.dumps({"alignment_heads": [[1, 2]], "lang_ids": [50259]}))
    row = _repo_row(client, "deepdml/faster-whisper-large-v3-turbo-ct2")
    assert row["task"] == "speech recognition"
    assert row["capability"] == _ai_registry.SPEECH_TO_TEXT
    assert row["engine"]["code"] == "faster-whisper"
    assert row["engine"]["available"] is True


@requires_symlinks
def test_an_mlx_whisper_repo_names_the_engine_it_needs_off_a_mac(client, hub, monkeypatch):
    """"Nothing here reads this" and "what reads it does not run here" are
    different sentences, and only the second tells a Windows user that the
    download was not a mistake — it is a Mac's model."""
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "AMD64")
    repo = _repo(hub, "models--mlx-community--whisper-large-v3-turbo", blobs={"w": 10},
                 snapshots={"c1": {"weights.npz": "w"}}, refs={"main": "c1"})
    engine = _engine(client, "mlx-community/whisper-large-v3-turbo")
    assert engine["code"] == "mlx-whisper"
    assert engine["available"] is False
    assert "Apple Silicon" in engine["reason"]


@requires_symlinks
def test_a_gguf_only_repo_is_not_called_a_text_model(client, hub):
    """`unsloth/FLUX.2-klein-4B-GGUF` is an IMAGE model — and in this app it is
    not even a model, it is the quantized transformer the diffusers recipe
    fetches for FLUX.2 klein. It read "Text generation" with a Load button,
    which is the exact failure `capability_for_task` warns about: a GGUF file
    is a container, not a modality. `formats.component()` is what excludes
    THIS repo specifically (it is a COMPONENT, never a `load()` target on its
    own); a GGUF repo that is not a known component additionally needs its
    own `general.architecture` metadata to name a real causal-text model
    before `llamacpp-text` (SPEC AI-11) calls it decisively text — the
    fixture's file has none, so it reads the same either way."""
    _repo(hub, "models--unsloth--FLUX.2-klein-4B-GGUF", blobs={"w": 10},
          snapshots={"c1": {"flux-2-klein-4b-Q4_K_M.gguf": "w"}}, refs={"main": "c1"})
    row = _repo_row(client, "unsloth/FLUX.2-klein-4B-GGUF")
    assert row["task"] is None
    assert row["capability"] is None
    assert row["library"] == "gguf"
    assert row["engine"] is None


@requires_symlinks
def test_the_apps_own_recommended_image_model_is_loadable(client, hub, monkeypatch):
    """`black-forest-labs/FLUX.2-klein-4B` is the FLUX.2 base pipeline the
    diffusers runner loads by id (it has a `_GGUF_RECIPES` row, and was
    `catalog.py`'s second diffusers suggestion until the int8 repo made it
    redundant), and its card's `pipeline_tag` says image-to-image — a label in
    NO_RUNNER_YET, so the page offered no Load for a model a user can reach. The card names the model FAMILY; the
    `model_index.json` in the snapshot names the pipeline that is actually
    here, written by the library that will load it."""
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "x86_64")
    repo = _repo(hub, "models--black-forest-labs--FLUX.2-klein-4B", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: image-to-image\n---\n")
    _snapshot_file(repo, "c1", "model_index.json",
                   json.dumps({"_class_name": "Flux2KleinPipeline"}))
    row = _repo_row(client, "black-forest-labs/FLUX.2-klein-4B")
    assert row["task"] == "text to image"
    assert row["capability"] == _ai_registry.IMAGE_GENERATION
    assert row["engine"]["code"] == "diffusers-image"


@requires_symlinks
def test_the_engine_payload_carries_the_family_name_beside_the_hardware_one(
        client, hub, monkeypatch):
    """Three names on the wire, because the card wants two different things.

    The card's TAG is a format claim, so it wears the family ("Diffusers") —
    every Diffusers row reads the identical safetensors and "(CPU)" on a tag
    describing a file on disk is noise plus a leak of which machine is reading
    it. The tag's hover keeps the hardware-qualified `shortLabel`, so nothing
    is lost. Both are asserted here rather than only in the registry because
    the payload is built at TWO sites in this router — the serving engine and
    the engine that merely reads the format — and one of them shipped without
    a key before now.
    """
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "x86_64")
    repo = _repo(hub, "models--black-forest-labs--FLUX.2-klein-4B", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "model_index.json",
                   json.dumps({"_class_name": "Flux2KleinPipeline"}))
    # The SERVING site: Diffusers CPU is the image engine on Linux.
    serving = _engine(client, "black-forest-labs/FLUX.2-klein-4B")
    assert serving["shortLabel"] == "Diffusers (CPU)"
    assert serving["familyLabel"] == "Diffusers"
    # …and the OTHER site: on a Mac the image engine is MLX FLUX, so this repo
    # comes back naming the engine that reads it with `available: false`. Same
    # row, same two names — a payload missing the key here would leave the tag
    # blank on exactly the cards that need explaining.
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "arm64")
    other = _engine(client, "black-forest-labs/FLUX.2-klein-4B")
    assert other["code"] == "diffusers-image" and other["available"] is False
    assert other["shortLabel"] == "Diffusers (CPU)"
    assert other["familyLabel"] == "Diffusers"


@requires_symlinks
def test_a_repo_the_OTHER_engine_reads_is_not_offered_a_load(client, hub, monkeypatch):
    """A capability holds one resident model and the registry picks which
    backend loads it — so on a Mac, whose image engine is MLX FLUX, a Diffusers
    repo is not loadable today however available the Diffusers runner is. The
    Load would reach mflux, which refuses it by name.

    The reason names the remedy, because there is one and it is one switch
    away: this is not a machine that cannot run the model."""
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "arm64")
    repo = _repo(hub, "models--black-forest-labs--FLUX.2-klein-4B", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "model_index.json",
                   json.dumps({"_class_name": "Flux2KleinPipeline"}))
    engine = _engine(client, "black-forest-labs/FLUX.2-klein-4B")
    assert engine["code"] == "diffusers-image"
    assert engine["available"] is False
    # Names the ENGINES TAB, which is on this same page since the engine picker
    # moved off Preferences. Asserted on the destination rather than loosened to
    # "there is a remedy in here somewhere": the whole value of the sentence is
    # that it points somewhere real, and a stale direction is worse than none.
    assert "MLX FLUX" in engine["reason"] and "Engines tab" in engine["reason"]


@requires_symlinks
def test_the_two_FLUX_klein_repos_read_the_same_and_the_label_matches_the_gate(
        client, hub, monkeypatch):
    """One model, two conversions, two cards in the same row — and they used to
    disagree about what it was. The diffusers repo says "Image generation" off
    its `model_index.json`; the MLX conversion has no config.json at all, so
    its card's `image-to-image` tag was the only evidence and stood, giving one
    model two labels side by side.

    It also made the Load button contradict the label: `image to image` is in
    NO_RUNNER_YET, yet the card offered Load, because the GATE reads the
    format (which is an mflux image model this machine serves) while the LABEL
    read the card. The gate was right and the label was stale, so decisive
    format evidence now settles the label too — and the two can no longer
    disagree, which is what the last assertion pins.
    """
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "arm64")
    mlx = _repo(hub, "models--mlx-community--FLUX.2-Klein-4B-4bit", blobs={"w": 10},
                snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(mlx, "c1", "README.md", "---\npipeline_tag: image-to-image\n---\n")
    for component in ("transformer", "text_encoder", "vae"):
        _snapshot_file(mlx, "c1", f"{component}/weights.safetensors",
                       _safetensors({"w": (4, 4)}))
    row = _repo_row(client, "mlx-community/FLUX.2-Klein-4B-4bit")
    assert row["task"] == "text to image"
    assert row["capability"] == _ai_registry.IMAGE_GENERATION
    assert row["engine"]["code"] == "mflux-image" and row["engine"]["available"]
    # The invariant behind both halves: a card that offers Load shows a task
    # this machine can actually serve.
    assert ai_tasks.capability_for_tag(row["taskTag"]) == row["capability"]


@requires_symlinks
def test_no_card_offers_a_load_under_a_task_the_app_cannot_serve(client, hub, monkeypatch):
    """The invariant behind (b) and (c), over a cache of every shape at once.

    Load is gated on the ENGINE — the weight format, plus whether a runner for
    it runs here — while the task line is read from the model card. Those are
    two different sources for one claim, and when they disagreed the card said
    "image to image" (a task in NO_RUNNER_YET) above a working Load button.

    Pinned as a property rather than as the one repo that showed it, because
    the next disagreement will be a different repo: for every row on the page,
    a Load offered means a task this machine can actually serve.
    """
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "arm64")
    # An MLX diffusion conversion whose card claims img2img…
    mlx = _repo(hub, "models--mlx-community--FLUX.2-Klein-4B-4bit", blobs={"w": 10},
                snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(mlx, "c1", "README.md", "---\npipeline_tag: image-to-image\n---\n")
    for component in ("transformer", "text_encoder", "vae"):
        _snapshot_file(mlx, "c1", f"{component}/weights.safetensors", _safetensors({"w": (4, 4)}))
    # …a CT2 conversion with no card at all…
    ct2 = _repo(hub, "models--deepdml--faster-whisper-large-v3-turbo-ct2", blobs={"w": 10},
                snapshots={"c1": {"model.bin": "w"}}, refs={"main": "c1"})
    _snapshot_file(ct2, "c1", "config.json", json.dumps({"alignment_heads": [[1, 2]]}))
    # …an audio-language model nothing here loads…
    audio = _repo(hub, "models--Qwen--Qwen2-Audio-7B-Instruct", blobs={"w": 10},
                  snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(audio, "c1", "config.json", json.dumps(
        {"architectures": ["Qwen2AudioForConditionalGeneration"],
         "model_type": "qwen2_audio", "audio_config": {}}))
    _snapshot_file(audio, "c1", "model.safetensors", _safetensors({"w": (8, 8)}))
    # …a GGUF-only repo, and an embedding model.
    _repo(hub, "models--unsloth--FLUX.2-klein-4B-GGUF", blobs={"w": 10},
          snapshots={"c1": {"m.gguf": "w"}}, refs={"main": "c1"})
    st = _repo(hub, "models--org--st", blobs={"w": 10}, snapshots={"c1": {"m": "w"}},
               refs={"main": "c1"})
    _snapshot_file(st, "c1", "modules.json", "[]")

    rows = _get(client)["repos"]
    assert len(rows) == 5
    for row in rows:
        if not (row["engine"] and row["engine"]["available"]):
            continue  # no Load button, so nothing to contradict
        assert ai_tasks.capability_for_tag(row["taskTag"]) == row["capability"], row


@requires_symlinks
def test_a_decisive_format_cannot_overrule_a_task_we_have_ruled_out(client, hub, monkeypatch):
    """The general gate, exercised against a still-ruled-out video task.

    `text-to-video` stopped being an example of a ruled-out task once
    `ltx-video` shipped (SPEC §40's LTX-2.3 plan) — every
    diffusers class name containing "video" maps to that tag
    (`_diffusers_task`), and it is genuinely SUPPORTED now (see the test
    right below this one for that new, correct shape). `image-to-video` is
    still ruled out — no runner here is image-conditioned — so it is the
    example now: the `FL2VA/` layout is DECISIVE about the format (D468
    dropped the runner that read it, but `formats.loaders` still returns
    early on it — see that function's own comment), and without this guard
    `_engine`'s "let the format answer"
    branch would resurrect the ruled-out task into video generation just
    because a decisive format happens to sit beside it. The branch itself is
    not removed — it fires for a real case (a CT2 conversion carries no tag,
    an MLX one carries no config, and both are speech models beyond doubt) —
    this pins that it fires ONLY where the task is UNKNOWN, never where it
    is refused with a sentence.
    """
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "arm64")
    repo = _repo(hub, "models--org--vid", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md",
                   "---\npipeline_tag: image-to-video\n---\n")
    _snapshot_file(repo, "c1", "FL2VA/model.safetensors", b"x")
    row = _repo_row(client, "org/vid")
    assert row["task"] == "image to video"
    assert row["support"] == "no-runner"
    assert row["capability"] is None
    # No engine row either: `_engine` returns nothing once the capability is
    # refused, so the card cannot promise a backend for it.
    assert row["engine"] is None
    # …and the same repo read by the LOAD route agrees, which is the invariant
    # that stops a card offering what a load then refuses.
    assert ai_models_mod.cached_capability("org/vid").capability is None


@requires_symlinks
def test_a_diffusers_video_pipeline_is_supported_but_has_no_engine(client, hub, monkeypatch):
    """The NEW, correct shape for a diffusers video pipeline, now that
    `text-to-video` genuinely has a runner (`ltx-video`).

    A `StableVideoDiffusionPipeline` snapshot's `_class_name` maps to
    `text-to-video` (`_diffusers_task`) — a real, SUPPORTED task, unlike the
    ruled-out case above. But `meta.loaders` for a `model_index.json` repo is
    `DIFFUSERS_RUNNERS` (image generation), and the video runner does not
    read THAT format at all (`has_ltx_split_layout` checks for a layout this
    repo does not have) — so `_engine` finds no
    VIDEO_GENERATION candidate among the format's own readers and correctly
    returns no engine. This is `_engine`'s own "task supported, format
    unreadable" trap (its docstring's `openai/whisper-large-v3` example),
    reachable through video for the first time: the card must say "video
    generation" without offering a Load button pointed at a runner that will
    refuse the file.
    """
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "arm64")
    repo = _repo(hub, "models--org--svd", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "model_index.json",
                   json.dumps({"_class_name": "StableVideoDiffusionPipeline"}))
    row = _repo_row(client, "org/svd")
    assert row["task"] == "video generation"
    assert row["support"] == "supported"
    assert row["capability"] == _ai_registry.VIDEO_GENERATION
    assert row["engine"] is None
    assert (ai_models_mod.cached_capability("org/svd").capability
            == _ai_registry.VIDEO_GENERATION)


@requires_symlinks
def test_a_genuine_image_to_image_repo_keeps_its_label_and_gets_no_load(client, hub):
    """The other side of the same rule: the override is for a task the app
    SERVES, read off decisive format evidence. A repo that really is img2img
    and carries no such evidence keeps the card's word for it, and gets no
    Load — nothing here does image-to-image."""
    repo = _repo(hub, "models--org--img2img", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: image-to-image\n---\n")
    _snapshot_file(repo, "c1", "model.safetensors", _safetensors({"w": (8, 8)}))
    row = _repo_row(client, "org/img2img")
    assert row["task"] == "image to image"
    assert row["capability"] is None
    assert row["engine"] is None


@requires_symlinks
def test_an_mlx_text_checkpoint_reports_the_mlx_engine(client, hub, monkeypatch):
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "arm64")
    repo = _repo(hub, "models--mlx-community--Qwen3-8B-4bit", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json", json.dumps(
        {"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3",
         "quantization": {"group_size": 64, "bits": 4}}))
    _snapshot_file(repo, "c1", "model.safetensors", _safetensors({"w": (8, 8)}))
    assert _engine(client, "mlx-community/Qwen3-8B-4bit")["code"] == "mlx-text"

    # The same checkpoint off a Mac: still MLX's, and nothing else here reads
    # it — an MLX `quantization` block with a `group_size` in it is bit-packed
    # for Metal kernels, which is what the removed transformers runner's
    # `_refuse_unloadable` raised about and why `loaders()` never offered it to
    # anything but `mlx-text`.
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "x86_64")
    engine = _engine(client, "mlx-community/Qwen3-8B-4bit")
    assert engine["code"] == "mlx-text" and engine["available"] is False


@requires_symlinks
def test_a_cards_engine_reason_comes_from_the_probe_that_PICKED_the_row(
        client, hub, monkeypatch):
    """One probe per candidate, and the row is described by ITS answer.

    `_engine` asked `available()` twice — once to choose the row
    (`next(r for r in candidates if r.available().ok)`) and again to read the
    reason off it. That was free while every probe was a `platform` fact and
    stopped being free when the per-hardware rows made a probe a live device read
    (AI-6): the two calls can straddle a `modprobe`, a container restart or an
    eGPU being unplugged, and the card then reports a row chosen by one answer and
    explained by another — including "not available" beside the reason the machine
    had a moment ago rather than the one it has.

    Driven with a probe whose refusal is NUMBERED, so the assertion names the
    call the reason came from instead of counting calls: `refusal 1` is the probe
    that selected the row, and anything later is a second look.
    """
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "x86_64")
    seen = []

    def numbered():
        seen.append(1)
        return _ai_registry.Availability(False, f"refusal {len(seen)}")

    runners = tuple(
        dataclasses.replace(r, _available=numbered) if r.code == "mlx-text" else r
        for r in _ai_registry.all_runners()
    )
    monkeypatch.setattr(_ai_registry, "_RUNNERS", runners)
    # The SERVING engine is answered without probing, so the only calls left are
    # the ones `_engine` makes about the candidate — otherwise this test would be
    # counting the resolution's probes too and would pass for the wrong reason.
    monkeypatch.setattr(_ai_registry, "for_capability",
                        lambda capability: _ai_registry.by_code("llamacpp-text"))

    # An MLX text checkpoint on Linux: the only runner that reads it is the one
    # that cannot run here, which is the branch that does the double probe.
    repo = _repo(hub, "models--mlx-community--Qwen3-8B-4bit", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json", json.dumps(
        {"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3",
         "quantization": {"group_size": 64, "bits": 4}}))
    _snapshot_file(repo, "c1", "model.safetensors", _safetensors({"w": (8, 8)}))
    engine = _engine(client, "mlx-community/Qwen3-8B-4bit")
    assert engine["code"] == "mlx-text" and engine["available"] is False
    assert engine["reason"] == "refusal 1", (engine["reason"], len(seen))


@requires_symlinks
def test_a_plain_safetensors_causal_lm_is_MLXS_AND_UNAVAILABLE_off_a_mac(client, hub, monkeypatch):
    """A cached bf16 causal LM off a Mac names `mlx-text` and says it cannot run.

    This asserted `transformers-text` and `available: True` until D416, and the
    change of answer is the one user-visible cost of removing that family rather
    than an accident of the test: `formats.loaders()` maps a directory of plain
    safetensors to exactly one engine now, and that engine is Apple-only. So a
    Linux user with a bf16 Qwen already on disk gets a card that names the
    engine which reads the format and the registry's own sentence about why this
    machine is not it — which is the honest answer, and is what the card is for.
    What that user CAN run is a GGUF of the same model through `llamacpp-text`;
    `catalog.SUGGESTIONS["llamacpp-text"]` is what the Discover tab offers them,
    and its 9B entry's note makes the size comparison to exactly this download.
    """
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "x86_64")
    repo = _repo(hub, "models--Qwen--Qwen3-8B", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json",
                   json.dumps({"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3"}))
    _snapshot_file(repo, "c1", "model.safetensors", _safetensors({"w": (8, 8)}))
    engine = _engine(client, "Qwen/Qwen3-8B")
    assert engine["code"] == "mlx-text" and engine["available"] is False
    assert "Apple Silicon" in engine["reason"]


# -- and the same reading answers a load that omitted one (D321) -----------------
# `cached_capability` is this page's join, exported for `ai_runtime.py`: a load
# with no `capability` used to mean text generation whatever was on disk, which
# sent an MLX diffusion repo to mlx-lm. The card and the load must agree, so
# there is one reading and both read it.


@requires_symlinks
def test_the_card_and_a_load_without_a_capability_agree(client, hub):
    """Every repo the PAGE offers a capability for resolves to that same
    capability when a load leaves it out. The forbidden direction is a card
    promising a load that then goes somewhere else."""
    text = _repo(hub, "models--org--chat", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(text, "c1", "config.json",
                   json.dumps({"architectures": ["LlamaForCausalLM"], "model_type": "llama"}))
    image = _repo(hub, "models--org--sd", blobs={"w": 10},
                  snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(image, "c1", "model_index.json",
                   json.dumps({"_class_name": "StableDiffusionXLPipeline"}))
    for row in _get(client)["repos"]:
        if row["capability"] is None:
            continue
        assert ai_models_mod.cached_capability(row["id"]).capability == row["capability"]


def test_a_repo_that_is_not_cached_says_so(client, hub):
    """`cached=False` is what lets the load route fall back to the catalog and
    then to the old default, instead of refusing a cold load."""
    assert ai_models_mod.cached_capability("org/never-downloaded")[:3] == (False, None, None)
    # A folder with no revision in it is an interrupted download, not evidence.
    (hub / "models--org--empty").mkdir()
    assert ai_models_mod.cached_capability("org/empty").cached is False


def test_a_repo_id_can_never_become_a_path(hub):
    """The lookup builds a cache folder name out of a request body's string."""
    for hostile in ("../../etc", "..", "a/../../b", "org\\evil"):
        assert ai_models_mod.cached_capability(hostile)[:3] == (False, None, None)


@requires_symlinks
def test_an_unreadable_cached_repo_reports_what_it_looks_like(client, hub):
    """The sentence the load route refuses with is built from this."""
    repo = _repo(hub, "models--org--tts", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: text-to-speech\n---\n")
    reading = ai_models_mod.cached_capability("org/tts")
    assert reading.cached is True and reading.capability is None
    assert reading.looks_like == "a text to speech model"


@requires_symlinks
def test_what_it_looks_like_reads_as_a_sentence(client, hub):
    """"a image to image model" is the kind of thing that makes a generated
    error look machine-written, in the one message meant to be acted on."""
    repo = _repo(hub, "models--org--img2img", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: image-to-image\n---\n")
    assert ai_models_mod.cached_capability("org/img2img").looks_like == (
        "an image to image model")


@requires_symlinks
def test_a_dataset_has_no_engine(client, hub):
    data = _repo(hub, "datasets--org--corpus", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(data, "c1", "config.json",
                   json.dumps({"architectures": ["LlamaForCausalLM"], "model_type": "llama"}))
    assert _repo_row(client, "org/corpus")["engine"] is None


# -- repos the user never chose --------------------------------------------------


@requires_symlinks
def test_a_component_repo_says_what_it_is_a_part_of(client, hub):
    """The GGUF transformer the diffusers recipe fetches is 2.4GB of somebody
    else's model sitting in the cache under its own name. It read `cached=True,
    capability=None` and wore the quiet "no engine" tag — so the page gave a
    user reclaiming disk no way to tell that deleting it breaks the image model
    that needs it."""
    _repo(hub, "models--unsloth--FLUX.2-klein-4B-GGUF", blobs={"w": 10},
          snapshots={"c1": {"flux-2-klein-4b-Q4_K_M.gguf": "w"}},
          refs={"main": "c1"})

    row = _repo_row(client, "unsloth/FLUX.2-klein-4B-GGUF")

    assert row["component"]["owner"] == "FLUX.2 klein 4B"
    assert row["component"]["part"] == "quantized transformer"
    assert row["component"]["of"] == "black-forest-labs/FLUX.2-klein-4B"
    assert "Deleting it" in row["component"]["what"]
    # Not loadable, and never was — but now the card can say why, and the size
    # is still on the page, because "what is eating my disk" is why it exists.
    assert row["engine"] is None and row["capability"] is None
    assert row["size"] > 0


@requires_symlinks
def test_a_two_megabyte_helper_reads_the_same_way(client, hub):
    """Silero is 2MB and ours, not the user's. The same treatment has to make
    sense at that scale: it had no task, no library and no engine, so its card
    carried NO explanation at all."""
    _repo(hub, "models--onnx-community--silero-vad", blobs={"w": 10},
          snapshots={"c1": {"model.onnx": "w"}}, refs={"main": "c1"})

    row = _repo_row(client, "onnx-community/silero-vad")

    assert row["component"]["owner"] == "MLX Whisper"
    assert row["component"]["part"] == "speech detector"
    # An engine's component, not a model's — nothing to point at, and the prose
    # says the cost of deleting it is a slower transcription rather than a
    # broken model.
    assert row["component"]["of"] is None
    assert "slower" in row["component"]["what"]


@requires_symlinks
def test_an_ordinary_model_is_not_a_component(client, hub):
    repo = _repo(hub, "models--org--m", blobs={"w": 10},
                 snapshots={"c1": {"model.bin": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: text-to-image\n---\n")
    assert _repo_row(client, "org/m")["component"] is None


@requires_symlinks
def test_loading_a_component_is_refused_by_name(client, hub):
    """`cached_capability` is what the load route refuses with (D321). "a
    speech detector that belongs to MLX Whisper" is a far more useful
    sentence than the "model repo" reading it used to produce."""
    _repo(hub, "models--onnx-community--silero-vad", blobs={"w": 10},
          snapshots={"c1": {"model.onnx": "w"}}, refs={"main": "c1"})

    reading = ai_models_mod.cached_capability("onnx-community/silero-vad")

    assert reading.cached is True and reading.capability is None
    assert reading.looks_like == "a speech detector that belongs to MLX Whisper"


# -- a download that never finished (D424) ----------------------------------------
# The reading is POSITIVE EVIDENCE ONLY, and every test here is about one of the
# two halves of that: the residue of a stopped fetch says "partial", and nothing
# else is allowed to — least of all a format no engine reads, which is what a
# perfectly complete SigLIP tower looks like.


@requires_symlinks
def test_a_part_file_marks_the_repo_partly_downloaded(client, hub):
    """The bug this whole reading exists for.

    Our fetcher publishes each blob and links it into `snapshots/<commit>/` as
    that FILE lands, so a cancel halfway through a repo leaves a real revision
    beside a part file — and the revision alone read as "downloaded", which took
    the recommendation and its working Download button off the page and left a
    card with no weights, no engine and a disabled Load.
    """
    repo = _repo(hub, "models--mlx-community--whisper-tiny.en-8bit",
                 blobs={"cfg": 10}, snapshots={"c1": {"config.json": "cfg"}})
    # The 4.6GB of weights that never arrived, mid-flight when the ✕ was pressed.
    (repo / "blobs" / "weights.fusedpart").write_bytes(b"x" * 64)

    row = _repo_row(client, "mlx-community/whisper-tiny.en-8bit")

    assert row["partial"] is True
    # And NOT because the revision is missing: it is there, which is exactly why
    # the count could not answer this question.
    assert row["revisions"] == 1


@requires_symlinks
def test_hugging_faces_own_incomplete_file_counts_too(client, hub):
    """The cache is shared. A pull by `hf`, transformers or a template a user
    pasted in leaves `.incomplete`, and this page reads that cache too."""
    repo = _repo(hub, "models--org--m", blobs={"w": 10},
                 snapshots={"c1": {"model.safetensors": "w"}}, refs={"main": "c1"})
    (repo / "blobs" / "abc123.incomplete").write_bytes(b"x" * 8)

    assert _repo_row(client, "org/m")["partial"] is True


def test_a_repo_with_no_snapshot_at_all_is_partly_downloaded(client, hub):
    """A folder with blobs and nothing to open — a cancel that landed before the
    first file did. hub search has always called this partial; the listing now
    says the same word."""
    _repo(hub, "models--org--m", blobs={"w": 10})

    assert _repo_row(client, "org/m")["partial"] is True


@requires_symlinks
def test_a_completed_repo_no_engine_reads_is_NOT_partial(client, hub):
    """The false positive that would have been worse than the bug.

    A SigLIP tower or an ACE-Step checkpoint downloads perfectly and no runner
    here opens it: `engine` is null and `capability` may be too. Reading THAT as
    "partly downloaded" would offer to resume a download that finished months
    ago — so the format never enters the question.
    """
    repo = _repo(hub, "models--google--siglip2-base", blobs={"w": 10},
                 snapshots={"c1": {"model.onnx": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json",
                   json.dumps({"architectures": ["Siglip2Model"]}))

    row = _repo_row(client, "google/siglip2-base")

    assert row["engine"] is None
    assert row["partial"] is False


@requires_symlinks
def test_a_repo_pinned_at_a_commit_is_not_partial_for_having_no_ref(client, hub):
    """The other tempting reading, and the other false positive: neither hf nor
    `_write_ref` writes a ref named after a sha, so "no refs/" is the ordinary
    state of a repo fetched at a pinned commit."""
    _repo(hub, "models--org--pinned", blobs={"w": 10},
          snapshots={"deadbeef": {"model.safetensors": "w"}})

    assert _repo_row(client, "org/pinned")["partial"] is False


@requires_symlinks
def test_a_repo_this_app_never_fetched_is_not_partial(client, hub):
    """`.fused-fetch-<commit>.json` is written only by our own fetcher, so its
    ABSENCE describes every repo pulled by the `hf` CLI or by a build older than
    the record — none of which is half-downloaded."""
    _repo(hub, "models--org--legacy", blobs={"w": 10},
          snapshots={"c1": {"model.safetensors": "w"}}, refs={"main": "c1"})

    assert _repo_row(client, "org/legacy")["partial"] is False


def test_a_dataset_is_never_partly_downloaded(client, hub):
    """A dataset with no snapshot is not something this page can resume, and the
    card it would draw the state on has no Download button."""
    _repo(hub, "datasets--squad", blobs={"a": 10})

    assert _repo_row(client, "squad")["partial"] is False


def test_the_part_suffix_is_the_fetchers_own(client):
    """Two modules, one name. This module reads a cache the fetcher writes, and
    the constant is duplicated deliberately (see `_PART_SUFFIXES`) — so the
    duplication is pinned rather than trusted."""
    from fused_render.ai.runners import worker_base

    assert worker_base.PART_SUFFIX in ai_models_mod._PART_SUFFIXES


@requires_symlinks
def test_deleting_a_partly_downloaded_repo_frees_it_and_it_leaves_the_listing(
        client, hub):
    """The second of the two ways out. Discarding the bytes is what puts the
    model back among the recommendations, so it has to actually work on a repo
    whose snapshot is incomplete."""
    repo = _repo(hub, "models--org--m", blobs={"cfg": 10},
                 snapshots={"c1": {"config.json": "cfg"}})
    (repo / "blobs" / "weights.fusedpart").write_bytes(b"x" * 128)
    assert _repo_row(client, "org/m")["partial"] is True

    r = client.post("/api/ai-models/delete", headers={"X-Fused": "1"},
                    json={"targets": [{"dir": "models--org--m"}]})

    assert r.status_code == 200
    body = r.json()
    assert body["failures"] == []
    assert body["freed"] >= 128
    assert [row["id"] for row in body["repos"]] == []
    assert not repo.exists()


@requires_symlinks
def test_cached_models_does_not_offer_half_a_snapshot(client, hub):
    """`cached_models()` is what `/api/ai/catalog` and every page's picker read
    (D323). A repo whose download stopped is not a model this disk HAS, and a
    picker offering one is a Load that fails."""
    repo = _repo(hub, "models--org--chat", blobs={"w": 10},
                 snapshots={"c1": {"model.safetensors": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json",
                   json.dumps({"architectures": ["LlamaForCausalLM"]}))
    assert "org/chat" in {m.repo_id for m in ai_models_mod.cached_models()}

    # The same repo, one interrupted fetch later.
    (repo / "blobs" / "shard2.fusedpart").write_bytes(b"x" * 32)

    assert "org/chat" not in {m.repo_id for m in ai_models_mod.cached_models()}


# -- the empty shell a stopped fetch leaves behind (D437) ----------------------
# The state a user hit: a cancelled download whose folder held one 40-byte
# `refs/main` and not a single blob. The listing has to call that partial (no
# snapshot IS the evidence), so the page drew a "partly downloaded" card under
# Unrecognised offering to resume a download with nothing to resume from. The
# fetch thread tidies it on its way out now; these pin what it may and may not
# take with it.


def test_a_refs_only_shell_is_discarded(client, hub):
    repo = _repo(hub, "models--org--never-started", refs={"main": "c1"})
    # …and the folder really is the state the field reported: partial, tiny, and
    # filed with no capability of its own.
    row = _repo_row(client, "org/never-started")
    assert row["partial"] is True
    assert row["capability"] is None

    assert ai_models_mod.discard_empty_shell("org/never-started") is True
    assert not repo.exists()


def test_a_stopped_fetch_with_bytes_on_disk_is_KEPT(client, hub):
    """The rule this must not break (D275/AI-5i). A part file is exactly what a
    resume picks up, so a folder holding one is a download in progress as far as
    this app is concerned — not litter."""
    repo = _repo(hub, "models--org--half")
    (repo / "blobs" / "weights.fusedpart").write_bytes(b"x" * 4096)

    assert ai_models_mod.discard_empty_shell("org/half") is False
    assert repo.exists()


@requires_symlinks
def test_a_finished_download_is_never_discarded(client, hub):
    """Called on every fetch's way out, including the successful ones — so the
    successful ones have to be a no-op. Read off the FOLDER, never off the job's
    outcome."""
    repo = _repo(hub, "models--org--done", blobs={"w": 10},
                 snapshots={"c1": {"model.safetensors": "w"}}, refs={"main": "c1"})

    assert ai_models_mod.discard_empty_shell("org/done") is False
    assert repo.exists()


def test_discarding_a_shell_that_is_not_there_is_not_an_error(client, hub):
    """The ordinary case: nothing was ever created, or a previous pass already
    tidied it. This runs in a `finally` and must never raise."""
    assert ai_models_mod.discard_empty_shell("org/nothing") is False


def test_a_shell_named_by_a_path_is_refused(client, hub):
    """Same discipline as every other destructive path here: a repo id is turned
    into ONE folder name, and anything that is not one is not looked at."""
    assert ai_models_mod.discard_empty_shell("../../etc") is False


# -- bytes that ARRIVED, vs blocks reserved for them (D440) --------------------
# Our fetcher preallocates a part file to the full length of the file it is
# fetching, so a repo 15% into a 1.6GB download measures 1.6GB on disk. `size`
# is right about the disk and wrong about the download, and a card drawing "how
# much of this is here" from it read as nearly finished.


def _sidecar(repo, blob, size, done_per_segment, segment=32 * 1024 * 1024):
    """A part file's sidecar in the shape `worker_base._FileFetch.flush` writes."""
    segments = []
    start = 0
    for done in done_per_segment:
        end = min(start + segment, size) - 1
        segments.append({"start": start, "end": end, "done": done})
        start = end + 1
    (repo / "blobs" / f"{blob}.fusedpart.json").write_text(
        json.dumps({"version": 3, "etag": blob, "size": size, "segments": segments})
    )


def test_fetched_bytes_reads_the_sidecar_not_the_part_files_length(client, hub):
    repo = _repo(hub, "models--org--pulling")
    # Preallocated to 96MB; only 40MB of it is durable.
    (repo / "blobs" / "w.fusedpart").write_bytes(b"x" * (96 * 1024 * 1024))
    _sidecar(repo, "w", 96 * 1024 * 1024,
             [32 * 1024 * 1024, 8 * 1024 * 1024, 0])

    row = _repo_row(client, "org/pulling")

    # `size` still counts the part file's whole length (plus its little sidecar):
    # that is what the folder holds, and it is what the page PRINTS.
    assert row["size"] > 96 * 1024 * 1024
    # …and this is what arrived: two full segments and a third of a third one.
    assert row["fetchedBytes"] == 40 * 1024 * 1024


def test_a_part_file_with_no_sidecar_counts_nothing(client, hub):
    """No sidecar means nothing has SAID any of those bytes are durable, and the
    file may be pure preallocation — so it contributes zero rather than its
    length. Same posture as `_unfinished_fetch`: positive evidence only."""
    repo = _repo(hub, "models--org--bare")
    (repo / "blobs" / "w.fusedpart").write_bytes(b"x" * 4096)

    assert _repo_row(client, "org/bare")["fetchedBytes"] == 0


def test_a_torn_sidecar_counts_nothing_rather_than_raising(client, hub):
    repo = _repo(hub, "models--org--torn")
    (repo / "blobs" / "w.fusedpart").write_bytes(b"x" * 4096)
    (repo / "blobs" / "w.fusedpart.json").write_text("{not json")

    assert _repo_row(client, "org/torn")["fetchedBytes"] == 0


def test_a_sidecar_claiming_more_than_its_segment_is_clamped(client, hub):
    """A sidecar is written by another process; a `done` past the segment's own
    width must not inflate the total."""
    repo = _repo(hub, "models--org--liar")
    (repo / "blobs" / "w.fusedpart").write_bytes(b"x" * 1024)
    _sidecar(repo, "w", 1024, [999_999_999], segment=1024)

    assert _repo_row(client, "org/liar")["fetchedBytes"] == 1024


def test_hf_incomplete_files_count_their_length(client, hub):
    """`huggingface_hub` APPENDS, so for its part files the length IS the
    progress — the opposite of ours, which is why the two are read differently."""
    repo = _repo(hub, "models--org--hf", blobs={"done": 100})
    (repo / "blobs" / "abc.incomplete").write_bytes(b"x" * 900)

    assert _repo_row(client, "org/hf")["fetchedBytes"] == 1000


@requires_symlinks
def test_a_finished_repo_reports_every_byte_as_fetched(client, hub):
    """The ordinary case, and the one the fraction never draws: nothing is
    outstanding, so the two numbers agree."""
    _repo(hub, "models--org--done", blobs={"w": 4096},
          snapshots={"c1": {"model.safetensors": "w"}}, refs={"main": "c1"})

    row = _repo_row(client, "org/done")

    assert row["fetchedBytes"] == row["size"]


# -- has_vision_tower: can a cached checkpoint be handed an image? (AI-11j) ---
#
# Read straight off `config.json`, WITHOUT loading the model — the question
# `ai_runtime._accepts_image` has to answer before an attach button is even
# drawn, let alone before a request reaches the worker.


def test_has_vision_tower_reads_the_vision_config_key(hub):
    """The same evidence `_architecture_task` already uses to route a unified
    checkpoint to `image-text-to-text` rather than plain `text-generation`."""
    repo = _repo(hub, "models--org--vlm", snapshots={"c1": {}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json",
                   json.dumps({"model_type": "qwen3_5", "vision_config": {"depth": 4}}))

    assert ai_models_mod.has_vision_tower("org/vlm") is True


def test_has_vision_tower_is_false_for_a_plain_text_checkpoint(hub):
    repo = _repo(hub, "models--org--chat", snapshots={"c1": {}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json", json.dumps({"model_type": "llama"}))

    assert ai_models_mod.has_vision_tower("org/chat") is False


def test_has_vision_tower_reads_the_optiq_sidecar_checkpoints_config_too(hub):
    """**The gotcha, verified by hand:** `Qwen3.5-*-OptiQ` keeps its vision
    tower in a SIDE-CAR `optiq/optiq_vision.safetensors`, not in
    `model.safetensors` — a checkpoint that would read as tower-less to
    anything that decided the answer by globbing weight files (non-recursively,
    which is the version of that mistake that is easy to write). `config.json`
    carries `vision_config`/`image_token_id` regardless of where the tower's
    OWN weights live, which is why this is never answered by a file listing."""
    repo = _repo(hub, "models--org--optiq", snapshots={"c1": {}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json",
                   json.dumps({"model_type": "qwen3_5", "image_token_id": 151655,
                               "vision_config": {"depth": 4}}))
    # The tower's weights, off in their own side-car — nothing here reads this
    # file, and that absence is the point of the test.
    _snapshot_file(repo, "c1", "optiq/optiq_vision.safetensors", b"\x00" * 8)

    assert ai_models_mod.has_vision_tower("org/optiq") is True


def test_has_vision_tower_is_false_for_a_never_cached_repo(hub):
    """No snapshot on disk at all: the answer cannot be determined, and False
    is the failure-closed direction — an attach button whose request 400s is
    exactly the failure AI-11j exists to prevent."""
    assert ai_models_mod.has_vision_tower("org/never-downloaded") is False


def test_has_vision_tower_is_false_for_a_hostile_repo_id(hub):
    """The same path-segment guard `cached_capability` applies to a request
    body's model id — a repo id is not a place to go looking for `..`."""
    assert ai_models_mod.has_vision_tower("../../etc/passwd") is False

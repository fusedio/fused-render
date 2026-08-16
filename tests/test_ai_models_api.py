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
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.ai import registry as _ai_registry
from fused_render.server.routers import ai_models as ai_models_mod

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
        (["T5ForConditionalGeneration"], "t5", "text-to-text generation"),
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
    assert row["task"] == "text-to-text generation"
    assert row["capability"] is None


@requires_symlinks
def test_diffusers_and_sentence_transformers_are_recognised(client, hub):
    a = _repo(hub, "models--org--sd", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(a, "c1", "model_index.json", json.dumps({"_class_name": "StableDiffusionXLPipeline"}))
    b = _repo(hub, "models--org--st", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(b, "c1", "modules.json", "[]")
    assert _repo_row(client, "org/sd")["task"] == "image generation"
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
    assert row["task"] == "image generation"
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
    """The one a llama.cpp user's cache is full of: no config.json, no card,
    just weights — and the library is all that can honestly be read off them.

    It used to say "text generation" here, which is a guess about the MODALITY
    made from a CONTAINER, and it was wrong on the first image repo it met
    (`unsloth/FLUX.2-klein-4B-GGUF`, the quantized transformer the diffusers
    recipe fetches). The guess never bought anything either: no runner that
    ships loads a GGUF-only repo, so all it ever produced was a Load button
    that fails.
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
    "class_name,expected",
    [
        ("StableVideoDiffusionPipeline", "video generation"),
        ("MusicGenPipeline", "audio generation"),
        ("AudioLDM2Pipeline", "audio generation"),
        ("StableDiffusionPipeline", "image generation"),
    ],
)
def test_a_diffusers_pipeline_names_its_medium(client, hub, class_name, expected):
    repo = _repo(hub, "models--org--p", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "model_index.json", json.dumps({"_class_name": class_name}))
    assert _repo_row(client, "org/p")["task"] == expected


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
def test_an_unknown_tag_still_shows_its_label_and_source(client, hub):
    # The Hub's vocabulary is open-ended, so a tag we have no sentence for
    # degrades to label + provenance rather than to nothing.
    repo = _repo(hub, "models--org--m", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "README.md", "---\npipeline_tag: graph-ml\n---\n")
    row = _repo_row(client, "org/m")
    assert row["task"] == "graph ml"
    assert row["taskSource"] == "the model card's pipeline_tag"
    assert row["taskHelp"] is None


def test_every_label_this_module_can_produce_is_explained():
    """The glossary is keyed by LABEL so one table serves both evidence paths —
    which only works while the paths agree on the label.

    They drifted once: a whisper model read from its card said "automatic
    speech recognition" and the same model read from its config said "speech
    recognition", so the card path — the preferred one — fell through the
    glossary. This pins the invariant instead of the two instances: every label
    the module's OWN tables can produce has a sentence. (Passthrough tags from
    the Hub's open vocabulary are deliberately not covered; those degrade to
    label + source.)
    """
    produced = set(ai_models_mod._FRIENDLIER_TAGS.values())
    produced |= {task for _, task in ai_models_mod._ARCH_TASKS}
    produced |= {ai_models_mod._diffusers_task(name)
                 for name in ("StableDiffusionPipeline", "StableVideoDiffusionPipeline", "MusicGenPipeline")}
    produced |= {"embeddings", "text generation"}  # the sentence-transformers and GGUF branches
    missing = sorted(label for label in produced if label not in ai_models_mod._TASK_HELP)
    assert not missing, f"labels with no explanation: {missing}"


def _labels_this_module_can_produce():
    """Every task label the listing's own tables can put on a card."""
    produced = set(ai_models_mod._FRIENDLIER_TAGS.values())
    produced |= {task for _, task in ai_models_mod._ARCH_TASKS}
    produced |= {ai_models_mod._diffusers_task(name)
                 for name in ("StableDiffusionPipeline", "StableVideoDiffusionPipeline",
                              "MusicGenPipeline")}
    produced |= {"embeddings", "text generation"}
    return {label for label in produced if label}


def test_every_task_label_is_classified():
    """Every label is either loadable by a runner or explicitly ruled out.

    A label nobody has thought about and a label that has been ruled out both
    produce `capability: null`, so they look identical from the page — and that
    is how "image + text to text" lost its Load button while the app's own
    Discover tab went on recommending the models that carry exactly that label
    — today every entry in the MLX catalog — as chat models.

    Pinning the CLASSIFICATION rather than the instance: growing the vocabulary
    without deciding what runs it now fails here instead of quietly removing a
    control from a card.
    """
    unclassified = sorted(
        label for label in _labels_this_module_can_produce()
        if label not in _ai_registry._TASK_CAPABILITIES
        and label not in _ai_registry.NO_RUNNER_YET
    )
    assert not unclassified, (
        "task labels neither mapped to a capability nor listed in "
        f"registry.NO_RUNNER_YET: {unclassified}"
    )


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
        assert runner.capability in set(_ai_registry._TASK_CAPABILITIES.values()), (
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
    rather than a button that is offered and always fails."""
    embed = _repo(hub, "models--org--st", blobs={"w": 10}, snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(embed, "c1", "modules.json", "[]")
    assert _repo_row(client, "org/st")["task"] == "embeddings"
    assert _repo_row(client, "org/st")["capability"] is None


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
    is a container, not a modality, and no shipping runner loads a GGUF-only
    repo at all."""
    _repo(hub, "models--unsloth--FLUX.2-klein-4B-GGUF", blobs={"w": 10},
          snapshots={"c1": {"flux-2-klein-4b-Q4_K_M.gguf": "w"}}, refs={"main": "c1"})
    row = _repo_row(client, "unsloth/FLUX.2-klein-4B-GGUF")
    assert row["task"] is None
    assert row["capability"] is None
    assert row["library"] == "gguf"
    assert row["engine"] is None


@requires_symlinks
def test_the_apps_own_recommended_image_model_is_loadable(client, hub, monkeypatch):
    """`black-forest-labs/FLUX.2-klein-4B` is `catalog.py`'s suggestion for the
    diffusers runner, and its card's `pipeline_tag` says image-to-image — a
    label in NO_RUNNER_YET, so the page offered no Load for a model the app
    recommends on the next tab. The card names the model FAMILY; the
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
    assert row["task"] == "image generation"
    assert row["capability"] == _ai_registry.IMAGE_GENERATION
    assert row["engine"]["code"] == "diffusers-image"


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
    assert row["task"] == "image generation"
    assert row["capability"] == _ai_registry.IMAGE_GENERATION
    assert row["engine"]["code"] == "mflux-image" and row["engine"]["available"]
    # The invariant behind both halves: a card that offers Load shows a task
    # this machine can actually serve.
    assert _ai_registry.capability_for_task(row["task"]) == row["capability"]


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
        assert _ai_registry.capability_for_task(row["task"]) == row["capability"], row


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

    # The same checkpoint off a Mac: still MLX's, and torch will not read it —
    # which is exactly what the transformers runner's `_refuse_unloadable`
    # raises about a `quantization` block with a `group_size` in it.
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "x86_64")
    engine = _engine(client, "mlx-community/Qwen3-8B-4bit")
    assert engine["code"] == "mlx-text" and engine["available"] is False


@requires_symlinks
def test_a_plain_safetensors_causal_lm_loads_on_transformers_off_a_mac(client, hub, monkeypatch):
    monkeypatch.setattr(_ai_registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_ai_registry.platform, "machine", lambda: "x86_64")
    repo = _repo(hub, "models--Qwen--Qwen3-8B", blobs={"w": 10},
                 snapshots={"c1": {"m": "w"}}, refs={"main": "c1"})
    _snapshot_file(repo, "c1", "config.json",
                   json.dumps({"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3"}))
    _snapshot_file(repo, "c1", "model.safetensors", _safetensors({"w": (8, 8)}))
    engine = _engine(client, "Qwen/Qwen3-8B")
    assert engine["code"] == "transformers-text" and engine["available"] is True


# -- and the same reading answers a load that omitted one (D307) -----------------
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
    assert ai_models_mod.cached_capability("org/never-downloaded") == (False, None, None)
    # A folder with no revision in it is an interrupted download, not evidence.
    (hub / "models--org--empty").mkdir()
    assert ai_models_mod.cached_capability("org/empty").cached is False


def test_a_repo_id_can_never_become_a_path(hub):
    """The lookup builds a cache folder name out of a request body's string."""
    for hostile in ("../../etc", "..", "a/../../b", "org\\evil"):
        assert ai_models_mod.cached_capability(hostile) == (False, None, None)


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
    """`cached_capability` is what the load route refuses with (D307). "a
    speech detector that belongs to MLX Whisper" is a far more useful sentence
    than the "model repo" reading it used to produce."""
    _repo(hub, "models--onnx-community--silero-vad", blobs={"w": 10},
          snapshots={"c1": {"model.onnx": "w"}}, refs={"main": "c1"})

    reading = ai_models_mod.cached_capability("onnx-community/silero-vad")

    assert reading.cached is True and reading.capability is None
    assert reading.looks_like == "a speech detector that belongs to MLX Whisper"

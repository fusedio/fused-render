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
    # The page is client-side, but a bookmark, a refresh, or a URL typed in
    # while the sidebar entry is hidden is a real GET the server has to answer
    # with the shell (routers/shell.py) — otherwise the route 404s for exactly
    # the people HF-8 says can still reach it.
    r = client.get("/ai-models")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_status_probe(client, hub, tmp_path, monkeypatch):
    assert client.get("/api/ai-models/status").json() == {
        "available": True,
        "cacheDir": str(hub).replace("\\", "/"),
    }
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "nope"))
    assert client.get("/api/ai-models/status").json()["available"] is False


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


def test_delete_needs_a_non_empty_target_list(client, hub):
    assert _delete(client, []).status_code == 400
    assert client.post("/api/ai-models/delete", json={}, headers={"X-Fused": "1"}).status_code == 400


# -- pruning by age --------------------------------------------------------------
# Prune is a client-side selection over `lastUsed` executed as a bulk delete of
# NAMED repos (D247), so what the server owes it is an honest read-time stamp.


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

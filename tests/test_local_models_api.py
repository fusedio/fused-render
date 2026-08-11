"""GET /api/local-models (+ /status) — the Hugging Face cache inventory behind
the sidebar's "Local models" page (server/routers/local_models.py).

Two things here are easy to get quietly wrong, so both are pinned:

* **Where the cache is.** huggingface_hub resolves it through four env vars
  with a precedence order; reading only ``~/.cache/huggingface/hub`` would
  report "nothing cached" on every machine that sets ``HF_HOME`` (which is
  most machines with a shared model disk).
* **What a repo costs.** Every snapshot entry is a symlink back into the same
  repo's ``blobs/``, so a naive walk multiplies a repo's size by its revision
  count — a page whose entire job is disk footprint would then be wrong by
  hundreds of GB on a big cache.

The layout the fixtures build is huggingface_hub's own CACHE_STRUCTURE:
``<hub>/models--org--name/{blobs,snapshots/<commit>,refs/<ref>}``.
"""
import os

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.server.routers import local_models as local_models_mod

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
    r = client.get("/api/local-models")
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

    monkeypatch.setattr(local_models_mod.os, "scandir", fake_scandir)
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

    monkeypatch.setattr(local_models_mod.os, "scandir", fake_scandir)
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
    r = client.get("/local-models")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_status_probe(client, hub, tmp_path, monkeypatch):
    assert client.get("/api/local-models/status").json() == {
        "available": True,
        "cacheDir": str(hub).replace("\\", "/"),
    }
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "nope"))
    assert client.get("/api/local-models/status").json()["available"] is False


# -- where the cache is --------------------------------------------------------


@pytest.fixture()
def clean_env(monkeypatch):
    for var in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)


def test_cache_dir_defaults_under_the_home_cache(clean_env):
    assert local_models_mod.hub_cache_dir() == os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface", "hub"
    )


def test_xdg_cache_home_moves_the_default(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert local_models_mod.hub_cache_dir() == os.path.join(str(tmp_path), "huggingface", "hub")


def test_hf_home_wins_over_xdg(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert local_models_mod.hub_cache_dir() == os.path.join(str(tmp_path / "hf"), "hub")


def test_hub_cache_vars_win_over_hf_home(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "legacy"))
    assert local_models_mod.hub_cache_dir() == str(tmp_path / "legacy")
    # The current name outranks the deprecated one.
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "current"))
    assert local_models_mod.hub_cache_dir() == str(tmp_path / "current")


def test_user_paths_are_expanded(clean_env, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", os.path.join("~", "models"))
    assert local_models_mod.hub_cache_dir() == os.path.join(os.path.expanduser("~"), "models")

"""Tests for /api/fs/compress (fused_render/server/fs_mutate.py).

Like test_server_fs_mutate.py these drive the module-level `_fs_compress`
helper directly (not through the TestClient), asserting both what lands on
disk and the wire error contract shared with the other mutations:
  400 bad path / bad format, 403 readonly ("readonly"), 404 missing source,
  409 conflict ("conflict"), plus the X-Fused guard.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

import pytest
from fastapi.responses import JSONResponse

from _thread_scoped import this_thread_only

from fused_render.server.fs_mutate import _fs_compress as COMPRESS

from tests.test_server_git_repo import git, make_repo

skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _data(resp) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return resp


def make_tree(root):
    root.mkdir(parents=True)
    # newline="": Path.write_text's default text-mode translation rewrites
    # "\n" to os.linesep on write, so a Windows run stores "beta\r\n" for
    # content this fixture asked to be "beta\n" — a fixture artifact tests
    # that check exact bytes (test_zip_writes_sibling_archive_and_returns_stat)
    # would then see, not anything the archiver does. newline="" writes
    # exactly what's given, the same on every platform.
    (root / "a.txt").write_text("alpha\n", newline="")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("beta\n", newline="")
    (root / "empty").mkdir()
    return root


# ------------------------------------------------------------ X-Fused guard

def test_guard_rejects_missing_header(tmp_path):
    resp = COMPRESS({"path": str(tmp_path), "format": "zip"}, x_fused=None)
    assert _status(resp) == 403
    assert "X-Fused" in _data(resp)["error"]


# ------------------------------------------------------------------- zip

def test_zip_writes_sibling_archive_and_returns_stat(tmp_path):
    src = make_tree(tmp_path / "proj")
    out = _data(COMPRESS({"path": str(src), "format": "zip"}, x_fused="1"))
    dest = tmp_path / "proj.zip"
    assert dest.is_file()
    assert out["path"] == str(dest)
    assert out["name"] == "proj.zip"
    assert out["is_dir"] is False
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
        assert "proj/a.txt" in names
        assert "proj/sub/b.txt" in names
        assert z.read("proj/sub/b.txt") == b"beta\n"
    # An empty subdirectory survives as a directory entry.
    assert any(n.rstrip("/") == "proj/empty" for n in names)


# Round-tripping is asserted through a REAL extractor, not just namelist():
# a zip can list an entry and still not produce the tree, and the whole point
# of the empty-folder case is what appears on disk after unzipping.
def _unzip(archive, into):
    into.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["unzip", "-q", str(archive), "-d", str(into)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.returncode == 0, proc.stderr.decode()
    return into


def _tree(root):
    """Every path under `root`, relative and "/"-separated, dirs marked.

    Zip archive entries are always "/"-separated by the ZIP spec (and this
    app's own `_zip_tree.arcname` enforces exactly that), regardless of host
    OS — so the tree this reads back off a REAL extraction has to be
    normalized the same way, or a nested relpath's OS-native separator
    (backslash on Windows) never matches the forward-slash entries the
    archiver wrote.
    """
    out = set()
    for base, dirs, files in os.walk(root):
        for name in dirs:
            out.add(os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/") + "/")
        for name in files:
            out.add(os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/"))
    return out


needs_unzip = pytest.mark.skipif(shutil.which("unzip") is None,
                                 reason="no unzip binary to verify extraction")


@needs_unzip
def test_an_empty_folder_round_trips_to_an_empty_folder(tmp_path):
    # Finder's behaviour: compressing an empty folder and unzipping gives you
    # the empty folder back. A zip with zero members extracts to nothing.
    src = tmp_path / "hollow"
    src.mkdir()
    out = _data(COMPRESS({"path": str(src), "format": "zip"}, x_fused="1"))
    with zipfile.ZipFile(out["path"]) as z:
        assert z.namelist(), "an empty folder must still have an entry for itself"
    assert _tree(_unzip(tmp_path / "hollow.zip", tmp_path / "out")) == {"hollow/"}


@needs_unzip
def test_a_folder_of_only_an_empty_subdirectory_round_trips(tmp_path):
    src = tmp_path / "shell"
    (src / "inner").mkdir(parents=True)
    COMPRESS({"path": str(src), "format": "zip"}, x_fused="1")
    assert _tree(_unzip(tmp_path / "shell.zip", tmp_path / "out")) == {
        "shell/", "shell/inner/"}


@needs_unzip
def test_a_normal_tree_round_trips_with_no_stray_or_duplicate_entries(tmp_path):
    src = make_tree(tmp_path / "proj")
    out = _data(COMPRESS({"path": str(src), "format": "zip"}, x_fused="1"))
    with zipfile.ZipFile(out["path"]) as z:
        names = z.namelist()
    assert len(names) == len(set(names)), f"duplicate entries: {names}"
    assert _tree(_unzip(tmp_path / "proj.zip", tmp_path / "out")) == {
        "proj/", "proj/a.txt", "proj/sub/", "proj/sub/b.txt", "proj/empty/"}


def test_zip_honours_an_explicit_dest(tmp_path):
    src = make_tree(tmp_path / "proj")
    dest = tmp_path / "proj 2.zip"
    out = _data(COMPRESS({"path": str(src), "format": "zip", "dest": str(dest)},
                         x_fused="1"))
    assert dest.is_file() and out["name"] == "proj 2.zip"


def test_zip_does_not_contain_itself(tmp_path):
    # Pathological but cheap to guard: a dest INSIDE the folder being zipped
    # must not be archived into itself.
    src = make_tree(tmp_path / "proj")
    dest = src / "inner.zip"
    COMPRESS({"path": str(src), "format": "zip", "dest": str(dest)}, x_fused="1")
    with zipfile.ZipFile(dest) as z:
        assert not any(n.endswith("inner.zip") for n in z.namelist())


def test_zip_stores_symlinks_without_following_them(tmp_path):
    src = make_tree(tmp_path / "proj")
    outside = tmp_path / "secret.txt"
    outside.write_text("do not inline me\n")
    try:
        os.symlink(str(outside), str(src / "link.txt"))
        os.symlink(str(src), str(src / "loop"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    out = _data(COMPRESS({"path": str(src), "format": "zip"}, x_fused="1"))
    with zipfile.ZipFile(out["path"]) as z:
        info = z.getinfo("proj/link.txt")
        assert stat.S_ISLNK(info.external_attr >> 16)
        # _zip_tree stores os.readlink()'s own return value verbatim (by
        # design — it never rewrites a symlink's target). On Windows that can
        # come back with the "\\?\" extended-length prefix even for a link
        # created from a plain absolute path — a cosmetic artifact of how
        # NTFS reports an absolute SubstituteName, not a different target —
        # so it is stripped before comparing, same as the readlink() check in
        # test_server_fs_mutate.py's trashed-symlink test.
        stored_target = z.read("proj/link.txt")  # target, not content
        if stored_target.startswith(b"\\\\?\\"):
            stored_target = stored_target[4:]
        assert stored_target == str(outside).encode()
        # The self-referential dir symlink is stored, never descended into.
        assert stat.S_ISLNK(z.getinfo("proj/loop").external_attr >> 16)
        assert not any(n.startswith("proj/loop/") for n in z.namelist())


def test_zip_leaves_nothing_behind_when_it_fails(tmp_path, monkeypatch):
    # The archive is built to a temp file and renamed, so a mid-build failure
    # never leaves a truncated .zip sitting at the final path.
    src = make_tree(tmp_path / "proj")
    from fused_render.server import fs_mutate as mod
    real = mod.zipfile.ZipFile

    class Boom(real):
        def write(self, *a, **k):
            raise OSError("disk full")

    monkeypatch.setattr(mod.zipfile, "ZipFile", Boom)
    resp = COMPRESS({"path": str(src), "format": "zip"}, x_fused="1")
    assert _status(resp) == 400
    assert not (tmp_path / "proj.zip").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]


# ------------------------------------------------------------- git formats

def test_git_bundle_of_a_repo_root(tmp_path):
    repo = make_repo(tmp_path / "repo")
    out = _data(COMPRESS({"path": str(repo), "format": "git-bundle"}, x_fused="1"))
    dest = tmp_path / "repo.bundle"
    assert dest.is_file() and out["name"] == "repo.bundle"
    # A real bundle: git can verify it and clone from it.
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(dest), str(clone))
    assert (clone / "a.txt").read_text() == "hello\n"


def test_git_archive_of_head(tmp_path):
    repo = make_repo(tmp_path / "repo")
    (repo / "untracked.txt").write_text("nope\n")
    out = _data(COMPRESS({"path": str(repo), "format": "git-archive"}, x_fused="1"))
    dest = tmp_path / "repo.tar.gz"
    assert dest.is_file() and out["name"] == "repo.tar.gz"
    with tarfile.open(dest, "r:gz") as t:
        names = set(t.getnames())
    assert "a.txt" in names
    assert "untracked.txt" not in names  # tracked files at HEAD only


def test_git_bundle_of_a_repo_with_no_commits_400(tmp_path):
    repo = make_repo(tmp_path / "empty", commit=False)
    resp = COMPRESS({"path": str(repo), "format": "git-bundle"}, x_fused="1")
    assert _status(resp) == 400
    assert "no commits" in _data(resp)["error"]
    assert not (tmp_path / "empty.bundle").exists()


def test_git_archive_of_a_repo_with_no_commits_400(tmp_path):
    repo = make_repo(tmp_path / "empty", commit=False)
    resp = COMPRESS({"path": str(repo), "format": "git-archive"}, x_fused="1")
    assert _status(resp) == 400
    assert "no commits" in _data(resp)["error"]


# An UNBORN HEAD is not an empty repository. `git checkout --orphan` leaves
# HEAD pointing at a branch with no commit while the repo's real history sits
# on other refs — so the two git formats have genuinely different preconditions
# and must not share one preflight: `bundle --all` packs every ref and does not
# care about HEAD, while `archive HEAD` needs exactly HEAD.

def _orphan_branch_repo(root):
    """A repo whose HEAD is unborn while its history sits on another branch.
    Returns the repo and the refname holding that history — READ OFF the repo,
    never assumed: which branch `git init` creates is a property of the git
    binary and its config (`main` on Apple git, `master` on a bare CI runner),
    so a literal here is a test that passes on one machine and fails on another.
    (It did: the fixture helper pins `init.defaultBranch` now, and this reads it
    back anyway — the assertion is about the ref carrying the history, not about
    what that ref happens to be called.)"""
    repo = make_repo(root)
    history_ref = git(repo, "symbolic-ref", "HEAD").strip()
    git(repo, "checkout", "-q", "--orphan", "fresh")
    return repo, history_ref


def test_git_bundle_works_on_an_orphan_branch_with_history_elsewhere(tmp_path):
    repo, history_ref = _orphan_branch_repo(tmp_path / "repo")
    out = _data(COMPRESS({"path": str(repo), "format": "git-bundle"}, x_fused="1"))
    dest = tmp_path / "repo.bundle"
    assert dest.is_file() and out["name"] == "repo.bundle"
    # The bundle really carries the branch that HEAD is not on.
    heads = git(tmp_path, "bundle", "list-heads", str(dest))
    assert history_ref in heads, heads


def test_git_archive_still_refuses_an_orphan_branch(tmp_path):
    # `archive HEAD` genuinely cannot resolve an unborn HEAD, and the message
    # must describe THAT rather than claiming the repo has no commits.
    repo, _history_ref = _orphan_branch_repo(tmp_path / "repo")
    resp = COMPRESS({"path": str(repo), "format": "git-archive"}, x_fused="1")
    assert _status(resp) == 400
    assert "no commit checked out" in _data(resp)["error"]
    assert not (tmp_path / "repo.tar.gz").exists()


def test_git_format_on_a_non_repo_400(tmp_path):
    d = make_tree(tmp_path / "plain")
    resp = COMPRESS({"path": str(d), "format": "git-bundle"}, x_fused="1")
    assert _status(resp) == 400
    assert "not a git repository" in _data(resp)["error"]


def test_git_format_on_a_repo_subdirectory_400(tmp_path):
    # The menu never offers this, but the endpoint must not quietly bundle the
    # whole parent repo when asked for a subdirectory.
    repo = make_repo(tmp_path / "repo")
    sub = repo / "src"
    sub.mkdir()
    resp = COMPRESS({"path": str(sub), "format": "git-bundle"}, x_fused="1")
    assert _status(resp) == 400
    assert "not a git repository" in _data(resp)["error"]


def test_zip_of_a_repo_subdirectory_is_fine(tmp_path):
    repo = make_repo(tmp_path / "repo")
    sub = repo / "src"
    sub.mkdir()
    (sub / "c.txt").write_text("gamma\n")
    out = _data(COMPRESS({"path": str(sub), "format": "zip"}, x_fused="1"))
    assert out["name"] == "src.zip"
    assert (repo / "src.zip").is_file()


# ------------------------------------------------------------ error contract

@pytest.mark.parametrize("fmt", ["zip", "git-bundle", "git-archive"])
def test_relative_path_400(fmt):
    resp = COMPRESS({"path": "relative/dir", "format": fmt}, x_fused="1")
    assert _status(resp) == 400


def test_missing_path_400():
    assert _status(COMPRESS({"format": "zip"}, x_fused="1")) == 400


@pytest.mark.parametrize("fmt", [None, "", "tar", "rar", "zip;rm -rf /", "ZIP", 7])
def test_bad_format_400(tmp_path, fmt):
    src = make_tree(tmp_path / "proj")
    resp = COMPRESS({"path": str(src), "format": fmt}, x_fused="1")
    assert _status(resp) == 400
    assert "format" in _data(resp)["error"]


def test_source_missing_404(tmp_path):
    resp = COMPRESS({"path": str(tmp_path / "nope"), "format": "zip"}, x_fused="1")
    assert _status(resp) == 404


def test_source_is_a_file_400(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    resp = COMPRESS({"path": str(f), "format": "zip"}, x_fused="1")
    assert _status(resp) == 400
    assert "not a directory" in _data(resp)["error"]


def test_existing_destination_409(tmp_path):
    src = make_tree(tmp_path / "proj")
    (tmp_path / "proj.zip").write_text("in the way")
    resp = COMPRESS({"path": str(src), "format": "zip"}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"
    assert (tmp_path / "proj.zip").read_text() == "in the way"


def test_relative_dest_400(tmp_path):
    src = make_tree(tmp_path / "proj")
    resp = COMPRESS({"path": str(src), "format": "zip", "dest": "out.zip"}, x_fused="1")
    assert _status(resp) == 400


def test_missing_dest_parent_400(tmp_path):
    src = make_tree(tmp_path / "proj")
    resp = COMPRESS({"path": str(src), "format": "zip",
                     "dest": str(tmp_path / "gone" / "proj.zip")}, x_fused="1")
    assert _status(resp) == 400
    assert "parent directory does not exist" in _data(resp)["error"]


@skip_root
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the readonly guard falls back to os.access(parent, os.W_OK) for a "
           "destination that doesn't exist yet (mount._writable), and Windows' "
           "directory read-only attribute is largely vestigial — long used only "
           "by Explorer's folder-customization UI — so it does not gate creating "
           "entries inside the directory the way a POSIX missing write-bit does. "
           "os.access keeps reporting the chmod'd holder writable and the zip is "
           "written (200), so the 403 this test expects cannot occur there (see "
           "test_server_fs_mutate.py's test_mkdir_readonly_parent_403).")
def test_readonly_destination_403(tmp_path):
    holder = tmp_path / "ro"
    holder.mkdir()
    src = make_tree(holder / "proj")
    os.chmod(holder, 0o555)
    try:
        resp = COMPRESS({"path": str(src), "format": "zip"}, x_fused="1")
        assert _status(resp) == 403
        assert _data(resp)["error"] == "readonly"
    finally:
        os.chmod(holder, 0o755)


# -------------------------------------------------------------------- mounts

def test_mount_backed_source_is_refused_without_walking_it(tmp_path, monkeypatch):
    from fused_render.shell import mounts as shell_mounts

    monkeypatch.setattr(shell_mounts, "is_mount_backed", lambda p: True)
    monkeypatch.setattr(shell_mounts, "mount_read_only", lambda p: False)
    walked = []
    # Thread-scoped: this patch is process-wide and it does not merely record —
    # it returns an EMPTY walk to every caller, so another thread's legitimate
    # walk would silently see nothing, and its arguments would land in `walked`
    # and break `walked == []` for a tree this test never named. Under the
    # fused-engine job the `openfused-invoke-dispatcher` thread enumerates its
    # own request directory on its own schedule. `_fs_compress` is synchronous on
    # the calling thread, so the claim ("the mount source is refused before
    # anything walks it") is proved exactly as before. Do not re-globalise.
    monkeypatch.setattr(
        os, "walk",
        this_thread_only(os.walk, lambda *a, **k: walked.append(a) or iter(())))
    resp = COMPRESS({"path": str(tmp_path / "mnt" / "proj"), "format": "zip"},
                    x_fused="1")
    assert _status(resp) == 400
    assert "compress unsupported" in _data(resp)["error"]
    assert walked == []


def test_read_only_mount_source_is_readonly_403(tmp_path, monkeypatch):
    from fused_render.shell import mounts as shell_mounts

    monkeypatch.setattr(shell_mounts, "is_mount_backed", lambda p: True)
    monkeypatch.setattr(shell_mounts, "mount_read_only", lambda p: True)
    resp = COMPRESS({"path": str(tmp_path / "mnt" / "proj"), "format": "zip"},
                    x_fused="1")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"


def test_mount_backed_destination_is_refused(tmp_path, monkeypatch):
    from fused_render.shell import mounts as shell_mounts

    src = make_tree(tmp_path / "proj")
    dest = tmp_path / "mnt" / "proj.zip"
    monkeypatch.setattr(shell_mounts, "is_mount_backed", lambda p: str(p).startswith(str(tmp_path / "mnt")))
    monkeypatch.setattr(shell_mounts, "mount_read_only", lambda p: False)
    resp = COMPRESS({"path": str(src), "format": "zip", "dest": str(dest)}, x_fused="1")
    assert _status(resp) == 400
    assert "compress unsupported" in _data(resp)["error"]


# ------------------------------------------------------------ no shell, ever

def test_git_is_run_as_an_argv_list_with_no_shell(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    seen = {}
    from fused_render.server import fs_mutate as mod
    real_run = mod.subprocess.run

    def spy(cmd, **kw):
        # argv[0] is now the ABSOLUTE git path — required to reach posix_spawn, since CPython forks unless os.path.dirname(executable) is truthy and a fork with libproj resident SIGSEGVs before exec (tests/test_git_posix_spawn.py). Still one basename, still a list, still no shell, which is what this test is about.
        # .lower(): _git_bin() resolves this via shutil.which("git"), and on
        # Windows shutil.which appends an extension straight from %PATHEXT%
        # (".EXE" by default, uppercase) rather than whatever case the file
        # happens to be stored under — so the basename here is "git.EXE", not
        # "git.exe". NTFS is case-insensitive for the same reason.
        if isinstance(cmd, list) and cmd and os.path.basename(
                str(cmd[0])).lower() in ("git", "git.exe"):
            seen.setdefault("calls", []).append((cmd, kw))
        return real_run(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", spy)
    COMPRESS({"path": str(repo), "format": "git-bundle"}, x_fused="1")
    assert seen["calls"], "expected git to be invoked"
    for cmd, kw in seen["calls"]:
        assert isinstance(cmd, list)
        assert kw.get("shell", False) is False
        assert kw.get("stdin") is subprocess.DEVNULL
        assert kw.get("timeout")

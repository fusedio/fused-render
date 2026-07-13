"""Shape tests for GET /api/fs/list and /api/fs/stat (fused_render/server.py).

Both paths were optimized to minimize stat() round-trips (each is a remote
call under a mount): list() uses os.scandir (type + one stat per entry instead
of os.stat + os.path.isdir's second stat), and stat() does a single os.stat
instead of exists()+isdir()+stat(). These tests pin the observable output so
the round-trip reduction can't silently change is_dir/size/mtime or the 404.
"""
from fastapi.testclient import TestClient

from fused_render.server import create_app


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_list_reports_is_dir_size_and_mtime(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
    data = _client(tmp_path).get("/api/fs/list", params={"path": str(tmp_path)}).json()
    by_name = {e["name"]: e for e in data["entries"]}

    assert by_name["sub"]["is_dir"] is True
    assert by_name["sub"]["size"] is None  # dirs carry no size

    assert by_name["file.txt"]["is_dir"] is False
    assert by_name["file.txt"]["size"] == 5
    assert isinstance(by_name["file.txt"]["mtime"], (int, float))


def test_list_follows_symlink_to_dir(tmp_path):
    # scandir's is_dir()/stat() follow symlinks by default, matching the old
    # os.stat/os.path.isdir behavior: a symlink to a dir reads as a dir.
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    data = _client(tmp_path).get("/api/fs/list", params={"path": str(tmp_path)}).json()
    by_name = {e["name"]: e for e in data["entries"]}
    assert by_name["link"]["is_dir"] is True


def test_list_skips_broken_symlink(tmp_path):
    # stat() on a broken symlink raises -> entry dropped silently (unchanged).
    (tmp_path / "ok.txt").write_text("x", encoding="utf-8")
    (tmp_path / "dangling").symlink_to(tmp_path / "does-not-exist")
    data = _client(tmp_path).get("/api/fs/list", params={"path": str(tmp_path)}).json()
    names = {e["name"] for e in data["entries"]}
    assert "ok.txt" in names
    assert "dangling" not in names


def test_stat_file_shape(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"abcd")
    data = _client(tmp_path).get("/api/fs/stat", params={"path": str(tmp_path / "f.bin")}).json()
    assert data["is_dir"] is False
    assert data["size"] == 4
    assert data["name"] == "f.bin"


def test_stat_dir_shape(tmp_path):
    (tmp_path / "d").mkdir()
    data = _client(tmp_path).get("/api/fs/stat", params={"path": str(tmp_path / "d")}).json()
    assert data["is_dir"] is True
    assert data["size"] is None


def test_stat_missing_path_is_404(tmp_path):
    r = _client(tmp_path).get("/api/fs/stat", params={"path": str(tmp_path / "nope")})
    assert r.status_code == 404

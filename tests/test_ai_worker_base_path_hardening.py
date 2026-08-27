"""Tests for the path-hardening audit (SPEC AI-29, D533).

`mirror.py`'s manifest reader was already hardened (`_safe_name`/
`_safe_filename`/`_safe_etag`) — a manifest is untrusted-origin by
construction (a CDN we do not control) and every one of those checks pinned
in this audit.

The Hub metadata path (`worker_base._resolve` -> `_hub_file_meta`/
`HfApi.model_info`) had NO equivalent check on the `rfilename`/`etag`
`huggingface_hub` hands back before joining either straight into a cache
path (`_FileFetch.blob`, `_FileFetch.link`'s snapshot symlink target) — a
gap this item closes with `_safe_repo_relative_name`/`_safe_blob_name`,
enforced in `_resolve` before a `_FileFetch` is ever constructed.
"""
import threading

import pytest

from test_ai_hub_fetch import _fresh_base, _start_server  # noqa: F401


@pytest.fixture()
def base():
    return _fresh_base()


def _wire_meta(base, monkeypatch, tmp_path, *, name="model.safetensors",
              etag="e7ag", commit="c0m", size=4):
    folder = str(tmp_path / "hub" / "models--org--m")
    monkeypatch.setattr(base, "repo_folder",
                        lambda model_id, repo_type="model": folder)
    monkeypatch.setattr(base, "_hub_file_meta", lambda repo, n, revision: {
        "url": "http://127.0.0.1:1/x", "location": "http://127.0.0.1:1/x",
        "etag": etag, "commit": commit, "size": size})
    return folder


@pytest.mark.parametrize("bad_name", [
    "../../../../etc/cronjob",
    "/etc/passwd",
    "..\\..\\windows\\system32\\evil.dll",
    "a/../../b",
    "",
])
def test_a_hub_reported_filename_that_escapes_the_snapshot_is_refused(
        base, monkeypatch, tmp_path, bad_name):
    _wire_meta(base, monkeypatch, tmp_path)
    with pytest.raises(base._Unsegmentable):
        base._segmented_fetch("org/m", [bad_name], "c0m")


@pytest.mark.parametrize("bad_etag", [
    "../../../../etc/cronjob",
    "/etc/passwd",
    "a/b",
    "a\\b",
    "",
])
def test_a_hub_reported_etag_that_escapes_the_blob_dir_is_refused(
        base, monkeypatch, tmp_path, bad_etag):
    _wire_meta(base, monkeypatch, tmp_path, etag=bad_etag)
    with pytest.raises(base._Unsegmentable):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")


def test_an_ordinary_filename_and_etag_are_unaffected(base, monkeypatch, tmp_path, payload=None):
    """The hardening must not reject the shapes real Hub responses actually
    use — a nested repo-relative path and a hex etag."""
    folder = _wire_meta(base, monkeypatch, tmp_path,
                        name="onnx/model.onnx", etag="deadbeef")
    url, state = _start_server(b"1234")
    monkeypatch.setattr(base, "_hub_file_meta", lambda repo, n, revision: {
        "url": url, "location": url, "etag": "deadbeef", "commit": "c0m", "size": 4})
    snapshot = base._segmented_fetch("org/m", ["onnx/model.onnx"], "c0m")
    import os
    assert os.path.isfile(os.path.join(snapshot, "onnx", "model.onnx"))

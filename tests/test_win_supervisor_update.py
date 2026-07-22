"""Update manifest verification: a tampered version, signature, or binary
must be refused, and only a signature over the exact (version, sha256) the
download hashes to is accepted."""
import base64
import hashlib
import json
import os
import tempfile

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fused_render.win_supervisor import update


class _Response:
    def __init__(self, payload: bytes):
        self._payload = payload
        self._done = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        # Deliver the whole payload once, then EOF — works for both a single
        # capped read() and the streaming chunk loop.
        if self._done:
            return b""
        self._done = True
        return self._payload


def _sign(key: Ed25519PrivateKey, version: str, sha256: str) -> str:
    message = f"{update._SIGNING_CONTEXT}\n{version}\n{sha256}\n".encode("utf-8")
    return base64.b64encode(key.sign(message)).decode()


def _install_key(monkeypatch) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setattr(update, "_PUBLIC_KEY", public)
    return key


@pytest.mark.parametrize(
    "candidate,current,expected",
    [("0.4.0", "0.3.7", True), ("0.3.7", "0.3.7", False),
     ("0.3.6", "0.3.7", False), ("0.3.7.1", "0.3.7", True)],
)
def test_is_newer(candidate, current, expected):
    assert update._is_newer(candidate, current) is expected


def test_fetch_manifest_rejects_malformed(monkeypatch):
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda *a, **k: _Response(json.dumps({"schema": 2}).encode()))
    with pytest.raises(ValueError):
        update._fetch_manifest()


def test_download_verified_roundtrip(monkeypatch):
    key = _install_key(monkeypatch)
    installer = b"the real installer bytes"
    sha256 = hashlib.sha256(installer).hexdigest()
    manifest = {"schema": 1, "version": "0.4.0", "url": "https://x/setup.exe",
                "sha256": sha256, "signature": _sign(key, "0.4.0", sha256)}
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda *a, **k: _Response(installer))

    path = update._download_verified(manifest)
    with open(path, "rb") as f:
        assert f.read() == installer


def test_fetch_manifest_verifies_signature_before_returning(monkeypatch):
    # The version is only trusted after the signature checks out, so a bad
    # signature is rejected at fetch — before any "up to date"/prompt decision.
    _install_key(monkeypatch)
    sha256 = hashlib.sha256(b"x").hexdigest()
    manifest = {"schema": 1, "version": "0.4.0", "url": "https://x/setup.exe",
                "sha256": sha256, "signature": base64.b64encode(b"\x00" * 64).decode()}
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda *a, **k: _Response(json.dumps(manifest).encode()))
    with pytest.raises(ValueError):
        update._fetch_manifest()


def test_fetch_manifest_accepts_valid_signature(monkeypatch):
    key = _install_key(monkeypatch)
    sha256 = hashlib.sha256(b"x").hexdigest()
    manifest = {"schema": 1, "version": "0.4.0", "url": "https://x/setup.exe",
                "sha256": sha256, "signature": _sign(key, "0.4.0", sha256)}
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda *a, **k: _Response(json.dumps(manifest).encode()))
    assert update._fetch_manifest()["version"] == "0.4.0"


@pytest.mark.parametrize("payload", ["null", "[]", "42", '"a string"'])
def test_fetch_manifest_rejects_non_object(monkeypatch, payload):
    # Valid JSON that isn't an object must not crash with AttributeError.
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda *a, **k: _Response(payload.encode()))
    with pytest.raises(ValueError):
        update._fetch_manifest()


def test_download_verified_rejects_tampered_binary(monkeypatch):
    key = _install_key(monkeypatch)
    sha256 = hashlib.sha256(b"the signed installer").hexdigest()
    manifest = {"schema": 1, "version": "0.4.0", "url": "https://x/setup.exe",
                "sha256": sha256, "signature": _sign(key, "0.4.0", sha256)}
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda *a, **k: _Response(b"a different, swapped installer"))
    with pytest.raises(ValueError):
        update._download_verified(manifest)


class _Paths:
    def __init__(self):
        self.logs = []

    def log(self, message):
        self.logs.append(message)


@pytest.fixture
def paths():
    return _Paths()


@pytest.fixture(autouse=True)
def _reset_prompted(monkeypatch):
    monkeypatch.setattr(update, "_prompted_versions", set())


def _wire(monkeypatch, *, current, available, decision, installer="C:/tmp/setup.exe"):
    started, prompts, alerts = [], [], []
    monkeypatch.setattr(update, "__version__", current)
    monkeypatch.setattr(update, "_fetch_manifest", lambda: {"version": available, "sha256": "ok"})
    monkeypatch.setattr(update, "_download_verified", lambda manifest: installer)
    monkeypatch.setattr(update, "_sha256_file", lambda path: "ok")  # pre-launch re-verify passes
    monkeypatch.setattr(update, "_prompt_install", lambda version: prompts.append(version) or decision)
    monkeypatch.setattr(update, "_alert", lambda text, icon: alerts.append(text))
    monkeypatch.setattr(update.os, "startfile", lambda path: started.append(path))
    return started, prompts, alerts


def test_auto_check_silent_when_up_to_date(monkeypatch, paths):
    started, prompts, alerts = _wire(monkeypatch, current="9.9.9", available="0.0.1", decision=update._IDYES)
    update._auto_check(paths)
    assert not started and not prompts and not alerts


def test_auto_check_installs_when_accepted(monkeypatch, paths):
    started, prompts, _ = _wire(monkeypatch, current="0.3.7", available="0.4.0", decision=update._IDYES)
    update._auto_check(paths)
    assert prompts == ["0.4.0"] and started == ["C:/tmp/setup.exe"]


def test_auto_check_no_install_when_declined(monkeypatch, paths):
    started, prompts, alerts = _wire(monkeypatch, current="0.3.7", available="0.4.0", decision=7)  # IDNO
    update._auto_check(paths)
    assert prompts == ["0.4.0"] and not started and not alerts  # decline stays silent


def test_auto_check_prompts_once_per_version(monkeypatch, paths):
    _started, prompts, _ = _wire(monkeypatch, current="0.3.7", available="0.4.0", decision=7)
    update._auto_check(paths)
    update._auto_check(paths)
    assert prompts == ["0.4.0"]  # second tick is suppressed


def test_auto_check_silent_on_error(monkeypatch, paths):
    _started, _prompts, alerts = _wire(monkeypatch, current="0.3.7", available="0.4.0", decision=7)

    def boom():
        raise OSError("network down")

    monkeypatch.setattr(update, "_fetch_manifest", boom)
    update._auto_check(paths)
    assert not alerts and paths.logs  # logged, never a dialog


def test_manual_check_reports_up_to_date(monkeypatch, paths):
    _started, _prompts, alerts = _wire(monkeypatch, current="9.9.9", available="0.0.1", decision=update._IDYES)
    update.check(paths)
    assert alerts and "up to date" in alerts[0]


def test_auto_check_survives_nonnumeric_version(monkeypatch, paths):
    monkeypatch.setattr(update, "__version__", "0.3.7")
    monkeypatch.setattr(update, "_fetch_manifest", lambda: {"version": "0.4.0rc1"})
    offered = []
    monkeypatch.setattr(update, "_offer_install", lambda *a, **k: offered.append(a))
    update._auto_check(paths)  # a non-numeric version must not raise out and kill the loop
    assert not offered and paths.logs


def test_manual_check_busy_reports_in_progress(monkeypatch, paths):
    alerts = []
    monkeypatch.setattr(update, "_alert", lambda text, icon: alerts.append(text))
    update._check_lock.acquire()
    try:
        update.check(paths)
    finally:
        update._check_lock.release()
    assert alerts and "in progress" in alerts[0]


def test_download_verified_rejects_non_https(monkeypatch):
    key = _install_key(monkeypatch)
    sha256 = hashlib.sha256(b"x").hexdigest()
    manifest = {"schema": 1, "version": "0.4.0", "url": "http://x/setup.exe",
                "sha256": sha256, "signature": _sign(key, "0.4.0", sha256)}
    with pytest.raises(ValueError):
        update._download_verified(manifest)


def test_offer_install_declined_is_remembered_and_cleaned(monkeypatch, paths):
    fd, staged = tempfile.mkstemp(prefix="FusedRenderPy-", suffix="-setup.exe")
    os.close(fd)
    monkeypatch.setattr(update, "_download_verified", lambda manifest: staged)
    monkeypatch.setattr(update, "_prompt_install", lambda version: 7)  # IDNO
    update._offer_install(paths, {"version": "0.4.0"}, announce_errors=False)
    assert "0.4.0" in update._prompted_versions and not os.path.exists(staged)


def test_offer_install_accept_but_launch_fails_reoffers(monkeypatch, paths):
    fd, staged = tempfile.mkstemp(prefix="FusedRenderPy-", suffix="-setup.exe")
    with os.fdopen(fd, "wb") as f:
        f.write(b"installer bytes")
    sha256 = hashlib.sha256(b"installer bytes").hexdigest()
    alerts = []
    monkeypatch.setattr(update, "_download_verified", lambda manifest: staged)
    monkeypatch.setattr(update, "_prompt_install", lambda version: update._IDYES)
    monkeypatch.setattr(update, "_alert", lambda text, icon: alerts.append(text))

    def boom(path):
        raise OSError("no shell association")

    monkeypatch.setattr(update.os, "startfile", boom)
    # announce_errors=False (the background path) — a post-accept failure must
    # still alert, since the user clicked Install.
    update._offer_install(paths, {"version": "0.4.0", "sha256": sha256}, announce_errors=False)
    # An accepted-but-failed launch must NOT be suppressed — it retries later.
    assert "0.4.0" not in update._prompted_versions and not os.path.exists(staged) and alerts


def test_offer_install_rejects_binary_swapped_during_prompt(monkeypatch, paths):
    fd, staged = tempfile.mkstemp(prefix="FusedRenderPy-", suffix="-setup.exe")
    with os.fdopen(fd, "wb") as f:
        f.write(b"swapped after verify")
    started, alerts = [], []
    monkeypatch.setattr(update, "_download_verified", lambda manifest: staged)
    monkeypatch.setattr(update, "_prompt_install", lambda version: update._IDYES)
    monkeypatch.setattr(update.os, "startfile", lambda path: started.append(path))
    monkeypatch.setattr(update, "_alert", lambda text, icon: alerts.append(text))
    # manifest sha256 is for different bytes → the pre-launch re-verify fails.
    update._offer_install(paths, {"version": "0.4.0", "sha256": hashlib.sha256(b"original").hexdigest()},
                          announce_errors=False)
    assert not started and not os.path.exists(staged) and alerts  # refused + user told


def test_sweep_skips_while_check_in_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(update.tempfile, "gettempdir", lambda: str(tmp_path))
    staged = tmp_path / "FusedRenderPy-x-setup.exe"
    staged.write_bytes(b"x")
    update._check_lock.acquire()
    try:
        update._sweep_stale_downloads()
        assert staged.exists()  # a check holds the lock → staged file untouched
    finally:
        update._check_lock.release()
    update._sweep_stale_downloads()
    assert not staged.exists()  # lock free → swept


def test_start_auto_checks_disabled_by_env(monkeypatch, paths):
    monkeypatch.setenv("FUSED_RENDER_NO_AUTO_UPDATE", "1")

    def fail(*a, **k):
        raise AssertionError("must not spawn the loop when disabled")

    monkeypatch.setattr(update.threading, "Thread", fail)
    update.start_auto_checks(paths)

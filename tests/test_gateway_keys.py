"""`ai/gateway_keys.py` (docs/LOCAL_MODEL_GATEWAY.md §1): the persisted
machine key and the in-memory session keys the `/v1` gateway accepts."""
import os
import stat

import pytest

from fused_render.ai import gateway_keys


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    gateway_keys._reset_for_tests()
    yield
    gateway_keys._reset_for_tests()


def test_machine_key_is_persisted_at_home_ai_v1_key_with_0600(tmp_path):
    key = gateway_keys.machine_key()
    path = gateway_keys.key_path()
    assert path == os.path.join(str(tmp_path / "home"), "ai", "v1_key")
    assert os.path.isfile(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert len(key) >= 40  # urlsafe of 32 random bytes
    with open(path, encoding="utf-8") as fh:
        assert fh.read().strip() == key


def test_machine_key_is_stable_across_calls_and_re_reads_from_disk():
    first = gateway_keys.machine_key()
    assert gateway_keys.machine_key() == first
    with open(gateway_keys.key_path(), "w", encoding="utf-8") as fh:
        fh.write("pasted-by-the-user\n")
    assert gateway_keys.machine_key() == "pasted-by-the-user"


def test_session_keys_are_valid_until_released():
    key = gateway_keys.mint_session_key()
    assert gateway_keys.is_valid(key)
    other = gateway_keys.mint_session_key()
    assert other != key and gateway_keys.is_valid(other)
    gateway_keys.release_key(key)
    assert not gateway_keys.is_valid(key)
    assert gateway_keys.is_valid(other)
    gateway_keys.release_key("never-minted")  # a no-op, never a raise


def test_is_valid_accepts_the_machine_key_and_nothing_else():
    assert gateway_keys.is_valid(gateway_keys.machine_key())
    assert not gateway_keys.is_valid("")
    assert not gateway_keys.is_valid(None)
    assert not gateway_keys.is_valid("wrong")
    assert not gateway_keys.is_valid(gateway_keys.machine_key() + "x")


def test_session_keys_never_touch_the_disk():
    gateway_keys.mint_session_key()
    assert not os.path.exists(gateway_keys.key_path())

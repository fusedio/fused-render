"""rclone_bin() resolution order: FUSED_RENDER_RCLONE_BIN override →
macOS bundle → PATH. The override is what the packaged Windows installer and
Linux AppImage set (via the supervisor's child_environment) so bundled rclone
wins over path-guessing. Monkeypatched env/fs — no real rclone needed."""
import pytest

import fused_render.shell.mounts as mounts_mod


@pytest.fixture(autouse=True)
def _clear_override(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_RCLONE_BIN", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)


def test_env_override_wins_when_it_is_a_file(tmp_path, monkeypatch):
    bundled = tmp_path / "rclone"
    bundled.write_text("")
    monkeypatch.setenv("FUSED_RENDER_RCLONE_BIN", str(bundled))
    # Even a packaged macOS bundle + a PATH hit must lose to the explicit env.
    monkeypatch.setattr(mounts_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/usr/bin/rclone")
    assert mounts_mod.rclone_bin() == str(bundled)


def test_env_override_ignored_when_not_a_file(monkeypatch):
    # A stale/wrong override must not shadow a real PATH rclone (dev safety).
    monkeypatch.setenv("FUSED_RENDER_RCLONE_BIN", "/nonexistent/rclone")
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/usr/local/bin/rclone")
    assert mounts_mod.rclone_bin() == "/usr/local/bin/rclone"


def test_macos_bundle_used_when_no_override(tmp_path, monkeypatch):
    contents = tmp_path / "FusedRender.app" / "Contents"
    bundled = contents / "Resources" / "bin" / "rclone"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    monkeypatch.setattr(mounts_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "executable", str(contents / "MacOS" / "python"))
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/should/not/be/used")
    assert mounts_mod.rclone_bin() == str(bundled)


def test_path_fallback_when_no_override_no_bundle(monkeypatch):
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/usr/local/bin/rclone")
    assert mounts_mod.rclone_bin() == "/usr/local/bin/rclone"


# -- macOS version gate on the bundled binary (D1318) ------------------------
#
# The upstream osx-arm64 rclone carries LC_BUILD_VERSION minos 15.0, which dyld
# refuses on macOS 14 and older. rclone_bin() reads the minimum from the Mach-O
# header (never otool at runtime) and skips the bundle on an older Mac.

import platform  # noqa: E402
import struct  # noqa: E402


def _macho(minos: tuple[int, int], *, legacy: bool = False) -> bytes:
    """A minimal thin arm64 Mach-O header with one version load command."""
    if legacy:
        cmd = struct.pack("<IIII", 0x24, 16, (minos[0] << 16) | (minos[1] << 8), 0)
    else:
        cmd = struct.pack("<IIIIII", 0x32, 24, 1, (minos[0] << 16) | (minos[1] << 8), 0, 0)
    hdr = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, len(cmd), 0, 0)
    return hdr + cmd


def _bundle(tmp_path, monkeypatch, content: bytes, mac_ver: str):
    contents = tmp_path / "FusedRender.app" / "Contents"
    bundled = contents / "Resources" / "bin" / "rclone"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(content)
    monkeypatch.setattr(mounts_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "executable", str(contents / "MacOS" / "python"))
    monkeypatch.setattr(platform, "mac_ver", lambda: (mac_ver, ("", "", ""), ""))
    return bundled


def test_old_macos_skips_bundle_and_uses_path(tmp_path, monkeypatch):
    _bundle(tmp_path, monkeypatch, _macho((15, 0)), "14.6")
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/opt/homebrew/bin/rclone")
    assert mounts_mod.rclone_bin() == "/opt/homebrew/bin/rclone"
    assert mounts_mod.rclone_unavailable_reason() is None


def test_old_macos_no_path_rclone_gives_version_reason(tmp_path, monkeypatch):
    _bundle(tmp_path, monkeypatch, _macho((15, 0)), "14.6")
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: None)
    assert mounts_mod.rclone_bin() is None
    reason = mounts_mod.rclone_unavailable_reason()
    assert "macOS 15" in reason and "14.6" in reason and "brew install rclone" in reason
    assert mounts_mod.rclone_missing_message() == reason


def test_new_enough_macos_uses_bundle(tmp_path, monkeypatch):
    bundled = _bundle(tmp_path, monkeypatch, _macho((15, 0)), "15.1")
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/should/not/be/used")
    assert mounts_mod.rclone_bin() == str(bundled)
    assert mounts_mod.rclone_unavailable_reason() is None


def test_legacy_version_min_command_is_read(tmp_path, monkeypatch):
    _bundle(tmp_path, monkeypatch, _macho((15, 0), legacy=True), "14.0")
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: None)
    assert mounts_mod.rclone_bin() is None


def test_unreadable_header_keeps_legacy_behaviour(tmp_path, monkeypatch):
    bundled = _bundle(tmp_path, monkeypatch, b"not a mach-o", "14.6")
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: "/should/not/be/used")
    assert mounts_mod.rclone_bin() == str(bundled)


def test_unknown_os_version_keeps_bundle(tmp_path, monkeypatch):
    # mac_ver() can answer "" (or a compat-mode 10.16); never skip on a guess.
    for ver in ("", "10.16"):
        bundled = _bundle(tmp_path / ver.replace(".", "_") / "x", monkeypatch,
                          _macho((15, 0)), ver)
        monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: None)
        assert mounts_mod.rclone_bin() == str(bundled)


def test_env_override_beats_version_gate(tmp_path, monkeypatch):
    _bundle(tmp_path, monkeypatch, _macho((15, 0)), "14.6")
    override = tmp_path / "my-rclone"
    override.write_text("")
    monkeypatch.setenv("FUSED_RENDER_RCLONE_BIN", str(override))
    assert mounts_mod.rclone_bin() == str(override)


def test_automount_skips_quietly_without_rclone_or_daemon(monkeypatch):
    from fused_render.shell.mounts import health as health_mod
    monkeypatch.setattr(health_mod, "prune_builtin_mounts", lambda: None)
    monkeypatch.setattr(mounts_mod, "rclone_bin", lambda: None)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda **k: None)
    monkeypatch.setattr(mounts_mod, "list_mounts", lambda: [{"id": "m", "name": "m"}])

    def boom(m):
        raise AssertionError("attach must not run without rclone")

    monkeypatch.setattr(health_mod, "attach_mount", boom)
    health_mod.run_automount()


def test_missing_message_is_generic_without_skip(monkeypatch):
    monkeypatch.setattr(mounts_mod.shutil, "which", lambda name: None)
    assert mounts_mod.rclone_unavailable_reason() is None
    assert mounts_mod.rclone_missing_message() == "rclone is not installed"


@pytest.mark.parametrize("bad_size", [0, 1, 4, 7])
def test_macho_load_command_smaller_than_its_header_is_rejected(tmp_path, monkeypatch, bad_size):
    # cmdsize < 8 would seek backwards/in place and re-read bytes; a real
    # LC_BUILD_VERSION follows, but the parser must give up, not wander.
    bad = struct.pack("<II", 0x1, bad_size)
    good = struct.pack("<IIIIII", 0x32, 24, 1, (15 << 16), 0, 0)
    hdr = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 2, len(bad + good), 0, 0)
    p = tmp_path / "rclone"
    p.write_bytes(hdr + bad + good)
    # No byte pattern can make the re-read decode as a version command, so
    # also pin the mechanism: the parser must never seek backwards.
    real_open = open
    back = []

    class Spy:
        def __init__(self, f):
            self._f = f

        def seek(self, off, whence=0):
            if whence == 1 and off < 0:
                back.append(off)
            return self._f.seek(off, whence)

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self._f.__exit__(*a)

    monkeypatch.setattr("builtins.open", lambda *a, **k: Spy(real_open(*a, **k)))
    assert mounts_mod.rcd._macho_min_os(str(p)) is None
    assert back == []

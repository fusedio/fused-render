"""parse_args() for the supervisor CLI. Pure wire-protocol argument parsing —
no OS backend, no win32pipe — so it imports and runs on every platform."""
import pytest

from fused_render.supervisor import protocol


def test_no_args_is_open_home():
    assert protocol.parse_args([]) == protocol.OpenHome()


def test_startup_flag():
    assert protocol.parse_args(["--startup"]) == protocol.StartInBackground()


def test_shutdown_flag():
    assert protocol.parse_args(["--shutdown-for-upgrade"]) == protocol.ShutdownForUpgrade()


def test_single_file_path_is_open():
    assert protocol.parse_args(["/data/x.parquet"]) == protocol.Open("/data/x.parquet")


def test_single_deep_link_flows_through_as_open():
    # A fused-render:// deep link is a single non-flag arg -> Open, verbatim
    # (the /clone routing happens later in core._open_command).
    raw = "fused-render://open?git=https://github.com/o/r"
    assert protocol.parse_args([raw]) == protocol.Open(raw)


def test_multiple_args_error_mentions_url():
    with pytest.raises(protocol.ProtocolError, match="URL"):
        protocol.parse_args(["a", "b"])

"""What canvas_mirror.py copies from the `fused` CLI, checked against it.

The runner classifies a `canvas mirror` run by strings and numbers the CLI
owns: the words of its "another run holds this root" error, the name of its
lock file and the `pid` field in it, the shape of a progress line, and two
exit codes. A `fused` pin bump that changed any of them would silently turn
`busy` into `failed`, or leave a killed child's lock in place for an hour.

Skipped while the installed `fused` predates the command (the pinned one
does); runs as soon as one that has it is installed.
"""
import json
import os
import types
from pathlib import Path

import pytest

from fused_render import canvas_marker, canvas_mirror

lock_mod = pytest.importorskip("fused.workbench.cli.canvas_mirror_lock")
mirror_mod = pytest.importorskip("fused.workbench.cli.canvas_mirror")
click = pytest.importorskip("click")


def test_the_lock_file_and_its_pid_field(tmp_path):
    assert lock_mod.LOCK_FILENAME == canvas_mirror.LOCK_NAME
    with lock_mod.mirror_lock(tmp_path, lambda line: None):
        record = json.loads((tmp_path / canvas_mirror.LOCK_NAME).read_text())
        assert record[canvas_mirror.LOCK_PID_FIELD] == os.getpid()
    assert not (tmp_path / canvas_mirror.LOCK_NAME).exists()
    # The staleness the run's hard timeout must stay under.
    assert canvas_mirror.RUN_TIMEOUT_S < lock_mod.LOCK_STALE_AFTER.total_seconds()


def test_the_busy_message_and_its_exit_code(tmp_path):
    with lock_mod.mirror_lock(tmp_path, lambda line: None):
        with pytest.raises(lock_mod.MirrorLockError) as held:
            with lock_mod.mirror_lock(tmp_path, lambda line: None):
                pass
    assert canvas_mirror.BUSY_NEEDLE in held.value.format_message().lower()
    # A ClickException: exit 1 with no document on stdout, which the runner
    # tells from a partial result only by that message.
    assert held.value.exit_code == canvas_mirror.EXIT_PARTIAL


def test_an_unknown_command_is_a_usage_error_with_those_words():
    assert click.UsageError.exit_code == canvas_mirror.EXIT_USAGE

    @click.group()
    def group():
        pass

    with pytest.raises(click.UsageError) as unknown:
        group.main(["mirror"], standalone_mode=False)
    assert canvas_mirror.NO_COMMAND_NEEDLE in unknown.value.format_message().lower()


def test_a_progress_line_parses():
    outcome = types.SimpleNamespace(action="pulled", path="public/Some Canvas", error=None,
                                    scope="public", name="Some Canvas", id="c")
    line = mirror_mod._progress_line(12, 340, outcome)
    match = canvas_mirror._PROGRESS_RE.match(line.splitlines()[0])
    assert match is not None, line
    assert (int(match.group(1)), int(match.group(2))) == (12, 340)
    assert match.group(3) == "pulled" and match.group(4).startswith("public/Some Canvas")


def test_the_marker_file_name_is_the_clis():
    cli_marker = pytest.importorskip("fused.workbench.cli.canvas_marker")
    names = {v for v in vars(cli_marker).values() if isinstance(v, str)}
    assert canvas_marker.MARKER_NAME in names
    assert Path(canvas_marker.marker_path("/x")).name == canvas_marker.MARKER_NAME

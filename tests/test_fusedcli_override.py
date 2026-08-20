"""FUSED_RENDER_FUSED_BIN -> argv, across both platforms' quoting rules.

The override is how a dev points at a local build and how the test suite
substitutes a stub CLI, so it is parsed on the canvases request path. It used
to be a plain `override.split()`, which is fine for the documented compound
form ("uv run fused") and wrong for the single most common interpreter
location on Windows: `C:\\Program Files\\...` came apart mid-path, and the
override then "wasn't found" (or ran something else) with nothing in the
failure naming the space as the cause.

`os.name` is patched rather than skipping per platform: the branch is selected
on os.name alone, so both halves are checked from any host. That matters here
because the two lexers are NOT interchangeable — each is actively wrong on the
other platform, which is the whole reason the branch exists.
"""
import os
from unittest import mock

import pytest

from fused_render import fusedcli


@pytest.fixture()
def as_posix(monkeypatch):
    monkeypatch.setattr(fusedcli.os, "name", "posix")


@pytest.fixture()
def as_windows(monkeypatch):
    monkeypatch.setattr(fusedcli.os, "name", "nt")


# -- the documented compound form, unchanged on both ---------------------------


@pytest.mark.parametrize("name", ["posix", "nt"])
def test_a_compound_command_still_splits(monkeypatch, name):
    monkeypatch.setattr(fusedcli.os, "name", name)
    assert fusedcli._split_override("uv run fused") == ["uv", "run", "fused"]


@pytest.mark.parametrize("name", ["posix", "nt"])
def test_a_plain_single_path_is_one_arg(monkeypatch, name):
    monkeypatch.setattr(fusedcli.os, "name", name)
    assert fusedcli._split_override("/usr/bin/fused") == ["/usr/bin/fused"]


# -- the bug: a path containing spaces -----------------------------------------


def test_a_quoted_windows_program_files_path_survives(as_windows):
    """The reported bug. Quoting has to be the fix — an unquoted path with a
    space is ambiguous by construction (see the test below)."""
    assert fusedcli._split_override(
        r'"C:\Program Files\Python\python.exe" -m fused'
    ) == [r"C:\Program Files\Python\python.exe", "-m", "fused"]


def test_a_quoted_posix_path_with_spaces_survives(as_posix):
    assert fusedcli._split_override('"/opt/my apps/py" -m fused') == [
        "/opt/my apps/py", "-m", "fused"]


def test_an_unquoted_path_with_spaces_still_splits(as_windows):
    """Documented limitation, asserted so it stays a known shape rather than a
    surprise: no lexer can tell where this path ends, and a shell could not
    read it either."""
    assert fusedcli._split_override(
        r'C:\Program Files\Python\python.exe -m fused'
    ) == [r"C:\Program", r"Files\Python\python.exe", "-m", "fused"]


# -- why the lexer is chosen per platform --------------------------------------


def test_windows_backslashes_are_not_eaten_as_escapes(as_windows):
    """posix=True would parse this to "C:Pythonpython.exe" — not a path at all.
    This is the assertion that pins the non-posix lexer on Windows."""
    assert fusedcli._split_override(r"C:\Python\python.exe -m fused") == [
        r"C:\Python\python.exe", "-m", "fused"]


def test_posix_backslash_escapes_still_work(as_posix):
    """The mirror of the above: posix=False would leave this split in two, so
    the posix lexer has to stay on POSIX."""
    assert fusedcli._split_override("/opt/my\\ apps/py -m fused") == [
        "/opt/my apps/py", "-m", "fused"]


# -- malformed input degrades, it does not raise -------------------------------


@pytest.mark.parametrize("name", ["posix", "nt"])
def test_an_unbalanced_quote_falls_back_instead_of_raising(monkeypatch, name):
    """fused_cli() is on the canvases request path; a malformed env var must
    not turn every call into a 500."""
    monkeypatch.setattr(fusedcli.os, "name", name)
    assert fusedcli._split_override('"C:\\unbalanced') == ['"C:\\unbalanced']


# -- and the resolution around it ----------------------------------------------


def test_fused_cli_uses_the_parsed_override(monkeypatch, as_windows):
    monkeypatch.setenv("FUSED_RENDER_FUSED_BIN",
                       r'"C:\Program Files\Python\python.exe" -m fused')
    cli = fusedcli.fused_cli()
    assert cli is not None
    assert cli.external is True
    assert cli.command == [r"C:\Program Files\Python\python.exe", "-m", "fused"]


def test_a_whitespace_only_override_resolves_to_no_cli(monkeypatch):
    """An override that parses to nothing must not become an empty argv a
    subprocess call would choke on."""
    monkeypatch.setenv("FUSED_RENDER_FUSED_BIN", "   ")
    with mock.patch.object(fusedcli.importlib.util, "find_spec",
                           return_value=None):
        assert fusedcli.fused_cli() is None

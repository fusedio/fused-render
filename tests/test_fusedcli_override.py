"""FUSED_RENDER_FUSED_BIN -> argv, honouring POSIX shell quoting.

The override is how a dev points at a local build and how the test suite
substitutes a stub CLI, so it is parsed on the canvases request path. It used
to be a plain `override.split()`, which is fine for the documented compound
form ("uv run fused") and wrong for any path containing a space: it came apart
mid-path, and the override then "wasn't found" (or ran something else) with
nothing in the failure naming the space as the cause.
"""
from unittest import mock

from fused_render import fusedcli


# -- the documented compound form, unchanged -----------------------------------


def test_a_compound_command_still_splits():
    assert fusedcli._split_override("uv run fused") == ["uv", "run", "fused"]


def test_a_plain_single_path_is_one_arg():
    assert fusedcli._split_override("/usr/bin/fused") == ["/usr/bin/fused"]


# -- the bug: a path containing spaces -----------------------------------------


def test_a_quoted_posix_path_with_spaces_survives():
    assert fusedcli._split_override('"/opt/my apps/py" -m fused') == [
        "/opt/my apps/py", "-m", "fused"]


def test_an_unquoted_path_with_spaces_still_splits():
    """Documented limitation, asserted so it stays a known shape rather than a
    surprise: no lexer can tell where this path ends, and a shell could not
    read it either."""
    assert fusedcli._split_override("/opt/my apps/py -m fused") == [
        "/opt/my", "apps/py", "-m", "fused"]


def test_posix_backslash_escapes_still_work():
    """posix=False would leave this split in two, so the posix lexer has to
    stay."""
    assert fusedcli._split_override("/opt/my\\ apps/py -m fused") == [
        "/opt/my apps/py", "-m", "fused"]


# -- malformed input degrades, it does not raise -------------------------------


def test_an_unbalanced_quote_falls_back_instead_of_raising():
    """fused_cli() is on the canvases request path; a malformed env var must
    not turn every call into a 500."""
    assert fusedcli._split_override('"/opt/unbalanced') == ['"/opt/unbalanced']


# -- and the resolution around it ----------------------------------------------


def test_fused_cli_uses_the_parsed_override(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_FUSED_BIN", '"/opt/my apps/py" -m fused')
    cli = fusedcli.fused_cli()
    assert cli is not None
    assert cli.external is True
    assert cli.command == ["/opt/my apps/py", "-m", "fused"]


def test_a_whitespace_only_override_resolves_to_no_cli(monkeypatch):
    """An override that parses to nothing must not become an empty argv a
    subprocess call would choke on."""
    monkeypatch.setenv("FUSED_RENDER_FUSED_BIN", "   ")
    with mock.patch.object(fusedcli.importlib.util, "find_spec",
                           return_value=None):
        assert fusedcli.fused_cli() is None

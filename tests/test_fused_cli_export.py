"""The `fused` CLI handed to the Claude sessions we spawn (D334).

Three parties have to agree, and each half is pinned here:

* `fusedcli.export_fused_cli_env` writes a wrapper under home_dir()/fused-bin,
  prepends that dir to PATH and publishes it as FUSED_RENDER_FUSED_CLI_DIR —
  or unpublishes everything when there is no CLI to wrap.
* the wrapper itself bakes in the same env-targeting canvases.py sets for its
  own runs (FUSED_ENV defaults to workbench_env(); external CLIs get the
  PYTHONHOME/PYTHONPATH scrub) so a bare `fused` from a chat hits the same
  environment the canvases iframe shows.
* the claude template pre-allows `Bash(fused:*)` and discloses the CLI in its
  prompt exactly when the var is set — a machine without the CLI must never
  get a prompt promising a command that would fail.
"""
import importlib.util
import json
import os
import stat
import sys

import pytest

from fused_render import fusedcli
from fused_render.shell.storage import home_dir

TEMPLATES = os.path.join(os.path.dirname(__file__), "..",
                         "fused_render", "templates")


def _load_agent():
    path = os.path.abspath(os.path.join(TEMPLATES, "claude", "agent.py"))
    shared = os.path.abspath(os.path.join(TEMPLATES, "shared"))
    if shared not in sys.path:
        sys.path.insert(0, shared)
    spec = importlib.util.spec_from_file_location("claude_agent_fusedcli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load_agent()


def _export_with_stub(monkeypatch, tmp_path, external=True):
    # Own home per test: the wrapper path derives from home_dir(), and a
    # shared one is a write-write race under xdist.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    stub = fusedcli.FusedCli(command=["/opt/stub/fused-real", "sub cmd"],
                             external=external)
    monkeypatch.setattr(fusedcli, "fused_cli", lambda: stub)
    return fusedcli.export_fused_cli_env()


def test_export_writes_an_executable_wrapper_and_publishes_it(monkeypatch, tmp_path):
    monkeypatch.delenv(fusedcli.CLI_DIR_ENV, raising=False)
    bin_dir = _export_with_stub(monkeypatch, tmp_path)
    assert bin_dir == os.path.join(home_dir(), fusedcli.CLI_BIN_SUBDIR)
    assert os.environ[fusedcli.CLI_DIR_ENV] == bin_dir
    assert bin_dir in os.environ["PATH"].split(os.pathsep)
    wrapper = os.path.join(bin_dir,
                           "fused.cmd" if os.name == "nt" else "fused")
    assert os.path.isfile(wrapper)
    if os.name != "nt":
        assert os.stat(wrapper).st_mode & stat.S_IXUSR
    text = open(wrapper, encoding="utf-8").read()
    # The resolved command is in there, quoted as one argv (the space inside
    # the second element must survive as a single argument).
    assert "/opt/stub/fused-real" in text
    assert "'sub cmd'" in text or '"sub cmd"' in text


def test_export_is_idempotent_on_path(monkeypatch, tmp_path):
    monkeypatch.delenv(fusedcli.CLI_DIR_ENV, raising=False)
    bin_dir = _export_with_stub(monkeypatch, tmp_path)
    _export_with_stub(monkeypatch, tmp_path)
    assert os.environ["PATH"].split(os.pathsep).count(bin_dir) == 1


def test_no_cli_unpublishes_the_var(monkeypatch):
    monkeypatch.setenv(fusedcli.CLI_DIR_ENV, "/stale")
    monkeypatch.setattr(fusedcli, "fused_cli", lambda: None)
    assert fusedcli.export_fused_cli_env() is None
    assert fusedcli.CLI_DIR_ENV not in os.environ


def test_wrapper_defaults_fused_env_but_never_clobbers_an_explicit_one(
        monkeypatch, tmp_path):
    """The one correctness rule of the wrapper: a bare `fused` targets the
    workbench env canvases.py syncs against (not the CLI's own default), while
    `FUSED_ENV=x fused ...` from the model still wins — a default, not an
    export-over-the-top."""
    monkeypatch.setenv("FUSED_RENDER_WORKBENCH_ENV", "stg")
    bin_dir = _export_with_stub(monkeypatch, tmp_path)
    if os.name == "nt":
        pytest.skip("sh wrapper semantics; the .cmd branch mirrors them")
    text = open(os.path.join(bin_dir, "fused"), encoding="utf-8").read()
    # Assign only when unset/empty — "default, not override". The assignment
    # sits outside any double quotes so shlex.quote's single quotes actually
    # quote (inside "${VAR:=...}" they would become literal characters).
    assert '[ -n "${FUSED_ENV:-}" ] || FUSED_ENV=stg' in text
    assert "export FUSED_ENV" in text


def test_wrapper_scrubs_interpreter_vars_only_for_an_external_cli(
        monkeypatch, tmp_path):
    """Same rule as fusedcli.child_env: FUSED_RENDER_FUSED_BIN CLIs get
    PYTHONHOME/PYTHONPATH unset (the packaged app's bundle-scoped values break
    any other Python); the in-interpreter shim keeps them — they are what make
    sys.executable work in the bundle."""
    if os.name == "nt":
        pytest.skip("asserted on the sh branch; the .cmd branch mirrors it")
    bin_dir = _export_with_stub(monkeypatch, tmp_path, external=True)
    text = open(os.path.join(bin_dir, "fused"), encoding="utf-8").read()
    assert "unset PYTHONHOME PYTHONPATH" in text
    bin_dir = _export_with_stub(monkeypatch, tmp_path, external=False)
    text = open(os.path.join(bin_dir, "fused"), encoding="utf-8").read()
    assert "unset PYTHONHOME" not in text


def test_wrapper_default_matches_the_canvases_default(monkeypatch):
    """canvases.py and the wrapper share one knob through
    fusedcli.workbench_env — this pins that the canvases module actually
    reads it rather than keeping a default of its own (D146: a duplicated
    rule needs a test)."""
    monkeypatch.delenv("FUSED_RENDER_WORKBENCH_ENV", raising=False)
    assert fusedcli.workbench_env() == "prod"
    import fused_render.canvases as canvases
    src = open(canvases.__file__, encoding="utf-8").read()
    assert "WORKBENCH_ENV = workbench_env()" in src


# --------------------------------------------------- the template's half


def _spawn(agent, monkeypatch, target, tmp_path):
    seen = {}

    class _Proc:
        pid = 4242

    agent.RUNS = str(tmp_path / "runs")
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kw: (seen.__setitem__("cmd", cmd),
                                           _Proc())[1])
    out = agent._start(str(target), "hi", "", "", "")
    assert "error" not in out, out
    return seen["cmd"]


def _spawn_kw(agent, monkeypatch, target, tmp_path):
    """Like `_spawn`, but returns the Popen kwargs instead of the argv —
    for asserting on the child env rather than the CLI flags."""
    seen = {}

    class _Proc:
        pid = 4242

    agent.RUNS = str(tmp_path / "runs")
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kw: (seen.__setitem__("kw", kw),
                                           _Proc())[1])
    out = agent._start(str(target), "hi", "", "", "")
    assert "error" not in out, out
    return seen["kw"]


def test_no_exported_cli_means_no_rule_and_no_promise(agent, tmp_path,
                                                      monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_FUSED_CLI_DIR", raising=False)
    target = tmp_path / "notes.txt"
    target.write_text("hi", encoding="utf-8")
    cmd = _spawn(agent, monkeypatch, target, tmp_path)
    allowed = cmd[cmd.index("--allowed-tools") + 1].split(",")
    assert "Bash(fused:*)" not in allowed
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "fused" not in prompt.lower() or "`fused` CLI" not in prompt


def test_an_exported_cli_pre_allows_bare_fused_and_discloses_it(
        agent, tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", str(tmp_path / "bin"))
    target = tmp_path / "notes.txt"
    target.write_text("hi", encoding="utf-8")
    cmd = _spawn(agent, monkeypatch, target, tmp_path)
    allowed = cmd[cmd.index("--allowed-tools") + 1].split(",")
    assert "Bash(fused:*)" in allowed
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "`fused` CLI is on PATH" in prompt
    # The three guard rails the note exists for: bare-command shape, no login
    # flows, no hand-pushing inside an auto-synced canvas clone.
    assert "NEVER run" in prompt and "login" in prompt
    assert "canvas" in prompt
    # Every target shape carries it — a directory too.
    folder = tmp_path / "plain"
    folder.mkdir()
    cmd2 = _spawn(agent, monkeypatch, folder, tmp_path)
    assert "`fused` CLI is on PATH" in cmd2[
        cmd2.index("--append-system-prompt") + 1]


def test_spawn_strips_ambient_fused_env_so_the_wrapper_default_still_fires(
        agent, tmp_path, monkeypatch):
    """The wrapper only defaults FUSED_ENV when unset (test above), so an
    ambient value inherited from the SERVER's own process would look exactly
    like a deliberate `FUSED_ENV=x fused ...` and silently skip the workbench
    default — diverging from canvases.py's own runs, which always force
    FUSED_ENV=WORKBENCH_ENV regardless of ambient state. The spawn must not
    hand that ambient value down."""
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", str(tmp_path / "bin"))
    monkeypatch.setenv("FUSED_ENV", "some-unrelated-env")
    target = tmp_path / "notes.txt"
    target.write_text("hi", encoding="utf-8")
    kw = _spawn_kw(agent, monkeypatch, target, tmp_path)
    assert "FUSED_ENV" not in kw["env"]
    # Nothing else about the inherited environment is disturbed.
    assert kw["env"]["FUSED_RENDER_FUSED_CLI_DIR"] == str(tmp_path / "bin")


def test_mcp_config_written(agent, tmp_path, monkeypatch):
    """The pre-allowance rides the same argv as everything else — sanity that
    the spawn still produces a loadable mcp.json beside it."""
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", str(tmp_path / "bin"))
    target = tmp_path / "notes.txt"
    target.write_text("hi", encoding="utf-8")
    cmd = _spawn(agent, monkeypatch, target, tmp_path)
    cfg_path = cmd[cmd.index("--mcp-config") + 1]
    assert json.load(open(cfg_path, encoding="utf-8"))["mcpServers"]


# -- which fused is in effect (a DEVELOPER diagnostic) -------------------------
#
# For a shipping user there is no other fused: the DMG bakes the pre-release
# wheel into the app's own interpreter and `fused_cli()`'s second branch is the
# only path they take. Both states below are reachable only from a dev checkout,
# which is exactly why they get a log line and no user-facing UI — the failures
# they cause (a canvas sync quietly missing its manifest shims, or SIGSEGV on
# every /api/run from a stale wheel) surface far from their cause.


def test_an_override_is_reported(monkeypatch):
    from fused_render import fusedcli

    monkeypatch.setenv("FUSED_RENDER_FUSED_BIN", "/opt/other/fused")
    message = fusedcli.log_cli_provenance()
    assert message and "/opt/other/fused" in message
    # It names the two consequences that make this worth a log at all.
    assert "shims" in message
    assert "interception" in message


def test_the_override_still_works(monkeypatch):
    """HARD constraint: the test suite substitutes its stub CLI through this
    variable, so the diagnostic must not gate or alter resolution."""
    from fused_render import fusedcli

    monkeypatch.setenv("FUSED_RENDER_FUSED_BIN", "/opt/other/fused --flag")
    cli = fusedcli.fused_cli()
    assert cli is not None
    assert cli.command == ["/opt/other/fused", "--flag"]
    assert cli.external is True


def test_a_matching_install_is_silent(monkeypatch):
    from fused_render import fusedcli

    monkeypatch.delenv("FUSED_RENDER_FUSED_BIN", raising=False)
    monkeypatch.setattr(fusedcli, "_installed_fused_version", lambda: "2.9.3b4")
    monkeypatch.setattr(fusedcli, "_pinned_fused_version", lambda: "2.9.3b4")
    assert fusedcli.log_cli_provenance() is None


def test_version_drift_is_reported(monkeypatch):
    """A recorded failure mode, not a hypothetical: a dev venv held an older
    fused than pyproject pinned and every /api/run died with SIGSEGV."""
    from fused_render import fusedcli

    monkeypatch.delenv("FUSED_RENDER_FUSED_BIN", raising=False)
    monkeypatch.setattr(fusedcli, "_installed_fused_version", lambda: "2.9.3b3")
    monkeypatch.setattr(fusedcli, "_pinned_fused_version", lambda: "2.9.3b4")
    message = fusedcli.log_cli_provenance()
    assert message and "2.9.3b3" in message and "2.9.3b4" in message


def test_no_fused_at_all_adds_nothing(monkeypatch):
    """`fused_cli()` returns None and every canvases endpoint already explains
    that in its own error — a second voice here would just be noise."""
    from fused_render import fusedcli

    monkeypatch.delenv("FUSED_RENDER_FUSED_BIN", raising=False)
    monkeypatch.setattr(fusedcli, "_installed_fused_version", lambda: None)
    assert fusedcli.log_cli_provenance() is None


def test_the_version_is_read_from_distribution_metadata():
    """NOT `fused.__version__`: in the drift incident that attribute reported
    misleadingly, so it is not evidence of which wheel is installed."""
    import inspect

    from fused_render import fusedcli

    src = inspect.getsource(fusedcli._installed_fused_version)
    assert "importlib.metadata" in src
    # Prose may explain WHY the attribute is wrong; the code must not read it.
    body = src.split('"""')[-1]
    assert "__version__" not in body, body


def test_the_pin_is_discoverable_at_runtime():
    """A shipped app has no pyproject.toml, so the pin has to come from
    fused-render's own installed metadata. In this dev checkout that resolves;
    if it stops, the drift check silently becomes a no-op."""
    from fused_render import fusedcli

    pinned = fusedcli._pinned_fused_version()
    assert pinned, "could not read the fused pin from fused-render's metadata"
    assert pinned[0].isdigit(), pinned

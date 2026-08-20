"""The `mcp` template's app-surface reader (SPEC §44 / MC-2, MC-4).

`inspect_app.py` is the panel's one read of the app folder: what could become a
tool (every top-level `.py`'s entrypoints, by AST — never by importing the app,
which would run its top-level code and touch the author's token files), what the
page already pins at each call site, whether `fused` can be resolved for
registration, and whether the manifest's recorded signatures still match the
code (the drift verdict, MC-4).

What is pinned here:

* signatures come from the AST, with defaults and annotations, in source order;
* a `main` that is a METHOD or nested is not an entrypoint (the server looks the
  name up in the executed module's namespace);
* the page's `runPython` call sites are reported with their literal arguments —
  the hint the panel turns into proposed pins;
* drift is reported per manifest tool, both ways: a changed signature and a
  vanished file/entrypoint are distinct verdicts, and an unchanged one is `ok`;
* a folder that is not an app, or a path that does not exist, is a refusal
  PAYLOAD (`ok: False`) rather than an exception — the panel renders it.
"""
import importlib.util
import json
import os

import pytest

INSPECT = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "mcp", "inspect_app.py")

_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="fused-app" /><title>mail</title></head>
<body><script>
  const inbox = await fused.runPython("./mail.py", { op: "list", limit: "20" });
  await fused.runPython("./mail.py", { op: "send", to: to });
  await fused.runPython("./stats.py", {});
</script></body></html>
"""

_MAIL = '''\
"""Local mail app.

Longer prose the panel does not need.
"""


def main(op: str = "list", to=None, subject: str = None, count: int = 10):
    """Dispatch one mail operation."""
    return {"op": op, "to": to, "subject": subject, "count": count}


def _helper():
    return 1
'''

_STATS = """\
def main():
    return {"unread": 3}
"""


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def inspect_app():
    return _load(INSPECT, "mcp_inspect_app")


@pytest.fixture
def app(tmp_path):
    """An app folder: a page plus two entrypoint files."""
    folder = tmp_path / "open-mail"
    folder.mkdir()
    (folder / "index.html").write_text(_PAGE, encoding="utf-8")
    (folder / "mail.py").write_text(_MAIL, encoding="utf-8")
    (folder / "stats.py").write_text(_STATS, encoding="utf-8")
    return str(folder)


def _file(report, name):
    return next(f for f in report["files"] if f["file"] == name)


# ------------------------------------------------------------------ the surface


def test_the_report_describes_the_folder(inspect_app, app):
    report = inspect_app.main(path=app)

    assert report["ok"] is True
    assert report["path"] == app
    assert report["name"] == "open-mail"
    assert [f["file"] for f in report["files"]] == ["mail.py", "stats.py"]


def test_an_entrypoint_carries_its_signature_defaults_and_doc(inspect_app, app):
    entry = _file(inspect_app.main(path=app), "mail.py")["entrypoints"][0]

    assert entry["name"] == "main"
    assert entry["signature"] == "main(op: str='list', to=None, subject: str=None, count: int=10)"
    assert entry["doc"] == "Dispatch one mail operation."
    assert [p["name"] for p in entry["params"]] == ["op", "to", "subject", "count"]
    assert entry["params"][0] == {
        "name": "op", "annotation": "str", "default": "'list'", "required": False}
    assert entry["params"][1] == {
        "name": "to", "annotation": "", "default": "None", "required": False}


def test_a_parameter_without_a_default_is_required(inspect_app, app):
    with open(os.path.join(app, "mail.py"), "w", encoding="utf-8") as fh:
        fh.write("def main(op, count: int = 1):\n    return op\n")

    entry = _file(inspect_app.main(path=app), "mail.py")["entrypoints"][0]

    assert entry["params"][0] == {
        "name": "op", "annotation": "", "default": "", "required": True}


def test_the_module_docstring_summary_is_reported(inspect_app, app):
    # First non-empty line only: it is a description SEED for the curation, and
    # the panel shows it in one row.
    assert _file(inspect_app.main(path=app), "mail.py")["doc"] == "Local mail app."


def test_private_and_nested_functions_are_not_entrypoints(inspect_app, app):
    with open(os.path.join(app, "mail.py"), "w", encoding="utf-8") as fh:
        fh.write(
            "class App:\n"
            "    def main(self):\n"
            "        return 1\n"
            "\n\n"
            "def outer():\n"
            "    def main():\n"
            "        return 2\n"
            "    return main\n"
        )

    # A method and a closure are both unreachable by a namespace lookup, which
    # is how the server invokes an entrypoint — so neither `main` is offered.
    # The top-level `outer` is, because every top-level function is a candidate:
    # an app that grew a second dispatcher is exactly what the manifest's
    # `entrypoint` field is for.
    names = [e["name"] for e in _file(inspect_app.main(path=app), "mail.py")["entrypoints"]]
    assert names == ["outer"]


def test_a_private_function_is_not_offered(inspect_app, app):
    # `_helper` is in the fixture's mail.py beside `main`.
    names = [e["name"] for e in _file(inspect_app.main(path=app), "mail.py")["entrypoints"]]
    assert names == ["main"]


def test_an_unparseable_file_is_reported_not_fatal(inspect_app, app):
    with open(os.path.join(app, "broken.py"), "w", encoding="utf-8") as fh:
        fh.write("def main(:\n")

    report = inspect_app.main(path=app)

    assert report["ok"] is True
    broken = _file(report, "broken.py")
    assert broken["entrypoints"] == []
    assert "syntax" in broken["error"].lower() or "invalid" in broken["error"].lower()


def test_the_report_is_json_serialisable(inspect_app, app):
    # The whole point of a runPython backend: what it returns crosses to the page
    # as JSON (SPEC PY-15).
    json.dumps(inspect_app.main(path=app))


# ------------------------------------------------------------- the page's pins


def test_run_python_call_sites_and_their_literal_arguments(inspect_app, app):
    calls = inspect_app.main(path=app)["page"]["calls"]

    assert [c["file"] for c in calls] == ["mail.py", "mail.py", "stats.py"]
    # Literal values are reported; a non-literal (`to: to`) is reported as a
    # named argument with no value, because the page decides it at runtime and
    # the panel must not propose it as a fixed pin.
    assert calls[0]["args"] == {"op": "list", "limit": "20"}
    assert calls[1]["args"] == {"op": "send", "to": None}
    assert calls[2]["args"] == {}


def test_a_page_with_no_calls_is_an_empty_list(inspect_app, tmp_path):
    folder = tmp_path / "bare"
    folder.mkdir()
    (folder / "index.html").write_text(
        '<meta name="fused-app" /><h1>nothing</h1>', encoding="utf-8")
    (folder / "run.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    assert inspect_app.main(path=str(folder))["page"]["calls"] == []


def test_the_page_is_the_TAGGED_entry_not_index_html(inspect_app, app):
    # D301, via the shared `app_entry.entry_html`: the marker is the only signal.
    # An untagged `index.html` beside a tagged `mail.html` used to make the panel
    # read its pin hints out of the wrong file.
    with open(os.path.join(app, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<html><body>not the app</body></html>")
    with open(os.path.join(app, "mail.html"), "w", encoding="utf-8") as fh:
        fh.write(_PAGE)

    page = inspect_app.main(path=app)["page"]

    assert page["file"] == "mail.html"
    assert [c["file"] for c in page["calls"]] == ["mail.py", "mail.py", "stats.py"]


def test_a_folder_with_no_tagged_page_reports_no_page(inspect_app, app):
    # The gate refuses such a folder, but a hand-written `?_mode=mcp` reaches
    # here anyway (MD-11) and must get a payload, not an exception.
    with open(os.path.join(app, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<html><body>untagged</body></html>")

    report = inspect_app.main(path=app)

    assert report["ok"] is True
    assert report["page"] == {"file": "", "exists": False, "calls": []}


# ----------------------------------------------------------- registration probe


def _fake_cli(app, monkeypatch, name="fused"):
    """A `fused` wrapper in a dir exported the way the SERVER exports it (D334)."""
    bin_dir = os.path.join(app, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    path = os.path.join(bin_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
    os.chmod(path, 0o755)
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", bin_dir)
    return path


def test_the_fused_executable_comes_from_the_servers_own_export(
    inspect_app, app, monkeypatch
):
    # Registration writes `{command: <this>, args: [app, serve, <dir>]}` into a
    # GLOBAL ~/.claude.json, so the binary has to be the one the server vetted
    # and exported (D334) — never whatever `fused` a PATH lookup happens to find.
    fake = _fake_cli(app, monkeypatch)
    monkeypatch.delenv("PATH", raising=False)

    assert inspect_app.main(path=app)["fused"] == fake


def test_a_fused_on_PATH_is_NOT_offered(inspect_app, app, monkeypatch):
    # The D334 regression this guards: a venv without the [fused] extra plus a
    # stray pipx `fused` on PATH must not get that binary registered globally.
    stray = os.path.join(app, "stray")
    os.makedirs(stray)
    with open(os.path.join(stray, "fused"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
    os.chmod(os.path.join(stray, "fused"), 0o755)
    monkeypatch.setenv("PATH", stray)
    monkeypatch.delenv("FUSED_RENDER_FUSED_CLI_DIR", raising=False)

    assert inspect_app.main(path=app)["fused"] == ""


def test_an_exported_dir_with_no_wrapper_in_it_is_an_empty_string(
    inspect_app, app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", str(tmp_path / "empty"))

    assert inspect_app.main(path=app)["fused"] == ""


# ------------------------------------------------------------------------ drift


_MANIFEST = """\
[[tool]]
name = "send_email"
description = "Send an email."
file = "mail.py"
signature = "main(op: str='list', to=None, subject: str=None, count: int=10)"
[tool.pinned]
op = "send"
"""


def test_no_manifest_means_no_drift_and_an_empty_tool_list(inspect_app, app):
    report = inspect_app.main(path=app)

    assert report["manifest"]["exists"] is False
    assert report["manifest"]["tools"] == []
    assert report["drift"] == []


def test_an_unchanged_signature_is_ok(inspect_app, app):
    with open(os.path.join(app, "mcp.toml"), "w", encoding="utf-8") as fh:
        fh.write(_MANIFEST)

    report = inspect_app.main(path=app)

    assert report["manifest"]["exists"] is True
    assert [t["name"] for t in report["manifest"]["tools"]] == ["send_email"]
    assert report["drift"] == [{
        "name": "send_email",
        "file": "mail.py",
        "entrypoint": "main",
        "status": "ok",
        "recorded": "main(op: str='list', to=None, subject: str=None, count: int=10)",
        "current": "main(op: str='list', to=None, subject: str=None, count: int=10)",
    }]


def test_a_changed_signature_is_drift(inspect_app, app):
    with open(os.path.join(app, "mcp.toml"), "w", encoding="utf-8") as fh:
        fh.write(_MANIFEST)
    with open(os.path.join(app, "mail.py"), "w", encoding="utf-8") as fh:
        fh.write("def main(op: str = 'list', recipient=None):\n    return op\n")

    drift = inspect_app.main(path=app)["drift"][0]

    assert drift["status"] == "changed"
    assert drift["current"] == "main(op: str='list', recipient=None)"


def test_a_vanished_entrypoint_is_its_own_verdict(inspect_app, app):
    with open(os.path.join(app, "mcp.toml"), "w", encoding="utf-8") as fh:
        fh.write(_MANIFEST)
    with open(os.path.join(app, "mail.py"), "w", encoding="utf-8") as fh:
        fh.write("def dispatch(op='list'):\n    return op\n")

    drift = inspect_app.main(path=app)["drift"][0]

    # Distinct from "changed": the served tool is BROKEN, not merely stale, so
    # the panel says something different about it.
    assert drift["status"] == "missing"
    assert drift["current"] == ""


def test_a_vanished_file_is_missing_too(inspect_app, app):
    with open(os.path.join(app, "mcp.toml"), "w", encoding="utf-8") as fh:
        fh.write(_MANIFEST)
    os.remove(os.path.join(app, "mail.py"))

    assert inspect_app.main(path=app)["drift"][0]["status"] == "missing"


def test_a_tool_curated_without_a_snapshot_is_unknown(inspect_app, app):
    with open(os.path.join(app, "mcp.toml"), "w", encoding="utf-8") as fh:
        fh.write(_MANIFEST.replace(
            'signature = "main(op: str=\'list\', to=None, subject: str=None, count: int=10)"\n',
            ""))

    # A hand-written manifest carries no snapshot; there is nothing to compare,
    # so the panel must not claim the tool is either fine or drifted.
    assert inspect_app.main(path=app)["drift"][0]["status"] == "unknown"


def test_an_entrypoint_whose_docstring_is_only_whitespace_survives(inspect_app, app):
    # A docstring that is TRUTHY but strips to nothing (a lone form feed) used to
    # index an empty list — and the exception was outside every try, so one odd
    # docstring in one file blanked the entire panel.
    with open(os.path.join(app, "mail.py"), "w", encoding="utf-8") as fh:
        fh.write('def main(op="list"):\n    """\x0c"""\n    return op\n')

    report = inspect_app.main(path=app)

    assert report["ok"] is True
    entry = _file(report, "mail.py")["entrypoints"][0]
    assert entry["doc"] == ""
    assert entry["signature"] == "main(op='list')"


def test_a_module_docstring_of_only_whitespace_survives(inspect_app, app):
    with open(os.path.join(app, "mail.py"), "w", encoding="utf-8") as fh:
        fh.write('"""\x0c"""\n\n\ndef main():\n    return 1\n')

    assert _file(inspect_app.main(path=app), "mail.py")["doc"] == ""


def test_a_manifest_that_is_not_utf8_is_reported_not_raised(inspect_app, app):
    # tomllib raises UnicodeDecodeError — a ValueError, not a TOMLDecodeError — on
    # a hand-edited Latin-1 file. This module's contract is a payload, and the
    # surface above the manifest is still worth drawing.
    with open(os.path.join(app, "mcp.toml"), "wb") as fh:
        fh.write(b'[[tool]]\nname = "caf\xe9"\n')

    report = inspect_app.main(path=app)

    assert report["ok"] is True
    assert report["manifest"]["error"]
    assert report["drift"] == []
    assert [f["file"] for f in report["files"]] == ["mail.py", "stats.py"]


def test_an_unparseable_manifest_is_reported_as_an_error(inspect_app, app):
    with open(os.path.join(app, "mcp.toml"), "w", encoding="utf-8") as fh:
        fh.write("[[tool]\nname =")

    report = inspect_app.main(path=app)

    assert report["ok"] is True          # the surface is still readable
    assert report["manifest"]["exists"] is True
    assert report["manifest"]["error"]
    assert report["drift"] == []


# --------------------------------------------------------------------- refusals


def test_a_missing_path_is_a_refusal_payload(inspect_app, tmp_path):
    report = inspect_app.main(path=str(tmp_path / "nope"))

    assert report["ok"] is False
    assert report["reason"] == "not_a_folder"
    assert report["message"]


def test_a_file_target_is_a_refusal_payload(inspect_app, app):
    report = inspect_app.main(path=os.path.join(app, "mail.py"))

    assert report["ok"] is False
    assert report["reason"] == "not_a_folder"


def test_an_empty_path_is_a_refusal_payload(inspect_app):
    assert inspect_app.main(path="")["ok"] is False

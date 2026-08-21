"""The `mcp` template's manifest writer (SPEC §44 / MC-3).

`manifest.py` is the write half of the panel: it reads and writes the `[[tool]]`
tables in the app folder's `mcp.toml` — the contract `fused app serve` consumes
(openfused's spec/serve/app-mcp.md §2). Everything pinned here is a property
that contract depends on:

* a round trip preserves every field, including `[tool.pinned]`;
* the `signature` snapshot is captured FRESH on write, from the file's current
  AST — it is what the drift verdict later compares against, so a stale one
  written by the panel would report drift that does not exist;
* unrelated tables in `mcp.toml` survive a write (the file may be shared);
* validation refuses exactly what the server's loader refuses, as a PAYLOAD
  (`ok: False`) — a panel that could write a manifest the server rejects would
  turn a typo into a broken registration the user finds out about from Claude;
* the write is atomic and never leaves a half-written manifest.
"""
import importlib.util
import json
import os

import pytest

MANIFEST = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "mcp", "manifest.py")

_MAIL = '''\
"""Local mail app."""


def main(op: str = "list", to=None, count: int = 10):
    return [op, to, count]
'''

_SIGNATURE = "main(op: str='list', to=None, count: int=10)"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def manifest():
    return _load(MANIFEST, "mcp_manifest_module")


@pytest.fixture
def app(tmp_path):
    folder = tmp_path / "open-mail"
    folder.mkdir()
    (folder / "index.html").write_text("<h1>mail</h1>", encoding="utf-8")
    (folder / "mail.py").write_text(_MAIL, encoding="utf-8")
    return str(folder)


# --------------------------------------------------- the parser on 3.10 vs 3.11


@pytest.fixture
def hidden_tomllib(monkeypatch):
    """Count the `tomli` requests the modules make with `tomllib` unavailable.

    `requires-python` is >=3.10 and `tomllib` is 3.11+ stdlib, so on the 3.10
    lane these modules genuinely get `tomli` (a dependency declared for
    `python_version < "3.11"`), while on 3.11+ that branch is otherwise never
    executed. This fixture makes ONE test cover both, which is the only honest
    shape: the thing being tested is the fallback, so the version where the
    fallback is REAL must not be the version where the test errors.

    * **On 3.11+** it simulates: `tomllib` is denied and `tomli` is answered
      with the stdlib module standing in for it (this interpreter has no reason
      to carry the real one).
    * **On 3.10** there is nothing to simulate — `tomllib` does not exist and
      `tomli` is installed — so the real one answers and only the counting is
      added.

    The stand-in is resolved BEFORE the patch either way: resolving it inside
    `deny` would re-enter the patch. And note the first version of this fixture
    was itself the bug it was written to prevent — a bare `import tomllib` at
    setup, which errored on the one lane that mattered.
    """
    import builtins

    try:
        import tomllib as stand_in
    except ImportError:  # 3.10: no simulation needed, the real tomli answers
        # importorskip, not a bare import: on 3.10 `tomli` is a declared
        # dependency and present in CI, but a hand-made 3.10 venv without it has
        # nothing for this test to exercise — a skip says that, an ImportError
        # would read as a failure of the code under test.
        stand_in = pytest.importorskip("tomli")

    real_import = builtins.__import__
    calls = []

    def deny(name, *args, **kwargs):
        if name == "tomllib":
            raise ImportError("no tomllib on 3.10")
        if name == "tomli":
            calls.append(name)
            return stand_in
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)
    return calls


@pytest.fixture
def no_toml_parser(monkeypatch):
    """Neither parser importable — a project venv (SPEC PY-16) need not carry one."""
    import builtins

    real_import = builtins.__import__

    def deny(name, *args, **kwargs):
        if name in ("tomllib", "tomli"):
            raise ImportError("no TOML parser")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)


_ONE = [{
    "name": "send_email",
    "description": "Send an email from the local mail app.",
    "file": "mail.py",
    "entrypoint": "main",
    "pinned": {"op": "send"},
}]


def _read_toml(app):
    # Same two-name lookup the modules under test do: `tomllib` is 3.11+ stdlib
    # and this repo supports 3.10, where the `tomli` dependency supplies it. An
    # unconditional `import tomllib` here is what turned the whole file into
    # collection errors on the 3.10 lane.
    try:
        import tomllib as toml
    except ImportError:  # pragma: no cover — 3.10 only
        import tomli as toml

    with open(os.path.join(app, "mcp.toml"), "rb") as fh:
        return toml.load(fh)


# ------------------------------------------------------------------- read/write


def test_reading_an_absent_manifest(manifest, app):
    out = manifest.main(action="read", path=app)

    assert out == {"ok": True, "path": os.path.join(app, "mcp.toml"),
                   "exists": False, "tools": []}


def test_write_then_read_round_trips_every_field(manifest, app):
    written = manifest.main(action="write", path=app, tools=_ONE)

    assert written["ok"] is True
    back = manifest.main(action="read", path=app)
    assert back["exists"] is True
    assert back["tools"] == [{
        "name": "send_email",
        "description": "Send an email from the local mail app.",
        "file": "mail.py",
        "entrypoint": "main",
        "pinned": {"op": "send"},
        "signature": _SIGNATURE,
    }]


def test_the_signature_snapshot_is_captured_from_the_current_source(manifest, app):
    manifest.main(action="write", path=app, tools=_ONE)
    assert _read_toml(app)["tool"][0]["signature"] == _SIGNATURE

    # Change the code, write the SAME curation again: the snapshot must follow
    # the code, because it is what the drift check compares against.
    with open(os.path.join(app, "mail.py"), "w", encoding="utf-8") as fh:
        fh.write("def main(op='list'):\n    return op\n")
    manifest.main(action="write", path=app, tools=_ONE)

    assert _read_toml(app)["tool"][0]["signature"] == "main(op='list')"


def test_the_written_toml_is_what_the_server_reads(manifest, app):
    manifest.main(action="write", path=app, tools=_ONE)

    raw = _read_toml(app)

    # The shape `fused app serve` loads: an array of `[[tool]]` tables, pins in a
    # `pinned` sub-table (openfused spec/serve/app-mcp.md §2).
    assert raw["tool"][0]["name"] == "send_email"
    assert raw["tool"][0]["file"] == "mail.py"
    assert raw["tool"][0]["pinned"] == {"op": "send"}


def test_writing_replaces_the_previous_tool_set(manifest, app):
    manifest.main(action="write", path=app, tools=_ONE)
    manifest.main(action="write", path=app, tools=[dict(_ONE[0], name="list_inbox",
                                                        pinned={"op": "list"})])

    tools = manifest.main(action="read", path=app)["tools"]
    # The panel edits the WHOLE set: a tool the user deleted must not survive as
    # a stale table the server keeps serving.
    assert [t["name"] for t in tools] == ["list_inbox"]


def test_an_empty_tool_set_is_allowed_and_clears_the_manifest(manifest, app):
    manifest.main(action="write", path=app, tools=_ONE)
    out = manifest.main(action="write", path=app, tools=[])

    assert out["ok"] is True
    assert manifest.main(action="read", path=app)["tools"] == []


def test_tools_may_arrive_as_a_json_string(manifest, app):
    # runPython params cross as strings, so the panel may hand the array over
    # JSON-encoded; both forms must work.
    out = manifest.main(action="write", path=app, tools=json.dumps(_ONE))

    assert out["ok"] is True
    assert [t["name"] for t in manifest.main(action="read", path=app)["tools"]] == ["send_email"]


def test_optional_fields_default(manifest, app):
    out = manifest.main(action="write", path=app, tools=[{
        "name": "list_inbox", "description": "List it.", "file": "mail.py"}])

    assert out["ok"] is True
    tool = manifest.main(action="read", path=app)["tools"][0]
    assert tool["entrypoint"] == "main"
    assert tool["pinned"] == {}


# ------------------------------------------------------- unrelated content lives


def test_unrelated_tables_survive_a_write(manifest, app):
    with open(os.path.join(app, "mcp.toml"), "w", encoding="utf-8") as fh:
        fh.write('[other]\nsetting = 1\nname = "keep me"\n\n[[tool]]\n'
                 'name = "old"\ndescription = "Old."\nfile = "mail.py"\n')

    manifest.main(action="write", path=app, tools=_ONE)

    raw = _read_toml(app)
    assert raw["other"] == {"setting": 1, "name": "keep me"}
    assert [t["name"] for t in raw["tool"]] == ["send_email"]


def test_a_comment_above_an_unrelated_table_survives(manifest, app):
    with open(os.path.join(app, "mcp.toml"), "w", encoding="utf-8") as fh:
        fh.write("# hand-written, keep\n[other]\nsetting = 1\n")

    manifest.main(action="write", path=app, tools=_ONE)

    with open(os.path.join(app, "mcp.toml"), encoding="utf-8") as fh:
        assert "# hand-written, keep" in fh.read()


# ------------------------------------------------------------- string encoding


def test_awkward_strings_round_trip(manifest, app):
    tools = [{
        "name": "send_email",
        "description": 'He said "hi" \\ then left\ttab',
        "file": "mail.py",
        "pinned": {"op": 'a "quoted" \\ value'},
    }]

    assert manifest.main(action="write", path=app, tools=tools)["ok"] is True

    raw = _read_toml(app)
    assert raw["tool"][0]["description"] == 'He said "hi" \\ then left\ttab'
    assert raw["tool"][0]["pinned"]["op"] == 'a "quoted" \\ value'


def test_non_string_pins_round_trip(manifest, app):
    tools = [{"name": "t", "description": "d", "file": "mail.py",
              "pinned": {"count": 3, "dry_run": True, "ratio": 1.5, "tags": ["a", "b"]}}]

    assert manifest.main(action="write", path=app, tools=tools)["ok"] is True

    assert _read_toml(app)["tool"][0]["pinned"] == {
        "count": 3, "dry_run": True, "ratio": 1.5, "tags": ["a", "b"]}


# --------------------------------------------------------------------- refusals


@pytest.mark.parametrize("bad,reason", [
    ({"description": "d", "file": "mail.py"}, "invalid_tool"),
    ({"name": "send email", "description": "d", "file": "mail.py"}, "invalid_tool"),
    ({"name": "t", "description": "", "file": "mail.py"}, "invalid_tool"),
    ({"name": "t", "description": "d", "file": "gone.py"}, "invalid_tool"),
    ({"name": "t", "description": "d", "file": "index.html"}, "invalid_tool"),
    ({"name": "t", "description": "d", "file": "../mail.py"}, "invalid_tool"),
    ({"name": "t", "description": "d", "file": "mail.py", "entrypoint": "no go"},
     "invalid_tool"),
    ({"name": "t", "description": "d", "file": "mail.py", "pinned": {"not an arg": 1}},
     "invalid_tool"),
])
def test_a_tool_the_server_would_reject_is_refused(manifest, app, bad, reason):
    # Exactly the loader's rules (openfused spec/serve/app-mcp.md §3), enforced
    # here so a typo cannot become a registration that fails inside Claude.
    out = manifest.main(action="write", path=app, tools=[bad])

    assert out["ok"] is False
    assert out["reason"] == reason
    assert out["message"]
    assert not os.path.exists(os.path.join(app, "mcp.toml"))


def test_duplicate_names_are_refused(manifest, app):
    out = manifest.main(action="write", path=app, tools=_ONE + _ONE)

    assert out["ok"] is False
    assert "duplicate" in out["message"].lower()


def test_a_missing_entrypoint_is_refused(manifest, app):
    out = manifest.main(action="write", path=app,
                        tools=[dict(_ONE[0], entrypoint="dispatch")])

    assert out["ok"] is False
    assert "dispatch" in out["message"]


def test_a_refused_write_leaves_an_existing_manifest_untouched(manifest, app):
    manifest.main(action="write", path=app, tools=_ONE)
    before = open(os.path.join(app, "mcp.toml"), encoding="utf-8").read()

    manifest.main(action="write", path=app, tools=[{"name": "bad"}])

    assert open(os.path.join(app, "mcp.toml"), encoding="utf-8").read() == before


@pytest.mark.parametrize("pinned", [
    {"op": None},
    {"tags": ["a", None]},
    {"nested": {"k": None}},
])
def test_a_null_pin_is_refused_not_written_as_the_string_None(manifest, app, pinned):
    # TOML has no null, so a null pin has nothing to be written as. It used to
    # pass validation and fall through the renderer's "unreachable" branch, which
    # wrote the STRING "None" — a pin the server would hand the entrypoint as
    # text, i.e. a truthy value where the author meant nothing.
    out = manifest.main(action="write", path=app,
                        tools=[dict(_ONE[0], pinned=pinned)])

    assert out["ok"] is False
    assert out["reason"] == "invalid_tool"
    assert not os.path.exists(os.path.join(app, "mcp.toml"))


def test_a_manifest_that_is_not_utf8_refuses_both_ways(manifest, app):
    # tomllib raises UnicodeDecodeError — a ValueError, not a TOMLDecodeError — on
    # a hand-edited Latin-1 file. Neither read nor write may let it escape as an
    # exception when this module's contract is a refusal payload; and a write in
    # particular must not proceed, because it would discard the user's file.
    latin1 = b'[[tool]]\nname = "caf\xe9"\n'
    with open(os.path.join(app, "mcp.toml"), "wb") as fh:
        fh.write(latin1)

    read = manifest.main(action="read", path=app)
    assert read["ok"] is False and read["reason"] == "bad_manifest"

    write = manifest.main(action="write", path=app, tools=_ONE)
    assert write["ok"] is False and write["reason"] == "bad_manifest"
    with open(os.path.join(app, "mcp.toml"), "rb") as fh:
        assert fh.read() == latin1


def test_a_non_folder_path_is_refused(manifest, tmp_path):
    out = manifest.main(action="write", path=str(tmp_path / "nope"), tools=_ONE)

    assert out["ok"] is False
    assert out["reason"] == "not_a_folder"


def test_an_unknown_action_is_refused(manifest, app):
    out = manifest.main(action="destroy", path=app)

    assert out["ok"] is False
    assert out["reason"] == "unknown_action"


def test_read_reports_an_unparseable_manifest(manifest, app):
    with open(os.path.join(app, "mcp.toml"), "w", encoding="utf-8") as fh:
        fh.write("[[tool]\nname =")

    out = manifest.main(action="read", path=app)

    assert out["ok"] is False
    assert out["reason"] == "bad_manifest"


def test_writing_over_an_unparseable_manifest_refuses(manifest, app):
    # Rewriting it would silently discard whatever the user was editing, so the
    # panel is told to send them to the file instead.
    with open(os.path.join(app, "mcp.toml"), "w", encoding="utf-8") as fh:
        fh.write("[[tool]\nname =")

    out = manifest.main(action="write", path=app, tools=_ONE)

    assert out["ok"] is False
    assert out["reason"] == "bad_manifest"


# The two modules each carry a `_signature`, because a template backend is exec'd
# standalone and cannot import its sibling (SPEC PY-15). The snapshot one WRITES is
# the snapshot the other COMPARES, so a divergence would report drift on every
# tool of every folder — the exact failure manifest.py's own docstring warns about.
def test_the_two_signature_formatters_agree(manifest, tmp_path):
    import ast
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mcp_inspect_for_parity",
        os.path.join(os.path.dirname(__file__), "..", "fused_render", "templates",
                     "mcp", "inspect_app.py"))
    inspect_app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inspect_app)

    sources = [
        "def main(): pass",
        "def main(op): pass",
        "def main(op='list', to=None, count: int = 10): pass",
        "def main(a, /, b, *args, c=1, **kw): pass",
        "def main(x: str = 'a b', y: dict = {}): pass",
        "async def main(op: bool = False): pass",
    ]
    for source in sources:
        fn = ast.parse(source).body[0]
        assert manifest._signature("main", fn) == inspect_app._signature("main", fn), source


def test_tomli_serves_where_tomllib_does_not_exist(manifest, app, hidden_tomllib):
    written = manifest.main(action="write", path=app, tools=_ONE)
    back = manifest.main(action="read", path=app)

    assert written["ok"] is True
    assert back["ok"] is True
    assert [t["name"] for t in back["tools"]] == ["send_email"]
    # ...and it really went through the fallback rather than the stdlib. Twice:
    # the write's verify-by-reparse, and the read. (The write's own pre-read
    # needs no parser here — there was no manifest yet to preserve.)
    assert hidden_tomllib == ["tomli", "tomli"]


def test_no_parser_at_all_refuses_instead_of_raising(manifest, app, monkeypatch):
    # A manifest that EXISTS is the case that needs a parser — an absent one is
    # answered without parsing anything.
    manifest.main(action="write", path=app, tools=_ONE)
    before = open(os.path.join(app, "mcp.toml"), encoding="utf-8").read()

    import builtins

    real_import = builtins.__import__

    def deny(name, *args, **kwargs):
        if name in ("tomllib", "tomli"):
            raise ImportError("no TOML parser")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)

    read = manifest.main(action="read", path=app)
    assert read["ok"] is False and read["reason"] == "bad_manifest"
    assert "tomli" in read["message"]

    write = manifest.main(action="write", path=app, tools=_ONE)
    assert write["ok"] is False
    # The user's file is untouched: an environment that cannot PARSE the manifest
    # must not have one written over it unverified.
    assert open(os.path.join(app, "mcp.toml"), encoding="utf-8").read() == before


def test_the_return_value_is_json_serialisable(manifest, app):
    json.dumps(manifest.main(action="write", path=app, tools=_ONE))
    json.dumps(manifest.main(action="read", path=app))

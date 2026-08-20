"""The MCP curation panel's own behaviour (SPEC §44 / MC-3, MC-5, MC-7).

Two things are pinned here, and they are the two that were wrong:

* **A pin keeps its TYPE.** The manifest is TOML and the server hands pins to
  the entrypoint as JSON, so a `dry_run: bool = False` pinned "off" has to land
  as `dry_run = false`. It landed as the STRING `"False"` — which the server
  passes through verbatim and Python reads as truthy, so a pinned-OFF safety
  flag behaved as on. The three pure functions that decide a pin's value are run
  for real (`_mcp_pins_probe.mjs`), because a source assertion can only say they
  exist.
* **A registration failure REACHES the user.** `/api/claude-config/mcp` answers
  HTTP 200 with `{ok: false}` for its own refusals, and `add`/`remove` carry the
  claude CLI's `{stdout, stderr}` with NO `error` key
  (`fused_render/claude_config/mcp.py`) — so reading only `error` reported
  "could not add" with the actual reason sitting unread. That, and the buttons
  being dead until the first successful load, are source-level contracts: they
  live in event handlers over a real iframe, which is the user's to verify.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "fused_render", "templates", "mcp", "template.html")
PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mcp_pins_probe.mjs")
CONFIG_MODULE = os.path.join(ROOT, "fused_render", "claude_config", "mcp.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def run_cases(cases):
    node = shutil.which("node")
    if not node:  # pragma: no cover — node is present on CI runners
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [node, PROBE, TEMPLATE, json.dumps(cases)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def param(name="op", annotation="", default="", required=False):
    return {"name": name, "annotation": annotation, "default": default,
            "required": required}


# --------------------------------------------------------------- pin typing


def test_the_kind_comes_from_the_annotation_first():
    kinds = run_cases([
        {"fn": "pinKind", "param": param(annotation="bool", default="'yes'")},
        {"fn": "pinKind", "param": param(annotation="int", default="'3'")},
        {"fn": "pinKind", "param": param(annotation="float")},
        {"fn": "pinKind", "param": param(annotation="str", default="10")},
    ])
    # The annotation is the author's own declaration; a default that disagrees
    # with it is the author's problem, not a reason to guess from the literal.
    assert kinds == ["bool", "number", "number", "str"]


def test_the_kind_falls_back_to_the_default_literal():
    kinds = run_cases([
        {"fn": "pinKind", "param": param(default="False")},
        {"fn": "pinKind", "param": param(default="True")},
        {"fn": "pinKind", "param": param(default="10")},
        {"fn": "pinKind", "param": param(default="-1.5")},
        {"fn": "pinKind", "param": param(default="'list'")},
        {"fn": "pinKind", "param": param(default="None")},
        {"fn": "pinKind", "param": param()},
        # A non-literal default (a call) is not something to type-guess from.
        {"fn": "pinKind", "param": param(default="dict()")},
    ])
    assert kinds == ["bool", "bool", "number", "number", "str", "str", "str", "str"]


def test_a_boolean_default_seeds_a_boolean_not_the_string_False():
    # THE BUG, at its source: ticking the pin box on `dry_run: bool = False` used
    # to seed the AST's source text, so the manifest got `dry_run = "False"` and
    # the entrypoint received a truthy string.
    seeded = run_cases([
        {"fn": "seedPin", "param": param("dry_run", "bool", "False")},
        {"fn": "seedPin", "param": param("force", "bool", "True")},
    ])
    assert seeded == [False, True]
    assert [type(v) for v in seeded] == [bool, bool]


def test_numbers_seed_as_numbers_and_strings_lose_only_their_quotes():
    seeded = run_cases([
        {"fn": "seedPin", "param": param("count", "int", "10")},
        {"fn": "seedPin", "param": param("ratio", "float", "1.5")},
        {"fn": "seedPin", "param": param("op", "str", "'send'")},
        {"fn": "seedPin", "param": param("op", "", '"send"')},
        # `None` and an absent default are an empty box for the user to fill —
        # never the literal text "None", which would be written as a pin.
        {"fn": "seedPin", "param": param("to", "", "None")},
        {"fn": "seedPin", "param": param("to")},
    ])
    assert seeded == [10, 1.5, "send", "send", "", ""]


def test_an_edited_box_keeps_its_kind():
    coerced = run_cases([
        {"fn": "coercePin", "kind": "bool", "value": "true"},
        {"fn": "coercePin", "kind": "bool", "value": "False"},
        {"fn": "coercePin", "kind": "number", "value": "42"},
        {"fn": "coercePin", "kind": "number", "value": " 1.5 "},
        {"fn": "coercePin", "kind": "str", "value": "send"},
        # A model's JSON answer arrives already typed and must stay so...
        {"fn": "coercePin", "kind": "bool", "value": False},
        {"fn": "coercePin", "kind": "number", "value": 3},
        # ...unless the parameter is a string, where a typed value is text.
        {"fn": "coercePin", "kind": "str", "value": True},
    ])
    assert coerced == [True, False, 42, 1.5, "send", False, 3, "true"]


def test_a_number_box_holding_nonsense_stays_a_string():
    # NaN cannot cross JSON, so it would reach the manifest as a null and be
    # written as nothing. A string reaches `manifest.py`'s type check instead,
    # which refuses it with a message naming the parameter.
    assert run_cases([
        {"fn": "coercePin", "kind": "number", "value": "twelve"},
        {"fn": "coercePin", "kind": "number", "value": ""},
    ]) == ["twelve", ""]


def test_the_panel_no_longer_stringifies_a_pin_anywhere():
    # The two call sites the bug lived at, pinned by absence: seeding used
    # `stripQuotes(p.default)` (source text) and the AI path `String(...)`.
    panel = _read(TEMPLATE)
    assert "stripQuotes" not in panel
    assert "String(item.pinned" not in panel


# ------------------------------------------------------ registration errors


def test_the_panel_reads_the_config_modules_actual_failure_shape():
    panel = _read(TEMPLATE)
    module = _read(CONFIG_MODULE)
    # The shape this is written against: `add`/`remove` return the CLI's streams
    # and no `error` key at all. If that ever grows an `error`, this test is the
    # place that says the panel's fallback chain can be simplified.
    assert '"stdout": res.get("stdout", ""), "stderr": res.get("stderr", "")' in module
    assert "out.error || out.stderr || out.stdout" in panel


def test_every_in_band_refusal_is_surfaced():
    panel = _read(TEMPLATE)
    # `list` (whose refusal used to be read as success — leaving Register looking
    # available), and both writes.
    assert panel.count("out.ok === false") == 3
    # Three call sites plus the helper's own definition.
    assert panel.count("configError(out") == 4


def test_the_final_status_line_cannot_wipe_a_warning():
    # `load()` used to end with an unconditional `say("")`, which erased whatever
    # `refreshRegistration` had just put in the footer.
    panel = _read(TEMPLATE)
    body = panel[panel.index("async function load()"):panel.index("async function callConfig")]
    load = body[:body.index("\n}\n")]
    # The last two statements are the probe and the paint; the status line is set
    # BEFORE them, conditionally, so a warning either of them leaves survives.
    assert load.rstrip().endswith("await refreshRegistration();\n  render();")
    assert 'say("mcp.toml does not parse' in load


# ----------------------------------------------------- nothing dead-clickable


def test_the_action_buttons_start_disabled_and_reload_does_not():
    panel = _read(TEMPLATE)
    for button in ("curate", "add", "save", "register"):
        assert f'id="{button}" disabled' in panel, button
    # Reload is how a user retries a failed load, so it must never be stuck.
    assert 'id="reload">' in panel


def test_a_failed_load_leaves_the_actions_disabled():
    panel = _read(TEMPLATE)
    # `render()` returns early without a report — so the early return has to be
    # the thing that disables, not a line after it.
    render = panel[panel.index("function render()"):]
    early = render[:render.index("el(\"app-name\")")]
    assert "setEnabled(false)" in early


def test_registration_is_null_guarded_independently_of_the_button():
    panel = _read(TEMPLATE)
    toggle = panel[panel.index("async function toggleRegistration()"):]
    guard = toggle[:toggle.index("await guard(")]
    assert "!report || !report.ok || !report.fused" in guard

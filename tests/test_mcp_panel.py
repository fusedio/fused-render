"""The MCP curation panel's own behaviour (SPEC §44 / MC-3, MC-5, MC-7).

Two things are pinned here, and they are the two that were wrong:

* **A pin keeps its TYPE.** The manifest is TOML and the server hands pins to
  the entrypoint as JSON, so a `dry_run: bool = False` pinned "off" has to land
  as `dry_run = false`. It landed as the STRING `"False"` — which the server
  passes through verbatim and Python reads as truthy, so a pinned-OFF safety
  flag behaved as on. The three pure functions that decide a pin's value are run
  for real (`_mcp_pins_probe.mjs`), because a source assertion can only say they
  exist.
* **The panel paints before the registration probe answers.** That probe is
  `claude mcp list`, which health-checks every configured MCP server by
  connecting to it — 10.9s wall on a machine with a dozen claude.ai connectors —
  and it used to be awaited before the first paint, so the panel sat on
  "Loading…" for eleven seconds holding an editor that was already in hand.
* **The toolbar is two verbs.** Curate and Save act on the whole set and belong
  there; `add tool` appends one row and lives at the end of the list; the
  registration state is a line above the footer, because it reports as much as it
  acts; and Reload is gone, so the failures that wanted it offer their own retry.
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


# --------------------------------------------------------------- the AI call


def test_the_curation_call_names_its_model_and_its_effort():
    """The proposal is the panel's one hard reasoning task, so it names sonnet.

    Omitting `model` resolves to the user's default-model preference — Haiku
    where they have none — and a weaker model answers this prompt with one tool
    per function and nothing pinned, which is the curation the user would then
    have to redo by hand. `effort: "low"` is deliberate and, on the Claude path
    with a model that honours it, now actually applies.
    """
    call = _fn_body(_read(TEMPLATE), "async function curate()")
    options = call[call.index("{ systemPrompt: CURATION_SYSTEM"):]
    options = options[:options.index("}")]
    assert 'model: "sonnet"' in options
    assert 'effort: "low"' in options
    # Local-model-only options must not appear here: the Claude path answers a
    # 400 for them rather than ignoring them (the AI bridge's contract).
    for local_only in ("history", "raw", "temperature", "topP", "maxTokens"):
        assert local_only not in options, local_only


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


def _load_body(panel):
    """`load()`'s source, from its signature to its closing brace."""
    body = panel[panel.index("async function load()"):]
    return body[:body.index("\n}\n")]


def _fn_body(panel, signature):
    """One function's source, from its signature to its closing brace."""
    body = panel[panel.index(signature):]
    return body[:body.index("\n}\n")]


def test_the_status_line_is_set_before_the_probe_not_after_it():
    # `load()` used to end with an unconditional `say("")`, which erased whatever
    # `refreshRegistration` had just put in the footer. The ordering is what
    # protects that warning, and it has to survive the probe becoming async: the
    # manifest's own complaint is said first, so the probe (whenever it lands)
    # overwrites it rather than the reverse.
    load = _load_body(_read(TEMPLATE))
    said = load.index('say("mcp.toml does not parse')
    probed = load.index("probe();")
    assert said < probed
    # ...and nothing says anything after the probe is kicked off.
    assert "say(" not in load[probed:]


# ------------------------------------------- the 11s stall: paint, then probe


def test_the_editor_paints_before_the_registration_probe_resolves():
    """The panel must not wait on `claude mcp list`.

    That command health-checks every MCP server the user has configured by
    connecting to each one — 10.9s wall on a machine with a dozen claude.ai
    connectors — and `load()` used to `await` it before the first `render()`. So
    the panel sat on "Loading…" for eleven seconds holding an editor whose
    contents were already in hand, and again on every Reload. Nothing in the
    editor depends on the answer.
    """
    panel = _read(TEMPLATE)
    load = _load_body(panel)
    painted = load.index("\n  render();")
    probed = load.index("probe();")
    assert painted < probed, "render() must come before the probe is started"
    # Not awaited anywhere: the whole point. `.then` re-renders when it lands.
    assert "await refreshRegistration" not in panel
    assert "refreshRegistration().then(" in _fn_body(panel, "function probe()")


def test_a_stale_probe_cannot_overwrite_a_fresh_one():
    # Reload twice inside the sweep and two probes are in flight; the first to
    # return is not the one that owns the panel.
    probe = _fn_body(_read(TEMPLATE), "function probe()")
    assert "probeSeq += 1" in probe
    assert "if (seq !== probeSeq) return;" in probe


def test_editing_is_unlocked_by_the_inspect_alone():
    # A user must not wait on the registration sweep to rename a tool — so the
    # three editor buttons are `setEnabled`'s business and `register` is not.
    panel = _read(TEMPLATE)
    enabled = _fn_body(panel, "function setEnabled(")
    assert '["curate", "save"]' in enabled
    assert "register" not in enabled


def test_the_registration_line_says_which_of_four_states_it_is_in():
    """Not a yes/no: "still checking" and "could not tell" are their own states.

    A line that said "Not registered" while the probe was still running would be
    asserting the opposite of what might be true, and one that said it after a
    FAILED probe would offer a toggle whose direction nobody knows.
    """
    render = _fn_body(_read(TEMPLATE), "function renderRegistration()")
    assert "Checking registration…" in render        # the probe is out
    assert "Registration state unknown" in render    # the probe could not tell
    assert "Registered in Claude" in render          # yes
    assert "Not registered" in render                # no
    assert "Registration unavailable" in render      # no `fused` CLI at all
    # The two that must not be clickable are the two that have no direction.
    assert "!registrationChecked" in render
    assert "disabled = true" in render


def test_a_failed_probe_re_probes_instead_of_toggling():
    # The registration line is the ONLY affordance that can recover an unreadable
    # server list now that Reload is gone, so a click there has to mean "look
    # again" rather than "toggle whatever I last guessed".
    toggle = _fn_body(_read(TEMPLATE), "async function toggleRegistration()")
    assert toggle.index("if (probeFailed)") < toggle.index("await guard(")
    assert "probe();" in toggle
    # And the ordinary guards still refuse an unanswered probe.
    assert "!registrationChecked" in toggle[:toggle.index("await guard(")]


# ----------------------------------------------------- nothing dead-clickable


def test_the_toolbar_is_two_verbs_and_they_start_disabled():
    panel = _read(TEMPLATE)
    header = panel[panel.index("<header>"):panel.index("</header>")]
    # Curate and Save act on the whole tool set; nothing else belongs beside
    # them. Both disabled in the markup, since both need the report.
    ids = [line.split('id="')[1].split('"')[0]
           for line in header.splitlines() if 'id="' in line and "<button" in line]
    assert ids == ["curate", "save"]
    for button in ids:
        assert f'id="{button}" disabled' in panel, button
    assert ">Save<" in header, "the label is Save, not Save manifest"


def test_add_tool_is_a_row_at_the_end_of_the_list():
    # It appends ONE row, so it sits where that row will appear rather than
    # reading as a peer of Curate and Save in the toolbar.
    panel = _read(TEMPLATE)
    render = _fn_body(panel, "function render()")
    made = render.index('text("button", "add-row", "+ add tool")')
    listed = render.index("mainEl.appendChild(renderTool(i))")
    assert listed < made, "the add row goes after the tools, not before them"
    assert 'add.addEventListener("click", addTool)' in render
    assert 'id="add"' not in panel


def test_there_is_no_reload_button_and_a_failed_load_offers_a_retry():
    # Reload is gone (Save re-reads the folder itself), so the one state that
    # needed it — a failed inspect — has to offer the retry itself.
    panel = _read(TEMPLATE)
    assert 'id="reload"' not in panel
    assert "Reload</button>" not in panel
    load = _load_body(panel)
    assert "sayWithRetry(" in load
    retry = _fn_body(panel, "function sayWithRetry(")
    assert "guard(load)" in retry


def test_a_failed_load_leaves_the_actions_disabled():
    panel = _read(TEMPLATE)
    # `render()` returns early without a report — so the early return has to be
    # the thing that disables, not a line after it.
    render = panel[panel.index("function render()"):]
    early = render[:render.index("el(\"app-name\")")]
    assert "setEnabled(false)" in early
    # ...and the registration line is drawn in that state too, rather than left
    # holding whatever it last said about a different folder.
    assert "renderRegistration()" in early


def test_registration_is_null_guarded_independently_of_the_button():
    panel = _read(TEMPLATE)
    toggle = panel[panel.index("async function toggleRegistration()"):]
    guard = toggle[:toggle.index("await guard(")]
    assert "!report || !report.ok || !report.fused" in guard

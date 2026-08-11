"""Node-probe tests for the claude template's segment transcript (task 3).

The page used to render one flat string per assistant turn. `_poll` and
`_history` now return `segments` — the ordered text/thinking/tool record
(`_segments_from_rows`, agent.py) — and this is the renderer for it: one
`<details class="toolchip">` per tool call, a collapsed `<details
class="thinking">` per thinking block, markdown for prose.

**Why node probes and not a browser:** same reason as
tests/test_claude_template_markdown.py — the template is one HTML file with
no module boundary, so the shipping source of each function is *extracted
verbatim* by textual anchors and executed under node. An anchor that stops
matching is a test error, not a silent pass, which is what keeps these
probes attached to the code that actually ships.

**The DOM.** These renderers build real element trees, so unlike the
markdown probes they need a DOM. `_DOM` below is a hand-built minimal one
(the same narrow-stub approach as tests/test_claude_app_state.py's `_DOM`,
widened to what these functions touch: createElement/createTextNode,
append/appendChild/remove/after/replaceChildren, className + classList,
textContent, innerHTML, `open`). No jsdom — the dependency task 1 was told
not to add, and not needed: nothing here lays out, parses HTML or measures.
Two properties of the stub are load-bearing for the assertions:

  - `textContent` and `innerHTML` are stored SEPARATELY and dumped
    separately. That is what lets a test prove tool output went in through
    `textContent` and never through `innerHTML` — the single most important
    rule in this file, because tool output is arbitrary bytes from the
    filesystem/network and is never markdown.
  - every element carries a monotonic `uid`, so "updated in place" can be
    asserted as node identity rather than guessed at from content.

`renderMd` and `attachCodeCopy` are STUBBED here (`<md>…</md>` and a call
recorder). Their behaviour is task 1's contract and is already covered by
test_claude_template_markdown.py; what matters at this seam is *which*
segments are handed to markdown at all, and that is exactly what a stub
makes visible.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "claude",
    "template.html")


@pytest.fixture(scope="module")
def source():
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


def _block(src, start, end):
    """The shipping source from `start` up to and including `end`, verbatim."""
    i = src.index(start)
    j = src.index(end, i) + len(end)
    return src[i:j]


# formatEditDiff + summarizePermission + leftoverInput: one contiguous region.
# The chip renderers reuse the first and the last of those three, so the probes
# concatenate this block with the segment block below — which is also why
# `test_one_diff_formatter_serves_both_the_card_and_the_chip` can check there is
# only ONE implementation of the `-`/`+` marking left.
_PERM_START = "function formatEditDiff(input) {"
_PERM_END = ("  return rest.length ? Object.fromEntries(rest.map((k) => "
             "[k, input[k]])) : null;\n}")
# The segment renderers, in dependency order (glyphs, summary, chip, views,
# renderSegments). Absent entirely before this task, so every probe errors
# rather than silently passing against the old text-only renderer.
_SEG_START = "const TOOL_STATUS_GLYPH = {"
_SEG_END = "  return tail >= 0 ? segText(list[tail]) : null;\n}"
# makeTyper, for the one probe that exercises its new retarget() contract.
_TYPER_START = "function makeTyper(bodyEl) {"
_TYPER_END = "abort() { if (raf) cancelAnimationFrame(raf); cur.remove(); },\n  };\n}"


# A DOM with exactly the surface these renderers use. `_html` (innerHTML) and
# `_text` (textContent) are deliberately distinct storage: the dump below
# reports both, so a test can prove which door a string came in through.
_DOM = r"""
let _uid = 0;
function textNode(s) {
  const t = {nodeType: 3, nodeValue: String(s), parentElement: null};
  Object.defineProperty(t, "textContent", {get: () => t.nodeValue});
  return t;
}
function makeEl(tag) {
  const e = {
    uid: ++_uid, tagName: String(tag).toUpperCase(), nodeType: 1,
    className: "", children: [], parentElement: null, open: false,
    _text: "", _html: null, attrs: {}, src: undefined, alt: undefined,
    appendChild(n) {
      if (n.parentElement) n.parentElement.removeChild(n);
      n.parentElement = e; e.children.push(n); return n;
    },
    append(...nodes) {
      nodes.forEach((n) => e.appendChild(typeof n === "string" ? textNode(n) : n));
    },
    removeChild(n) {
      const i = e.children.indexOf(n);
      if (i >= 0) e.children.splice(i, 1);
      n.parentElement = null; return n;
    },
    remove() { if (e.parentElement) e.parentElement.removeChild(e); },
    after(n) {
      const p = e.parentElement;
      if (!p) return;
      if (n.parentElement) n.parentElement.removeChild(n);
      p.children.splice(p.children.indexOf(e) + 1, 0, n);
      n.parentElement = p;
    },
    replaceChildren(...nodes) {
      e.children.slice().forEach((k) => e.removeChild(k));
      e._text = ""; e._html = null;
      nodes.forEach((n) => e.appendChild(typeof n === "string" ? textNode(n) : n));
    },
    setAttribute(k, v) { e.attrs[String(k)] = String(v); },
    getAttribute(k) { return k in e.attrs ? e.attrs[k] : null; },
    // attachCodeCopy is stubbed in these probes, so nothing walks the tree by
    // selector; present so a future caller fails loudly instead of silently.
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
  Object.defineProperty(e, "textContent", {
    get: () => e._text + e.children.map((k) => k.textContent).join(""),
    set: (v) => {
      e.children.slice().forEach((k) => e.removeChild(k));
      e._html = null; e._text = String(v);
    },
  });
  Object.defineProperty(e, "innerHTML", {
    get: () => (e._html === null ? "" : e._html),
    set: (v) => {
      e.children.slice().forEach((k) => e.removeChild(k));
      e._text = ""; e._html = String(v);
    },
  });
  Object.defineProperty(e, "firstChild", {get: () => e.children[0] || null});
  Object.defineProperty(e, "lastChild",
                        {get: () => e.children[e.children.length - 1] || null});
  e.classList = {
    add(...cs) {
      const have = e.className ? e.className.split(/\s+/) : [];
      cs.forEach((c) => { if (have.indexOf(c) < 0) have.push(c); });
      e.className = have.join(" ");
    },
    remove(...cs) {
      e.className = (e.className ? e.className.split(/\s+/) : [])
        .filter((c) => cs.indexOf(c) < 0).join(" ");
    },
    contains(c) { return (e.className ? e.className.split(/\s+/) : []).indexOf(c) >= 0; },
    toggle(c, on) { if (on) e.classList.add(c); else e.classList.remove(c); },
  };
  return e;
}
const document = {createElement: makeEl, createTextNode: textNode};
// Serialize a subtree for the python side. `text` is textContent, `html` is
// innerHTML — separately, on purpose (see the module docstring).
function dump(n) {
  if (n.nodeType === 3) return {tag: "#text", text: n.nodeValue};
  return {
    uid: n.uid, tag: n.tagName.toLowerCase(), cls: n.className,
    text: n.textContent, html: n.innerHTML, open: !!n.open,
    src: n.src === undefined ? null : n.src, attrs: n.attrs,
    children: n.children.map(dump),
  };
}
// Every node of a dumped tree, flat, root included.
function flat(d) {
  const out = [d];
  (d.children || []).forEach((k) => out.push(...flat(k)));
  return out;
}
"""

# renderMd/attachCodeCopy stubs + a container to render into. The `<md>` wrapper
# makes "this text went through markdown" visible in the dump.
_STUBS = r"""
const mdCalls = [];
const renderMd = (t) => { mdCalls.push(t); return "<md>" + t + "</md>"; };
const attachCalls = [];
const attachCodeCopy = (el) => { attachCalls.push(el.uid); };
function fakeTyper() {
  const calls = [];
  return {
    calls,
    retarget(el) { calls.push(["retarget", el ? el.uid : null]); },
    update(t) { calls.push(["update", t]); },
    finish(t) { calls.push(["finish", t]); return Promise.resolve(); },
    abort() { calls.push(["abort", null]); },
  };
}
"""


def _node(script, tmp_path):
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is preinstalled on the CI runners
        pytest.skip("node is required to drive the template's own JS")
    harness = tmp_path / "harness.mjs"
    harness.write_text(script, encoding="utf-8")
    out = subprocess.run([node, str(harness)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class _Probe:
    """The shipping perm-card + segment blocks, run over the stub DOM."""

    def __init__(self, source, tmp_path):
        self.perm = _block(source, _PERM_START, _PERM_END)
        self.seg = _block(source, _SEG_START, _SEG_END)
        self._tmp_path = tmp_path

    def run(self, body):
        script = "\n".join([_DOM, _STUBS, self.perm, self.seg, body])
        return _node(script, self._tmp_path)

    def render(self, segments, typer=False, twice=False):
        """renderSegments over a fresh container; returns the dumped tree."""
        script = """
const container = document.createElement("span");
container.className = "body";
const typer = %s;
const segs = %s;
const tail = renderSegments(container, segs, {typer});
%s
console.log(JSON.stringify({
  tree: dump(container), tail, mdCalls, attachCalls,
  typerCalls: typer ? typer.calls : [],
}));
""" % ("fakeTyper()" if typer else "null", json.dumps(segments),
       "renderSegments(container, segs, {typer});" if twice else "")
        return self.run(script)


@pytest.fixture()
def probe(source, tmp_path):
    return _Probe(source, tmp_path)


def _tool(name, inp, status="running", output=None, images=None, tid="t1"):
    return {"kind": "tool", "id": tid, "name": name, "input": inp,
            "status": status, "output": output, "images": images or []}


def _nodes(tree):
    out = [tree]
    for kid in tree.get("children") or []:
        out.extend(_nodes(kid))
    return out


def _by_class(tree, cls):
    return [n for n in _nodes(tree)
            if n.get("cls") and cls in n["cls"].split()]


# --- formatEditDiff: one formatter, two call sites ---------------------------


def test_format_edit_diff_marks_every_old_and_new_line(probe):
    got = probe.run("""
console.log(JSON.stringify({
  d: formatEditDiff({old_string: "a\\nb", new_string: "c"}),
  empty: formatEditDiff({}),
  nonString: formatEditDiff({old_string: 7, new_string: ["x"]}),
}));
""")
    assert got["d"] == "- a\n- b\n+ c"
    # Missing keys still render both markers — the perm card has always done
    # this (an Edit with no old_string is a real, if odd, input) and the chip
    # must not diverge from the card on the same input.
    assert got["empty"] == "- \n+ "
    # Non-strings are JSON-stringified, not "[object Object]" — same `str()`
    # rule the perm card's other fields use.
    assert got["nonString"] == "- 7\n+ [\"x\"]"


def test_one_diff_formatter_serves_both_the_card_and_the_chip(source):
    # The `-`/`+` marking existed only inside summarizePermission's Edit case.
    # Task 3 needed it in the chip too, and a second copy would drift from the
    # card on the next change — so it is extracted, and this pins that the
    # extraction actually replaced the original rather than sitting beside it.
    perm = _block(source, _PERM_START, _PERM_END)
    assert "formatEditDiff(" in perm.split("function summarizePermission")[1], (
        "summarizePermission must render its Edit body through formatEditDiff"
    )
    # Exactly one place builds `<marker> <line>` lines, anywhere in the file.
    assert len(re.findall(r'ch \+ " " \+ line', source)) == 1


# --- the chip's one-line summary --------------------------------------------


@pytest.mark.parametrize("name,inp,expected", [
    ("Bash", {"command": "ls -la"}, "$ ls -la"),
    # Only the first line, and marked as clipped — a heredoc must not push the
    # summary to twenty lines tall.
    ("Bash", {"command": "cat <<EOF\nhi\nEOF"}, "$ cat <<EOF …"),
    ("Read", {"file_path": "/tmp/a.py"}, "/tmp/a.py"),
    ("Read", {"path": "/tmp/b.py"}, "/tmp/b.py"),
    ("Glob", {"pattern": "**/*.py"}, "**/*.py"),
    ("Glob", {"pattern": "*.py", "path": "/src"}, "*.py  in /src"),
    ("Grep", {"pattern": "TODO", "glob": "*.js"}, "TODO  in *.js"),
    # +added -removed, counted in lines, so the size of the change is legible
    # without opening the chip.
    ("Edit", {"file_path": "/a.py", "old_string": "x\ny", "new_string": "z"},
     "/a.py  +1 -2"),
    ("Write", {"file_path": "/a.py", "content": "one\ntwo\nthree"}, "/a.py  +3"),
    ("Task", {"description": "hunt the bug", "subagent_type": "Explore"},
     "hunt the bug"),
    ("TodoWrite", {"todos": [{"content": "a", "status": "completed"},
                             {"content": "b", "status": "pending"}]},
     "1/2 done"),
    ("WebFetch", {"url": "https://example.com", "prompt": "summarize"},
     "https://example.com"),
    ("WebSearch", {"query": "duckdb parquet"}, "duckdb parquet"),
    # Unknown/MCP tool: nothing is invented. The chip's own name span already
    # carries the tool name (asserted separately below), so the one-liner is
    # empty rather than a duplicate of it.
    ("mcp__whatever__do_thing", {"a": 1}, ""),
])
def test_tool_chip_summary_per_tool(probe, name, inp, expected):
    got = probe.run("console.log(JSON.stringify({s: toolChipSummary(%s)}));"
                    % json.dumps({"name": name, "input": inp}))
    assert got["s"] == expected


def test_summary_never_spans_lines_even_for_multiline_input(probe):
    # The summary is a one-line control (it is the <summary> of a <details>);
    # a raw newline in it breaks the row layout.
    got = probe.run("""
const names = ["Bash", "Read", "Edit", "Write", "Task", "WebSearch", "Grep"];
const inp = {command: "a\\nb", file_path: "/a\\nb", old_string: "x", new_string: "y",
             content: "c", description: "d\\ne", query: "q\\nr", pattern: "p\\nq"};
console.log(JSON.stringify({
  bad: names.filter((n) => toolChipSummary({name: n, input: inp}).indexOf("\\n") >= 0),
}));
""")
    assert got["bad"] == []


def test_unknown_tool_chip_shows_its_name_in_the_summary_row(probe):
    got = probe.render([_tool("mcp__linear__create_issue", {"title": "x"})])
    chip = _by_class(got["tree"], "toolchip")[0]
    summary = [n for n in _nodes(chip) if n["tag"] == "summary"][0]
    assert "mcp__linear__create_issue" in summary["text"]


# --- the chip's body --------------------------------------------------------


def test_edit_chip_renders_a_line_classed_diff_and_opens_by_default(probe):
    got = probe.render([_tool(
        "Edit", {"file_path": "/a.py", "old_string": "old", "new_string": "new"})])
    chip = _by_class(got["tree"], "toolchip")[0]
    assert chip["open"] is True, "an Edit chip must be open — seeing the diff is the point"
    pre = [n for n in _nodes(chip) if n["tag"] == "pre"][0]
    assert "diff" in pre["cls"].split()
    # Colouring is per-line CSS classes, never inline styles.
    assert [n["text"] for n in _by_class(pre, "diff-del")] == ["- old"]
    assert [n["text"] for n in _by_class(pre, "diff-add")] == ["+ new"]
    # Newlines survive as real text nodes, so the pre reads (and copies) as a
    # diff rather than one run-together line.
    assert pre["text"] == "- old\n+ new"


def test_write_chip_opens_and_shows_path_plus_content(probe):
    got = probe.render([_tool("Write", {"file_path": "/a.py", "content": "x = 1"})])
    chip = _by_class(got["tree"], "toolchip")[0]
    assert chip["open"] is True
    pres = [n for n in _nodes(chip) if n["tag"] == "pre"]
    assert any(p["text"] == "x = 1" for p in pres)
    assert "/a.py" in chip["text"]


def test_bash_chip_is_collapsed_and_shows_command_then_output(probe):
    got = probe.render([_tool("Bash", {"command": "ls"}, status="ok",
                              output="a.py\nb.py")])
    chip = _by_class(got["tree"], "toolchip")[0]
    assert chip["open"] is False, "only Edit/Write open by default"
    pres = [n["text"] for n in _nodes(chip) if n["tag"] == "pre"]
    assert pres.index("ls") < pres.index("a.py\nb.py"), "command above its output"


def test_a_tool_with_no_result_yet_renders_no_output_block(probe):
    # `output: null` means "no result has arrived", which is not the same fact
    # as an empty result — neither may print an empty output box.
    got = probe.render([_tool("Bash", {"command": "ls"})])
    chip = _by_class(got["tree"], "toolchip")[0]
    assert _by_class(chip, "chip-out") == []


def test_todo_write_body_is_a_checklist(probe):
    got = probe.render([_tool("TodoWrite", {"todos": [
        {"content": "read the file", "status": "completed"},
        {"content": "write the fix", "status": "in_progress"},
        {"content": "run the tests", "status": "pending"},
    ]})])
    chip = _by_class(got["tree"], "toolchip")[0]
    rows = [n["text"] for n in _by_class(chip, "chip-todo")]
    assert rows == ["☑ read the file", "☐ write the fix",
                    "☐ run the tests"]


def test_error_status_shows_the_failure_glyph_and_its_output(probe):
    got = probe.render([_tool("Bash", {"command": "false"}, status="error",
                              output="exit 1: nope")])
    chip = _by_class(got["tree"], "toolchip")[0]
    glyph = _by_class(chip, "chip-status")[0]
    assert glyph["text"] == "✗"
    assert "error" in glyph["cls"].split(), "the class carries the colour, not a style attr"
    assert any(n["text"] == "exit 1: nope" for n in _by_class(chip, "chip-out")), (
        "a failed tool's output is the only explanation of the failure — it must render"
    )


def test_running_status_shows_a_spinner_glyph(probe):
    got = probe.render([_tool("Bash", {"command": "sleep 1"})])
    glyph = _by_class(got["tree"], "chip-status")[0]
    assert glyph["text"] == TOOL_RUNNING_GLYPH
    assert "running" in glyph["cls"].split()


TOOL_RUNNING_GLYPH = "◜"


# --- nothing is dropped, nothing is trusted ---------------------------------


def test_unknown_tool_body_dumps_every_input_key(probe):
    # An unrecognised tool (every MCP tool, and anything the CLI adds after
    # this was written) shows its whole input as JSON: no per-tool renderer can
    # exist for it, and a chip that showed part of it would be a chip that lied
    # about what ran. `__proto__` is in here because res.json() really does
    # produce it as an OWN key and Object.keys/JSON.stringify keep it — the
    # same trap leftoverInput was written for.
    got = probe.run("""
const inp = JSON.parse('{"__proto__": {"x": 1}, "beta": 2, "alpha": "three"}');
const container = document.createElement("span");
renderSegments(container, [{kind: "tool", id: "t", name: "mcp__x__y", input: inp,
                            status: "ok", output: null, images: []}], {});
console.log(JSON.stringify({tree: dump(container)}));
""")
    body = got["tree"]["text"]
    for key in ("__proto__", "beta", "alpha", "three"):
        assert key in body, key


def test_known_tool_leftover_keys_are_dumped_not_dropped(probe):
    # Bash's renderer shows command + output. `run_in_background` changes what
    # the call DOES, so it cannot be silently invisible just because the
    # renderer has no line for it — same leftover rule as the perm card's.
    got = probe.render([_tool("Bash", {"command": "sleep 30",
                                       "run_in_background": True})])
    assert "run_in_background" in got["tree"]["text"]


def test_hostile_tool_output_and_input_never_reach_innerhtml(probe):
    # THE rule of this file. Tool output is bytes off the filesystem or the
    # network and tool input is model-authored; neither is ever markdown, so
    # neither may travel through renderMd/innerHTML. `mdCalls` is empty and no
    # node's innerHTML holds the payload — it is only ever textContent.
    nasty = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    got = probe.render([_tool("Bash", {"command": nasty}, status="ok",
                              output=nasty)])
    assert got["mdCalls"] == [], (
        "tool input/output must never be handed to the markdown renderer"
    )
    nodes = _nodes(got["tree"])
    assert not any("<script>" in (n.get("html") or "") for n in nodes), (
        "tool text reached innerHTML — this is XSS in the transcript"
    )
    assert sum(1 for n in nodes if n.get("text") == nasty) >= 2, (
        "the payload must still be VISIBLE, verbatim, as text"
    )


def test_images_render_as_capped_data_uris_and_reject_non_images(probe):
    got = probe.render([_tool("Read", {"file_path": "/a.png"}, status="ok",
                              output="", images=[
                                  {"media_type": "image/png", "data": "QUJD"},
                                  # Not an image type / not base64: skipped,
                                  # never interpolated into a src.
                                  {"media_type": "text/html", "data": "QUJD"},
                                  {"media_type": "image/png",
                                   "data": '"><script>x</script>'},
                              ])])
    imgs = [n for n in _nodes(got["tree"]) if n["tag"] == "img"]
    assert [n["src"] for n in imgs] == ["data:image/png;base64,QUJD"]
    assert "chip-img" in imgs[0]["cls"].split(), (
        "the max-width cap is a CSS class, so both themes get it"
    )


# --- thinking ---------------------------------------------------------------


def test_thinking_is_a_collapsed_details_rendered_as_markdown(probe):
    got = probe.render([{"kind": "thinking", "text": "**weighing** options"}])
    block = _by_class(got["tree"], "thinking")[0]
    assert block["tag"] == "details"
    assert block["open"] is False
    summary = [n for n in _nodes(block) if n["tag"] == "summary"][0]
    assert summary["text"] == "Thought for a moment"
    assert got["mdCalls"] == ["**weighing** options"]
    assert any("<md>**weighing** options</md>" == (n.get("html") or "")
               for n in _nodes(block))


# --- idempotency, in-place updates, and the streaming tail ------------------


def test_rendering_the_same_segments_twice_adds_no_nodes(probe):
    segs = [{"kind": "text", "text": "hi"},
            _tool("Read", {"file_path": "/a.py"}, status="ok", output="x = 1"),
            {"kind": "thinking", "text": "hm"},
            {"kind": "text", "text": "done"}]
    once = probe.render(segs)
    twice = probe.render(segs, twice=True)
    # Same tree, node ids included: the second pass created nothing.
    assert twice["tree"] == once["tree"]
    assert len(twice["tree"]["children"]) == 4
    # And it did no work: no re-render of unchanged markdown either.
    assert twice["mdCalls"] == once["mdCalls"]


def test_status_flip_updates_the_same_chip_in_place(probe):
    got = probe.run("""
const container = document.createElement("span");
const seg = {kind: "tool", id: "t1", name: "Bash", input: {command: "ls"},
             status: "running", output: null, images: []};
renderSegments(container, [seg], {});
const first = dump(container);
const done = Object.assign({}, seg, {status: "ok", output: "a.py"});
renderSegments(container, [done], {});
const second = dump(container);
console.log(JSON.stringify({first, second}));
""")
    a, b = got["first"], got["second"]
    assert len(a["children"]) == len(b["children"]) == 1
    assert a["children"][0]["uid"] == b["children"][0]["uid"], (
        "a status flip must update the existing chip, not append a second one"
    )
    assert _by_class(a, "chip-status")[0]["text"] == TOOL_RUNNING_GLYPH
    assert _by_class(b, "chip-status")[0]["text"] == "✓"
    assert any(n["text"] == "a.py" for n in _by_class(b, "chip-out"))


def test_a_chips_input_is_treated_as_immutable_and_not_re_stringified(probe):
    # The chip's dirty-key covers status/output/image-count and NOT the input,
    # because a Write's `content` is uncapped and keying on it would
    # re-stringify the whole file being written on every 400 ms poll. That is
    # only sound because the input cannot change under a live chip: agent.py
    # reads tool calls from FINALIZED assistant rows only, deduped by tool id.
    # Pinned as the LIMITATION it is — if the backend ever starts revising a
    # call's input mid-turn, this test is the thing that says the key must grow.
    got = probe.run("""
const container = document.createElement("span");
const seg = {kind: "tool", id: "t1", name: "Write",
             input: {file_path: "/a.py", content: "one"},
             status: "ok", output: "", images: []};
renderSegments(container, [seg], {});
renderSegments(container, [Object.assign({}, seg, {
  input: {file_path: "/a.py", content: "two"}})], {});
console.log(JSON.stringify({tree: dump(container)}));
""")
    pres = [n["text"] for n in _nodes(got["tree"]) if n["tag"] == "pre"]
    assert "one" in pres and "two" not in pres


def test_only_the_trailing_text_segment_streams_through_the_typer(probe):
    got = probe.render([{"kind": "text", "text": "first"},
                        _tool("Read", {"file_path": "/a.py"}),
                        {"kind": "text", "text": "second"}], typer=True)
    texts = _by_class(got["tree"], "seg-text")
    # The earlier text segment is final (a tool call came after it) so it is
    # rendered statically; only the last one is handed to the typer.
    assert texts[0]["html"] == "<md>first</md>"
    assert texts[1]["html"] == ""
    assert got["typerCalls"] == [["retarget", texts[1]["uid"]], ["update", "second"]]
    assert got["tail"] == "second", "the caller needs the tail text for typer.finish"


def test_a_tool_after_the_streaming_text_finalizes_it_and_parks_the_typer(probe):
    # The edge the whole design turns on: the typer is mid-stream on the last
    # text segment when a tool call appears after it. That text is final now —
    # it must be statically re-rendered whole (the typer may have drawn only
    # part of it) and highlighted, and the typer must stop pointing at it.
    got = probe.run("""
const container = document.createElement("span");
const typer = fakeTyper();
const a = {kind: "text", text: "thinking out loud"};
renderSegments(container, [a], {typer});
const midStream = typer.calls.slice();
const withTool = [a, {kind: "tool", id: "t", name: "Bash", input: {command: "ls"},
                      status: "running", output: null, images: []}];
const tail = renderSegments(container, withTool, {typer});
console.log(JSON.stringify({tree: dump(container), tail, mdCalls, attachCalls,
                            midStream, after: typer.calls.slice(midStream.length)}));
""")
    text = _by_class(got["tree"], "seg-text")[0]
    assert got["midStream"] == [["retarget", text["uid"]], ["update", "thinking out loud"]]
    assert got["after"] == [["retarget", None]], (
        "with a tool at the tail there is no prose streaming — park the typer"
    )
    assert text["html"] == "<md>thinking out loud</md>", (
        "the finalized segment must be re-rendered whole, not left half-typed"
    )
    assert got["attachCalls"] == [text["uid"]], (
        "a finalized text segment gets its copy buttons/highlighting once"
    )
    assert got["tail"] is None


def test_the_typer_moves_to_a_new_trailing_text_segment(probe):
    got = probe.run("""
const container = document.createElement("span");
const typer = fakeTyper();
const a = {kind: "text", text: "before"};
const tool = {kind: "tool", id: "t", name: "Bash", input: {command: "ls"},
              status: "ok", output: "out", images: []};
renderSegments(container, [a], {typer});
renderSegments(container, [a, tool], {typer});
const before = typer.calls.length;
renderSegments(container, [a, tool, {kind: "text", text: "after"}], {typer});
console.log(JSON.stringify({tree: dump(container),
                            moved: typer.calls.slice(before)}));
""")
    texts = _by_class(got["tree"], "seg-text")
    assert len(texts) == 2
    assert got["moved"] == [["retarget", texts[1]["uid"]], ["update", "after"]]


def test_no_typer_renders_every_text_segment_statically(probe):
    # The history-restore path: a finished turn has nothing to stream.
    got = probe.render([{"kind": "text", "text": "one"},
                        _tool("Read", {"file_path": "/a.py"}, status="ok"),
                        {"kind": "text", "text": "two"}])
    assert [n["html"] for n in _by_class(got["tree"], "seg-text")] == \
        ["<md>one</md>", "<md>two</md>"]
    assert got["mdCalls"] == ["one", "two"]


def test_a_growing_text_segment_re_renders_when_it_is_not_the_tail(probe):
    # Same index, different text (the streamed-vs-finalized fallback in
    # _segments_from_rows can restate a segment): the DOM must follow.
    got = probe.run("""
const container = document.createElement("span");
const tool = {kind: "tool", id: "t", name: "Bash", input: {command: "ls"},
              status: "ok", output: "o", images: []};
renderSegments(container, [{kind: "text", text: "short"}, tool], {});
renderSegments(container, [{kind: "text", text: "much longer"}, tool], {});
console.log(JSON.stringify({tree: dump(container), mdCalls}));
""")
    assert _by_class(got["tree"], "seg-text")[0]["html"] == "<md>much longer</md>"
    assert got["mdCalls"] == ["short", "much longer"]


def test_leading_blank_lines_in_a_text_segment_go_to_markdown_not_a_pre(probe):
    # `_segments_from_rows` puts the "\n\n" separator INSIDE the segment that
    # follows it (a join invariant with _poll's flat `text`). It is markdown
    # whitespace, not content: hand it to renderMd verbatim and let the parser
    # ignore it — never <pre> it, which would print two blank lines.
    got = probe.render([_tool("Read", {"file_path": "/a.py"}, status="ok"),
                        {"kind": "text", "text": "\n\nAfter the tool."}])
    assert got["mdCalls"] == ["\n\nAfter the tool."]
    assert not any(n["tag"] == "pre" for n in _by_class(got["tree"], "seg-text"))


def test_a_shorter_segment_list_drops_the_stale_views(probe):
    # Defensive: within one turn segments only ever grow, but a container
    # reused for a different (shorter) turn must not keep the old tail on
    # screen.
    got = probe.run("""
const container = document.createElement("span");
renderSegments(container, [{kind: "text", text: "a"}, {kind: "text", text: "b"}], {});
renderSegments(container, [{kind: "text", text: "a"}], {});
console.log(JSON.stringify({tree: dump(container)}));
""")
    assert len(got["tree"]["children"]) == 1


# --- makeTyper's new retarget() contract ------------------------------------


def test_typer_can_be_built_parked_and_retargeted(source, tmp_path):
    # renderSegments needs a typer that (a) can exist before any text segment
    # does, (b) can be pointed at a new element, and (c) can be parked with
    # nothing to stream. The existing call sites still pass an element and must
    # keep working — the legacy-text probe below covers that.
    block = _block(source, _TYPER_START, _TYPER_END)
    script = "\n".join([_DOM, _STUBS, r"""
let rafQ = [];
const requestAnimationFrame = (fn) => { rafQ.push(fn); return rafQ.length; };
// Coarse on purpose: these probes never have two frames in flight, so
// cancelling means "drop the queue".
const cancelAnimationFrame = () => { rafQ = []; };
function flush() {
  for (let i = 0; i < 500 && rafQ.length; i++) {
    const q = rafQ; rafQ = []; q.forEach((f) => f());
  }
}
const nearBottom = () => false;
const scrollBottom = () => {};
""", block, r"""
const parent = document.createElement("span");
const a = document.createElement("div");
const b = document.createElement("div");
parent.append(a, b);
const t = makeTyper(null);
const parked = dump(parent);
t.update("ignored while parked");
flush();
const stillParked = dump(parent);
t.retarget(a);
t.update("hello");
flush();
const onA = dump(parent);
t.retarget(b);
t.update("world");
flush();
const onB = dump(parent);
t.retarget(null);
console.log(JSON.stringify({parked, stillParked, onA, onB,
                            final: dump(parent), attachCalls}));
"""])
    got = _node(script, tmp_path)

    def cursors(tree):
        return [n for n in _nodes(tree) if n.get("cls") == "cursor"]

    assert cursors(got["parked"]) == [], "a parked typer puts no cursor on screen"
    assert got["stillParked"] == got["parked"], "update() on a parked typer is a no-op"
    # Cursor sits directly after the element being typed into, both times.
    on_a = got["onA"]["children"]
    assert [c["tag"] for c in on_a][:2] == ["div", "span"]
    assert on_a[1]["cls"] == "cursor"
    assert on_a[0]["html"] == "<md>hello</md>"
    on_b = got["onB"]["children"]
    assert len(cursors(got["onB"])) == 1, "the cursor moves, it is not duplicated"
    assert on_b[-1]["cls"] == "cursor"
    assert on_b[1]["html"] == "<md>world</md>"
    assert on_b[0]["html"] == "<md>hello</md>", "retarget leaves the old element alone"
    assert cursors(got["final"]) == [], "parking again removes the cursor"


def test_typer_still_streams_into_an_element_passed_at_construction(source, tmp_path):
    # The legacy (segment-less) call site: makeTyper(bodyEl) + update + finish,
    # unchanged behaviour including finish()'s attachCodeCopy on the parent.
    block = _block(source, _TYPER_START, _TYPER_END)
    script = "\n".join([_DOM, _STUBS, r"""
let rafQ = [];
const requestAnimationFrame = (fn) => { rafQ.push(fn); return rafQ.length; };
const cancelAnimationFrame = () => { rafQ = []; };
function flush() {
  for (let i = 0; i < 500 && rafQ.length; i++) {
    const q = rafQ; rafQ = []; q.forEach((f) => f());
  }
}
const nearBottom = () => false;
const scrollBottom = () => {};
""", block, r"""
const parent = document.createElement("span");
const body = document.createElement("div");
parent.appendChild(body);
const t = makeTyper(body);
const started = dump(parent);
t.update("partial");
flush();
const mid = dump(parent);
let settled = null;
t.finish("the whole reply").then(() => { settled = dump(parent); });
const pump = setInterval(() => {
  flush();
  if (settled) {
    clearInterval(pump);
    console.log(JSON.stringify({started, mid, settled, attachCalls,
                                parentUid: parent.uid}));
  }
}, 0);
"""])
    got = _node(script, tmp_path)
    assert [n["cls"] for n in _nodes(got["started"]) if n.get("cls") == "cursor"] == ["cursor"]
    assert got["mid"]["children"][0]["html"] == "<md>partial</md>"
    assert got["settled"]["children"][0]["html"] == "<md>the whole reply</md>"
    assert [n for n in _nodes(got["settled"]) if n.get("cls") == "cursor"] == [], (
        "finish() removes the cursor"
    )
    assert got["attachCalls"] == [got["parentUid"]]


# --- wiring: poll, history, CSS (source contracts) --------------------------


def test_poll_renders_segments_instead_of_the_flat_text_never_both(source):
    # The double-render trap: on a run with no stream deltas the text segments
    # join back to exactly `data.text`, so rendering both shows the reply
    # twice. Segments are authoritative (see _segments_from_rows' docstring) —
    # `data.text` is the fallback arm, not a second render.
    region = _block(source, "const segs = Array.isArray(data.segments)",
                    "if (data.done) {")
    assert "renderSegments(" in region
    flat_text = re.findall(r"typer\.update\(data\.text\)", region)
    assert len(flat_text) == 1, flat_text
    assert re.search(r"\}\s*else[^{;]*typer\.update\(data\.text\)", region), (
        "typer.update(data.text) must sit in the no-segments ELSE arm"
    )
    # The token estimate still reads data.text — it is byte-identical to what
    # it always was, and counting the segments instead would change the number.
    assert 'Math.round((data.text || "").length / 4)' in source


def test_one_static_turn_renderer_serves_history_and_the_reattach_repair(source):
    # FOUR places render a FINISHED assistant turn: history restore, the two
    # re-attach repair branches (which work off a poll payload, so they carry
    # segments too — a run that finished while the frame was away used to lose
    # its whole tool timeline here), and the poll loop's typer-less tail (a run
    # whose text only ever arrived on the poll that ENDED it). One function for
    # all four: the tail used to hand-roll `innerHTML = renderMd(data.text)`, so
    # a payload that carried segments rendered its flat text instead of them.
    helper = _block(source, "function addAssistantTurn(text, segments) {",
                    "  return turn;\n}")
    # Optional-key access: history USER turns carry no `segments` key at all.
    assert "Array.isArray(segments)" in helper
    assert "renderSegments(" in helper
    assert re.search(r"else[^;]*renderMd\(text", helper), (
        "a transcript recorded before segments existed still renders its text"
    )
    history = _block(source, "for (const t of turns) {", "attachCodeCopy(log);")
    assert "addAssistantTurn(t.text, t.segments)" in history
    assert len(re.findall(r"addAssistantTurn\(probe\.text, probe\.segments\)",
                          source)) == 2
    assert "addAssistantTurn(data.text, data.segments)" in source, (
        "the poll loop's typer-less tail is the fourth finished-turn render, "
        "and it must go through the same function as the other three"
    )
    # ...and nowhere still renders a finished turn's flat text on its own.
    assert "renderMd(probe.text)" not in source
    assert "renderMd(t.text)" not in source
    assert "renderMd(data.text)" not in source


# The whole set of functions allowed to put renderMd output into an innerHTML.
# ENUMERATED, not described: "one renderer" (D244) is only a rule if a NEW
# hand-rolled site is a test failure, and the poll loop's tail evaded the checks
# above for exactly as long as they only named the sites that already existed.
# Each entry is a deliberate seam:
#   makeTyper          the streaming target, rewritten per animation frame
#   buildTextView      one prose segment of a turn
#   buildThinkingView  a folded reasoning block
#   addAssistantTurn   every FINISHED turn (all four callers above)
#   buildPlanCard      the one tool INPUT that is genuinely markdown (D246)
# Adding to this set is a decision about what may reach innerHTML at all; every
# other payload on this page goes in through textContent.
_MD_INNERHTML_OWNERS = {
    "makeTyper", "buildTextView", "buildThinkingView", "addAssistantTurn",
    "buildPlanCard",
}


def test_only_the_sanctioned_renderers_put_markdown_into_an_innerhtml(source):
    js = source[source.index("<script>"):]
    # Comments out first, both kinds: they QUOTE code (this very fix's own
    # comment names `innerHTML = renderMd(...)` to say what it replaced), and a
    # scanner that counts prose as a call site fails for the wrong reason.
    # Line-anchored for `//` so a `https://` inside a string survives.
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"^[ \t]*//.*$", "", js, flags=re.M)
    # TOP-LEVEL `function name(` declarations only (column 0), so a site inside
    # a nested helper is attributed to the function that owns it — makeTyper's
    # two are inside its own `tick()`, and "tick" is not an answer to which
    # renderer this is.
    decls = [(m.start(), m.group(1))
             for m in re.finditer(r"^function\s+(\w+)\s*\(", js, re.M)]
    sites = list(re.finditer(r"innerHTML\s*=\s*renderMd\(", js))
    assert sites, "no renderMd render sites found at all — anchor rotted"
    owners = set()
    for site in sites:
        above = [name for pos, name in decls if pos < site.start()]
        assert above, "a renderMd innerHTML outside any function"
        owners.add(above[-1])
    assert owners == _MD_INNERHTML_OWNERS, (
        "renderMd output reaches an innerHTML somewhere new, or a sanctioned "
        "renderer stopped doing it: %s" % sorted(owners)
    )


def test_diff_colours_are_theme_variables_defined_in_both_themes(source):
    style = source[source.index("<style>"):source.index("</style>")]
    dark = style[style.index(":root {"):style.index(':root[data-theme="light"] {')]
    light = style[style.index(':root[data-theme="light"] {'):]
    for var in ("--diff-add-bg", "--diff-add-fg", "--diff-del-bg", "--diff-del-fg"):
        assert var + ":" in dark, "%s missing from the dark palette" % var
        assert var + ":" in light, "%s missing from the light palette" % var
    for cls in (".diff-add", ".diff-del"):
        rule = re.search(re.escape(cls) + r"\s*\{([^}]*)\}", style)
        assert rule, "no %s rule" % cls
        assert "var(--diff-" in rule.group(1)


# --- the question card (AskUserQuestion) --------------------------------------
# The one permission card that is not an approval: `buildPermCard` routes
# tool_name AskUserQuestion to `buildQuestionCard`, whose controls send a chosen
# option LABEL instead of a verdict (agent.py + permission_server validate it
# against the parked request; tests/test_claude_permission_bridge.py pins that
# half). These probes run the shipping builder over the stub DOM, because what
# matters is the tree the user clicks: which controls exist, what they send, and
# that every model-authored string went in through textContent.

_CARD_CONSTS_START = "const WHOLE_TOOL_GRANTABLE = new Set(["
_CARD_CONSTS_END = "const PLAN_NOTE_LIMIT = 2000;"
_CARD_START = "function permChoices(tool, label, liveMode) {"
# buildPlanCard's last two lines — the LAST builder in the region, and unique to
# it (the other two append their status differently), so the window closes on the
# whole card region and an edit that moves the end is a test error, not a silent
# pass.
_CARD_END = "  el.append(note, actions, status);\n  return { el, resolve };\n}"

# What buildPermCard/buildQuestionCard reach for outside their own region. The
# recorder is the point: `sent` is exactly the `decide` payload that would cross
# into python, so a probe asserts on the wire rather than on the DOM's intent.
_CARD_STUBS = r"""
const AGENT = "agent.py";
const DEFAULT_PERMISSION = "prompt";
const PERMISSION_LABELS = {auto: "Claude decides", acceptEdits: "Auto-accept edits"};
const sent = [];
const extra = {};   // a probe's own mid-run observations, dumped with the tree
let reply = {};
// The picker's params, keyed: the plan card reads `permission` to decide which
// mode (if any) the approved session should land in, so a probe has to be able
// to sit the picker somewhere.
const paneParams = {};
const paramWrites = [];
const fused = {
  params: {
    set(k, v) { paneParams[k] = v; paramWrites.push([k, v]); },
    get(k) { return paneParams[k] || ""; },
  },
  runPython(agent, params) { sent.push(params); return Promise.resolve(reply); },
};
function syncSelects() {}
// Task 1's markdown funnel, stubbed the way _STUBS stubs it: the `<md>` wrapper
// is what makes "this text went through renderMd" visible in the dump, which is
// the whole assertion for the plan card's body.
const mdCalls = [];
const renderMd = (t) => { mdCalls.push(t); return "<md>" + t + "</md>"; };
const attachCalls = [];
const attachCodeCopy = (el) => { attachCalls.push(el.className); };
// Live element helpers (dump() loses the handlers, so clicking needs the tree).
function walk(el) {
  const out = [el];
  (el.children || []).forEach((k) => { if (k.nodeType === 1) out.push(...walk(k)); });
  return out;
}
const byClass = (el, cls) => walk(el).filter(
  (n) => (n.className || "").split(/\s+/).indexOf(cls) >= 0);
const byTag = (el, tag) => walk(el).filter((n) => n.tagName === tag);
const settle = () => new Promise((r) => setTimeout(r, 0));
// dump() plus the form state these cards carry.
function cdump(n) {
  if (n.nodeType === 3) return {tag: "#text", text: n.nodeValue, children: []};
  return {
    tag: n.tagName.toLowerCase(), cls: n.className, text: n.textContent,
    html: n.innerHTML, type: n.type === undefined ? null : n.type,
    name: n.name === undefined ? null : n.name,
    checked: !!n.checked, disabled: !!n.disabled, attrs: n.attrs || {},
    children: n.children.map(cdump),
  };
}
"""

_ONE_QUESTION = {
    "questions": [{
        "question": "Alpha or Beta?",
        "header": "Choice",
        "options": [{"label": "Alpha", "description": "Pick Alpha"},
                    {"label": "Beta", "description": "Pick Beta"}],
        "multiSelect": False,
    }],
}
_MULTI = {
    "questions": [{
        "question": "Which libraries?",
        "header": "Libs",
        "options": [{"label": "Alpha", "description": "a"},
                    {"label": "Beta", "description": "b"},
                    {"label": "Gamma", "description": "c"}],
        "multiSelect": True,
    }],
}


class _CardProbe:
    """The shipping permission-card region, run over the stub DOM."""

    def __init__(self, source, tmp_path):
        self.consts = _block(source, _CARD_CONSTS_START, _CARD_CONSTS_END)
        self.perm = _block(source, _PERM_START, _PERM_END)
        self.cards = _block(source, _CARD_START, _CARD_END)
        self._tmp_path = tmp_path

    def build(self, tool_input, actions="", reply=None, perm=None, params=None,
              tool="AskUserQuestion"):
        """Build the card for one parked request, run `actions`, dump it."""
        request = {"id": "req-1", "tool": tool, "input": tool_input,
                   "decision": "", "scope": "", "mode": "", "answers": {}}
        request.update(perm or {})
        # `before` is the card as built, `tree` the card after `actions` ran — a
        # resolved card removes its own controls, so the structural assertions
        # read the first and the outcome assertions the second.
        body = """
const p = %s;
reply = %s;
Object.assign(paneParams, %s);
const card = buildPermCard(p, "run-1", "prompt");
const before = cdump(card.el);
(async () => {
  %s
  console.log(JSON.stringify({before, tree: cdump(card.el), sent, extra,
                              mdCalls, attachCalls, paramWrites}));
})();
""" % (json.dumps(request), json.dumps(reply if reply is not None else {}),
       json.dumps(params or {}), actions)
        script = "\n".join([_DOM, _CARD_STUBS, self.consts, self.perm,
                            self.cards, body])
        return _node(script, self._tmp_path)


@pytest.fixture()
def card(source, tmp_path):
    return _CardProbe(source, tmp_path)


def _texts(tree, cls):
    return [n["text"] for n in _by_class(tree, cls)]


def test_a_question_card_renders_the_header_question_and_every_option(card):
    got = card.build(_ONE_QUESTION)
    tree = got["tree"]
    assert "ask" in tree["cls"].split(), tree["cls"]
    assert _texts(tree, "qhead") == ["Choice"]
    assert _texts(tree, "qtext") == ["Alpha or Beta?"]
    # One control per option, each showing the label AND its description — the
    # label is the only thing that can be sent, so nothing about it is elided.
    assert _texts(tree, "lbl") == ["Alpha", "Beta"]
    assert _texts(tree, "desc") == ["Pick Alpha", "Pick Beta"]
    assert len(_by_class(tree, "qopt")) == 2
    # A single-choice question answers on click, so there is no submit step.
    assert not _by_class(tree, "qsend")


def test_every_string_on_a_question_card_goes_in_through_text_content(card):
    """The question, the labels and the descriptions are model-authored, and this
    card is read to decide an answer — so markup in any of them must render as
    the characters it is, exactly like a tool input on an approval card."""
    hostile = {"questions": [{
        "question": "<img src=x onerror=alert(1)>Which?",
        "header": "<b>hdr</b>",
        "options": [
            {"label": "<script>alert(1)</script>", "description": "<i>desc</i>"},
            {"label": "plain", "description": ""},
        ],
        "multiSelect": False,
    }]}
    got = card.build(hostile)
    nodes = _nodes(got["tree"])
    assert all(not n.get("html") for n in nodes), (
        "something on a question card was written as markup")
    assert "<script>alert(1)</script>" in _texts(got["tree"], "lbl")
    assert _texts(got["tree"], "qtext") == ["<img src=x onerror=alert(1)>Which?"]
    assert _texts(got["tree"], "qhead") == ["<b>hdr</b>"]


def test_clicking_an_option_sends_that_label_as_the_answer(card):
    got = card.build(_ONE_QUESTION, actions="""
  byClass(card.el, "qopt")[1].onclick();
  await settle();
""", reply={"decision": "allow", "answers": {"Alpha or Beta?": "Beta"}})
    assert got["sent"] == [{
        "action": "decide", "run_id": "run-1", "request_id": "req-1",
        "scope": "once", "decision": "allow",
        # A JSON string keyed by the exact question text, value = the label.
        "answers": '{"Alpha or Beta?":"Beta"}',
    }]
    status = _by_class(got["tree"], "perm-status")[0]
    assert status["text"] == "✓ You chose: Beta"
    assert "allow" in status["cls"].split()
    # The options are gone once it is answered — the card is a record now.
    assert not _by_class(got["tree"], "qopt")
    assert "resolved" in got["tree"]["cls"].split()


def test_a_multi_select_question_submits_the_ticked_labels_joined(card):
    """", "-joined, in the order the options were offered — that is the string
    the CLI validates against its own option list (a bare JSON list downgrades
    the tool_result to "follow what they actually say")."""
    got = card.build(_MULTI, actions="""
  const boxes = byTag(card.el, "INPUT");
  boxes[2].checked = true;   // Gamma first, to prove the order is normalised
  boxes[0].checked = true;   // Alpha
  byClass(card.el, "qsend")[0].children[0].onclick();
  await settle();
""", reply={"decision": "allow", "answers": {"Which libraries?": "Alpha, Gamma"}})
    assert [n["type"] for n in _nodes(got["before"]) if n["tag"] == "input"] \
        == ["checkbox"] * 3
    assert json.loads(got["sent"][0]["answers"]) == {
        "Which libraries?": "Alpha, Gamma"}
    assert _by_class(got["tree"], "perm-status")[0]["text"] == \
        "✓ You chose: Alpha, Gamma"


def test_a_multi_question_card_will_not_send_a_half_answer(card):
    """One `answers` record covers every question, so the submit is refused
    until each has a choice rather than sending a partial one and letting the
    model read the gaps as "not answered"."""
    two = {"questions": [_ONE_QUESTION["questions"][0], _MULTI["questions"][0]]}
    got = card.build(two, actions="""
  const submit = () => byClass(card.el, "qsend")[0].children[0].onclick();
  const boxes = byTag(card.el, "INPUT");
  boxes[0].checked = true;         // only the first question answered
  submit();
  await settle();
  extra.halfway = {sent: sent.length,
                   status: byClass(card.el, "perm-status")[0].textContent};
  boxes[3].checked = true;         // Beta, on the second question
  submit();
  await settle();
""", reply={"decision": "allow"})
    assert got["extra"]["halfway"] == {
        "sent": 0, "status": "Pick an answer for every question."}
    assert json.loads(got["sent"][0]["answers"]) == {
        "Alpha or Beta?": "Alpha", "Which libraries?": "Beta"}
    # Two questions ⇒ radios for the single-choice one, checkboxes for the other,
    # grouped per question so one question cannot steal another's selection.
    inputs = [n for n in _nodes(got["before"]) if n["tag"] == "input"]
    assert [n["type"] for n in inputs] == ["radio"] * 2 + ["checkbox"] * 3
    assert len({n["name"] for n in inputs}) == 2


def test_a_question_card_offers_neither_a_verdict_nor_a_mode_switch(card):
    """No Allow, no Deny, no "allow all", no "let Claude decide from here".

    An answerless allow reaches the model as "the user did not answer", and a
    grant or a mode switch riding on a question would loosen approvals for every
    later tool on the back of a click that said nothing about permissions.
    """
    got = card.build(_ONE_QUESTION, actions="""
  byClass(card.el, "qopt")[0].onclick();
  await settle();
""", reply={"decision": "allow", "answers": {"Alpha or Beta?": "Alpha"}})
    # `before`, not the resolved card: a resolved card has removed its controls,
    # so reading that one would pass however many verdict buttons it had offered.
    buttons = [n["text"] for n in _nodes(got["before"]) if n["tag"] == "button"]
    assert buttons, "no controls at all — the probe is asserting about nothing"
    for banned in ("Allow", "Deny", "Allow all", "let Claude decide"):
        assert not any(banned in b for b in buttons), buttons
    payload = got["sent"][0]
    assert payload["scope"] == "once" and "mode" not in payload


def test_the_card_shows_the_answer_that_won_the_latch_not_the_click(card):
    """First writer wins on disk, so the card renders what agent.py reports —
    the same rule the approval card follows for its verdict."""
    got = card.build(_ONE_QUESTION, actions="""
  byClass(card.el, "qopt")[1].onclick();   // clicked Beta
  await settle();
""", reply={"decision": "allow", "answers": {"Alpha or Beta?": "Alpha"}})
    assert json.loads(got["sent"][0]["answers"]) == {"Alpha or Beta?": "Beta"}
    assert _by_class(got["tree"], "perm-status")[0]["text"] == "✓ You chose: Alpha"


def test_a_failed_send_brings_the_options_back(card):
    """The tool call is still blocked, so an error must not leave a dead card."""
    got = card.build(_ONE_QUESTION, actions="""
  byClass(card.el, "qopt")[0].onclick();
  await settle();
""", reply={"error": "could not record that decision"})
    assert len(_by_class(got["tree"], "qopt")) == 2
    assert not any(n["disabled"] for n in _nodes(got["tree"])
                   if n["tag"] in ("button", "input"))
    status = _by_class(got["tree"], "perm-status")[0]
    assert "could not record that decision" in status["text"]
    assert "resolved" not in got["tree"]["cls"].split()


@pytest.mark.parametrize("decision,answers,expected", [
    ("allow", {"Alpha or Beta?": "Beta"}, "✓ You chose: Beta"),
    ("allow", {}, "✓ Answered"),
    ("expired", {}, "◦ Unanswered — the reply ended before you answered"),
    ("deny", {}, "✗ Not answered"),
])
def test_a_re_attaching_frame_rebuilds_what_was_already_answered(
        card, decision, answers, expected):
    """poll replays the whole request list with the answer it recorded, so a
    frame that arrived after the click (mode switch, reload) shows the choice
    rather than a live card for a question that is already settled."""
    got = card.build(_ONE_QUESTION, actions="""
  card.resolve(p.decision, p.scope, p.mode, p.answers);
""", perm={"decision": decision, "answers": answers})
    assert _by_class(got["tree"], "perm-status")[0]["text"] == expected
    assert not _by_class(got["tree"], "qopt")


def test_a_question_text_named_proto_still_reaches_the_answer_record(card):
    """`answers` is built with Object.fromEntries, not assignment — the same
    prototype-setter trap the leftover dump fell into (D161). Assigning would
    drop the key and the answer would go back empty."""
    got = card.build({"questions": [{
        "question": "__proto__",
        "options": [{"label": "yes", "description": ""}],
        "multiSelect": False,
    }]}, actions="""
  byClass(card.el, "qopt")[0].onclick();
  await settle();
""", reply={"decision": "allow", "answers": {"__proto__": "yes"}})
    assert json.loads(got["sent"][0]["answers"]) == {"__proto__": "yes"}


@pytest.mark.parametrize("tool_input", [
    {},                                             # no questions at all
    {"questions": []},
    {"questions": [{"question": "Q?", "options": []}]},
    {"questions": [{"question": "Q?"}]},
    {"questions": [{"options": [{"label": "a"}]}]},  # no question text
    # One good, one unusable: the record is validated as a whole, so this cannot
    # be answered either — and rendering only the good half would hide that.
    {"questions": [_ONE_QUESTION["questions"][0], {"question": "Q?"}]},
    # Two questions with the SAME text — the quietest unanswerable payload, and
    # the one that used to render live controls: `answers` is keyed by question
    # text, so the submit would collapse the pair and the validator would reject a
    # key it cannot attribute, latching a permanent deny on a card that had
    # offered the user real-looking choices.
    {"questions": [{"question": "Same?", "options": [{"label": "a"}]},
                   {"question": "Same?", "options": [{"label": "b"}]}]},
], ids=range(7))
def test_an_unanswerable_question_shows_the_payload_and_only_dismisses(
        card, tool_input):
    got = card.build(tool_input, actions="""
  byClass(card.el, "qsend")[0].children[0].onclick();
  await settle();
""", reply={"decision": "deny"})
    # Nothing to click but Dismiss, and the raw input is on screen instead.
    assert not _by_class(got["before"], "qopt")
    assert [n["text"] for n in _nodes(got["before"])
            if n["tag"] == "button"] == ["Dismiss"]
    body = [n["text"] for n in _nodes(got["before"]) if n["tag"] == "pre"]
    assert body and json.loads(body[0]) == tool_input
    assert got["sent"] == [{"action": "decide", "run_id": "run-1",
                            "request_id": "req-1", "scope": "once",
                            "decision": "deny"}]
    assert _by_class(got["tree"], "perm-status")[0]["text"] == "✗ Not answered"


def test_only_askuserquestion_takes_the_question_branch(card):
    """Every other tool keeps the approval card it always had."""
    got = card.build({"command": "ls"}, perm={"tool": "Bash"})
    assert "ask" not in got["tree"]["cls"].split()
    buttons = [n["text"] for n in _nodes(got["tree"]) if n["tag"] == "button"]
    assert "Allow" in buttons and "Deny" in buttons
    assert not _by_class(got["tree"], "qopt")


def test_an_extra_input_key_on_a_question_card_is_still_shown(card):
    """The approval card's disclosure rule holds here too: `questions` is
    rendered as the options, and anything else the CLI sent rides back out in
    `updatedInput`, so it is printed rather than assumed unimportant."""
    got = card.build(dict(_ONE_QUESTION, metadata={"source": "cli"}))
    dumps = [n["text"] for n in _nodes(got["before"]) if n["tag"] == "pre"]
    assert dumps and json.loads(dumps[0]) == {"metadata": {"source": "cli"}}
    # …and a card with nothing extra grows no second block.
    assert not [n for n in _nodes(card.build(_ONE_QUESTION)["before"])
                if n["tag"] == "pre"]


# --- the plan card (ExitPlanMode) ---------------------------------------------
# The other card that is not an ordinary approval: what is parked is a PLAN, in
# markdown, and the two answers are "go ahead" and "keep planning". Two things
# make it different from every other card and both are asserted below:
#
#   * `input.plan` IS markdown, so it is the one payload on any card that goes
#     through `renderMd` (stubbed here as `<md>…</md>`) rather than textContent —
#     which is safe only because that funnel is marked + DOMPurify, and only
#     because nothing else on the card takes that door;
#   * the "Keep planning" note is free text the USER typed, and it must reach the
#     `decide` payload as a plain param — never the DOM, never tool input.
_PLAN = {"plan": "## Plan\n\n1. Read `condition.py`\n2. Split the parser\n"}


def _plan_card(card, **kw):
    kw.setdefault("tool", "ExitPlanMode")
    return card.build(kw.pop("plan_input", _PLAN), **kw)


def _buttons(tree):
    return [n["text"] for n in _nodes(tree) if n["tag"] == "button"]


def test_a_plan_card_renders_the_plan_as_markdown_and_nothing_else(card):
    got = _plan_card(card)
    tree = got["tree"]
    assert "plan" in tree["cls"].split(), tree["cls"]
    assert _texts(tree, "perm-head") == ["Claude has a plan"]
    # The plan reached renderMd verbatim, and the card's markdown body is exactly
    # what came back out of it — no second door into innerHTML.
    assert got["mdCalls"] == [_PLAN["plan"]]
    body = _by_class(tree, "plan-body")
    assert len(body) == 1
    assert body[0]["html"] == "<md>" + _PLAN["plan"] + "</md>"
    assert [n for n in _nodes(tree) if n.get("html")] == body
    # Code fences in a plan get the same copy buttons a reply's do.
    assert got["attachCalls"] == ["plan-body"]


def test_a_hostile_plan_is_inert_because_it_goes_through_the_funnel(card):
    """The plan is model-authored and may quote a file, an issue title or a
    payload it found on the web — so the card's safety is entirely that the ONLY
    route to markup is `renderMd` (marked + DOMPurify, pinned by
    test_claude_template_markdown.py). This asserts the routing: nothing about
    the plan is written to the DOM except what that funnel returned."""
    nasty = "<img src=x onerror=alert(1)>\n\n<script>alert(1)</script>\n"
    got = _plan_card(card, plan_input={"plan": nasty})
    assert got["mdCalls"] == [nasty]
    marked_up = [n for n in _nodes(got["tree"]) if n.get("html")]
    assert len(marked_up) == 1 and "plan-body" in marked_up[0]["cls"]
    assert marked_up[0]["html"] == "<md>" + nasty + "</md>"
    # …and no node took it as text either, which would be the other bug: a plan
    # rendered twice, once escaped and once not.
    assert nasty not in "".join(n.get("text") or "" for n in _nodes(got["tree"])
                               if "plan-body" not in (n.get("cls") or ""))


def test_a_plan_card_offers_exactly_approve_and_keep_planning(card):
    """No Allow, no Deny, and above all no "allow all": there is one plan, and a
    session-wide grant for ExitPlanMode would approve the NEXT one unseen."""
    got = _plan_card(card)
    assert _buttons(got["before"]) == ["Approve plan", "Keep planning"]
    for banned in ("Allow", "Deny", "let Claude decide"):
        assert not any(banned in b for b in _buttons(got["before"]))
    # One note field, and it is a textarea (free text, not an option list),
    # named for anyone who cannot see the placeholder.
    notes = [n for n in _nodes(got["before"]) if n["tag"] == "textarea"]
    assert [n["tag"] for n in notes] == ["textarea"]
    assert notes[0]["attrs"].get("aria-label")


def test_approving_a_plan_sends_a_plain_allow(card):
    """The spike's finding, from the page's side: the CLI leaves plan mode on the
    allow alone, so an approval that changes nothing else sends no mode."""
    got = _plan_card(card, actions="""
  byClass(card.el, "perm-actions")[0].children[0].onclick();
  await settle();
""", reply={"decision": "allow"})
    assert got["sent"] == [{
        "action": "decide", "run_id": "run-1", "request_id": "req-1",
        "decision": "allow", "scope": "once", "mode": "", "note": "",
    }]
    status = _by_class(got["tree"], "perm-status")[0]
    assert status["text"] == "✓ Plan approved"
    assert "allow" in status["cls"].split()
    assert "resolved" in got["tree"]["cls"].split()
    assert not _buttons(got["tree"]) and not [
        n for n in _nodes(got["tree"]) if n["tag"] == "textarea"]
    # No `setMode` rode along (the picker was never given one to sit on in this
    # probe), so the approval lands the picker on the CLI default rather than
    # leaving it on whatever it was — see the parametrized test below for the
    # full landing-mode matrix.
    assert got["paramWrites"] == [["permission", "prompt"]]


@pytest.mark.parametrize("picked,mode", [
    ("acceptEdits", "acceptEdits"),   # the picker sits looser: land there
    ("auto", "auto"),
    ("plan", ""),                     # …and where it does not, send nothing
    ("prompt", ""),                   # tightening mid-turn is the picker's job
    ("", ""),
    ("bypassPermissions", ""),        # unreachable from a card, by the same gate
    ("nonsense", ""),
])
def test_the_landing_mode_is_the_pickers_only_when_it_is_switchable(
        card, picked, mode):
    got = _plan_card(card, params={"permission": picked}, actions="""
  byClass(card.el, "perm-actions")[0].children[0].onclick();
  await settle();
""", reply={"decision": "allow", "mode": mode})
    assert got["sent"][0]["mode"] == mode, got["sent"]
    # Approving ALWAYS moves the picker forward, off "plan" — to the granted
    # `setMode` when there was one, "prompt" (the CLI default) otherwise — so a
    # picker left on "plan" (or on anything else that sends no setMode) does not
    # silently re-enter plan mode on the very next turn.
    assert got["paramWrites"] == [["permission", mode or "prompt"]], got["paramWrites"]


def test_keeping_planning_denies_and_carries_the_users_note(card):
    """The note is the only free text on the card. It rides the `decide` payload
    as a param — agent.py composes the message the model reads — and it is never
    put back into the page."""
    got = _plan_card(card, actions="""
  byTag(card.el, "TEXTAREA")[0].value = "smaller steps, and leave the tests";
  byClass(card.el, "perm-actions")[0].children[1].onclick();
  await settle();
""", reply={"decision": "deny"})
    assert got["sent"] == [{
        "action": "decide", "run_id": "run-1", "request_id": "req-1",
        "decision": "deny", "scope": "once", "mode": "",
        "note": "smaller steps, and leave the tests",
    }]
    status = _by_class(got["tree"], "perm-status")[0]
    assert status["text"] == "◦ Sent back for revision"
    assert "resolved" in got["tree"]["cls"].split()
    # "Keep planning" touches the picker not at all — the session is still
    # planning, so there is nothing to land.
    assert got["paramWrites"] == []


def test_keeping_planning_with_no_note_still_sends_the_deny(card):
    got = _plan_card(card, actions="""
  byClass(card.el, "perm-actions")[0].children[1].onclick();
  await settle();
""", reply={"decision": "deny"})
    assert got["sent"][0]["decision"] == "deny"
    assert got["sent"][0]["note"] == ""


def test_a_hostile_note_is_a_string_in_the_payload_and_nothing_more(card):
    """It is the user's own text, so it is not sanitised — it is CONFINED: the
    only place it goes is the `note` param, and the card never renders it."""
    got = _plan_card(card, actions="""
  byTag(card.el, "TEXTAREA")[0].value = "<img src=x onerror=alert(1)>";
  byClass(card.el, "perm-actions")[0].children[1].onclick();
  await settle();
""", reply={"decision": "deny"})
    assert got["sent"][0]["note"] == "<img src=x onerror=alert(1)>"
    assert got["mdCalls"] == [_PLAN["plan"]], "the note reached the markdown funnel"
    assert not any("onerror" in (n.get("html") or "") + (n.get("text") or "")
                   for n in _nodes(got["tree"]))


def test_a_failed_send_brings_the_plan_buttons_back(card):
    """The subprocess is still blocked on this card, so an error must not leave
    a plan nobody can answer."""
    got = _plan_card(card, actions="""
  byClass(card.el, "perm-actions")[0].children[0].onclick();
  await settle();
""", reply={"error": "could not record that decision"})
    assert _buttons(got["tree"]) == ["Approve plan", "Keep planning"]
    assert not any(n["disabled"] for n in _nodes(got["tree"])
                   if n["tag"] in ("button", "textarea"))
    assert "could not record that decision" in \
        _by_class(got["tree"], "perm-status")[0]["text"]
    assert "resolved" not in got["tree"]["cls"].split()
    # Nothing landed, so the picker must not move either.
    assert got["paramWrites"] == []


@pytest.mark.parametrize("decision,expected", [
    ("allow", "✓ Plan approved"),
    ("deny", "◦ Sent back for revision"),
    ("expired", "◦ Unanswered — the reply ended before you decided"),
])
def test_a_re_attaching_frame_rebuilds_a_plan_that_was_already_answered(
        card, decision, expected):
    got = _plan_card(card, perm={"decision": decision}, actions="""
  card.resolve(p.decision, p.scope, p.mode, p.answers);
""")
    assert _by_class(got["tree"], "perm-status")[0]["text"] == expected
    assert not _buttons(got["tree"])


def test_a_plan_card_shows_the_verdict_that_won_the_latch(card):
    """Same rule as every other card: what is rendered is what agent.py read
    back off disk, not what was clicked."""
    got = _plan_card(card, actions="""
  byClass(card.el, "perm-actions")[0].children[0].onclick();   // Approve
  await settle();
""", reply={"decision": "deny"})
    assert got["sent"][0]["decision"] == "allow"
    assert _by_class(got["tree"], "perm-status")[0]["text"] == \
        "◦ Sent back for revision"
    # The losing half of a double-click must not move the picker either — the
    # plan was NOT actually approved, whatever was clicked.
    assert got["paramWrites"] == []


def test_a_plan_request_with_no_plan_shows_what_did_arrive(card):
    """Nothing about the card is a claim that a plan was read: an ExitPlanMode
    with no usable `plan` prints its whole input verbatim (textContent) instead
    of an empty markdown body, and still offers both answers — the subprocess is
    blocked either way."""
    got = _plan_card(card, plan_input={"note": "no plan key at all"})
    assert not _by_class(got["before"], "plan-body")
    assert got["mdCalls"] == []
    dumps = [n["text"] for n in _nodes(got["before"]) if n["tag"] == "pre"]
    assert dumps and json.loads(dumps[0]) == {"note": "no plan key at all"}
    assert _buttons(got["before"]) == ["Approve plan", "Keep planning"]


def test_an_extra_input_key_on_a_plan_card_is_still_shown(card):
    """Disclosure rule, unchanged: `plan` is the body, anything else the CLI sent
    is printed rather than assumed unimportant."""
    got = _plan_card(card, plan_input=dict(_PLAN, metadata={"source": "cli"}))
    dumps = [n["text"] for n in _nodes(got["before"]) if n["tag"] == "pre"]
    assert dumps and json.loads(dumps[0]) == {"metadata": {"source": "cli"}}
    assert not [n for n in _nodes(_plan_card(card)["before"]) if n["tag"] == "pre"]


def test_only_exitplanmode_takes_the_plan_branch(card):
    """A question card is not a plan card and an approval card is neither."""
    for tool, tool_input in (("AskUserQuestion", _ONE_QUESTION),
                             ("Bash", {"command": "ls"})):
        tree = card.build(tool_input, tool=tool)["tree"]
        assert "plan" not in tree["cls"].split(), tool
        assert not _by_class(tree, "plan-body")
        assert "Approve plan" not in _buttons(tree)

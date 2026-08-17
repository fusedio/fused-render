"""The git view must actually PAINT — asserted by running its real script.

This closes the last hole this branch went through. Everything else the suite has
looked at the template's SOURCE: the channel contract, the DESTRUCTIVE mirror, the
single `op: "resolve"` call site. All of that passes on a template that renders a
blank page, and one really did: while the "Resolve with AI" work was mid-edit the
working tree briefly carried two `let streamed` declarations, and because
templates are served LIVE from the working tree (FUSED_RENDER_CORE_TEMPLATES) the
full-page git view was a blank document for anyone who loaded it in that window.
The call log recorded it exactly once:

    kind=page-error  SyntaxError: Cannot declare a let variable twice: 'streamed'

Nothing else could see it:

  * `node --check` on the extracted script — passes. It is a REDECLARATION across
    the script's top-level lexical scope, which V8 reports at evaluation, and in
    any case the tree was fixed before it was ever committed, so a source check
    on HEAD proves nothing about what the server was serving.
  * `window.onerror` — silent for the OTHER shape of this failure. `draw()` is
    async, so a throw inside `render()` becomes an unhandled REJECTION, which the
    page-error hook does not observe: the page calls Python, gets good data,
    records no error, and paints nothing.
  * the source-contract tests — all green, because the source reads correctly.

So the only thing that catches "renders blank" is rendering it. The probe
(`_git_view_probe.mjs`) runs the template's own `<script>` verbatim against a DOM
stub — the `_DOM_STUB` pattern from test_annotate_revert.py, sized up — with a
`fused` stub that answers `runPython` from a REAL log.py payload. No copy of the
view's logic lives in the harness; if the script throws, or finishes without
filling `#view`, the test fails and says so.

Both states are covered, because the conflicted one is the whole point of the
feature and it takes a different path through `changeLine`/`resolveButton`.
"""
import importlib.util
import json
import os
import shutil
import subprocess

import pytest

from _git_repo import git, git_available

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "fused_render", "templates", "git", "template.html")
READER = os.path.join(ROOT, "fused_render", "templates", "git", "log.py")
PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_git_view_probe.mjs")

pytestmark = pytest.mark.skipif(not git_available(), reason="git binary not installed")


@pytest.fixture(scope="module")
def reader():
    spec = importlib.util.spec_from_file_location("git_log_render", READER)
    # Asserted rather than ignored or cast: a None spec/loader means log.py did
    # not load, and every payload below would then be built from a module that
    # was never executed — the unverifiable-reads-as-fine shape this whole file
    # exists to prevent. Same guard as test_git_reader.py's loader.
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _put(root, rel, text):
    full = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(text)


def clean_repo(root):
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q", root)
    _put(root, "README.md", "# hi\n")
    _put(root, "pkg/mod.py", "one\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    _put(root, "pkg/mod.py", "one\ntwo\n")       # an unstaged change to list
    _put(root, "extra.txt", "untracked\n")       # and an untracked one
    return root


def conflicted_repo(root):
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q", root)
    _put(root, "mod.py", "one\ntwo\nthree\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "branch", "other")
    _put(root, "mod.py", "one\nOURS\nthree\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "ours")
    git(root, "checkout", "-q", "other")
    _put(root, "mod.py", "one\nTHEIRS\nthree\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "theirs")
    git(root, "checkout", "-q", "-")
    git(root, "merge", "other", check=False)      # conflicts on purpose
    return root


def render(reader, repo, tmp_path, params=None):
    """Run the template's script against `repo`'s real reader payloads."""
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is present on CI runners
        pytest.skip("node is required to run the git view")
    payloads = {
        "overview": reader.main(file=repo, op="overview"),
        "stashes": reader.main(file=repo, op="stashes"),
        "conflicts": reader.main(file=repo, op="conflicts"),
    }
    assert payloads["overview"]["ok"] is True, payloads["overview"]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "params": dict({"_file": repo}, **(params or {})),
        "payloads": payloads,
    }))
    proc = subprocess.run([node, PROBE, TEMPLATE, str(fixture)],
                          capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, f"probe crashed:\n{proc.stderr[-3000:]}"
    return json.loads(proc.stdout)


def _assert_painted(out, what):
    assert out["error"] is None, f"{what}: the template's script threw:\n{out['error']}"
    assert not out["unhandled"], (
        f"{what}: an unhandled promise rejection — this is the shape "
        f"window.onerror never sees:\n" + "\n".join(out["unhandled"]))
    assert out["painted"], (
        f"{what}: the script ran without error and left #view EMPTY — a blank "
        f"page. calls={out['calls']}")


def test_a_clean_repo_paints(reader, tmp_path):
    out = render(reader, clean_repo(str(tmp_path / "clean")), tmp_path)
    _assert_painted(out, "clean repo")
    # Not just "some node" — the things the view exists to show.
    text = out["viewText"]
    assert "Commit" in text or "Changes" in text, text
    assert out["skeletonHidden"] is True, "the loading skeleton was never hidden"


def test_a_conflicted_repo_paints(reader, tmp_path):
    """The feature's own state. `changeLine` takes the `change.conflicted` branch
    here and builds `resolveButton`, which the clean case never reaches."""
    out = render(reader, conflicted_repo(str(tmp_path / "conflicted")), tmp_path)
    _assert_painted(out, "conflicted repo")
    assert "mod.py" in out["viewText"], out["viewText"]


def test_the_view_reads_the_reader_on_distinct_channels(reader, tmp_path):
    """Both reads happen, which is what proves the render got real data rather
    than painting an empty state (see test_git_view.py for the channel rule)."""
    out = render(reader, clean_repo(str(tmp_path / "chan")), tmp_path)
    ops = [c["op"] for c in out["calls"]]
    assert "overview" in ops and "stashes" in ops, ops


def test_the_probe_fails_on_a_template_that_throws(reader, tmp_path):
    """The harness's own regression test.

    A render check that cannot fail is worth nothing — that is the lesson this
    file exists to encode — so the probe is pointed at a deliberately broken copy
    of the real template and must report the throw instead of a clean paint.
    """
    broken = tmp_path / "broken.html"
    src = open(TEMPLATE, encoding="utf-8").read()
    # The exact failure that shipped: a second top-level `let streamed`.
    src = src.replace("let streamed = \"\";",
                      "let streamed = \"\";\nlet streamed = \"\";", 1)
    broken.write_text(src)

    payloads = {"overview": reader.main(file=str(tmp_path), op="overview")}
    fixture = tmp_path / "f.json"
    fixture.write_text(json.dumps({"params": {"_file": str(tmp_path)},
                                   "payloads": payloads}))
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node is required")
    proc = subprocess.run([node, PROBE, str(broken), str(fixture)],
                          capture_output=True, text=True, timeout=90)
    out = json.loads(proc.stdout)
    assert out["error"] is not None, "the probe did not notice a duplicate `let`"
    assert "streamed" in out["error"], out["error"]
    assert out["painted"] is False


def test_the_probe_fails_on_a_template_that_paints_nothing(reader, tmp_path):
    """The insidious shape: no error at all, and an empty page.

    A throw is the easy case. The one that actually reached a reviewer's browser
    on this feature's sibling path is a script that runs clean, calls Python, gets
    good data — and paints nothing, because the failure happened after an `await`
    where nothing is watching. `painted` is the assertion that catches it, so it
    gets its own negative control: suppress the paint, keep everything else, and
    the probe must still object.
    """
    repo = clean_repo(str(tmp_path / "silent"))
    broken = tmp_path / "silent.html"
    src = open(TEMPLATE, encoding="utf-8").read()
    # Neuter the one call that fills #view, leaving the rest of the run intact.
    assert "function render(data, stashes, diff) {" in src
    src = src.replace("function render(data, stashes, diff) {",
                      "function render(data, stashes, diff) {\n  return;", 1)
    broken.write_text(src)

    fixture = tmp_path / "f2.json"
    fixture.write_text(json.dumps({
        "params": {"_file": repo},
        "payloads": {"overview": reader.main(file=repo, op="overview"),
                     "stashes": reader.main(file=repo, op="stashes")},
    }))
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node is required")
    proc = subprocess.run([node, PROBE, str(broken), str(fixture)],
                          capture_output=True, text=True, timeout=90)
    out = json.loads(proc.stdout)
    assert out["error"] is None, "this control is about a SILENT blank"
    assert not out["unhandled"], out["unhandled"]
    assert out["painted"] is False, "the probe called an empty #view 'painted'"
    # And the real assertion helper must reject it.
    with pytest.raises(AssertionError, match="EMPTY"):
        _assert_painted(out, "suppressed render")

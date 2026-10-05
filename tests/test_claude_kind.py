"""The `claude` gate and left pane across the two target KINDS (D235).

The split view used to be the app builder's alone: its gate offered it for a
project folder only, and its left pane always resolved that folder's app entry.
D235 bound it to 47 file keys as well, because the annotation / app_state
machinery lives here and nowhere else — chatting about a standalone file with
those tools is the whole point — so the gate now answers for a file too and the
pane renders that file in its OWN default view.

Then the plain chat template was deleted and this became the ONLY chat: the gate's
directory branch widened from "an app folder" to "any directory", and the pane
grew a fallback for the folder that has no app entry to frame. Both are pinned
below, because both replace a rule this file used to assert the opposite of.

Then D239 removed that fallback again — not back to the `throw`, but to NO PANE:
an ordinary folder gets a full-width chat. The embedded file browser reported to
nobody (no `postMessage`, no listener), annotate was hard-disabled over it and
the view picker was inert for it, so it was half the width spent on decoration.
There are now TWO pane shapes and a no-pane case, and the tests below say which
is which.

Three things are worth pinning down, and each of them broke once:

* the FILE branch of the gate must test `isfile`, not `not isdir`. The loose form
  reads every path that does not exist as "a file", which is how a nonexistent
  child of a registered folder once got a `True` out of a gate.
* the pane must resolve the file's template the way the SHELL does — the first
  non-`conditional` entry from stat — rather than from a per-extension table,
  which drifts from the registry the moment a binding changes and ignores a user
  override entirely (§16).
* a directory with no app entry must not leave an error panel beside a working
  chat, which is what the old `throw` did for every folder that is not an app.

The gate is exec'd standalone here, the way `server._run_condition` execs it —
never imported as part of a package, since a template may not import
fused_render (SPEC PY-15 / D166).

D1308: the page half of this file (source pins and node probes over the
retired iframe chat page, templates/claude/template.html) went with that
page; the native chat under frontend/src/apps/claude owns it now. What
stays is the backend half.
"""
import importlib.util
import json
import os

import pytest


def _gate():
    path = os.path.join("fused_render", "templates", "claude", "condition.py")
    spec = importlib.util.spec_from_file_location("test_claude_condition", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A workspace root, so the directory branch's <tag>/<project> rule has
    something to measure against."""
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


# ----------------------------------------------------------------- file branch

def test_any_existing_file_is_offered_the_split_chat(tmp_path, workspace):
    """The point of D235: a file nowhere near the workspace still gets the chat
    with the annotation tools. No repository, no app, no project — a file being
    looked at is enough, because the registry already decided which extensions
    offer the mode and the gate has nothing left to add."""
    f = tmp_path / "elsewhere" / "notes.md"
    f.parent.mkdir()
    f.write_text("# hi")
    assert _gate().main(str(f)) is True


def test_a_path_that_does_not_exist_is_refused(tmp_path, workspace):
    """`isfile`, not `not isdir` — the regression this file exists for.

    A gate cannot tell a missing path from a file, and CT-12 says "cannot tell"
    reads as "refuse". The loose form also silently generalised the directory
    rule from "the app folder itself" to "any name under it", because a
    nonexistent child is not a directory either.
    """
    gate = _gate()
    assert gate.main(str(tmp_path / "nope.md")) is False
    assert gate.main(str(tmp_path / "nope" / "deeper" / "nope.md")) is False


def test_an_empty_path_is_refused(workspace):
    assert _gate().main("") is False


# ------------------------------------------------------------ directory branch

def test_every_directory_is_offered_the_chat(tmp_path, workspace):
    """The directory rule is now "any directory". It used to be exactly
    <workspace>/<tag>/<project> — a narrowing that
    existed only because an ordinary folder's chat was the separate `claude` mode,
    whose pane had no app entry to render. `claude` is deleted, so narrowing here
    would leave an ordinary folder with no chat at all — the capability the delete
    was meant to preserve. D239 has since conceded the pane half of the old
    reasoning (such a folder really does have nothing to frame, and gets no pane)
    without conceding the gate: a folder worth talking to an agent about does not
    become less worth it because there is nothing to render beside the
    conversation."""
    gate = _gate()
    project = workspace / "local" / "demo"
    project.mkdir(parents=True)
    (project / "sub").mkdir()
    hidden = workspace / ".hidden" / "demo"
    hidden.mkdir(parents=True)
    ordinary = tmp_path / "just-a-folder"
    ordinary.mkdir()

    for d in (project, workspace / "local", workspace, project / "sub",
              hidden, ordinary):
        assert gate.main(str(d)) is True, d


def test_a_directory_that_does_not_exist_is_refused(tmp_path):
    """`isdir`, not `not isfile`: "cannot tell" has to read as "refuse" (CT-12),
    or a stat of a path that vanished between listing and gate offers a chat
    about nothing."""
    assert _gate().main(str(tmp_path / "gone")) is False


def test_a_mount_backed_path_is_still_refused(tmp_path, monkeypatch):
    """The ONE thing the gate still answers, and the reason it was not deleted
    outright once the directory branch widened: bytes under the mounts dir come
    from a remote over FUSE, and an agent turned loose there walks and rewrites
    the tree through the mount. Both kinds are refused — a file and a directory
    alike — because the objection is about the transport, not the target."""
    mounts = tmp_path / "home" / "mounts"
    (mounts / "pub").mkdir(parents=True)
    f = mounts / "pub" / "page.html"
    f.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    gate = _gate()
    assert gate.main(str(f)) is False
    assert gate.main(str(mounts / "pub")) is False


def test_the_gate_never_walks_the_directory():
    """It runs for every directory the explorer stats, some of them on remote
    mounts, so listing one would turn a stat into a directory read. Pinned as
    source because the cost is invisible in behaviour."""
    src = open(os.path.join("fused_render", "templates", "claude",
                            "condition.py"), encoding="utf-8").read()
    body = src[src.index("def main("):]
    for banned in ("os.listdir", "os.scandir", "glob", "os.walk", "realpath"):
        assert banned not in body, banned


# ------------------------------------------------------------------- the pane


def test_the_dead_framing_params_are_gone_from_their_consumers_too():
    """`?modechip=false` existed for exactly one caller — the chat template's
    folder pane — and that caller is deleted. A URL param with no producer is a
    branch in the shell that nothing can ever take, so `Preview.tsx` loses the
    read and the guard rather than keeping an untestable tolerance alive.

    `?preview=true|false` has since gone the same way, for the same reason and
    one better: the pane's visibility is no longer a state at all. It became a
    measurement of the container's width, and then not even that — D282 deleted
    the 700px threshold too, so a listing that has a pane simply has one. Nothing
    writes the param, nothing carries it across a folder hop, and nothing may read
    it — a stale one in an old bookmark must not resurrect a toggle that does not
    exist.
    """
    with open(os.path.join("frontend", "src", "apps", "explorer", "Preview.tsx"),
              encoding="utf-8") as f:
        tsx = "\n".join(line for line in f.read().split("\n")
                        if not line.lstrip().startswith("//"))
    assert 'get("modechip")' not in tsx
    assert "modeChipOff" not in tsx
    # The chip itself survives for every other embed — only the opt-out is gone.
    assert "otherEntry" in tsx

    # No producer and no consumer for `preview`, anywhere in the shell: not the
    # pane hook that used to write it, not the router that used to carry it
    # between folders, not the template that used to frame an embed with it.
    for rel in (
        ("frontend", "src", "apps", "explorer", "listing", "pane.ts"),
        ("frontend", "src", "platform", "lib", "router.ts"),
    ):
        with open(os.path.join(*rel), encoding="utf-8") as f:
            code = "\n".join(line for line in f.read().split("\n")
                             if not line.lstrip().startswith("//"))
        assert "preview=false" not in code, f"{rel[-1]} still speaks the dead param"
        assert 'get("preview")' not in code, f"{rel[-1]} still reads the dead param"


def _preview_tsx() -> str:
    with open(os.path.join("frontend", "src", "apps", "explorer", "Preview.tsx"),
              encoding="utf-8") as f:
        return f.read()


# A kind CLAIM, as opposed to a mention of the words. "the JSON file at
# `dom_path`" names a real file on disk and is not a claim about the target;
# "the app" over a `.md` preview is. So the patterns are the demonstrative and
# possessive forms plus the noun-adjunct ones ("app pane", "File preview"), which
# is what every recurrence of this bug has actually looked like.
_KIND_CLAIMS = (
    r"\b(?:the|this|your|whole|visible|running)\s+(?:app|project|folder|file)\b",
    r"\b(?:app|project|folder|file)\s+(?:pane|preview|browser)\b",
    r"\b(?:app|project|folder|file)'s\b",
)

# Every place a string reaches a HUMAN (an attribute that is spoken or shown, an
# element's text, the tab title) or the MODEL (the three block preambles). The
# invariant asserted over them is one line: a chrome sink either DERIVES its noun
# or contains no kind noun. Nothing else is allowed, and a new sink that hardcodes
# one fails here because it references none of the derivation tokens.
_SINK_STARTS = (
    r"\.(?:title|alt|placeholder|textContent)\s*=",
    r"""setAttribute\("(?:aria-label|title|alt)",""",
)
_SINK_FNS = ("function appStateBlock(", "function formatAnnotations(")
# Reading any of these means the noun came from the target's kind, so whatever
# literals sit in that branch are selected BY kind and are correct there.
_DERIVED = ("paneNoun", "targetNoun", "noun")


# -------------------------------------------- the left pane's view PICKER

# The narrow-layout breakpoint, in one place because three tests and the block
# extractor all have to name it. Raised from D236's original 560px (see
# test_the_split_collapses_only_when_two_columns_are_useful), then lowered from
# the 880 round-up to 800 — deliberately a little below the 864 useful-width
# floor, to keep the split alive on more hosts.
NARROW_PX = 800


# --------------------------------------------- the narrow single-view layout


# ------------------------------------------------------- the binding it needs

def test_the_registry_binds_the_split_view_to_files_and_keeps_the_directory_key():
    """Both halves of D235's binding, in one place. The file keys are what makes
    the annotation tools reachable while editing a standalone file; the `/` key
    is what keeps the mode in the app-builder view (App.tsx APP_MODES), where
    dropping it would have silently removed the chat from every app and broken
    app creation."""
    with open(os.path.join("fused_render", "templates", "registry.json"),
              encoding="utf-8") as f:
        registry = json.load(f)

    assert "claude" in registry["/"]
    for key in (".py", ".md", ".html", ".parquet", ".tsx", ".toml", ".ipynb"):
        assert "claude" in registry[key], key
    # It is also the ONLY chat on the `/` key: the second chat mode that used to
    # sit beside it there is deleted, and a second entry labelled "Chat" on the
    # one target kind where the two differed was the whole reason the surviving
    # mode needed a display name of its own.
    assert registry["/"].count("claude") == 1

    # The chat sits after the CONTENT views on every file key that has it: it is
    # a companion to reading the bytes, never the way you read them. The `/` key
    # is excluded deliberately — its order is the directory story (`_listing`
    # first, then the gated peers).
    for key, names in registry.items():
        if key.endswith("/") or not isinstance(names, list):
            continue
        if "claude" in names:
            assert names.index("claude") > 0, key


def test_the_gates_docstring_does_not_justify_itself_with_the_deleted_pane():
    """The gate's directory branch is wide for ONE reason — the plain chat mode is
    deleted (D237), so narrowing would leave an ordinary folder with no chat at
    all. Its docstring used to give a second reason: that the pane "falls back to
    /embed/<dir> — fused-render's own file browser", citing `paneURL` by name.
    D239 deleted that pane, so the stated justification rested on a branch that now
    returns `null`.

    Pinned because a gate is the first thing read when asking "why is this mode
    offered here", and a reason that points at deleted code is worse than no
    reason: it sends the reader to `paneURL` to find the opposite of what they were
    told. Also pinned: the gate does not open by calling this template "the split
    view", which is true of two of its three target shapes and is not what the
    gate decides anyway."""
    src = open(os.path.join("fused_render", "templates", "claude",
                            "condition.py"), encoding="utf-8").read()
    doc = src[:src.index('"""', 3) + 3]
    assert "/embed" in doc, "the history is kept — it is the citation that goes"
    assert "returns `null`" in doc or "It is gone" in doc, \
        "the embed must be named as REMOVED, not cited as live"
    assert "(see paneURL in template.html)" not in doc
    assert not doc.lstrip('"').lstrip().startswith("Gate for the `claude` template"
                                                   " — the split view")
    # The delete, not D235, is what widened the branch.
    assert "D237 deleted the second" in doc


# ------------------------------- committing framedMode only after the frame swaps


_LEFT_STUBS = """
let framedMode = "markdown", entry = null;
const frame = { src: "" };
const FILE = "/w/notes.md";
let paneRemote = false;
let synced = null;
function curLeftEntry() { return entry; }
function syncLeftPicker(e) { synced = e ? e.mode : null; }
document = { getElementById: (id) => (id === "leftframe" ? frame : null) };
"""



"""The `claude` gate and left pane across the two target KINDS (D230).

The split view used to be the app builder's alone: its gate offered it for a
project folder only, and its left pane always resolved that folder's app entry.
D230 bound it to 47 file keys as well, because the annotation / app_state
machinery lives here and nowhere else — chatting about a standalone file with
those tools is the whole point — so the gate now answers for a file too and the
pane renders that file in its OWN default view.

Then the plain chat template was deleted and this became the ONLY chat: the gate's
directory branch widened from "an app folder" to "any directory", and the pane
grew a fallback for the folder that has no app entry to frame. Both are pinned
below, because both replace a rule this file used to assert the opposite of.

Then D234 removed that fallback again — not back to the `throw`, but to NO PANE:
an ordinary folder gets a full-width chat. The embedded file browser reported to
nobody (no `postMessage`, no listener), annotate was hard-disabled over it and
the view picker was inert for it, so it was half the width spent on decoration.
There are now TWO pane shapes and a no-pane case, and the tests below say which
is which.

Three things are worth pinning down, and each of them broke once:

* the FILE branch of the gate must test `isfile`, not `not isdir`. The loose form
  reads every path that does not exist as "a file", which is how a nonexistent
  child of a linked-app folder got a `True` out of a gate.
* the pane must resolve the file's template the way the SHELL does — the first
  non-`conditional` entry from stat — rather than from a per-extension table,
  which drifts from the registry the moment a binding changes and ignores a user
  override entirely (§16).
* a directory with no app entry must not leave an error panel beside a working
  chat, which is what the old `throw` did for every folder that is not an app.

The gate is exec'd standalone here, the way `server._run_condition` execs it —
never imported as part of a package, since a template may not import
fused_render (SPEC PY-15 / D166).
"""
import importlib.util
import json
import os
import re

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
    """The point of D230: a file nowhere near the workspace still gets the chat
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
    <workspace>/<tag>/<project>, plus a registered linked app — a narrowing that
    existed only because an ordinary folder's chat was the separate `claude` mode,
    whose pane had no app entry to render. `claude` is deleted and the pane falls
    back to the folder's own /embed browser, so every one of these is a folder
    worth talking about and none of them is a special case."""
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

def _pane_source() -> str:
    with open(os.path.join("fused_render", "templates", "claude",
                           "template.html"), encoding="utf-8") as f:
        return f.read()


def _pane_code() -> str:
    """The template with its whole-line `//` comments, `/* … */` blocks and HTML
    comments removed.

    Needed by every "X is gone" assertion, because this file's comments RECORD
    what was removed and why — that is the repo's convention — so grepping the
    raw source would make a rejected design, described in prose, read as the
    implementation. Same reasoning as `_media_rules()` below, applied to the
    script rather than the stylesheet. Trailing `// …` on a code line survives;
    nothing here depends on that, and stripping it needs a real tokenizer."""
    src = re.sub(r"/\*.*?\*/", "", _pane_source(), flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return "\n".join(line for line in src.split("\n")
                     if not line.lstrip().startswith("//"))


def test_the_pane_resolves_a_file_through_stat_not_a_per_extension_table():
    """The pane asks stat which view a file gets and takes the first
    non-`conditional` entry — the shell's own rule (Preview.tsx
    `defaultTemplate`). Hardcoding a template per extension would drift from the
    registry on the next rebinding and would ignore a user override (§16)."""
    page = _pane_source()
    assert "/api/fs/stat?path=" in page
    assert "e.conditional" in page
    assert "_file=" in page


def test_the_pane_never_frames_a_chat_mode():
    """`claude` framing itself would nest the split view inside its own
    left pane, recursively.

    The pinned string lost `"claude"` when that template was deleted. The set
    filters stat's entries, and stat can no longer report a mode whose folder
    does not exist — so the name was pruned rather than left in as documentation,
    and this assertion moves with it instead of being relaxed to a substring."""
    page = _pane_source()
    assert 'PANE_SKIP_MODES = new Set(["claude"])' in page


def test_the_pane_renders_a_page_target_as_itself():
    """`_render` is a shell sentinel (PT-12), not a template folder: for an
    `.html` target the file IS the document, so a bare /render on the file is
    the only correct src — routing it through a template would frame the source
    view of a page the user expects to see rendered."""
    page = _pane_source()
    assert 'if (t.mode === "_render") return "/render?path=" + encodeURIComponent(FILE);' in page


def test_a_folder_target_still_resolves_its_app_entry():
    """The builder path must be untouched: an app folder's pane is the app's
    entry html via ./app.py, which is what `HomeHero` lands a newly created app
    on (`?_mode=claude` over the FOLDER)."""
    page = _pane_source()
    assert 'fused.runPython("./app.py", { dir: FILE })' in page
    assert 'if (entry) {' in page, "the app entry is still the first answer"


def test_an_ordinary_folder_gets_no_left_pane_at_all():
    """D234: the ordinary folder's pane is GONE, and the chat is full-width.

    The history in one line. The branch used to `throw` (`no app entry…`), which
    put a permanent error panel beside a working chat; D232 replaced the throw
    with `/explorer/embed/<dir>`, fused-render's own file browser. That framing
    earned nothing: there is no `postMessage` and no message listener in this
    template, so selecting a file in that pane attached nothing, fed nothing to
    the composer and changed no agent context; annotate was hard-disabled over
    it, and the `leftmode` picker was inert for it. A column that reports to
    nobody is not a pane, it is decoration taking half the width away from the
    one thing the folder chat is for. So `paneURL()` answers `null` for that
    kind and the loader takes the no-pane branch.
    """
    page = _pane_source()
    assert "no app entry (index.html" not in page, "the throw must be gone"
    # The embed framing and BOTH of its nesting guards go with it — `modechip`
    # loses its only producer in the codebase, so the param's plumbing is gone
    # from its consumer too (asserted below). Matched as CODE, since the comment
    # beside the branch records what it replaced.
    code = _pane_code()
    assert '"/explorer/embed/"' not in code
    assert "modechip=false" not in code
    assert "preview=false" not in code
    # `null` is the pane's answer for "there is no pane", read at the one place
    # that frames the iframe.
    assert "if (src === null) { enterNoPane(); return; }" in code


def test_the_dead_modechip_param_is_gone_from_its_consumer_too():
    """`?modechip=false` existed for exactly one caller — the chat template's
    folder pane — and that caller is deleted. A URL param with no producer is a
    branch in the shell that nothing can ever take, so `Preview.tsx` loses the
    read and the guard rather than keeping an untestable tolerance alive.

    `preview=false` is NOT removed alongside it: the listing writes that one
    itself when the user closes the pane (`listing/pane.ts`), so it still has a
    producer and still means something.
    """
    with open(os.path.join("frontend", "src", "apps", "explorer", "Preview.tsx"),
              encoding="utf-8") as f:
        tsx = "\n".join(line for line in f.read().split("\n")
                        if not line.lstrip().startswith("//"))
    assert 'get("modechip")' not in tsx
    assert "modeChipOff" not in tsx
    # The chip itself survives for every other embed — only the opt-out is gone.
    assert "otherEntry" in tsx
    with open(os.path.join("frontend", "src", "apps", "explorer", "listing",
                           "pane.ts"), encoding="utf-8") as f:
        assert "preview=false" in f.read(), "preview=false keeps its own producer"


def test_the_no_pane_state_removes_the_column_the_divider_and_the_strip():
    """The no-pane case is a DESIGNED state, not a missing element — and the
    difference is load-bearing.

    Simply dropping `#leftframe` from the markup would have thrown a TypeError
    at `annFrame.addEventListener("load", …)`, which is top-level script: every
    declaration after it — the agent poll loop, `setViewShot`, the composer
    wiring — would never have been created, and the boot catch would then have
    thrown INSIDE the catch (it calls `.remove()` on the same missing element),
    so not even the error panel would have appeared. So the markup ships the
    pane exactly as before and the no-pane target REMOVES it, from the async
    loader — which, because `paneURL()` awaits a fetch, runs after the whole
    script has finished: every declaration is initialised before anything is
    taken away.

    Three things go, and each one is chrome that acts on the pane: `#left` (the
    frame, the pins, the highlight, the popover), `#divider` (no ratio to drag)
    and `#anntools` (the annotate switch, the `leftmode` picker and the view
    toggle all live in that strip, which is a child of `#chat` and would
    otherwise stay behind as an empty bordered row).
    """
    code = _pane_code()
    i = code.index("function enterNoPane()")
    body = code[i:code.index("\n}", i)]
    assert ('for (const id of ["left", "divider", "anntools", "viewshot", '
            '"hviewshot"]) {') in body
    assert "if (el) el.remove();" in body


def test_the_no_pane_state_undoes_what_boot_did_from_a_stale_param():
    """The ordering inside `enterNoPane`, which is where two real bugs lived.

    Boot runs before `paneURL` has answered, so it acts on params in ignorance of
    the target's kind — and two of those actions are visible:

    * `renderAnn()` paints a chip per composer from the `annotations` param. A
      `noPane` early-return added to renderAnn would have FROZEN those chips on
      screen: a note the send can no longer carry, still shown as pending. So the
      list is emptied and repainted while `noPane` is still false, and only then
      is the flag set — the assertion below is the order, not the statements.
    * `applyNarrowView()` stamps `view-preview` on the body when `paneview=preview`
      is on the URL. Inside the 880px block that class collapses `#chat` to its
      `#anntools` strip — and this state removes that strip and has no pane to
      show instead, so a narrow host rendered a BLANK PAGE. The class is removed,
      not left inert.
    """
    code = _pane_code()
    i = code.index("function enterNoPane()")
    body = code[i:code.index("\n}", i)]
    assert body.index("annotations = [];") < body.index("renderAnn();")
    assert body.index("renderAnn();") < body.index("noPane = true;")
    assert ('document.body.classList.remove("view-preview", "view-chat");'
            in body)
    # Removal happens from the async loader, i.e. after the script — pinned as
    # the call site, since that ordering is the whole reason this is safe.
    loader = code[code.index("const src = await paneURL();"):]
    assert loader.index("enterNoPane()") < loader.index("} catch (err) {")


def test_nothing_drives_the_pane_machinery_once_the_pane_is_gone():
    """One flag, checked in the four functions that would otherwise write to
    detached nodes or to a param describing a layout that no longer exists.

    `paneEmbedded` — the "this pane is the embedded listing, refuse to arm"
    flag — is deleted rather than repurposed: there is no embedded listing any
    more, and the condition it guarded (an annotate button over a pane whose
    clicks can never resolve) is now impossible by construction, because the
    button is not in the document.
    """
    code = _pane_code()
    assert "let noPane = false;" in code
    assert "paneEmbedded" not in _pane_source(), "the flag is deleted, not renamed"
    # annSetMode stays the ONE door in and out of the mode, and it refuses
    # without writing `annmode`: a stale param on a folder URL is ignored, not
    # rewritten.
    ann = code[code.index("function annSetMode(on) {"):]
    ann = ann[:ann.index("\nannBtn.addEventListener")]
    assert "if (noPane) { annOn = false; return; }" in ann
    assert ann.index("if (noPane)") < ann.index('fused.params.set("annmode"')
    for fn in ("function applySplit() {", "function applyNarrowView() {",
               "function renderAnn() {"):
        body = code[code.index(fn):code.index(fn) + 300]
        assert "if (noPane) return;" in body, fn


def test_a_stale_split_param_on_a_folder_url_is_ignored_not_an_error():
    """`split`, `paneview`, `leftmode` and `annmode` left on a folder URL by an
    old bookmark describe a layout that folder no longer has. They are ignored
    SILENTLY — not stripped, not an error — the same forgiving posture PT-9
    takes for an unknown `_mode`: the folder chat simply opens full-screen.

    Pinned as "nothing deletes them", because the tempting fix is to tidy the
    URL, and tidying it means a bookmark that no longer round-trips when the
    same folder later grows an `index.html` and gets its pane back.
    """
    page = _pane_source()
    no_pane = page[page.index("function enterNoPane()"):]
    no_pane = no_pane[:no_pane.index("\n}")]
    for param in ("split", "paneview", "leftmode", "annmode", "annotations"):
        assert 'params.set("%s"' % param not in no_pane, param
        assert 'params.delete("%s"' % param not in no_pane, param


def test_the_no_pane_state_cannot_ship_a_stale_entry_or_a_pane_screenshot():
    """Two things the app-state channel must not do for a target with no pane.

    `appEntry` is write-once and never cleared, and `entry` is the only field in
    the payload that distinguishes the user's real app from our own UI — so the
    no-pane path must never set it. And the pane-shot pill captures "the WHOLE
    visible app pane", captioned unconditionally; with no pane there is nothing
    to photograph, so both copies of the pill are removed and the block is
    unreachable rather than merely unused.
    """
    page = _pane_source()
    folder = page[page.index("if (st.is_dir) {"):page.index("setTargetNoun(\"file\")")]
    assert "appEntry" not in folder.split('setTargetNoun("folder")')[1]
    # And the unreadable sentence does not become the explanation for it: the
    # prose enumerates causes, and "this target has no pane" is none of them.
    assert "APP_STATE_UNREADABLE" in page
    unreadable = page[page.index("const APP_STATE_UNREADABLE"):]
    unreadable = unreadable[:unreadable.index(";\n")]
    assert "no app entry" not in unreadable


def test_the_composer_placeholder_names_the_targets_kind():
    """The placeholder used to be the hardcoded "Ask Claude about this project…",
    which was true for exactly one of the three targets this mode is bound to: it
    called every ordinary folder and all 47 file keys a "project".

    The noun is derived from the kind instead, from the SAME resolution the pane
    already does — stat's `is_dir`, then whether `./app.py` (shared/app_entry
    `entry_html`) resolves an entry — so the three nouns line up one-for-one with
    `_split_system_prompt`'s three shapes. No new round trip and no new signal:
    the branches were already there, only the placeholder was not set from them.
    Saying "project" over ~/Downloads is the same lie in the box as in the prompt.

    The markup keeps the kind-FREE wording rather than a guess, because the kind
    is not known until stat answers and a placeholder that flips from a wrong noun
    to a right one is worse than one that gains a noun.
    """
    page = _pane_source()
    assert 'placeholder="Ask Claude about this project…"' not in page
    assert 'id="homebox" rows="2" placeholder="Ask Claude…"' in page
    # One builder, three call sites — one per pane shape.
    assert 'homebox.placeholder = "Ask Claude about this " + noun + "…";' in page
    assert page.count("setTargetNoun(") == 3          # one call per pane shape
    for noun in ("project", "folder", "file"):
        assert 'setTargetNoun("%s")' % noun in page
    # The app-folder noun hangs off the entry resolution, the prompt's predicate.
    app_branch = page[page.index("const { entry } = await fused.runPython"):]
    app_branch = app_branch[:app_branch.index('setTargetNoun("file")')]
    assert app_branch.index('setTargetNoun("project")') < app_branch.index(
        'setTargetNoun("folder")')


def test_no_piece_of_chrome_hardcodes_the_targets_kind():
    """The general form of the bug above, so the next instance fails here instead
    of reaching a reviewer.

    The placeholder was made kind-derived and the FOOTNOTE beneath it was missed:
    it still read "Claude can read and edit files in this project", so a chat
    about `notes.md` described the wrong kind of thing the moment it left the home
    view. Both now go through `setTargetNoun`, the single writer — a second,
    independent kind lookup is a second thing to forget.

    So: NO kind noun appears anywhere in the rendered markup. Asserted over the
    <body> (the stylesheet's comments talk about panes and projects at length, and
    that prose is not chrome), and only over what a user can read — element text
    and the three attributes that get spoken or shown.
    """
    page = _pane_source()
    body = page[page.index("<body>"):page.index("<script>")]
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    # What a user can actually READ or HEAR: element text plus the three
    # attributes that get shown or spoken. Ids and class names are excluded on
    # purpose — `tb-file` and `home-file` are selectors, not sentences.
    spoken = re.findall(r'(?:title|aria-label|placeholder)="([^"]*)"', body)
    visible = " ".join(spoken) + " " + re.sub(r"<[^>]*>", " ", body)
    # SINGULAR and word-bounded: the plural is generic ("Claude can read and edit
    # files here" is true of every target and names no kind), the singular is a
    # claim about THIS target and belongs to setTargetNoun.
    for noun in ("project", "folder", "file"):
        assert not re.search(r"\b%s\b" % noun, visible, re.I), noun
    # The kind-free wording the markup ships instead, for both pieces.
    assert 'placeholder="Ask Claude…"' in page
    assert ">Claude can read and edit files here." in page
    # One writer, and it writes BOTH.
    setter = page[page.index("const setTargetNoun = (noun) => {"):]
    setter = setter[:setter.index("\n};")]
    assert "homebox.placeholder" in setter
    assert 'getElementById("footnote").textContent' in setter
    # "files in this file" is not a sentence — the file case is its own shape.
    assert 'noun === "file"' in setter


# -------------------------------------------- the left pane's view PICKER

# The narrow-layout breakpoint, in one place because three tests and the block
# extractor all have to name it. Raised from D231's original 560px: see
# test_the_split_collapses_only_when_two_columns_are_useful.
NARROW_PX = 880


def _media_block() -> str:
    """The body of the `@media (max-width: {NARROW_PX}px)` rule, brace-matched.

    Asserting against the block rather than the whole file is the point: a rule
    that collapses the split layout is only correct INSIDE the narrow query, and
    a test that greps the file would pass just as happily if the same
    declaration leaked into the wide layout and broke the split for everyone.
    """
    page = _pane_source()
    head = "@media (max-width: %dpx) {" % NARROW_PX
    i = page.index(head) + len(head)
    depth = 1
    j = i
    while depth:
        if page[j] == "{":
            depth += 1
        elif page[j] == "}":
            depth -= 1
        j += 1
    return page[i:j - 1]


def _media_rules() -> str:
    """The same block with its `/* … */` comments stripped — for the assertions
    that are about what the layout DOES. The comments in here are long and
    explanatory (they carry the reasoning for the collapse), so they mention the
    very tokens some of these tests forbid; matching against them would make a
    rejected alternative, described in prose, read as the implementation."""
    return re.sub(r"/\*.*?\*/", "", _media_block(), flags=re.S)


def _style_rules() -> str:
    """The whole <style> element, comments stripped — same reason as
    _media_rules, for the assertions that have to hold across the stylesheet
    rather than inside the narrow block."""
    page = _pane_source()
    css = page[page.index("<style>"):page.index("</style>")]
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_the_left_view_is_switchable_through_a_pane_local_leftmode_param():
    """The picker's whole round trip, which is the `split` param's pattern: the
    control WRITES `leftmode` and nothing else, and a single applier reads it back
    and swaps the iframe. One path serves a click, a reload with the param already
    set, and any later change — and because the param is pane-local (fused.params,
    never the shell URL) a `.md` file's chat can sit on Code without the choice
    escaping into the address bar of whatever pane hosts it.

    The regression this pins is the original bug: `paneURL()` took the first
    non-conditional entry with NO way to change it, so a markdown file's chat was
    stuck on the markdown view even when the user wanted the source.
    """
    page = _pane_source()
    assert 'fused.params.get("leftmode")' in page
    assert 'fused.params.set("leftmode", leftSel.value)' in page
    # Applied from the param change, beside applySplit — not from the change
    # handler directly, which would be a second code path for the same swap.
    assert "applyLeftMode()" in page
    assert "applySplit(); applyLeftMode();" in page
    # Layout bookkeeping of THIS chat, so it must not be reported to the model as
    # a param the framed app is running with (see CHAT_PARAMS).
    assert '"annmode", "leftmode", "paneview",' in page


def test_an_unknown_leftmode_falls_back_to_the_default_view_silently():
    """SPEC PT-9's forgiving rule, applied to the pane's own copy of it: a
    `leftmode` naming a view this target does not offer — a renamed template, or
    a param carried across to a file of another type — resolves to the default
    view, silently. Throwing (or rendering an error) would turn a stale param
    into a dead pane, and the default is always right."""
    page = _pane_source()
    assert "return paneEntries.find((e) => e.mode === want) || paneEntries[0] || null;" in page


def test_the_picker_is_sourced_from_the_stat_call_the_pane_already_makes():
    """Two properties in one, because they fail together.

    The options come from stat's `templates` — the same resolved list the pane's
    default comes from — so the picker cannot drift from the registry when a
    binding changes and cannot miss a user override (§16). A per-extension table
    of "modes for `.md`" would do both. And there is exactly ONE stat in the
    file: filling the <select> from a second call would pay the round trip twice
    on open and could disagree with what the pane is actually framing, if a
    binding changed in between.

    The gate endpoint stays unused too: `conditional` entries are excluded rather
    than resolved, so the picker never offers a view whose gate might say no
    (CT-12 — unresolved reads as "not offered").
    """
    page = _pane_source()
    assert page.count("/api/fs/stat?path=") == 1
    assert "paneEntries = paneOfferable(st.templates);" in page
    assert "!e.conditional && !PANE_SKIP_MODES.has(e.mode)" in page
    # Named in a comment (that is where the reasoning lives), never fetched.
    assert 'fetch("/api/fs/conditions' not in page
    # Re-derived from the entries already held, never re-stat'ed.
    assert "frame.src = paneSrcFor(t);" in page


def test_a_single_view_target_shows_no_picker():
    """A directory target frames its app entry — resolved by app.py, not a stat
    entry at all — and a file with one offerable view has nothing to choose
    between. Either way a one-item control is chrome that cannot do anything, so
    the <select> ships `hidden` and only unhides when there is a real choice."""
    page = _pane_source()
    assert '<select id="leftmode" hidden' in page
    assert "if (paneEntries.length < 2) return;" in page


def test_switching_the_left_view_keeps_the_annotation_list():
    """The decision recorded in applyLeftMode, pinned so it is not quietly
    reversed: pins are anchored to elements of the LEFT document, so a new left
    view invalidates the anchors — but the notes are the user's WORDS, they live
    in the `annotations` param, and they are still sendable as text. Dropping
    them on a click of a view picker would be unannounced data loss. So the list
    survives and only the positions are recomputed, by the frame's own `load`
    handler (an unresolvable anchor already means "no pin, the chip stays")."""
    page = _pane_source()
    i = page.index("function applyLeftMode()")
    body = page[i:i + 1800]
    assert "ANNOTATIONS SURVIVE THE SWAP" in body
    # No annotation state is cleared here — the param is the store, and the load
    # handler is what repositions.
    assert "annotations = []" not in body


# --------------------------------------------- the narrow single-view layout

def test_the_split_collapses_to_one_view_below_the_layouts_width_floor():
    """#left's 200px + the 4px divider + #chat's 340px is a hard ~544px floor,
    and since D230 this view renders in hosts that go below it routinely — the
    explorer's listing preview pane floors at 220px and a Panel pane drags
    freely. Below the query the two halves squeezed past their minimums and then
    overflowed. (The breakpoint itself is above that floor and derived from the
    columns' minimum USEFUL widths — see
    test_the_split_collapses_only_when_two_columns_are_useful.)

    Responsive in the TEMPLATE, matching log_studio (780px), map (650),
    duckdb/sqlite (560) and bundle (640) — not the shell filtering split-layout
    modes out of a narrow pane. A pane's width is dynamic, which makes that
    filter wrong in both directions: the listing pane defaults to HALF its
    container, so on a wide window the split fits and the mode should be offered;
    and once offered, a divider drag would make the mode appear and disappear
    mid-drag and could yank the ACTIVE mode away. It would also need per-template
    width knowledge that no registry field carries and that every user template
    would lack.
    """
    block = _media_block()
    assert "flex-direction: column" in block
    # No shell-side width contract was invented to solve this.
    with open(os.path.join("fused_render", "templates", "registry.json"),
              encoding="utf-8") as f:
        assert "minWidth" not in f.read()


def test_the_split_collapses_only_when_two_columns_are_useful():
    """The breakpoint is 880px, not D231's original 560px, and the raise is the
    regression this pins.

    560 came from the layout's HARD floor (200 + 4 + 340 ≈ 544, rounded up): the
    width below which the columns overflow. But the explorer's listing preview
    pane defaults to HALF its split container — about 700px on a 1700px window —
    so at 560 the split engaged in a host that could hold it without overflowing
    and could not hold it usefully: two cramped columns where one readable view
    was wanted. The new figure comes from the columns' minimum USEFUL widths,
    #left 420 + divider 4 + #chat 440 = 864 → 880, and the arithmetic is written
    down beside the query so the next reader can check it instead of guessing —
    which is also why this asserts the sum, not just the number.

    The CSS query and the JS matchMedia string must carry the SAME number: they
    are one breakpoint with two readers, and a disagreement is a half-collapsed
    layout that only shows up in a window of a few pixels.
    """
    page = _pane_source()
    assert "@media (max-width: %dpx) {" % NARROW_PX in page
    assert 'window.matchMedia("(max-width: %dpx)")' % NARROW_PX in page
    # The old figure must not survive anywhere as a live breakpoint.
    assert "@media (max-width: 560px)" not in page
    assert 'matchMedia("(max-width: 560px)")' not in page
    # The arithmetic, in the comment beside the query.
    style = page[page.index("<style>"):page.index("</style>")]
    narrow_doc = style[:style.index("@media (max-width: %dpx) {" % NARROW_PX)]
    for term in ("420px", "440px", "864px", "880px"):
        assert term in narrow_doc, term


def test_the_divider_is_hidden_in_the_narrow_layout():
    """There is no ratio to drag when only one view is on screen, and a 4px
    col-resize strip that reorders nothing is a control that lies."""
    assert "#divider { display: none; }" in _media_block()


def test_the_narrow_layout_neutralises_the_inline_split_width_from_applysplit():
    """applySplit() writes `width: <split>%` INLINE on #left, and an inline
    declaration outranks any stylesheet rule — so the collapse would simply lose
    to it unless something neutralises it deliberately. The mechanism is JS, not
    `!important`: applySplit CLEARS the inline width while the narrow query
    matches and rewrites it from the param on the way back out. `!important` was
    the other candidate and reads cheaper, but this file holds a hard
    no-`!important` rule (D146, asserted in test_claude_shots) precisely so
    cascade problems are stated in the selectors, and one exception makes that
    rule unenforceable.

    What makes it correct in BOTH directions with no reload is that the `split`
    PARAM is never touched by the collapse — the inline style is the only thing
    cleared — plus a matchMedia listener that re-runs applySplit when the
    breakpoint is crossed without a resize event of the pane's own.
    """
    page = _pane_source()
    rules = _media_rules()
    assert "!important" not in rules
    # The media block deliberately declares NO width for #left; the floor does
    # need clearing, since 200px is a promise about the wide layout only.
    assert "#left { min-width: 0; }" in rules
    assert ('const NARROW_MQ = window.matchMedia("(max-width: %dpx)");' % NARROW_PX
            ) in page
    assert 'if (NARROW_MQ.matches) { leftEl.style.width = ""; return; }' in page
    assert ('NARROW_MQ.addEventListener("change", () => { applySplit(); '
            'applyNarrowView(); });') in page


def test_the_narrow_layout_keeps_the_view_toggle_reachable_from_both_views():
    """The Chat ⇄ Preview toggle lives in #anntools, which is a child of #chat —
    so hiding the chat column in Preview mode would take the toggle with it and
    strand the user in the preview with no way back. Preview mode collapses #chat
    to that one strip instead, which is also where the Annotate switch stays
    reachable while the preview owns the screen."""
    block = _media_block()
    assert "body.view-preview #chat > *:not(#anntools) { display: none; }" in block
    assert "#viewbtn { display: flex" in block
    # Default Chat: an unset param is the conversation, the reason the mode
    # exists, with the preview one click away.
    assert 'fused.params.get("paneview") === "preview"' in _pane_source()


def test_no_control_for_the_hidden_half_is_reachable_in_the_chat_only_view():
    """The rule for the narrow layout: a control that acts on the OTHER half is
    ABSENT while that half is hidden — not disabled, absent.

    A control acting on a hidden half is a dead control, and a disabled one is
    worse than absent: it still occupies the row and still advertises a feature
    this view cannot perform. Both of these act on the left preview and nothing
    else — the annotate toggle (nothing to point at, no frame to place a pin in)
    and the left-view picker (nothing to choose a view FOR) — so the chat-only
    view drops them, and Preview, where they both work, keeps them.

    `.viewshot` — the composer's "attach a screenshot of the app pane" pill — is
    in the list too, and it is the one that does NOT follow from "it cannot work
    here": the hidden column is parked with a real viewport, so shotPane
    rasterises the app correctly from the chat view. It is hidden under the
    stronger reading of the rule, the one this test is named for — a view showing
    no preview offers no features OF the preview — because attaching a picture of
    something the user cannot see is an affordance that misleads even while it
    works. Preview view is one click away and is where that decision can be made
    with the app on screen.
    """
    block = _media_block()
    assert ("body.view-chat #annbtn,\n"
            "    body.view-chat .viewshot,\n"
            "    body.view-chat #leftmode { display: none; }") in block
    # Absent, not disabled: nothing here reaches for `disabled` or aria-disabled
    # as a substitute.
    assert "disabled" not in _media_rules()
    # And Preview keeps them: the only view rules that hide anything in Preview
    # are the ones collapsing #chat to its strip.
    assert "body.view-preview #annbtn" not in block
    assert "body.view-preview #leftmode" not in block


def test_no_armed_preview_control_survives_leaving_the_preview_view():
    """The state reset the hiding rule implies — hiding a control is only half the
    job if it can be left ON behind the hiding.

    An armed annotate mode with no visible frame is worse than a dead control:
    the frame's capture-phase click handler goes on swallowing clicks in a
    document the user cannot see, and the mode sits ON behind a toggle its own
    view does not show, so nothing can tell them or let them undo it. Flipping
    away from Preview therefore disarms — through annSetMode, the one way in and
    out, which is what keeps the label, aria-pressed, the `annmode` param and the
    pins from disagreeing.

    The pane-shot pill is reset on the same flip: armed behind a hidden control it
    would put a picture of the app on the next message with nothing on screen
    saying so. It has no param to leak (viewShotWanted is per-message), so this
    one is purely about the send not carrying something invisible.

    The `narrowShown === true` guard is also what makes the reset safe to WRITE
    where it is: `viewShotWanted` is a `let` declared further down the script, so
    reading it during the boot call would be a temporal-dead-zone error — and boot
    is never a flip.

    Narrowly scoped on purpose, and each guard has its own failure: only on a real
    flip (or it would fight a user arming the mode in Preview), never at boot (or
    the first narrow open would persist annmode=0, and armed is the DEFAULT and a
    default the wide layout shares), and only below the breakpoint (where both
    halves are on screen, armed is simply correct).
    """
    page = _pane_source()
    assert "if (NARROW_MQ.matches && narrowShown === true && !preview) {" in page
    assert "if (annOn) annSetMode(false);" in page
    assert "setViewShot(false);" in page
    assert "let narrowShown = null;" in page


def test_nothing_from_the_pin_overlay_can_float_over_the_chat_only_view():
    """#annpins, #annhl and #annpop are positioned OVER the left frame and #annpop
    carries a z-index, so they are the parts of the annotation UI that could
    plausibly paint on top of a chat that has the screen to itself. They are all
    children of #left, so parking the column covers them: `visibility: hidden`
    inherits and `pointer-events: none` stops the parked box intercepting a click
    aimed at the chat. Pinned because a single `visibility: visible` under #left
    would undo it silently."""
    block = _media_block()
    parked = block[block.index("body.view-chat #left {"):]
    parked = parked[:parked.index("}")]
    assert "visibility: hidden;" in parked
    assert "pointer-events: none;" in parked
    # Nothing anywhere in the stylesheet re-shows a descendant.
    assert "visibility: visible" not in _style_rules()


def test_the_divider_is_not_a_drag_target_while_narrow():
    """"Hidden" has to mean gone, not merely invisible: a `visibility: hidden` or
    a transparent divider would still be a 4px col-resize strip under the cursor,
    and dragging it would write a `split` param that nothing in the narrow layout
    can honour. `display: none` removes the box, so the mousedown handler can
    never fire — which is why the handler itself needs no narrow guard."""
    rules = _media_rules()
    assert "#divider { display: none; }" in rules
    assert "visibility: hidden" not in rules.split("body.view-chat #left {")[0]


def test_the_view_toggle_names_the_annotate_surface_it_navigates_to():
    """The toggle read "Preview" outbound and "Chat" on the return, which named
    the destination but not what the destination is FOR: the preview column is
    where the annotation tools live, and that is the only reason a person leaves
    the conversation for it. So it reads "Annotate preview" and "Back to chat".

    It stays NAVIGATION, not a mode: `#annbtn` is still what arms annotate once
    you are there, and the two controls are deliberately not merged — one moves
    between views, the other changes what a click in the frame does.

    The aria-label is the SAME string as the visible label rather than a second
    wording, so the two cannot drift apart.
    """
    page = _pane_source()
    assert 'const viewLabel = preview ? "Back to chat" : "Annotate preview";' in page
    assert "viewBtn.textContent = viewLabel;" in page
    assert 'viewBtn.setAttribute("aria-label", viewLabel);' in page
    # The markup ships the outbound label, since Chat is the default view.
    assert 'aria-label="Annotate preview"' in page
    assert ">Annotate preview</button>" in page
    # The old wording must not survive as a second answer.
    assert 'viewBtn.textContent = preview ? "Chat" : "Preview";' not in page


def test_the_longer_toggle_label_cannot_squeeze_the_strip_into_an_overflow():
    """#anntools is a fixed 26px row that has to survive a 220px host (the
    listing preview pane's floor, FS-12), and the relabelled toggle is ~50px
    wider than "Preview" was. In Preview view the row carries the annotate
    switch AND the toggle, which is the tight case.

    The toggle never shrinks (its label is the navigation, and half of "Back to
    chat" is not a way back); the annotate switch is the thing that gives, by
    ellipsis on its own label. `min-width: 0` on both the row and the switch is
    what makes that possible at all — a flex item's default `min-width: auto`
    refuses to go below its content.
    """
    rules = _style_rules()
    for decl in ("#anntools", "#annbtn", "#viewbtn"):
        assert decl in rules
    assert "#viewbtn" in rules and "flex-shrink: 0" in rules
    assert "text-overflow: ellipsis" in rules
    # Still no cascade force flag anywhere (D146) — asserted whole-file in
    # test_claude_shots.py; repeated here because this is the change that
    # touched the button's own box.
    assert "!important" not in rules


def test_the_view_toggle_is_the_one_control_reachable_from_both_views():
    """It is the navigation between the two views, not a feature of either, so
    hiding it in either one would strand the user there. It lives in #anntools
    because that strip is the only row both views keep — which is also why
    Preview collapses #chat to the strip instead of hiding the column."""
    block = _media_block()
    assert "#viewbtn { display: flex" in block
    assert "body.view-chat #viewbtn { display: none" not in block
    assert "body.view-preview #viewbtn { display: none" not in block
    # #anntools is what carries it through Preview mode.
    assert "body.view-preview #chat > *:not(#anntools) { display: none; }" in block


def test_the_view_toggle_exists_only_in_the_collapsed_layout():
    """The other half of the rule the toggle sits under: a layout showing BOTH
    views offers no control whose only job is to pick one of them. So the wide
    stylesheet declares `#viewbtn { display: none }` and the media block is the
    ONLY thing that reveals it — asserted from both sides, because a second
    reveal anywhere outside the block, or a JS `viewBtn.style.display`, would put
    the toggle on screen next to the split with nothing to toggle.

    JS is checked explicitly: the layout is CSS-owned, and the one thing
    applyNarrowView is allowed to write on the button is its LABEL (which flips
    with the view) and its aria-label. The moment it writes `style` the CSS stops
    being the single answer to "is the toggle offered", which is how a control
    ends up visible in a state its stylesheet forbids.
    """
    page = _pane_source()
    rules = _style_rules()
    # Declared off in the wide layout...
    wide = rules[:rules.index("@media (max-width: %dpx)" % NARROW_PX)]
    assert "#viewbtn {\n    display: none;" in wide
    # ...and revealed in exactly one place: inside the collapsed block.
    assert rules.count("#viewbtn { display: flex") == 1
    assert "#viewbtn { display: flex" in _media_rules()
    # No JS reveal, at any width.
    assert "viewBtn.style" not in page
    assert 'getElementById("viewbtn").style' not in page


def test_the_hidden_view_is_parked_not_display_noned():
    """The narrow layout's likeliest trap, pinned. An iframe with no layout box
    gives its content document a 0x0 viewport, and two features measure through
    that box: shotPane rasterises `root.clientWidth` (a 1x1 PNG of the app the
    moment a pane screenshot is attached from the chat) and every pin resolves an
    element rect inside the frame (annotate mode would find nothing to point at,
    and renderAnn's `y > host.clientHeight` test would drop every pin). So the
    inactive view is taken out of flow and hidden with `visibility`, which keeps
    a real viewport, and the pins are recomputed when a view becomes visible
    because the two views give #leftview two different boxes."""
    block = _media_block()
    assert "body.view-chat #left {" in block
    assert "visibility: hidden;" in block
    assert "body.view-chat #left { display: none" not in block
    # Recomputed on the switch — and again next frame, once the framed document
    # has reflowed to the iframe's new box.
    page = _pane_source()
    i = page.index("function applyNarrowView()")
    body = page[i:page.index("\nviewBtn.addEventListener", i)]
    assert "renderAnn();" in body
    assert "requestAnimationFrame(renderAnn);" in body


# ------------------------------------------------------- the binding it needs

def test_the_registry_binds_the_split_view_to_files_and_keeps_the_directory_key():
    """Both halves of D230's binding, in one place. The file keys are what makes
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

    # Chat then history, adjacent, on every FILE key that has them. The `/` key
    # is excluded deliberately: its order is the directory story (`_listing`,
    # then the app modes, then the chat and the two history views).
    for key, names in registry.items():
        if key.endswith("/") or not isinstance(names, list):
            continue
        if "claude" in names and "versions" in names:
            assert names.index("claude") + 1 == names.index("versions"), key

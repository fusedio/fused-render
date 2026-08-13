"""What the `git` view is FOR, and where it is offered.

Two things are pinned here, and they are the same view seen from two sides.

**`git` is commit management, and that INCLUDES the commits.** Staging,
discarding, stashing, committing, branches, push/pull — the things you do to a
working tree — plus the log of what those acts produced, scoped to the same path
the working-tree lists are scoped to. Overlapping with `history` is not the same
as duplicating it: `history` is a repo-wide timeline you go to in order to read
the past, this is the "what just happened under this path" that belongs beside
the tree you are about to change. The section is fed by the overview read's own
`commits`/`has_more`/`capped` fields, so it costs no extra round trip, and
selecting a row fills the SAME diff pane a working-tree row fills.

**`git` is FOLDER-ONLY.** Staging, discarding, stashing, committing, branches,
push/pull are all repository-level acts — you do not stash a file, you stash a
tree — and the working tree a file sits in is its FOLDER's. So `git` is offered
on the universal `/` directory key and on no file extension at all, and its gate
refuses anything that is not a directory. Per-file history is a different
question with an answer that already ships: `history`, unchanged, still on every
file key it had.

`git` did once ride along on file keys, and the reason is gone. The explorer used
to give a FOLDER no mode switcher of its own — the only mode surface a browsing
user had was the preview pane's, which acted on the SELECTED ROW, always a file —
so a mode bound to `/` alone was unreachable without typing `?_mode=git`, and
riding the file keys was the workaround. The preview pane now selects and
previews FOLDER rows too (the folder peek), so a folder's mode switcher is
reachable and the workaround is retired.
"""
import importlib.util
import json
import os
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "fused_render", "templates")


def _load(name):
    path = os.path.join(_TEMPLATES, name, "condition.py")
    spec = importlib.util.spec_from_file_location(f"_cond_{name}", path)
    # Asserted rather than assumed: `spec_from_file_location` returns None for a
    # path with no loader, and `module_from_spec(None)` fails several lines later
    # with an error about NoneType instead of about the file that is missing.
    assert spec is not None and spec.loader is not None, f"no condition.py for {name}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def registry():
    with open(os.path.join(_TEMPLATES, "registry.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def source():
    with open(os.path.join(_TEMPLATES, "git", "template.html"),
              encoding="utf-8") as f:
        return f.read()


@pytest.fixture()
def work_tree(tmp_path, monkeypatch):
    """A real repository with a nested folder and a tracked file in it."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@e",
         "commit", "-qm", "init"], check=True, env=env)
    # A workspace elsewhere, so the app-dir rules are not accidentally in play.
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.delenv("FUSED_RENDER_LINKED_APPS", raising=False)
    return root


# ------------------------------------------------------------------ bindings


def test_git_is_offered_on_the_directory_key_alone(registry):
    # The working tree is a property of the FOLDER, so the folder is where the
    # mode lives: the universal "/" directory key, and nothing else. It used to
    # ride along on every key `history` was on, because a folder had no mode
    # surface to reach it from; the preview pane peeks folders now, so it does
    # not need the ride.
    offered = [key for key, value in registry.items()
               if isinstance(value, list) and "git" in value]
    assert offered == ["/"]


def test_no_file_extension_offers_git(registry):
    # The same rule from the other end, stated over the file keys, so a new
    # extension cannot quietly reintroduce a per-file working-tree view.
    file_keys = [key for key, value in registry.items()
                 if isinstance(value, list) and key != "/" and "git" in value]
    assert file_keys == []


def test_history_is_untouched_on_every_file_key(registry):
    # De-linking is a change to `git` ONLY. Per-file history is `history`'s job
    # and it keeps every key it had — which, before the split, was exactly the
    # set `git` rode along on. Pinned literally: a silent drop here would look
    # like "the split took history with it".
    assert sorted(key for key, value in registry.items()
                  if isinstance(value, list) and "history" in value
                  and key != "/") == [
        ".cfg", ".cjs", ".conf", ".csh", ".css", ".csv", ".cts", ".fish",
        ".geojson", ".hcl", ".htm", ".html", ".ini", ".ipynb", ".jpeg", ".jpg",
        ".js", ".json", ".jsonl", ".jsx", ".latex", ".log", ".ltx", ".markdown",
        ".md", ".mjs", ".mts", ".ndjson", ".parquet", ".plist", ".png", ".ps1",
        ".py", ".sh", ".svg", ".tex", ".tf", ".toml", ".ts", ".tsv", ".tsx",
        ".txt", ".vim", ".yaml", ".yml", ".zsh", ".zsh-theme",
    ]
    # And on the folder key too, where it sits beside `git`.
    assert "history" in registry["/"]


def test_git_is_still_never_a_default(registry):
    # A gated mode cannot be the default anyway (PT-8); the order is deliberate
    # too — the content view leads every list.
    leads = [key for key, value in registry.items()
             if isinstance(value, list) and value and value[0] == "git"]
    assert leads == []


# ------------------------------------------------------ folder-only vs. a file


def test_a_file_in_a_work_tree_is_git_s_no_and_history_yes(work_tree):
    # The whole change in one line. The file is inside a real work tree, so the
    # old gate said True; the working tree it is inside belongs to its FOLDER,
    # so the folder-only gate says False. The question a file DOES have an
    # answer for — what happened to this file — is `history`'s, and `history`
    # still says yes.
    target = str(work_tree / "pkg" / "mod.py")
    assert _load("git").main(target) is False
    assert _load("history").main(target) is True


def test_git_refuses_a_file_even_at_the_repository_root(work_tree):
    # Not a nesting rule: a file directly in the root of the work tree is still
    # a file, so it is still refused.
    (work_tree / "README.md").write_text("hi\n")
    assert _load("git").main(str(work_tree / "README.md")) is False
    # ...while the folder that holds it is exactly what the mode is for.
    assert _load("git").main(str(work_tree)) is True


def test_the_two_folder_gates_ask_different_questions(work_tree):
    # This is where the pair stops being symmetric, and deliberately so.
    #
    # `git` is the WORKING TREE of the repository a folder sits in, which every
    # folder in a work tree has — so it takes them all.
    #
    # `history` previews the target AS IT WAS, so it also asks whether there is
    # anything to preview: a folder qualifies when it has a top-level page, by
    # the shared entry rule. Without that half it put a history mode in the
    # switcher of every directory of every repository the user opens, whose
    # preview is a listing of a frozen tree.
    #
    # On the folder key the two are still offered side by side (the tests
    # above): what differs is which targets each GATE answers for, which is the
    # mechanism that exists for exactly this.
    for target in (str(work_tree), str(work_tree / "pkg")):
        assert _load("git").main(target) is True, target
        assert _load("history").main(target) is False, target

    # ...and the moment such a folder has a page, both take it.
    (work_tree / "pkg" / "page.html").write_text("<html></html>")
    assert _load("git").main(str(work_tree / "pkg")) is True
    assert _load("history").main(str(work_tree / "pkg")) is True


def test_neither_gate_excludes_the_other_on_an_app_folder(tmp_path, monkeypatch):
    # The old exclusions were symmetric refusals — `git` stepped aside inside a
    # fused app, `history` stepped aside outside one — held in place by a
    # comment in each file telling the reader to keep the two in step. Both are
    # gone: the modes answer different questions, so a folder gets both.
    workspace = tmp_path / "workspace"
    app = workspace / "tag" / "myapp"
    app.mkdir(parents=True)
    (app / "index.html").write_text("<p>hi</p>")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["git", "init", "-q", str(app)], check=True, env=env)
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE", str(workspace))
    monkeypatch.delenv("FUSED_RENDER_LINKED_APPS", raising=False)

    assert _load("git").main(str(app)) is True
    assert _load("history").main(str(app)) is True


def test_a_path_outside_any_repository_is_refused_by_both(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.delenv("FUSED_RENDER_LINKED_APPS", raising=False)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("hi")
    assert _load("git").main(str(plain)) is False
    assert _load("history").main(str(plain)) is False


# ------------------------------------------------------ the view draws commits


def test_the_view_draws_a_commits_section(source):
    assert "commitLine" in source
    assert 'listSection(\n    "Commits"' in source or 'listSection("Commits"' in source
    # Built with the section helper every other list uses, so Commits sits in the
    # same column, in the same vocabulary, as Staged/Changes/Untracked/Stashes.
    assert "more commits" in source


def test_the_commits_heading_carries_no_count(source):
    # The other sections' numbers are TOTALS. This list is one window of a
    # history nobody has measured (`limit = PAGE_SIZE * pages`), so a number here
    # would be the window size wearing a total's clothes — and it would grow by
    # PAGE_SIZE on every "load more". `null` is what makes `listSection` omit the
    # pill; `String(commits.length)` is the number that must not come back.
    assert '"Commits", null, null,' in source
    assert '"Commits", String(commits.length)' not in source
    # The real totals above it are untouched.
    for section in ("Staged changes", "Changes", "Untracked", "Stashes"):
        assert f'"{section}", String(' in source


def test_a_commit_row_is_a_subject_and_a_time(source):
    # The row is what you SKIM: the subject, and how long ago. Drawing the sha
    # and the author on it too turned the list into a table — an id to cross
    # before the sentence, and a column repeating the same name on every row.
    row = source[source.index("function commitLine("):]
    row = row[:row.index("\nfunction ")]
    assert 'className: "subject"' in row
    assert 'className: "when"' in row
    assert 'className: "sha"' not in row, "the id belongs to the selected state"
    assert 'className: "who"' not in row, "the author belongs to the selected state"
    # Not lost, though: the row's tooltip still answers "which commit is this?"
    assert "entry.short" in row and "entry.author" in row


def test_the_identity_moves_into_the_selected_state(source):
    # …and it is a node the CALLER builds, passed into the shared pane, so what
    # a commit selection shows can change without touching how a diff renders.
    assert "function commitMeta(" in source
    assert "function diffPane(title, sub, payload, meta)" in source
    assert "commitMeta(meta, rev)" in source
    # The placeholder pane is built from the row the user clicked, so the
    # heading does not swap under them when the read lands.
    assert "commitMeta(known, rev)" in source


def test_the_commit_selection_carries_the_full_object_name(source):
    # The row displays an abbreviation; the SELECTION holds the full object name.
    # It is what the shell resolves the content pane's bytes with, and an
    # abbreviation is a display form that would have to be re-resolved to be used.
    assert 'select("rev", selected ? null : entry.sha)' in source


def test_the_pane_has_exactly_one_master(source):
    # `rev` and `wt` both mean "what the right pane shows". That used to be TWO
    # params kept exclusive by convention — every writer of one cleared the other —
    # and missing either half left two selections claiming one pane. It is one
    # variable now, so the rule is structural: writing a commit into it IS clearing
    # the change, and there is no pair left to get wrong.
    assert "let selection = null;" in source
    assert 'selection.kind === "rev"' in source
    assert 'selection.kind === "wt"' in source
    # The four writers, all through the one setter.
    assert 'select("wt", change.path)' in source
    assert 'select("rev", selected ? null : entry.sha)' in source
    assert "select(null, null)" in source
    # And the old pair is gone from the file entirely — a leftover writer would
    # keep a param in the shell URL that this whole change exists to remove.
    for gone in ('param("rev")', 'param("wt")', "rev: null", "wt: null"):
        assert gone not in source, gone


def test_the_selection_is_not_a_param(source):
    # THE HARD CONSTRAINT. A param written by this template mirrors into the
    # explorer's own URL, and up there a revision is carried onto the next file by
    # a path change, saved into the file's session sidecar and replayed on the next
    # bare open, and stored in bookmarks. So the pane's subject is memory, and
    # `structural()` — the URL-derived repaint key — must not mention it.
    key = source[source.index("const structural = ()"):]
    key = key[:key.index(";")]
    assert '"pages"' in key and '"panel"' in key and '"ask"' in key
    assert "rev" not in key and "wt" not in key
    # `_side`/`panel` deep links still work, so the boundary trick is NOT used.
    assert "_fusedParamBoundary" not in source


def test_the_commit_reaches_the_shell_through_the_ancestor_global(source):
    # Not a param, not a postMessage: the runtime's ancestor-window hop, the same
    # idiom `_fusedFsChanged` uses (D3/D4). Guarded, because a page opened outside
    # the app's shell has no hop and no pane to drive.
    hop = source[source.index("function hopRev()"):]
    hop = hop[:hop.index("\n}")]
    assert 'typeof window._fusedSelectRev === "function"' in hop
    assert "window._fusedSelectRev(previewed)" in hop
    # ONE speaker: nothing else in the file may hop, or the pane and the rows
    # would be able to disagree about which commit is driving it.
    assert source.count("window._fusedSelectRev(") == 1
    # And `previewed` has ONE writer, which is what makes that true.
    # The declaration, `preview()`'s write, and the poll's clear — nothing else.
    assert source.count("previewed = ") == 3


def test_the_pane_subject_and_the_previewed_commit_are_separate_state(source):
    # Reading a commit's DIFF (this column) and putting that commit's version of
    # the open file in the pane next door are different requests. Collapsing them
    # would make a click meant for one always perform the other — including on a
    # surface where the second is impossible.
    assert "let previewed = null;" in source
    assert "let selection = null;" in source
    # `select` no longer speaks to the shell at all.
    setter = source[source.index("function select(kind, value)"):]
    setter = setter[:setter.index("\n/* ---")]
    assert "_fusedSelectRev" not in setter


def test_the_preview_control_needs_a_pane_to_drive(source):
    # THE CAPABILITY HANDSHAKE. The host MARKS the frame it can drive
    # (`data-fused-rev-target`, Preview.tsx); this reads the mark through
    # `window.parent` and offers the control only when it is there. In a folder's
    # listing preview pane nothing is marked, so there is no button rather than a
    # button that does nothing.
    assert '"[data-fused-rev-target]"' in source
    assert "function revMarkedFrame()" in source
    # Every failure — no parent, a cross-origin parent (`parent.document` throws),
    # a host that marks nothing — is the same answer, and it is "no".
    marked = source[source.index("function revMarkedFrame()"):]
    marked = marked[:marked.index("\n}")]
    assert "if (!host || host === window) return null;" in marked
    assert "catch (err)" in marked
    assert marked.count("return null") == 2 and "? el : null" in marked
    # ABSENCE is what gates the control: the row builds it inside `if (canPreview)`
    # and nothing else offers one.
    row = source[source.index("function commitLine(entry, selected)"):]
    row = row[:row.index("\nfunction ")]
    assert "if (canPreview) {" in row
    assert 'onClick: () => preview(on ? null : entry.sha)' in row
    assert source.count("preview:") == 1, "one preview affordance, on the commit row"
    # ...and `preview()` refuses even if something called it anyway.
    setter = source[source.index("function preview(sha)"):]
    setter = setter[:setter.index("\n}")]
    assert "const next = (canPreview && sha) ? sha : null;" in setter


def test_the_capability_is_polled_like_the_annotate_target(source):
    # The mark ARRIVES and DEPARTS after this page has mounted — the host's mode
    # switcher moves it, a listing removes it — so it is polled on focus plus a
    # slow timer, exactly as templates/claude/template.html polls its own mark. A
    # MutationObserver held on someone else's document for the life of the session
    # is not worth the one cheap read it would replace.
    assert "const REV_TARGET_POLL_MS = 750;" in source
    assert 'window.addEventListener("focus", () => pollRevTarget());' in source
    assert "setInterval(() => pollRevTarget(), REV_TARGET_POLL_MS);" in source
    # A capability that GOES takes the preview with it, and says so to the shell.
    poll = source[source.index("function pollRevTarget()"):]
    poll = poll[:poll.index("\n}")]
    assert "if (!has && previewed !== null)" in poll
    assert "hopRev();" in poll


def test_a_reload_of_this_frame_returns_the_pane_to_live(source):
    # `previewed` is memory, so a reload starts with nothing previewed; a shell
    # still holding the previous sha would leave the content pane on a revision no
    # row here claims.
    boot = source[source.index("/* Announce the empty preview"):]
    assert boot.index("hopRev();") < boot.index("draw(true);")


def test_a_selection_change_still_repaints(source):
    # The repaint used to be a side effect of the param write (`onChange` -> draw).
    # With the selection out of the URL, `select` has to ask for it — forced past
    # the structural short-circuit, exactly as a param change was.
    setter = source[source.index("function select(kind, value)"):]
    setter = setter[:setter.index("\nconst num")]
    assert "void draw(true);" in setter


def test_escape_still_unwinds_ask_then_panel_then_the_pane(source):
    handler = source[source.index('if (event.key !== "Escape") return;'):]
    # To the end of the listener, not to the first "});" — every `setParams({...})`
    # call inside it ends with those three characters.
    handler = handler[:handler.index("\n});")]
    order = [handler.index('param("ask")'), handler.index('param("panel")'),
             handler.index("if (selection)")]
    assert order == sorted(order)
    assert "select(null, null)" in handler


def test_the_view_asks_the_reader_for_the_log(source):
    # The overview carries the log again, so it must NOT opt out — `history: ""`
    # would leave the Commits section permanently empty while the section
    # itself still rendered its "no commits under this path" reading.
    assert 'history: ""' not in source
    # The window grows rather than pages: `limit` scales with `pages`, `page`
    # stays 0, and both `has_more` and `capped` gate the "load more" affordance.
    assert "limit: String(PAGE_SIZE * pages)" in source
    assert 'page: "0"' in source
    assert "data.has_more && !data.capped" in source
    # The selected-commit pane is a second read on its OWN channel: runPython
    # supersedes by channel and a superseded call never settles, so sharing the
    # worktree channel would deadlock the two reads against each other.
    assert 'chan("commit")' in source
    assert 'op: "commit", sha: rev' in source


# ------------------------------------------------- the file AS OF a commit
#
# Clicking a commit in this sidebar makes the explorer's CONTENT pane render the
# open file as that commit left it. Three layers hold that up and each is pinned
# below: the template hands the sha to the shell (above), the shell puts `_rev` on
# the content frame's src alone, and the injected runtime resolves every read
# through /api/git/show while refusing every write.
#
# Nothing is written to disk for any of it. The predecessor design (`history`)
# `git archive`d a snapshot into ~/.fused-render/app-versions/<key>/<sha>/; this
# resolves on read, so closing the sidebar leaves nothing behind.


@pytest.fixture(scope="module")
def runtime():
    path = os.path.join(_ROOT, "fused_render", "static", "runtime.js")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_the_runtime_reads_rev_off_the_frames_own_query(runtime):
    # `_file` falls back to an ANCESTOR's query; `_rev` deliberately never does.
    # An inheritable revision is exactly the leak that keeps this param off the
    # shell's URL in the first place.
    assert 'ownQuery("_rev")' in runtime
    assert "function revUrl(path)" in runtime
    assert '"/api/git/show?path="' in runtime


def test_the_runtime_resolves_reads_through_git_not_the_filesystem(runtime):
    # rawUrl is the choke point: readFile fetches rawUrl(), and every <img>/<embed>
    # src a template builds goes through it too. So ONE branch there is what makes
    # "templates change zero lines" true.
    raw = runtime[runtime.index("function rawUrl(path)"):]
    raw = raw[:raw.index("\n  }")]
    assert "if (revResolves(path)) return revUrl(path);" in raw
    assert "return fetch(rawUrl(path)" in runtime      # readFile rides on it
    # ...and a stat reports the size AT that revision, never today's.
    assert "function revStat(path, live)" in runtime
    assert 'method: "HEAD"' in runtime


def test_a_revision_stat_is_never_writable(runtime):
    # The field every template checks before it offers an edit UI. An editor that
    # renders enabled over bytes it cannot write back is the whole failure.
    stat = runtime[runtime.index("function revStat(path, live)"):]
    stat = stat[:stat.index("\n  // Write BYTES")]
    assert "writable: false," in stat


def test_the_mutators_refuse_under_a_revision(runtime):
    # A runtime-level gate, and the code says so: the server has no idea a caller
    # is looking at the past (the path is the LIVE path), so this is exactly the
    # promise that the documented helpers cannot silently write today's file.
    for fn in ("writeFile(path, content, opts)", "uploadFile(path, blob)",
               "mkdir(path)"):
        body = runtime[runtime.index("function " + fn):]
        assert "if (rev !== null) return revRefusal(" in body[:400], fn
    assert 'err.type = "readonly";' in runtime
    assert "RUNTIME-LEVEL refusal, not a server-enforced one" in runtime


def test_the_python_reader_gap_is_recorded_at_its_seam(runtime):
    # A deliberate phase-2b deferral, not a bug: `main()` receives the real path
    # and open()s the live file. The note lives immediately above runPython so the
    # next reader of that function finds it.
    head = runtime[:runtime.index("function runPython(pyPath, params, opts)")]
    assert "KNOWN GAP" in head[-3000:]
    assert "phase 2b" in head[-3000:]


def test_only_the_content_frame_carries_rev():
    # The shell's half of the constraint: `_rev` is appended to the CONTENT frame's
    # src and to nothing else — not the sidebar's frame (which must stay live, or
    # the git view would be reading its own repository as of a past commit) and
    # never to the address bar.
    preview = os.path.join(_ROOT, "frontend", "src", "apps", "explorer",
                           "Preview.tsx")
    with open(preview, encoding="utf-8") as f:
        shell = f.read()
    assert "revSrc(" in shell
    side = shell[shell.index("const sideSrcFor ="):]
    side = side[:side.index("\n  };")]
    assert "_rev" not in side and "revSrc" not in side
    # And the sha is state, never a param write: nothing in this file may put it
    # into a search string. `revSrc` is the ONLY producer of the param, and it
    # appends to a frame src it is handed.
    for forbidden in ('params.set("_rev"', 'params.get("_rev"', "&_rev="):
        assert forbidden not in shell, forbidden


def test_the_shell_marks_only_a_frame_a_revision_can_be_driven_into():
    # The other end of the capability handshake. The mark exists ONLY where the
    # capability does: `splitCapable` (the single-file explorer preview, the one
    # surface with both a content pane and a git sidebar) AND `m === shown` (the
    # frame the reader is looking at — the held-frame swap can leave two mounted).
    # A folder's listing preview pane renders no frame and stamps nothing, which
    # is how the git template running THERE learns it has nothing to drive.
    preview = os.path.join(_ROOT, "frontend", "src", "apps", "explorer",
                           "Preview.tsx")
    with open(preview, encoding="utf-8") as f:
        shell = f.read()
    mark = shell[shell.index("data-fused-rev-target={"):]
    mark = mark[:mark.index("}\n")]
    assert "splitCapable && m === shown" in mark
    # `undefined`, not `false`: an attribute React would still render (`="false"`)
    # is a mark, and `[data-fused-rev-target]` would match it.
    assert 'shown ? "" : undefined' in mark
    # A SECOND mark rather than a second reading of the annotate one: they mean
    # different things, and inferring one capability from another is wrong the day
    # either condition moves.
    assert "data-fused-annotate-target={" in shell
    assert shell.count("data-fused-rev-target") == 1


@pytest.fixture()
def show_client():
    """A client over the git_show router ALONE.

    Not `create_app`: the whole app stages the core templates into the test home
    on import and mounts them as StaticFiles, and building one per test races
    other workers through that staging (the dir is wiped and swapped). The route
    is self-contained — nothing it does depends on the rest of the app — so the
    router is mounted on a bare FastAPI and the REGISTRATION is pinned separately
    below.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from fused_render.server.routers.git_show import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_route_is_registered_with_the_app():
    # The other half of the fixture above: a route nothing includes is a route
    # nothing can call, and the fixture would never notice.
    with open(os.path.join(_ROOT, "fused_render", "server", "app.py"),
              encoding="utf-8") as f:
        app_src = f.read()
    assert "from fused_render.server.routers.git_show import router as git_show_router" \
        in app_src
    assert "app.include_router(git_show_router)" in app_src


@pytest.fixture()
def history_tree(work_tree):
    """`pkg/mod.py` committed twice, plus a file added only in the second commit.

    Returns `(first_sha, second_sha)`.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def sha():
        out = subprocess.run(["git", "-C", str(work_tree), "rev-parse", "HEAD"],
                             check=True, capture_output=True, env=env)
        return out.stdout.decode().strip()

    first = sha()
    (work_tree / "pkg" / "mod.py").write_text("x = 2\n")
    (work_tree / "pkg" / "later.py").write_text("later = True\n")
    subprocess.run(["git", "-C", str(work_tree), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(work_tree), "-c", "user.name=T",
                    "-c", "user.email=t@e", "commit", "-qm", "second"],
                   check=True, env=env)
    return first, sha()


def test_show_serves_the_file_as_of_the_commit(show_client, work_tree, history_tree):
    first, second = history_tree
    target = str(work_tree / "pkg" / "mod.py")
    # The working tree says x = 2; the first commit says x = 1, and that is the
    # whole feature.
    assert (work_tree / "pkg" / "mod.py").read_text() == "x = 2\n"
    old = show_client.get("/api/git/show", params={"path": target, "sha": first})
    assert old.status_code == 200
    assert old.text == "x = 1\n"
    new = show_client.get("/api/git/show", params={"path": target, "sha": second})
    assert new.text == "x = 2\n"


def test_show_takes_the_absolute_working_tree_path(show_client, work_tree,
                                                   history_tree):
    # The caller forwards the path it already has; the repo root and the
    # repo-relative path are resolved server-side. A relative path is refused
    # rather than guessed at, the same check every fs read route makes.
    first, _ = history_tree
    r = show_client.get("/api/git/show", params={"path": "pkg/mod.py", "sha": first})
    assert r.status_code == 400
    assert "absolute" in r.json()["error"]


def test_show_accepts_an_abbreviated_sha(show_client, work_tree, history_tree):
    first, _ = history_tree
    r = show_client.get("/api/git/show",
                        params={"path": str(work_tree / "pkg" / "mod.py"),
                                "sha": first[:8]})
    assert r.status_code == 200 and r.text == "x = 1\n"


def test_show_refuses_anything_that_is_not_hex(show_client, work_tree,
                                               history_tree):
    # No ref names, no revision expressions, no `--upload-pack=`: the value goes
    # into an argv, and hex-only is what makes it un-option-shaped by construction.
    target = str(work_tree / "pkg" / "mod.py")
    for sha in ("HEAD", "HEAD~1", "main", "--upload-pack=x", "", "zzz",
                ":/second"):
        r = show_client.get("/api/git/show", params={"path": target, "sha": sha})
        assert r.status_code == 400, sha
        assert "hex" in r.json()["error"], sha


def test_show_refuses_a_directory(show_client, work_tree, history_tree):
    # `git show <sha>:<dir>` answers a TREE LISTING, which would be served as if
    # it were content. Unreachable from the shell (only a file gets a content
    # pane), and refused anyway rather than allowed to look like an answer.
    first, _ = history_tree
    r = show_client.get("/api/git/show",
                        params={"path": str(work_tree / "pkg"), "sha": first})
    assert r.status_code == 400
    assert "not a directory" in r.json()["error"]


def test_show_404s_an_unknown_sha(show_client, work_tree, history_tree):
    r = show_client.get("/api/git/show",
                        params={"path": str(work_tree / "pkg" / "mod.py"),
                                "sha": "0" * 40})
    assert r.status_code == 404
    # git's own sentence, not a traceback.
    assert "error" in r.json() and "Traceback" not in r.text


def test_show_404s_a_path_that_did_not_exist_at_that_commit(show_client, work_tree,
                                                            history_tree):
    first, _ = history_tree
    r = show_client.get("/api/git/show",
                        params={"path": str(work_tree / "pkg" / "later.py"),
                                "sha": first})
    assert r.status_code == 404
    assert "Traceback" not in r.text


def test_show_404s_a_path_outside_any_work_tree(show_client, tmp_path,
                                                history_tree):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("hi")
    r = show_client.get("/api/git/show",
                        params={"path": str(plain / "a.txt"), "sha": "0" * 40})
    assert r.status_code == 404
    assert "work tree" in r.json()["error"]


def test_show_bounds_the_response(show_client, work_tree, history_tree,
                                  monkeypatch):
    # A blob over the cap is a clean 413, never a truncation: a pane labelled "as
    # of abc1234" showing the first N bytes would be a subtler lie than showing
    # nothing. The cap is read through its defining module so this override is the
    # one production reads see.
    from fused_render.server.routers import git_show

    first, second = history_tree
    monkeypatch.setattr(git_show, "MAX_SHOW_BYTES", 3)
    r = show_client.get("/api/git/show",
                        params={"path": str(work_tree / "pkg" / "mod.py"),
                                "sha": second})
    assert r.status_code == 413
    assert "larger than" in r.json()["error"]


def test_show_answers_head_with_the_size_at_that_revision(show_client, work_tree,
                                                          history_tree):
    # This is what `fused.stat()` under `_rev` reads: a live stat would report the
    # size of TODAY's file, which is the wrong number to guard a past read with.
    first, _ = history_tree
    (work_tree / "pkg" / "mod.py").write_text("x = 2\nand more\n")
    r = show_client.head("/api/git/show",
                         params={"path": str(work_tree / "pkg" / "mod.py"),
                                 "sha": first})
    assert r.status_code == 200
    assert r.headers["content-length"] == "6"      # len("x = 1\n")
    assert r.content == b""

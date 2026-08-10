"""The `history` template (app git history): the condition gate offers it
only inside git-backed app folders, and its Python backend can list the log,
materialise any commit as a rendered snapshot, and revert — always by adding
a commit on top, never by rewriting history.

Since D235 the mode is also the FILE-side history view: any existing file
inside any git work tree gets its own timeline, scoped to that one path, and
that timeline is READ-ONLY — the repository is the user's own, so `revert` is
refused there exactly as it is for a linked app. App-ness is still decided
first, so a file inside a fused app keeps the app's (writable) history.

Real git in tmp workspaces (FUSED_RENDER_DIR / FUSED_RENDER_WORKSPACE_DIR),
same fixtures shape as tests/test_app_git.py. The template modules are loaded
from the package source via importlib — they are exec'd standalone in
production too (conditions by server._run_condition, the backend by /api/run),
so nothing here goes through a package import.
"""
import importlib.util
import os
import subprocess

import pytest

from fused_render import app_git

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "history")


def _html():
    """The template's own source. The narrow-layout behaviour is CSS plus a few
    lines of matchMedia glue with no Python side at all, so source assertions
    are the only pin available short of a browser — the same trade the other
    template tests in this suite make."""
    with open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8") as f:
        return f.read()


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("test_history_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE_DIR", str(fdir))
    # Snapshots land under the shell home; keep them in the tmp tree.
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", str(tmp_path / "home"))
    return fdir


def _git(d, *args):
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t",
                           "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True)


def _make_app(workspace, tag="local", name="demo"):
    d = workspace / tag / name
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html>v1</html>")
    assert app_git.init_repo(str(d))
    return d


def _plain_repo(workspace, tmp_path, name="userrepo"):
    """A git repository that is NOT a fused app and lives nowhere near the
    workspace — the D235 case: a file whose only history is the user's own
    repo. Deliberately a sibling of the workspace, not inside it, so nothing
    here can accidentally satisfy the `<workspace>/<tag>/<name>` app rule."""
    d = tmp_path / name
    d.mkdir(parents=True)
    _git(d, "init", "-q")
    return d


def _commit(d, rel, text, msg):
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(d, "add", "--", ":(literal)" + rel)
    _git(d, "commit", "-q", "-m", msg)
    return _git(d, "rev-parse", "HEAD").stdout.strip()


def _log_subjects(d):
    return _git(d, "log", "--format=%s").stdout.strip().splitlines()


def _shas(d):
    return _git(d, "log", "--format=%H").stdout.strip().splitlines()


# ------------------------------------------------------------------- gate

def test_condition_true_inside_a_git_backed_app(workspace, tmp_path):
    # An app is a work tree whose folder has an entry page, so it satisfies both
    # halves of the folder rule. Worth pinning on its own, because it is the
    # case the app-builder view depends on (App.tsx APP_MODES).
    cond = _load("condition")
    d = _make_app(workspace)
    (d / "sub").mkdir()
    (d / "sub" / "x.py").write_text("x = 1\n")
    assert cond.main(str(d)) is True                     # the app dir itself
    assert cond.main(str(d / "index.html")) is True      # a file inside
    assert cond.main(str(d / "sub" / "x.py")) is True    # nested path
    # A nested FOLDER with no page of its own is not offered, app or not: the
    # folder rule is about what can be rendered, and `sub/` renders nothing.
    assert cond.main(str(d / "sub")) is False
    # The workspace and its tag level are not repositories, and have no page.
    assert cond.main(str(workspace)) is False            # workspace root
    assert cond.main(str(workspace / "local")) is False  # tag level
    assert cond.main(str(tmp_path / "elsewhere")) is False
    # App-shaped folder without a repo: no history to show.
    plain = workspace / "local" / "plain"
    plain.mkdir(parents=True)
    (plain / "index.html").write_text("<html></html>")
    assert cond.main(str(plain)) is False


def test_condition_true_for_a_file_in_any_git_repo(workspace, tmp_path):
    # D235: the file-side history view no longer needs a fused app anywhere.
    # Without this, a tracked file outside the workspace has no history view at
    # all — `git` is directory-only now, so nothing else would answer for it.
    cond = _load("condition")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "notes.md", "# one\n", "Add notes")
    assert cond.main(str(repo / "notes.md")) is True
    # An untracked file still lives in the work tree: the gate asks git where
    # the file IS, not whether git knows it yet (an empty log is the view's
    # story to tell, not a reason to hide the mode).
    (repo / "fresh.txt").write_text("new\n")
    assert cond.main(str(repo / "fresh.txt")) is True


def test_condition_offers_a_folder_only_when_it_has_a_page_to_render(
        workspace, tmp_path):
    # A folder needs BOTH halves: in a work tree, and renderable by the shared
    # entry rule (index.html, else the first top-level .html — the same
    # predicate the `app` view and the chat's pane resolve their page with).
    #
    # The gate briefly offered EVERY folder in a work tree. That put a history
    # mode in the switcher of every directory of every repository the user
    # opens, for a preview that is a listing of a frozen tree — worth having by
    # URL, not worth a mode everywhere.
    cond = _load("condition")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "sub/notes.md", "# one\n", "Add notes")
    assert cond.main(str(repo)) is False        # in a work tree, but no page
    assert cond.main(str(repo / "sub")) is False

    (repo / "sub" / "page.html").write_text("<html></html>")
    assert cond.main(str(repo / "sub")) is True   # first top-level .html
    (repo / "index.html").write_text("<html></html>")
    assert cond.main(str(repo)) is True           # index.html

    # A page NESTED below the folder does not make the folder renderable — the
    # entry rule is top-level only, and so is this.
    deep = repo / "deep"
    (deep / "inner").mkdir(parents=True)
    (deep / "inner" / "page.html").write_text("<html></html>")
    assert cond.main(str(deep)) is False


def test_condition_is_indifferent_to_the_page_being_tracked(workspace, tmp_path):
    # The two halves answer different questions: git says "is there a history
    # here", the entry rule says "is this a thing we render". An untracked page
    # still makes the folder renderable, and its folder still has a timeline.
    cond = _load("condition")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "notes.md", "# one\n", "Add notes")
    (repo / "fresh.html").write_text("<html></html>")
    assert cond.main(str(repo)) is True


def test_condition_false_for_a_path_that_does_not_exist(workspace, tmp_path):
    # The test must be "is a FILE", not "is not a directory": a MISSING path
    # inside a repo satisfies the loose form, so the mode gets offered for a
    # file that isn't there and the backend then reports an empty history for
    # it. `not isdir` says True to a missing file AND to a missing directory,
    # hence both shapes here.
    cond = _load("condition")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "notes.md", "# one\n", "Add notes")
    assert cond.main(str(repo / "gone.md")) is False
    assert cond.main(str(repo / "gone" / "deeper")) is False


def test_condition_false_when_the_parent_directory_is_missing(workspace, tmp_path):
    # The work-tree probe runs `git -C <parent>`; with no parent on disk git
    # exits non-zero from wherever it was launched, so the gate must decide this
    # itself rather than trusting a fork whose cwd it never established.
    cond = _load("condition")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "notes.md", "# one\n", "Add notes")
    assert cond.main(str(repo / "gone" / "deeper" / "x.md")) is False
    assert cond.main("") is False


def test_condition_false_for_a_file_outside_any_repo(workspace, tmp_path):
    # No repository means no history: the gate must ask git rather than assume
    # every existing file has a timeline.
    cond = _load("condition")
    plain = tmp_path / "loose"
    plain.mkdir()
    (plain / "notes.md").write_text("# one\n")
    assert cond.main(str(plain / "notes.md")) is False


def test_condition_false_for_a_mount_backed_file(workspace, tmp_path):
    # The mount refusal must come BEFORE the work-tree probe: `.git` discovery
    # over a kernel NFS mount is the exact stat this gate may never issue, and a
    # repo checked out on a remote would otherwise pull it in.
    cond = _load("condition")
    mount = tmp_path / "home" / "mounts" / "remote"
    mount.mkdir(parents=True)
    _git(mount, "init", "-q")
    _commit(mount, "notes.md", "# one\n", "Add notes")
    assert cond.main(str(mount / "notes.md")) is False


# -------------------------------------------------------------------- log

def test_log_lists_commits_newest_first(workspace):
    v = _load("history")
    d = _make_app(workspace)
    (d / "index.html").write_text("<html>v2</html>")
    app_git.commit(str(d / "index.html"), "Edit index.html")
    res = v.main(action="log", file=str(d / "index.html"))
    subjects = [c["subject"] for c in res["commits"]]
    assert subjects == ["Edit index.html", "New app from starter"]
    assert all(c["ts"] > 0 for c in res["commits"])
    # Same answer for the directory target — the repo is the app.
    assert v.main(action="log", file=str(d))["commits"][0]["sha"] == \
        res["commits"][0]["sha"]


def test_log_for_a_file_is_scoped_to_that_one_file(workspace, tmp_path):
    # The pathspec is the whole point of a file target: dropped (or widened to
    # the directory) the log would list the sibling's commits too, and every
    # snapshot offered would be a revision that never touched this file.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "a.md", "a1\n", "Add a")
    _commit(repo, "b.md", "b1\n", "Add b")
    _commit(repo, "a.md", "a2\n", "Edit a")
    res = v.main(action="log", file=str(repo / "a.md"))
    assert [c["subject"] for c in res["commits"]] == ["Edit a", "Add a"]
    assert [c["subject"] for c in
            v.main(action="log", file=str(repo / "b.md"))["commits"]] == ["Add b"]
    # Read-only outside an app: the UI must not offer a button the backend
    # refuses (see test_revert_is_refused_for_a_file_target).
    assert res["can_revert"] is False


def test_a_file_inside_an_app_still_resolves_to_the_app(workspace):
    # Kind precedence: app-ness is asked FIRST. If a file target won, opening
    # any file in an app would demote the view to that file's single-file log
    # and silently drop `revert` — losing the timeline the auto-commits build.
    v = _load("history")
    d = _make_app(workspace)
    (d / "extra.py").write_text("x = 1")
    app_git.commit(str(d), "Add extra")
    res = v.main(action="log", file=str(d / "extra.py"))
    # The app's whole history, including the commit that predates this file.
    assert [c["subject"] for c in res["commits"]] == \
        ["Add extra", "New app from starter"]
    assert res["can_revert"] is True
    assert res["app"] == str(d)


def test_a_pathspec_magic_filename_is_matched_literally(workspace, tmp_path):
    # `:(literal)` exists for this: unwrapped, `a[1].md` is a glob that matches
    # the sibling `a1.md`, so the file's log would show a commit that never
    # touched it (and its snapshot would materialise the wrong file).
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "a[1].md", "bracketed\n", "Add bracketed")
    _commit(repo, "a1.md", "plain\n", "Add plain")
    res = v.main(action="log", file=str(repo / "a[1].md"))
    assert [c["subject"] for c in res["commits"]] == ["Add bracketed"]
    snap = v.main(action="snapshot", file=str(repo / "a[1].md"),
                  sha=res["commits"][0]["sha"])
    assert os.path.basename(snap["file"]) == "a[1].md"
    with open(snap["file"], encoding="utf-8") as f:
        assert f.read() == "bracketed\n"


def test_log_for_a_plain_directory_is_scoped_to_its_subtree(workspace, tmp_path):
    # The gate offers `history` on any path in a work tree, and this module had
    # no answer for the plain-directory half of that: it fell through the app
    # rule and refused with "not inside a fused app folder" while the mode sat
    # in the switcher offering history for the folder.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "docs/a.md", "a1\n", "Add docs/a")
    _commit(repo, "src/b.py", "b = 1\n", "Add src/b")
    _commit(repo, "docs/c.md", "c1\n", "Add docs/c")

    res = v.main(action="log", file=str(repo / "docs"))
    assert [c["subject"] for c in res["commits"]] == ["Add docs/c", "Add docs/a"]
    assert res["kind"] == "dir"
    # The repo ROOT is a directory in a work tree like any other.
    assert [c["subject"] for c in v.main(action="log", file=str(repo))["commits"]] == \
        ["Add docs/c", "Add src/b", "Add docs/a"]


# --------------------------------------------------------------- log paging

def _many(repo, n, rel="notes.md"):
    """`n` commits touching one file, oldest first. Returns their shas in the
    order `git log` reports them — NEWEST first — so a slice of this list is
    exactly what a page of the log must be."""
    for i in range(n):
        _commit(repo, rel, "v%d\n" % i, "Edit %d" % i)
    return _shas(repo)


def test_log_returns_one_page_and_says_history_continues(workspace, tmp_path):
    # The reason this exists at all: `_log` had no --max-count, so a directory
    # target in the user's own long-lived repository formatted and shipped every
    # commit that ever touched the subtree — for a spine whose first screen is
    # twenty rows.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    shas = _many(repo, 25)

    res = v.main(action="log", file=str(repo))
    assert v.PAGE_SIZE == 20
    assert [c["sha"] for c in res["commits"]] == shas[:20]
    assert res["more"] is True
    assert res["skip"] == 0


def test_the_next_page_continues_with_no_overlap_and_no_gap(workspace, tmp_path):
    # `skip` is the cursor and `commits.length` is what the view passes for it,
    # so page 2 must begin exactly where page 1 stopped: an off-by-one either
    # way is a repeated row or a commit nobody can ever reach.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    shas = _many(repo, 25)

    first = v.main(action="log", file=str(repo))
    second = v.main(action="log", file=str(repo), skip=len(first["commits"]))
    assert [c["sha"] for c in second["commits"]] == shas[20:]
    assert second["skip"] == 20
    assert second["more"] is False
    got = [c["sha"] for c in first["commits"]] + [c["sha"] for c in second["commits"]]
    assert got == shas
    assert len(set(got)) == len(shas)


def test_a_short_history_is_one_page_with_no_more(workspace, tmp_path):
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    shas = _many(repo, 3)

    res = v.main(action="log", file=str(repo))
    assert [c["sha"] for c in res["commits"]] == shas
    assert res["more"] is False


def test_exactly_one_page_of_history_does_not_claim_more(workspace, tmp_path):
    # The +1 probe is the whole mechanism: fetch PAGE_SIZE + 1 and let the
    # overflow row (never shipped) answer `more`. At exactly PAGE_SIZE there is
    # no overflow row, so a `>=` here would offer a "show older" that pages to
    # nothing.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    shas = _many(repo, 20)

    res = v.main(action="log", file=str(repo))
    assert [c["sha"] for c in res["commits"]] == shas
    assert len(res["commits"]) == 20
    assert res["more"] is False
    # And the page past the end is empty rather than an error.
    tail = v.main(action="log", file=str(repo), skip=20)
    assert tail["commits"] == [] and tail["more"] is False


def test_paging_is_scoped_by_the_pathspec_like_the_first_page(workspace, tmp_path):
    # A page is a slice of the TARGET'S log, not of the repository's: the
    # pathspec has to survive onto the --skip call or page 2 starts listing a
    # sibling's commits.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    _many(repo, 22, rel="a.md")
    for i in range(5):
        _commit(repo, "b.md", "b%d\n" % i, "B %d" % i)

    first = v.main(action="log", file=str(repo / "a.md"))
    second = v.main(action="log", file=str(repo / "a.md"), skip=20)
    subjects = [c["subject"] for c in first["commits"] + second["commits"]]
    assert len(subjects) == 22
    assert all(s.startswith("Edit ") for s in subjects)
    assert second["more"] is False


def test_a_junk_skip_is_the_first_page_not_an_error(workspace, tmp_path):
    # `skip` arrives from a URL-driven client, so it is coerced rather than
    # trusted: a negative or unparseable value must not reach git as an option.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    shas = _many(repo, 3)
    for bad in (-5, "", "abc", None):
        res = v.main(action="log", file=str(repo), skip=bad)
        assert [c["sha"] for c in res["commits"]] == shas
        assert res["skip"] == 0


# -------------------------------------------------- a stale `rev` deep link

def test_has_rev_recognises_a_commit_of_the_targets_own_log(workspace, tmp_path):
    # The off-page deep link's own question: page 1 does not hold this commit,
    # but the target's history does, so the link is honoured as it always was.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    shas = _many(repo, 25, rel="notes.md")
    old = shas[-1]  # the oldest, well past the first page

    res = v.main(action="has_rev", file=str(repo / "notes.md"), sha=old)
    assert res["known"] is True
    # Resolved, so the caller never has to re-resolve the prefix it asked with.
    assert res["sha"] == old
    assert v.main(action="has_rev", file=str(repo / "notes.md"),
                  sha=old[:7])["sha"] == old


def test_has_rev_rejects_a_commit_that_never_touched_this_target(
        workspace, tmp_path):
    # THE BUG. `rev` is URL state, and in the explorer's preview pane it outlives
    # the target: pick a commit while previewing a.md, then click b.md in the
    # listing, and the pane reboots the template for b.md with a.md's `rev` still
    # on the URL. a.md's commit is a perfectly valid commit of this repository —
    # so `_resolve_sha` says yes and the old deep-link path resolved it as given,
    # previewing "this revision does not contain that file" about a revision the
    # user never picked. Membership has to be asked of the TARGET'S log.
    #
    # Note what a naive `git log <rev> -- b.md` would answer here: a row, because
    # b.md's own older commit is an ancestor of a.md's. The test is that the row
    # IS the rev.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "b.md", "b1\n", "Add b")
    a_sha = _commit(repo, "a.md", "a1\n", "Add a")

    assert v.main(action="has_rev", file=str(repo / "b.md"),
                  sha=a_sha) == {"known": False, "sha": ""}
    # Same story one level up: a commit that touched nothing under the folder.
    _commit(repo, "docs/d.md", "d\n", "Add docs/d")
    top = _commit(repo, "top.md", "t\n", "Add top")
    assert v.main(action="has_rev", file=str(repo / "docs"),
                  sha=top)["known"] is False


def test_has_rev_answers_a_junk_revision_with_a_flag_not_an_error(
        workspace, tmp_path):
    # The caller's next move is a silent fall back to the newest commit, so an
    # unknown or malformed rev must be the same shape of answer as a known one —
    # an `error` key here would surface a notice for the ordinary case of a
    # preview pane moving to another file.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    _many(repo, 2)
    for bad in ("", "zzzzzzz", "not a sha", "0" * 40, "--output=/tmp/x", None):
        res = v.main(action="has_rev", file=str(repo / "notes.md"), sha=bad)
        assert res == {"known": False, "sha": ""}, bad


def test_has_rev_is_scoped_like_the_log_for_every_kind(workspace, tmp_path):
    # Whatever `log` lists, `has_rev` accepts — the two share the pathspec so the
    # spine and the deep link can never disagree about what this target's history
    # is. An app is the widest scope (its whole subtree), a file the narrowest.
    v = _load("history")
    app = _make_app(workspace)
    _commit(app, "index.html", "<html>v2</html>", "Edit index")
    for sha in _shas(app):
        assert v.main(action="has_rev", file=str(app), sha=sha)["known"] is True
        assert v.main(action="has_rev", file=str(app / "index.html"),
                      sha=sha)["known"] is True

    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "a.md", "a\n", "Add a")
    _commit(repo, "sub/b.md", "b\n", "Add sub/b")
    listed = {c["sha"] for c in v.main(action="log",
                                       file=str(repo / "sub"))["commits"]}
    for sha in _shas(repo):
        assert v.main(action="has_rev", file=str(repo / "sub"),
                      sha=sha)["known"] is (sha in listed)


def test_the_view_verifies_an_off_page_rev_before_honouring_it():
    # No behavioural seam on this side (it is boot glue around the runPython
    # bridge), so the pin is on the source: the off-page branch must ask the
    # backend, must fall through to the newest commit when the answer is no, and
    # must not grow a second `rev` writer to do it — select() is the one funnel,
    # and an extra params.set on a pristine entry is an extra history entry.
    html = _html()
    body = html.split("async function loadLog(")[1].split("commitsEl.add")[0]
    assert 'action: "has_rev"' in body
    # The first-page hit still short-circuits: no round trip for the common case.
    hit = body.index("commits.find(c => c.sha.startsWith(preferSha))")
    probe = body.index('action: "has_rev"')
    assert hit < probe
    # `pick` starts at the newest commit and is only moved by a verified rev, so
    # "not in this history" is the fallback rather than a branch of its own.
    assert "let pick = commits[0].sha;" in body
    assert "if (known) pick = preferSha;" in body
    # Selection stays the single params writer.
    assert 'params.set("rev"' not in body
    assert html.count('params.set("rev"') == 1


def test_the_view_pages_by_skip_and_trusts_the_more_flag():
    # The page size is defined ONCE, in history.py. The template asks for the
    # next page by `skip` alone and reads `more` off the payload, so a change to
    # PAGE_SIZE cannot leave the two halves disagreeing about where a page ends.
    html = _html()
    assert "skip: commits.length" in html
    assert "more = !!res.more" in html
    # No second copy of the page size on this side (the name may appear in a
    # comment pointing AT the backend; a `--max-count` here would be a fork).
    assert "max-count" not in html


def test_a_reload_fences_out_paging_before_it_awaits():
    # The generation counter alone does NOT close this: a "show older" click that
    # lands DURING a reload's await reads the NEW generation, so its own guard
    # passes, while its `skip` came from the stale list — its page then appends
    # onto the fresh first page and leaves a hole between them (after a revert:
    # 20 fresh rows, then rows 40+, with 20-39 unreachable and `more` overwritten
    # by that page's flag). The fence is `more = false; renderList()` in the same
    # SYNCHRONOUS run as the bump, which takes the pager row out of the DOM for
    # the whole reload — so a click is either already in flight (dropped by the
    # generation check) or refused by `!more`, with no third case.
    html = _html()
    body = html.split("async function loadLog(")[1]
    fence = body.index("more = false;")
    bump = body.index("logGen++")
    first_await = body.index("await fused.runPython")
    assert bump < fence < first_await
    assert body.index("renderList();") < first_await
    # And the guard the fence works through is still the one on the way in.
    assert "if (loadingMore || !more) return;" in html


def test_the_pager_row_is_not_a_commit_row():
    # The "show older" row is a sibling of the rows inside #commits, so the
    # delegated click handler has to answer it FIRST — landing in the `.commit`
    # branch would select nothing and, below the breakpoint, would flip the
    # layout to the preview while the user was reading the list.
    html = _html()
    handler = html.split('commitsEl.addEventListener("click"')[1]
    older = handler.index('closest(".older")')
    row = handler.index('closest(".commit")')
    assert older < row
    # And the loading state lives on that row, not in the snapshot notice.
    assert 'loadingMore ? " loading" : ""' in html


def test_a_plain_directory_is_read_only_but_still_previews(workspace, tmp_path):
    # Read-only is about the WRITE (the repository is the user's own, so a
    # revert commit carrying the Fused identity is refused). It says nothing
    # about the read: a folder's history previews like everything else's.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    sha = _commit(repo, "docs/a.md", "a1\n", "Add docs/a")
    before = _shas(repo)

    assert v.main(action="log", file=str(repo / "docs"))["can_revert"] is False
    rev = v.main(action="revert", file=str(repo / "docs"), sha=sha)
    assert "error" in rev and "managed by you" in rev["error"]
    assert _shas(repo) == before
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


def test_snapshot_of_a_directory_materialises_its_subtree(workspace, tmp_path):
    # The dir kind archives exactly like an app: `-C <the dir>` with NO
    # pathspec, because the directory IS the scope — a workspace app (where the
    # app is the repo root) is the same call, degenerately.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "docs/a.md", "a1\n", "Add docs/a")
    _commit(repo, "docs/sub/b.md", "b1\n", "Add docs/sub/b")
    old_sha = _shas(repo)[-1]          # the commit BEFORE sub/ existed
    _commit(repo, "src/out.py", "x = 1\n", "Add src/out")

    snap = v.main(action="snapshot", file=str(repo / "docs"), sha=_shas(repo)[0])
    assert "error" not in snap
    # Entry names are relative to `-C`, so the folder's own contents sit at the
    # top of the snapshot — not `docs/a.md` under a rebuilt prefix.
    with open(os.path.join(snap["dir"], "a.md"), encoding="utf-8") as f:
        assert f.read() == "a1\n"
    with open(os.path.join(snap["dir"], "sub", "b.md"), encoding="utf-8") as f:
        assert f.read() == "b1\n"
    # Scoped to the subtree: a sibling folder's file is not in this snapshot.
    assert not os.path.exists(os.path.join(snap["dir"], "src"))
    # A folder is not a document /render can serve, so it is reported as
    # something to BROWSE instead — the view frames it through /explorer/embed.
    assert snap["browse"] == snap["dir"]
    assert snap["entry"] is None

    # ...and the past really is the past: at the older commit `sub/` is absent.
    older = v.main(action="snapshot", file=str(repo / "docs"), sha=old_sha)
    assert os.path.isfile(os.path.join(older["dir"], "a.md"))
    assert not os.path.exists(os.path.join(older["dir"], "sub"))
    assert older["dir"] != snap["dir"]


def test_a_directory_snapshot_is_extracted_once_and_then_reused(
        workspace, tmp_path):
    # A commit is immutable, so the completion marker is what makes the second
    # click free — the same contract the app snapshot has. It matters more here:
    # this is the user's own repository and the subtree can be large, so the
    # extraction is lazy (per commit clicked) and paid at most once.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    sha = _commit(repo, "docs/a.md", "a1\n", "Add docs/a")

    first = v.main(action="snapshot", file=str(repo / "docs"), sha=sha)
    # BESIDE the tree, not inside it: everything inside is content the
    # snapshot's own listing shows, and a marker row in a browsable historical
    # tree is a file the user never wrote and cannot explain.
    assert os.path.isfile(first["dir"] + ".complete")
    assert ".fused-snapshot-complete" not in os.listdir(first["dir"])
    # Prove the reuse rather than assert the path twice: a hand-edit of the
    # extracted tree survives a second call iff nothing re-extracted.
    with open(os.path.join(first["dir"], "a.md"), "w", encoding="utf-8") as f:
        f.write("touched\n")
    again = v.main(action="snapshot", file=str(repo / "docs"), sha=sha)
    assert again["dir"] == first["dir"]
    with open(os.path.join(again["dir"], "a.md"), encoding="utf-8") as f:
        assert f.read() == "touched\n"


def test_a_folder_with_a_page_previews_that_page_not_a_file_listing(
        workspace, tmp_path):
    # The gate admits a folder BECAUSE `entry_html` finds a top-level page, so
    # the snapshot has to resolve the page with the same predicate — otherwise
    # the very folders the gate lets in preview as a file listing of themselves,
    # which is what the user saw: "the history template does not render the
    # comfy.html file and instead shows me a file explorer in split view".
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "tool/readme.md", "docs\n", "Add docs")
    before_page = _shas(repo)[0]
    _commit(repo, "tool/tool.html", "<html>v1</html>", "Add the page")

    snap = v.main(action="snapshot", file=str(repo / "tool"), sha=_shas(repo)[0])
    assert os.path.basename(snap["entry"]) == "tool.html"
    assert snap["browse"] is None

    # Per-COMMIT, not per-target: the revision before the page existed has no
    # page to frame, so it browses. That is the whole reason the shape rides on
    # the snapshot payload rather than on the target's kind.
    older = v.main(action="snapshot", file=str(repo / "tool"), sha=before_page)
    assert older["entry"] is None
    assert older["browse"] == older["dir"]


def test_a_folder_snapshot_resolves_its_page_by_the_shared_rule(
        workspace, tmp_path):
    # Same rule as the app branch, and as the gate: index.html wins, and a
    # folder with no top-level page browses. One predicate across all of it, or
    # the gate offers a mode whose preview disagrees with why it was offered.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "site/zzz.html", "<html>z</html>", "Add zzz")
    _commit(repo, "site/index.html", "<html>i</html>", "Add index")
    snap = v.main(action="snapshot", file=str(repo / "site"), sha=_shas(repo)[0])
    assert os.path.basename(snap["entry"]) == "index.html"

    _commit(repo, "data/rows.csv", "a,b\n", "Add data")
    plain = v.main(action="snapshot", file=str(repo / "data"), sha=_shas(repo)[0])
    assert plain["entry"] is None and plain["browse"] == plain["dir"]


def test_a_snapshot_extracted_by_an_older_build_is_still_reused(
        workspace, tmp_path):
    # Cache compat: snapshots already on disk carry the marker INSIDE the tree.
    # Both locations are read and only the new one written, so an upgrade is not
    # a silent cache wipe (re-extracting every commit the user has ever opened).
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    sha = _commit(repo, "docs/a.md", "a1\n", "Add docs/a")
    first = v.main(action="snapshot", file=str(repo / "docs"), sha=sha)

    # Rewind to exactly what an older build left behind.
    os.remove(first["dir"] + ".complete")
    legacy = os.path.join(first["dir"], ".fused-snapshot-complete")
    with open(legacy, "w", encoding="utf-8") as f:
        f.write("whatever\n")
    with open(os.path.join(first["dir"], "a.md"), "w", encoding="utf-8") as f:
        f.write("touched\n")

    again = v.main(action="snapshot", file=str(repo / "docs"), sha=sha)
    assert again["dir"] == first["dir"]
    with open(os.path.join(again["dir"], "a.md"), encoding="utf-8") as f:
        assert f.read() == "touched\n"   # not re-extracted


def test_a_directory_and_a_file_snapshot_of_one_path_never_collide(
        workspace, tmp_path):
    # The cache key folds the pathspec in for every non-app kind, so a folder's
    # subtree snapshot and a file's one-file snapshot at the SAME commit land in
    # different trees. Sharing one would serve a folder listing where the file
    # was asked for (or the reverse), silently.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    sha = _commit(repo, "docs/a.md", "a1\n", "Add docs/a")
    d = v.main(action="snapshot", file=str(repo / "docs"), sha=sha)
    f = v.main(action="snapshot", file=str(repo / "docs" / "a.md"), sha=sha)
    assert d["dir"] != f["dir"]
    assert f["browse"] is None and f["file"].endswith("a.md")


def test_a_directory_outside_any_repository_is_still_refused(workspace, tmp_path):
    # Unchanged behaviour, and the reason the membership question is asked of
    # GIT rather than of workspace-relative path arithmetic.
    v = _load("history")
    plain = tmp_path / "just-a-folder"
    plain.mkdir()
    assert "error" in v.main(action="log", file=str(plain))


def test_an_app_directory_still_resolves_as_an_app(workspace):
    # App-ness is asked FIRST, or a workspace app would be demoted to a plain
    # directory target and silently lose `revert` — the timeline the auto-
    # commits actually produced is the app's, not a folder's.
    v = _load("history")
    d = _make_app(workspace)
    res = v.main(action="log", file=str(d))
    assert res["kind"] == "app"
    assert res["can_revert"] is True


def test_a_mount_backed_target_is_refused_before_git_is_asked(workspace, tmp_path):
    # Same refusal the gate makes, for the same reason: git over a kernel NFS
    # mount stats and lists its way through the work tree. The module holds the
    # line for a hand-crafted call, which the gate cannot.
    v = _load("history")
    mount = tmp_path / "home" / "mounts" / "remote"
    mount.mkdir(parents=True)
    _git(mount, "init", "-q")
    _commit(mount, "notes.md", "# one\n", "Add notes")
    for target in (str(mount), str(mount / "notes.md")):
        got = v.main(action="log", file=target)
        assert "error" in got and "mount" in got["error"]


def test_actions_refuse_paths_outside_apps(workspace, tmp_path):
    # Still all errors after D235, but for three different reasons now that a
    # file target is legal: `log` because the repo has no commits at all,
    # `snapshot` because the sha resolves to nothing, `revert` because it is
    # refused outright outside an app. Worth keeping as the composite: no action
    # may return a success payload for a path with no history behind it.
    v = _load("history")
    repo = tmp_path / "userrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    for action in ("log", "snapshot", "revert"):
        assert "error" in v.main(action=action, file=str(repo / "f.txt"),
                                 sha="deadbeef")


# --------------------------------------------------------------- snapshot

def test_snapshot_materialises_the_selected_commit(workspace):
    v = _load("history")
    d = _make_app(workspace)
    first = _shas(d)[0]
    (d / "index.html").write_text("<html>v2</html>")
    (d / "extra.py").write_text("x = 1")
    app_git.commit(str(d), "Add extra")
    res = v.main(action="snapshot", file=str(d / "index.html"), sha=first)
    assert res["entry"].endswith("index.html")
    with open(res["entry"], encoding="utf-8") as f:
        assert f.read() == "<html>v1</html>"
    assert not os.path.exists(os.path.join(res["dir"], "extra.py"))
    # Idempotent: a commit is immutable, the snapshot is reused.
    again = v.main(action="snapshot", file=str(d), sha=first[:7])
    assert again["dir"] == res["dir"]
    # Garbage sha never reaches git.
    assert "error" in v.main(action="snapshot", file=str(d), sha="; rm -rf /")
    assert "error" in v.main(action="snapshot", file=str(d), sha="a" * 40)


def test_snapshot_of_a_file_materialises_that_revision_only(workspace, tmp_path):
    # The reported `file` must be real extracted bytes of the SELECTED revision,
    # not the working copy — a preview that quietly shows "now" for every row in
    # the timeline is worse than no preview. `entry` stays None for a non-page so
    # the view frames it through the file's own default template instead of
    # handing a .md to /render as a document.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    first = _commit(repo, "notes.md", "# one\n", "Add notes")
    _commit(repo, "notes.md", "# two\n", "Edit notes")
    _commit(repo, "other.md", "other\n", "Add other")
    res = v.main(action="snapshot", file=str(repo / "notes.md"), sha=first)
    assert res["entry"] is None
    with open(res["file"], encoding="utf-8") as f:
        assert f.read() == "# one\n"
    # Only that file is archived: outside an app the surrounding directory is
    # the user's repository and could be enormous.
    assert set(os.listdir(res["dir"])) == {"notes.md"}
    # A revision that predates the file is a clean error, not an empty preview.
    older = v.main(action="snapshot", file=str(repo / "other.md"), sha=first)
    assert "error" in older


def test_snapshot_of_an_html_file_reports_it_as_the_entry(workspace, tmp_path):
    # A page CAN be served by /render directly, so `entry` must be set — with it
    # None the view would frame an .html through the html template instead of
    # rendering the historical page itself.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    sha = _commit(repo, "page.html", "<html>v1</html>", "Add page")
    res = v.main(action="snapshot", file=str(repo / "page.html"), sha=sha)
    assert res["entry"] == res["file"]
    with open(res["entry"], encoding="utf-8") as f:
        assert f.read() == "<html>v1</html>"


# ----------------------------------------------------------------- revert

def test_revert_adds_a_commit_and_restores_the_tree(workspace):
    v = _load("history")
    d = _make_app(workspace)
    first = _shas(d)[0]
    (d / "index.html").write_text("<html>v2</html>")
    (d / "extra.py").write_text("x = 1")
    app_git.commit(str(d), "Add extra")
    res = v.main(action="revert", file=str(d / "index.html"), sha=first)
    assert res.get("reverted") is True
    # Tree restored: edited file back, later file gone — via a NEW commit.
    assert (d / "index.html").read_text() == "<html>v1</html>"
    assert not (d / "extra.py").exists()
    subjects = _log_subjects(d)
    assert len(subjects) == 3 and subjects[0].startswith("Reverted to ")
    assert "New app from starter" in subjects[0]
    # History intact — the reverted-away commit is still reachable.
    assert len(_shas(d)) == 3
    # Reverting to HEAD is a no-op, not an empty commit.
    assert v.main(action="revert", file=str(d), sha=_shas(d)[0])["noop"] is True
    assert len(_shas(d)) == 3


def test_revert_is_refused_for_a_file_target(workspace, tmp_path):
    # The security boundary, not UI politeness: a revert records a commit with
    # the Fused identity and `read-tree -u --reset`s the working tree. Outside a
    # workspace app the repository is the USER'S, so a hand-crafted call must be
    # refused — and must leave the tree and the history byte-identical.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    first = _commit(repo, "notes.md", "# one\n", "Add notes")
    _commit(repo, "notes.md", "# two\n", "Edit notes")
    before = _shas(repo)
    (repo / "dirty.txt").write_text("uncommitted\n")
    res = v.main(action="revert", file=str(repo / "notes.md"), sha=first)
    assert "error" in res and "managed by you" in res["error"]
    # Provably untouched: no reset of the tracked file, no deletion of the file
    # added since `first`, and no new commit (nor any moved ref).
    assert (repo / "notes.md").read_text() == "# two\n"
    assert (repo / "dirty.txt").read_text() == "uncommitted\n"
    assert _shas(repo) == before
    assert _git(repo, "status", "--porcelain").stdout.strip() == "?? dirty.txt"


def test_same_tree_revert_preserves_uncommitted_edits(workspace):
    # A commit whose tree matches HEAD but whose sha differs (revert-of-a-
    # revert lands on a DIFFERENT commit with the SAME content as an earlier
    # one) must be reported a no-op WITHOUT ever running the destructive
    # working-tree reset — otherwise dirty, uncommitted edits would be
    # silently discarded to reach a tree that was already there.
    v = _load("history")
    d = _make_app(workspace)
    original = _shas(d)[0]  # v1
    (d / "index.html").write_text("<html>v2</html>")
    app_git.commit(str(d), "Edit index.html")
    # Revert to v1: lands on a NEW commit (c3) whose tree equals `original`'s.
    res = v.main(action="revert", file=str(d), sha=original)
    assert res.get("reverted") is True
    reverted_sha = _shas(d)[0]
    assert reverted_sha != original
    assert (d / "index.html").read_text() == "<html>v1</html>"
    # Dirty the working copy without committing.
    (d / "index.html").write_text("<html>UNCOMMITTED</html>")
    # Ask to revert to `original` again: different sha than HEAD (reverted_sha)
    # but an IDENTICAL tree — must noop without touching the dirty file.
    res = v.main(action="revert", file=str(d), sha=original)
    assert res.get("noop") is True
    assert (d / "index.html").read_text() == "<html>UNCOMMITTED</html>"
    assert _shas(d)[0] == reverted_sha  # no new commit, HEAD unmoved


# ------------------------------------------------------- narrow-host layout

# The narrow-layout breakpoint, in one place because four tests name it.
# 640 = #side 200 + divider 4 + a 420px preview (the "still a page" figure
# claude uses for its own framed column) = 624, rounded up for a scrollbar.
# Deliberately LOWER than claude's 800: the non-preview column here is a 200px
# commit spine, not a 440px chat, so the split stays useful in hosts claude's
# cannot survive. See test_the_breakpoint_is_the_useful_width_not_the_overflow_floor.
NARROW_PX = 640

def test_template_collapses_the_split_below_a_breakpoint():
    """D235 bound this mode to 47 file extensions, so it now renders in the
    explorer's listing preview pane (floor 220px, default width half its split
    container), in dragged Panel panes and in /embed. Without a media query the
    200px commit spine plus a divider plus the preview frame fight over ~220px
    and both halves become unusable slivers. Pinned as a source assertion because
    the fix is the shell-free one on purpose: the template adapts itself, so no
    per-template min-width lands in registry.json or the stat API and user
    templates (§16) get the behaviour for free."""
    html = _html()
    assert "@media (max-width: %dpx)" % NARROW_PX in html
    # The breakpoint must stay derivable from the layout's own arithmetic, not be
    # a magic number: #side's 200 + the 4px divider + a preview frame that is
    # still a page (420, the phone-viewport figure claude uses too) = 624.
    assert "min-width: 200px" in html          # #side, the 200 in that sum
    assert "width: 4px" in html                # #divider, the 4
    assert "624px floor" in html               # the arithmetic, in a comment


def test_the_breakpoint_is_the_useful_width_not_the_overflow_floor():
    """The regression this pins is the RAISE, from D236's original 560px.

    560 came from the hard floor (200 + 4 + a 320px frame = 524, rounded up) —
    the width below which the halves overflow. But the explorer's listing preview
    pane defaults to HALF its split container, about 700px on a 1700px window, so
    at 560 the split engaged in a pane that could hold it without overflowing and
    could not hold it usefully: a 320px frame is a viewport the framed template
    has already collapsed ITSELF for, i.e. a preview of some other template's
    narrow layout. 420 is the narrowest frame that is still a page (the same
    figure `claude` uses for its own framed column), so the useful-width floor
    is 200 + 4 + 420 = 624 → 640. That is deliberately LOWER than `claude`'s
    800px: its non-preview column is a 440px chat, ours a 200px commit spine,
    so the two templates no longer share a breakpoint.

    The CSS query and the JS matchMedia string are one breakpoint with two
    readers; a disagreement between them is a half-collapsed layout that only
    appears in a window a few pixels wide."""
    html = _html()
    assert "@media (max-width: %dpx)" % NARROW_PX in html
    assert 'matchMedia("(max-width: %dpx)")' % NARROW_PX in html
    assert "560px)" not in html                # no live 560 breakpoint survives
    doc = html[:html.index("@media (max-width: %dpx)" % NARROW_PX)]
    for term in ("200px", "4px", "420px", "624px", "640px"):
        assert term in doc, term


def test_narrow_layout_shows_one_view_and_drops_the_divider():
    """Below the breakpoint the two halves must not merely shrink: the divider
    has nothing left to resize (and a 4px drag target in a 220px pane is a
    misfeature), #main is hidden so the commit list owns the pane, and only the
    `narrow-preview` body class swaps in the snapshot. Every one of those rules
    lives INSIDE the media block — that is what makes crossing the breakpoint
    outward restore the split with no reload and no JS."""
    html = _html()
    block = html.split("@media (max-width: %dpx)" % NARROW_PX, 1)[1].split("\n  }", 1)[0]
    assert "#divider { display: none; }" in block
    assert "#main { order: 2; display: none; }" in block       # list is default
    assert "body.narrow-preview #commits { display: none; }" in block
    assert "body.narrow-preview #main { display: flex; }" in block
    # The toggle is the only way back, so it must live in the persistent
    # #side-head strip (which also carries Revert), not inside either view.
    head = html.split('id="side-head"', 1)[1].split("</div>", 1)[0]
    assert 'id="view-toggle"' in head
    assert 'id="revert"' in head
    # Hidden in the wide layout, revealed only by the media block.
    assert "#view-toggle { display: inline-block; }" in block


def test_narrow_layout_neutralises_the_inline_split_width():
    """applySplit() (and the divider drag) write an inline `width` onto #side
    from the shared `split` param. An inline style outranks any media query, so
    without `!important` the collapsed layout would still be pinned to 30% of a
    220px pane. `!important` is chosen over clearing/skipping the inline write so
    that applySplit() stays unconditional and the wide layout's width is still
    intact when the query stops matching — i.e. both directions work with no
    reload; the matchMedia listener only resets which view is showing."""
    html = _html()
    assert "sideEl.style.width" in html                        # the inline write
    assert "width: 100% !important" in html                    # beats it
    assert 'matchMedia("(max-width: %dpx)")' % NARROW_PX in html   # same query as CSS
    # Selecting a revision reveals the preview, but only from a real click:
    # loadLog() auto-selects the newest commit, so doing this inside select()
    # would open the narrow view on the preview and hide the timeline.
    click = html.split('commitsEl.addEventListener("click"', 1)[1].split("});", 1)[0]
    assert 'if (NARROW.matches) showNarrow("preview");' in click
    select_body = html.split("async function select(sha)", 1)[1].split(
        "async function loadLog", 1)[0]
    assert "showNarrow" not in select_body
    # And the load path opens on the list, never on an unvisited preview.
    assert 'showNarrow("list");' in html


def test_an_app_revision_with_no_page_is_browsable_instead_of_a_dead_end(
        workspace):
    # The dead end this removes: an app whose tree holds no html answered
    # `entry: None` and the view drew "this revision has no entry page —
    # nothing to render" over a tree full of perfectly viewable files. An app
    # whose page arrives in a LATER commit is the ordinary way to meet it, so
    # the early half of such a timeline was unviewable.
    v = _load("history")
    d = _make_app(workspace)
    (d / "index.html").unlink()
    (d / "notes.md").write_text("# not a page\n")
    app_git.commit(str(d), "Drop the page")

    snap = v.main(action="snapshot", file=str(d), sha=_shas(d)[0])
    assert "error" not in snap
    assert snap["entry"] is None
    assert snap["browse"] == snap["dir"]
    assert os.path.isfile(os.path.join(snap["dir"], "notes.md"))

    # ...and an entry-ful revision of the SAME app still renders its page.
    older = v.main(action="snapshot", file=str(d), sha=_shas(d)[-1])
    assert os.path.basename(older["entry"]) == "index.html"
    assert older["browse"] is None


def test_an_app_with_several_pages_and_no_index_renders_the_first(workspace):
    # The shared entry rule picks the first page in name order now (it used to
    # call several-without-an-index "ambiguous" and resolve to nothing), so this
    # revision renders a page rather than falling back to the browsable tree.
    v = _load("history")
    d = _make_app(workspace)
    (d / "index.html").unlink()
    for name in ("zzz.html", "about.html"):
        (d / name).write_text("<html></html>")
    app_git.commit(str(d), "Two pages, no index")

    snap = v.main(action="snapshot", file=str(d), sha=_shas(d)[0])
    assert os.path.basename(snap["entry"]) == "about.html"
    assert snap["browse"] is None


def test_a_revision_that_predates_the_folder_is_a_sentence_not_a_traceback(
        workspace, tmp_path):
    # `git archive` of a commit with nothing at this path is not an empty tar
    # this code can extract — it is a lone pax_global_header plus tar's EOF
    # blocks, and `tarfile.open` raises ReadError on it, which reaches the page
    # as the red /api/run traceback overlay. Latent while only apps had
    # snapshots; reachable the moment any directory could be a target.
    v = _load("history")
    repo = _plain_repo(workspace, tmp_path)
    first = _commit(repo, "README.md", "hi\n", "Add readme")
    _commit(repo, "docs/a.md", "a1\n", "Add docs/a")
    got = v.main(action="snapshot", file=str(repo / "docs"), sha=first)
    assert "error" in got and "nothing under this folder" in got["error"]


def test_a_directory_snapshot_is_framed_as_a_browsable_tree():
    # A folder snapshot is not a document, so it is framed through the shell's
    # chrome-free embed of the extracted tree — the user browses the folder as
    # it was, in the same column every other target previews in.
    src = _html()
    assert "snap.browse" in src
    assert "/explorer/embed/" in src
    # ...with the listing's OWN split pane suppressed (it is already inside a
    # preview column, and two previews deep is neither of them readable), and
    # under the frozen-tree framing, which drops the chrome that would act on a
    # snapshot as a live folder — the breadcrumb walking up into the cache's
    # internals, and the chips offering a chat on the extracted copy.
    assert "preview=false" in src
    assert "snapshot=1" in src
    # And nothing drops the preview column any more: the split is the one
    # layout, for all three kinds.
    assert "enterNoPreview" not in src
    assert "no-preview" not in src

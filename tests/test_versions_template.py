"""The `versions` template (app git history): the condition gate offers it
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
    "fused_render", "templates", "versions")


def _html():
    """The template's own source. The narrow-layout behaviour is CSS plus a few
    lines of matchMedia glue with no Python side at all, so source assertions
    are the only pin available short of a browser — the same trade the other
    template tests in this suite make."""
    with open(os.path.join(TEMPLATE_DIR, "template.html"), encoding="utf-8") as f:
        return f.read()


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("test_versions_" + name, path)
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
    # The app dir and everything real inside it. There is no longer an app-dir
    # RULE — the gate asks git whether the path is in a work tree, and an app is
    # a work tree — but the app case is still worth pinning, because it is the
    # one the app-builder view depends on (App.tsx APP_MODES).
    cond = _load("condition")
    d = _make_app(workspace)
    (d / "sub").mkdir()
    (d / "sub" / "x.py").write_text("x = 1\n")
    assert cond.main(str(d)) is True                     # the app dir itself
    assert cond.main(str(d / "index.html")) is True      # a file inside
    assert cond.main(str(d / "sub" / "x.py")) is True    # nested path
    assert cond.main(str(d / "sub")) is True             # nested directory
    # The workspace and its tag level are not repositories, so git says no —
    # which is now the only reason a path is refused.
    assert cond.main(str(workspace)) is False            # workspace root
    assert cond.main(str(workspace / "local")) is False  # tag level
    assert cond.main(str(tmp_path / "elsewhere")) is False
    # App-shaped folder without a repo: no history to show.
    plain = workspace / "local" / "plain"
    plain.mkdir(parents=True)
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


def test_condition_true_for_a_directory_inside_any_repo(workspace, tmp_path):
    # This used to be a refusal: folder-wide history outside a fused app was
    # said to belong to `git`, and two modes for one story was the thing to
    # avoid. `git` is the WORKING TREE view now and draws no history at all, so
    # there is no second story — a folder has a timeline like anything else,
    # wherever it lives. The mode is read-only there, enforced by versions.py
    # refusing `revert` rather than by hiding the view (MD-11).
    cond = _load("condition")
    repo = _plain_repo(workspace, tmp_path)
    _commit(repo, "sub/notes.md", "# one\n", "Add notes")
    assert cond.main(str(repo)) is True
    assert cond.main(str(repo / "sub")) is True


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
    v = _load("versions")
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
    v = _load("versions")
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
    v = _load("versions")
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
    v = _load("versions")
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
    # The gate offers `versions` on any path in a work tree, and this module had
    # no answer for the plain-directory half of that: it fell through the app
    # rule and refused with "not inside a fused app folder" while the mode sat
    # in the switcher offering history for the folder.
    v = _load("versions")
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


def test_a_plain_directory_is_read_only_and_has_nothing_to_frame(
        workspace, tmp_path):
    # Two different facts, reported separately: the repository is the user's own
    # (no revert, the Fused-identity rule), and a folder is not a document
    # /render can serve (no snapshot to frame). The second is why the view drops
    # its preview column for this kind rather than keeping an empty one.
    v = _load("versions")
    repo = _plain_repo(workspace, tmp_path)
    sha = _commit(repo, "docs/a.md", "a1\n", "Add docs/a")
    before = _shas(repo)

    res = v.main(action="log", file=str(repo / "docs"))
    assert res["can_revert"] is False
    assert res["can_snapshot"] is False

    # And refused at the module, not only hidden in the page (MD-11).
    snap = v.main(action="snapshot", file=str(repo / "docs"), sha=sha)
    assert "error" in snap
    rev = v.main(action="revert", file=str(repo / "docs"), sha=sha)
    assert "error" in rev and "managed by you" in rev["error"]
    assert _shas(repo) == before
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    # Nothing was extracted for a preview that was never going to appear.
    assert not os.path.isdir(os.path.join(str(tmp_path / "home"), "app-versions"))


def test_a_directory_outside_any_repository_is_still_refused(workspace, tmp_path):
    # Unchanged behaviour, and the reason the membership question is asked of
    # GIT rather than of workspace-relative path arithmetic.
    v = _load("versions")
    plain = tmp_path / "just-a-folder"
    plain.mkdir()
    assert "error" in v.main(action="log", file=str(plain))


def test_an_app_directory_still_resolves_as_an_app(workspace):
    # App-ness is asked FIRST, or a workspace app would be demoted to a plain
    # directory target and silently lose `revert` — the timeline the auto-
    # commits actually produced is the app's, not a folder's.
    v = _load("versions")
    d = _make_app(workspace)
    res = v.main(action="log", file=str(d))
    assert res["kind"] == "app"
    assert res["can_revert"] is True
    assert res["can_snapshot"] is True


def test_a_mount_backed_target_is_refused_before_git_is_asked(workspace, tmp_path):
    # Same refusal the gate makes, for the same reason: git over a kernel NFS
    # mount stats and lists its way through the work tree. The module holds the
    # line for a hand-crafted call, which the gate cannot.
    v = _load("versions")
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
    v = _load("versions")
    repo = tmp_path / "userrepo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    for action in ("log", "snapshot", "revert"):
        assert "error" in v.main(action=action, file=str(repo / "f.txt"),
                                 sha="deadbeef")


# --------------------------------------------------------------- snapshot

def test_snapshot_materialises_the_selected_commit(workspace):
    v = _load("versions")
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
    v = _load("versions")
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
    assert set(os.listdir(res["dir"])) == {"notes.md", ".fused-snapshot-complete"}
    # A revision that predates the file is a clean error, not an empty preview.
    older = v.main(action="snapshot", file=str(repo / "other.md"), sha=first)
    assert "error" in older


def test_snapshot_of_an_html_file_reports_it_as_the_entry(workspace, tmp_path):
    # A page CAN be served by /render directly, so `entry` must be set — with it
    # None the view would frame an .html through the html template instead of
    # rendering the historical page itself.
    v = _load("versions")
    repo = _plain_repo(workspace, tmp_path)
    sha = _commit(repo, "page.html", "<html>v1</html>", "Add page")
    res = v.main(action="snapshot", file=str(repo / "page.html"), sha=sha)
    assert res["entry"] == res["file"]
    with open(res["entry"], encoding="utf-8") as f:
        assert f.read() == "<html>v1</html>"


# ----------------------------------------------------------------- revert

def test_revert_adds_a_commit_and_restores_the_tree(workspace):
    v = _load("versions")
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
    v = _load("versions")
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
    v = _load("versions")
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
# Raised from D236's original 560px: see
# test_the_breakpoint_is_the_useful_width_not_the_overflow_floor.
NARROW_PX = 880

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
    # a magic number: #side's 200 + the 4px divider + a preview frame wide enough
    # that the FRAMED template still renders its own wide layout in it (640, the
    # `bundle` family number) = 844.
    assert "min-width: 200px" in html          # #side, the 200 in that sum
    assert "width: 4px" in html                # #divider, the 4
    assert "844px floor" in html               # the arithmetic, in a comment


def test_the_breakpoint_is_the_useful_width_not_the_overflow_floor():
    """The regression this pins is the RAISE, from D236's original 560px.

    560 came from the hard floor (200 + 4 + a 320px frame = 524, rounded up) —
    the width below which the halves overflow. But the explorer's listing preview
    pane defaults to HALF its split container, about 700px on a 1700px window, so
    at 560 the split engaged in a pane that could hold it without overflowing and
    could not hold it usefully: a 320px frame is a viewport the framed template
    has already collapsed ITSELF for, i.e. a preview of some other template's
    narrow layout. 640 is the narrowest frame that still shows a wide layout, so
    200 + 4 + 640 = 844 → 880 — the same figure `claude`, the other split-layout
    template, reaches from its own sum, so the two collapse together.

    The CSS query and the JS matchMedia string are one breakpoint with two
    readers; a disagreement between them is a half-collapsed layout that only
    appears in a window a few pixels wide."""
    html = _html()
    assert "@media (max-width: %dpx)" % NARROW_PX in html
    assert 'matchMedia("(max-width: %dpx)")' % NARROW_PX in html
    assert "560px)" not in html                # no live 560 breakpoint survives
    doc = html[:html.index("@media (max-width: %dpx)" % NARROW_PX)]
    for term in ("200px", "4px", "640px", "844px", "880px"):
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


def test_the_view_drops_its_preview_column_when_there_is_nothing_to_frame():
    # The layout answer for the third target kind, mirroring the chat template's
    # `enterNoPane`: the parts that describe a second column are REMOVED, not
    # hidden, so nothing left on the page implies a view that is not coming.
    src = _html()
    assert "can_snapshot === false" in src
    body = src[src.index("function enterNoPreview"):]
    body = body[: body.index("\n}")]
    for gone in ("main", "divider", "view-toggle"):
        assert '"' + gone + '"' in body
    # The narrow layout's one-view state must go with them: `narrow-preview`
    # hides the commit list to show a preview that no longer exists.
    assert 'classList.remove("narrow-preview")' in body
    # And the inline split width is beaten from CSS, the same way the narrow
    # block does it — applySplit stays unconditional and stateless.
    assert "body.no-preview #side { flex: 1; width: 100% !important" in src

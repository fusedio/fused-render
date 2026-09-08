"""Git status decoration for a directory listing (fused_render/server/git_status.py).

The explorer tints a row's NAME by what git says about that path — untracked,
modified, staged, conflicted — the way a file tree is expected to. Two halves
are pinned here and they need different kinds of test:

* the CODE TABLE and the folder roll-up are pure functions over porcelain
  records, so they are tested directly, including the codes a real repo is
  awkward to hold in one state (`DD`, `UU`, a stray `!!`);
* the wiring — that a real `git status` in a real repository actually produces
  those records, and that the listing endpoint carries them to the browser — is
  tested against `_git_repo.build_repo`, whose working tree is deliberately
  left modified + staged + untracked. A mocked subprocess here would only
  confirm our own fiction of porcelain output, which is the one thing worth
  doubting.
"""
import os

import pytest

from _git_repo import build_repo, git, git_available, write

from fused_render.server import git_status


# --------------------------------------------------------------- the code table

@pytest.mark.parametrize("code,expected", [
    ("??", "untracked"),
    (" M", "modified"),
    (" D", "modified"),
    ("MM", "modified"),   # staged AND dirty: the unstaged edit is the news
    ("AM", "modified"),
    ("M ", "staged"),
    ("A ", "staged"),
    ("D ", "staged"),
    ("UU", "conflicted"),
    ("AU", "conflicted"),
    ("UD", "conflicted"),
    ("AA", "conflicted"),
    ("DD", "conflicted"),
    ("  ", None),
    ("!!", None),         # never requested, and must not read as "modified"
    ("", None),
])
def test_classify(code, expected):
    assert git_status.classify(code) == expected


def test_status_order_covers_every_state_classify_can_return():
    """The roll-up ranking must rank everything `classify` can produce —
    otherwise a state reaches `entry_statuses` and raises on the rank lookup."""
    codes = ["??", " M", "M ", "UU", "!!", "  "]
    produced = {git_status.classify(c) for c in codes} - {None}
    assert produced == set(git_status.STATUS_ORDER)


# ------------------------------------------------------------------- the parser

def test_parse_porcelain_splits_nul_records():
    out = b"?? new.txt\x00 M edited.py\x00A  staged.txt\x00"
    assert git_status.parse_porcelain(out) == [
        ("??", "new.txt"),
        (" M", "edited.py"),
        ("A ", "staged.txt"),
    ]


def test_parse_porcelain_keeps_a_path_containing_a_newline():
    """The reason the format is `-z` and not line-based."""
    out = b"?? we\nird.txt\x00"
    assert git_status.parse_porcelain(out) == [("??", "we\nird.txt")]


def test_parse_porcelain_skips_malformed_chunks():
    out = b"\x00x\x00?? ok.txt\x00nope\x00"
    assert git_status.parse_porcelain(out) == [("??", "ok.txt")]


# ------------------------------------------------------- mapping and the rollup

def test_entry_statuses_maps_files_in_the_listed_folder():
    records = [("??", "new.txt"), (" M", "edited.py"), ("M ", "staged.py")]
    got = git_status.entry_statuses("", records, ["new.txt", "edited.py", "staged.py"])
    assert got == {"new.txt": "untracked", "edited.py": "modified",
                   "staged.py": "staged"}


def test_entry_statuses_is_relative_to_the_listed_folder():
    """Porcelain paths are repo-root relative wherever git ran, so a listing of
    a subdirectory has to strip its own prefix."""
    records = [(" M", "pkg/core.py"), ("??", "other/new.txt")]
    got = git_status.entry_statuses("pkg/", records, ["core.py", "notes.md"])
    assert got == {"core.py": "modified"}


def test_entry_statuses_rolls_a_subtree_up_to_its_folder():
    records = [("??", "pkg/deep/inner/new.txt")]
    got = git_status.entry_statuses("pkg/", records, ["deep"])
    assert got == {"deep": "untracked"}


def test_entry_statuses_folder_takes_the_most_urgent_state_beneath_it():
    """A folder holding both a staged file and an unresolved conflict must not
    advertise the calmer of the two — the point of the roll-up is that a closed
    folder cannot hide the thing you have to deal with."""
    records = [("M ", "pkg/a.py"), ("??", "pkg/b.py"), (" M", "pkg/c.py"),
               ("UU", "pkg/d.py")]
    assert git_status.entry_statuses("", records, ["pkg"]) == {"pkg": "conflicted"}

    records.remove(("UU", "pkg/d.py"))
    assert git_status.entry_statuses("", records, ["pkg"]) == {"pkg": "modified"}

    records.remove((" M", "pkg/c.py"))
    assert git_status.entry_statuses("", records, ["pkg"]) == {"pkg": "untracked"}

    records.remove(("??", "pkg/b.py"))
    assert git_status.entry_statuses("", records, ["pkg"]) == {"pkg": "staged"}


def test_entry_statuses_handles_a_wholly_untracked_directory_record():
    """git's default untracked mode collapses one to `dir/` — that trailing
    slash must credit the folder, not vanish."""
    records = [("??", "brand-new/")]
    assert git_status.entry_statuses("", records, ["brand-new"]) == \
        {"brand-new": "untracked"}


def test_entry_statuses_ignores_names_not_in_the_listing():
    records = [("??", "elsewhere.txt")]
    assert git_status.entry_statuses("", records, ["here.txt"]) == {}


def test_entry_statuses_ignores_the_listed_folders_own_record():
    """Looking INSIDE a wholly untracked tree, `sub/` can arrive as a record for
    the folder we are listing; stripping its prefix leaves nothing to name."""
    records = [("??", "sub/")]
    assert git_status.entry_statuses("sub/", records, ["a.txt"]) == {}


# ------------------------------------------------------------- against real git

pytestmark_git = pytest.mark.skipif(not git_available(), reason="git not installed")


@pytestmark_git
def test_listing_statuses_against_a_real_repository(tmp_path):
    root = str(tmp_path / "repo")
    build_repo(root)

    # Repo root: README.md is modified, pkg/ holds modified + staged + new.
    top = git_status.listing_statuses(root, ["README.md", "pkg", "assets"])
    assert top["README.md"] == "modified"
    assert top["pkg"] == "modified"  # the roll-up: core.py is dirty in there
    assert "assets" not in top       # clean subtree stays undecorated

    # Inside pkg/: each of the three states on its own row.
    inner = git_status.listing_statuses(
        os.path.join(root, "pkg"),
        ["core.py", "staged.txt", "fresh.txt", "notes.md"])
    assert inner == {"core.py": "modified", "staged.txt": "staged",
                     "fresh.txt": "untracked"}


@pytestmark_git
def test_listing_statuses_skips_a_gitignored_path(tmp_path):
    """Ignored entries already have their own dimming (server/gitignore.py) and
    must not also be tinted as untracked — `!!` never being requested is what
    guarantees it."""
    root = str(tmp_path / "repo")
    build_repo(root)
    write(root, ".gitignore", "junk/\n")
    os.makedirs(os.path.join(root, "junk"))
    write(root, "junk/build.log", "noise\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-q", "-m", "ignore junk")

    got = git_status.listing_statuses(root, ["junk", ".gitignore"])
    assert "junk" not in got


@pytestmark_git
def test_listing_statuses_is_empty_outside_a_repository(tmp_path):
    plain = str(tmp_path / "plain")
    os.makedirs(plain)
    write(plain, "a.txt", "hi\n")
    assert git_status.listing_statuses(plain, ["a.txt"]) == {}


@pytestmark_git
def test_listing_statuses_needs_no_names(tmp_path):
    """No rows, no spawn: the cheapest listing must not pay for git at all."""
    root = str(tmp_path / "repo")
    build_repo(root)
    assert git_status.listing_statuses(root, []) == {}


@pytestmark_git
def test_listing_statuses_survives_git_being_unusable(tmp_path, monkeypatch):
    """A tint is a display hint — a broken git costs the decoration, never the
    listing."""
    root = str(tmp_path / "repo")
    build_repo(root)
    monkeypatch.setattr(git_status, "_run_status", lambda cwd: None)
    assert git_status.listing_statuses(root, ["README.md"]) == {}


# ------------------------------------------------------- the endpoint's payload

@pytestmark_git
def test_list_endpoint_carries_the_git_field(tmp_path):
    """GET /api/fs/list is where the shell learns each row's state."""
    from fastapi.testclient import TestClient

    from fused_render.server import create_app

    root = tmp_path / "repo"
    build_repo(str(root))
    client = TestClient(create_app(start_dir=str(root)))

    pkg = client.get("/api/fs/list",
                     params={"path": str(root / "pkg")}).json()
    by_name = {e["name"]: e for e in pkg["entries"]}
    assert by_name["core.py"]["git"] == "modified"
    assert by_name["staged.txt"]["git"] == "staged"
    assert by_name["fresh.txt"]["git"] == "untracked"
    # Clean and committed: the field is ABSENT rather than null, so a shell that
    # has never heard of it sees exactly the payload it always saw.
    assert "git" not in by_name["notes.md"]


def test_list_endpoint_omits_git_outside_a_repository(tmp_path):
    """No git binary needed for this one: a plain folder has no state either
    way, and the field must not appear just because the code path ran."""
    from fastapi.testclient import TestClient

    from fused_render.server import create_app

    (tmp_path / "a.txt").write_text("hi\n", encoding="utf-8")
    data = TestClient(create_app(start_dir=str(tmp_path))).get(
        "/api/fs/list", params={"path": str(tmp_path)}).json()
    assert all("git" not in e for e in data["entries"])

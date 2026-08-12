"""The CODE in skills/fused-render-index/SKILL.md, checked rather than read.

The Python reader is copy-paste code: whatever it gets wrong propagates into every
app that follows the skill, and prose review cannot catch a path that resolves
to a directory which does not exist. So the block is extracted from the
markdown and run — its branch sanitization is checked against the real
``fused_render._branch.sanitize`` it has to match, and ``connect()`` is pointed
at the store shapes that made it raise. The JS example gets the two checks that
can be made statically: its cross-references, and the generation guard that
keeps a per-keystroke render honest.
"""
import os
import re
from pathlib import Path

import pytest

from fused_render._branch import sanitize

SKILL = (Path(__file__).resolve().parents[1] / "skills" / "fused-render-index"
         / "SKILL.md").read_text(encoding="utf-8")


def _reader():
    """Exec the ```python block under "The reader (copy this)"` and return it."""
    after = SKILL.split("### The reader (copy this)", 1)[1]
    code = after.split("```python", 1)[1].split("```", 1)[0]
    namespace = {}
    exec(compile(code, "SKILL.md:reader", "exec"), namespace)  # noqa: S102
    return namespace


# ------------------------------------------------------------- the skill itself

def test_the_frontmatter_description_stays_two_lines():
    """It is what the dispatcher routes on, not documentation — it was ~880
    characters of trigger list, which is a paragraph nobody reads."""
    description = SKILL.split("description:", 1)[1].split("\n", 1)[0].strip()
    assert len(description) < 420, len(description)


def test_the_skill_documents_only_the_two_bridge_methods():
    # The doc is where an author learns the surface, so a removed method left
    # documented is worse than one left implemented.
    # The old name survives in exactly one place, on purpose: the pitfall for a
    # reader who came looking for it.
    assert SKILL.count("fused.index.") == 1
    assert "Looking for `fused.index.*`" in SKILL
    for gone in ("stats(", "lookup(", "status(", "scan(", "cancel(", "config.",
                 "repos("):
        assert "fused.fileIndex." + gone not in SKILL, gone
    # And the routes behind them have to stay documented, since raw HTTP is now
    # the only way to reach them.
    for route in ("/api/index/scan", "/api/index/cancel", "/api/index/config",
                  "/api/git-repos", "/api/index/status"):
        assert route in SKILL, route


# --------------------------------------------------------------- the JS example

def test_every_section_cross_reference_names_a_section_that_exists():
    # Catches a pointer at a section that is not there. It cannot catch a
    # pointer at the WRONG existing section (the repos row said "section E",
    # the migration guide, instead of D) — that one is on review, and the
    # sections are lettered here so a rename at least fails loudly.
    letters = {m.group(1) for m in re.finditer(r"^## ([A-Z])\. ", SKILL, re.M)}
    for cited in re.findall(r"[Ss]ection ([A-Z])\b", SKILL):
        assert cited in letters, cited


def test_the_per_keystroke_example_guards_its_own_renders():
    """`fused.fileIndex.*` has no supersede channel (runPython's D114 is not it),
    so the copy-pasted example must not let a slower earlier query win."""
    example = SKILL.split("### The canonical shape", 1)[1].split("```", 2)[1]
    assert "let generation = 0;" in example
    assert "const mine = ++generation;" in example
    assert "if (mine !== generation) return;" in example
    # And the checklist has to say it in words, for a reader who writes their own.
    assert "not superseded like `runPython`" in SKILL


# ------------------------------------------------------------- branch scoping

# The refs that broke the unsanitized version: this very branch (truncated to
# _MAX_LEN), a default branch (baseline — no branches/ nesting at all), mixed
# case, and a ref whose 12-char cut lands on a separator.
_REFS = ["", "main", "MAIN", "master", "HEAD", "worktree-fused-index-api",
         "Worktree-Fused-Index-API", "feature/AB-123_fix", "a-very-long-branch-name",
         "abcdefghijk-more", "///", "Fix.The.Thing"]


@pytest.mark.parametrize("ref", _REFS)
def test_the_snippets_branch_ref_matches_the_real_sanitize(ref):
    assert _reader()["_branch_ref"](ref) == sanitize(ref), ref


@pytest.mark.parametrize("ref", _REFS)
def test_store_dir_resolves_where_the_server_actually_wrote(ref, tmp_path,
                                                            monkeypatch):
    from fused_render._branch import branch_dir

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path))
    monkeypatch.setenv("FUSED_RENDER_BRANCH", ref)
    expected = os.path.join(branch_dir(str(tmp_path), ref), "index")
    assert _reader()["store_dir"]() == expected, ref


def test_an_explicit_location_wins_over_the_env(tmp_path, monkeypatch):
    # The skill tells authors to pass the `location` /api/index/stats reports;
    # that path is the store dir itself, not a home to resolve again.
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "some-branch")
    assert _reader()["store_dir"](str(tmp_path)) == str(tmp_path)


# ---------------------------------------------------------- the empty store

pytest.importorskip("duckdb")


def _store(tmp_path, manifest, dirs=True, files=()):
    import json

    d = tmp_path / "index"
    (d / "files").mkdir(parents=True)
    (d / "partitions.json").write_text(json.dumps(manifest))
    if dirs or files:
        import duckdb

        con = duckdb.connect()
        for name in files:
            con.execute(
                f"COPY (SELECT '/a/b.txt' path, 3 size) TO '{d / 'files' / name}'")
        if dirs:
            con.execute(f"COPY (SELECT '/a' dir, 1 n_files) TO '{d / 'dirs.parquet'}'")
    return str(d)


def test_a_manifest_naming_zero_partitions_is_no_index_not_a_traceback(tmp_path):
    # store.py defaults a manifest to {"rows": 0, "partitions": []} and reads it
    # back defensively, so this is a real store shape — and read_parquet([])
    # raises InvalidInputException on it.
    location = _store(tmp_path, {"rows": 0, "partitions": [], "updated": None})
    assert _reader()["connect"](location) == (None, None)


def test_a_store_with_no_dirs_parquet_is_no_index_not_a_traceback(tmp_path):
    # git_repos._repos checks os.path.exists(cfg.dirs_parquet) separately from
    # read_manifest for exactly this state; read_parquet raises IOException.
    location = _store(tmp_path, {"rows": 1, "partitions": [{"file": "p0.parquet"}],
                                 "updated": 1}, dirs=False, files=["p0.parquet"])
    assert _reader()["connect"](location) == (None, None)


def test_a_missing_store_is_no_index(tmp_path):
    assert _reader()["connect"](str(tmp_path / "nope")) == (None, None)


def test_a_populated_store_still_reads_both_views(tmp_path):
    # The regression guard on the two returns above: they must not swallow a
    # store that IS there.
    location = _store(tmp_path, {"rows": 1, "partitions": [{"file": "p0.parquet"}],
                                 "updated": 1}, files=["p0.parquet"])
    con, manifest = _reader()["connect"](location)
    assert manifest["updated"] == 1
    assert con.execute("SELECT count(*) FROM files").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM dirs").fetchone()[0] == 1

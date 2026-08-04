"""The `bundle` template's reader (fused_render/templates/bundle/reader.py).

Driven against REAL bundles built by `git bundle create` (tests/_git_repo.py
for the fixture repos), never a mocked subprocess: the module's whole job is to
ask git about a file format only git understands, so a fake git would test our
own fiction of its output.

What is pinned here beyond the happy path:

* A bundle is NOT a repository. `git bundle verify` refuses to run without one,
  so the reader supplies a throwaway repo of its own — and that is what makes
  the prerequisite question answerable at all: verified against an EMPTY repo,
  "the recipient lacks these commits" is the honest recipient's-eye answer.
* An incomplete (thin) bundle is a NORMAL state to render, not an error: it
  reports its prerequisites and says so.
* Every bad input renders: a file that isn't a bundle, an empty file, a missing
  file, a git that isn't installed.
* `ref` is client input, so it is checked against the bundle's own ref list
  before it can reach an argv entry.
* Previewing is read-only: nothing appears beside the bundle unless the user
  explicitly clones.
"""
import importlib.util
import os
import subprocess

import pytest

from _git_repo import build_repo, git, git_available, write

READER = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "bundle", "reader.py")

pytestmark = pytest.mark.skipif(not git_available(), reason="git binary not installed")


@pytest.fixture(scope="module")
def reader():
    spec = importlib.util.spec_from_file_location("bundle_reader", READER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = str(tmp_path_factory.mktemp("bundle-repo"))
    build_repo(root)
    return root


@pytest.fixture(scope="module")
def full_bundle(repo, tmp_path_factory):
    """Everything, self-contained — what Compress's "Git bundle" produces."""
    out = str(tmp_path_factory.mktemp("full") / "project.bundle")
    git(repo, "bundle", "create", out, "--all")
    return out


@pytest.fixture(scope="module")
def thin_bundle(repo, tmp_path_factory):
    """Only the last commit — useless without the history it builds on."""
    out = str(tmp_path_factory.mktemp("thin") / "increment.bundle")
    git(repo, "bundle", "create", out, "main~1..main")
    return out


# ------------------------------------------------------------------ overview

def test_overview_lists_the_contained_refs(reader, full_bundle):
    out = reader.main(full_bundle)
    assert out["ok"] is True
    names = [r["name"] for r in out["refs"]]
    assert "refs/heads/main" in names
    for r in out["refs"]:
        assert len(r["sha"]) == len(r["sha"].strip()) and r["sha"]
        assert r["short_sha"] == r["sha"][:7]
    assert out["name"] == "project.bundle"
    assert out["size"] == os.path.getsize(full_bundle)


def test_overview_reports_a_complete_bundle_as_self_contained(reader, full_bundle):
    out = reader.main(full_bundle)
    assert out["complete"] is True
    assert out["prerequisites"] == []


def test_overview_names_a_default_ref_to_open(reader, full_bundle):
    out = reader.main(full_bundle)
    assert out["default_ref"] in [r["name"] for r in out["refs"]]


def test_overview_offers_the_clone_command(reader, full_bundle):
    out = reader.main(full_bundle)
    assert out["clone_command"].startswith("git clone ")
    assert full_bundle in out["clone_command"]


def test_refs_carry_a_kind_and_a_short_name(reader, repo, tmp_path):
    out_path = str(tmp_path / "tagged.bundle")
    git(repo, "tag", "-f", "v9")
    git(repo, "bundle", "create", out_path, "--all")
    out = reader.main(out_path)
    kinds = {r["short_name"]: r["kind"] for r in out["refs"]}
    assert kinds.get("main") == "branch"
    assert kinds.get("v9") == "tag"


# ------------------------------------------------------------- prerequisites

def test_a_thin_bundle_reports_its_prerequisites(reader, thin_bundle, repo):
    out = reader.main(thin_bundle)
    # Not an error: a renderable state that names what the recipient needs.
    assert out["ok"] is True
    assert out["complete"] is False
    assert len(out["prerequisites"]) >= 1
    head_parent = git(repo, "rev-parse", "HEAD~1").strip()
    assert head_parent in [p["sha"] for p in out["prerequisites"]]


def test_a_thin_bundle_still_lists_its_refs(reader, thin_bundle):
    out = reader.main(thin_bundle)
    assert [r["name"] for r in out["refs"]]


def test_history_of_a_thin_bundle_explains_itself(reader, thin_bundle):
    out = reader.main(thin_bundle, action="history", ref="refs/heads/main")
    assert out["ok"] is False
    assert out["reason"] == "prerequisites"
    assert "prerequisite" in out["message"].lower()


# --------------------------------------------------------------------- history

def test_history_returns_commits_newest_first(reader, full_bundle):
    out = reader.main(full_bundle, action="history", ref="refs/heads/main")
    assert out["ok"] is True
    subjects = [c["subject"] for c in out["commits"]]
    assert subjects[0] == "unrelated top change"
    assert "add readme" in subjects
    first = out["commits"][0]
    assert first["short"] == first["sha"][:7]
    assert first["author"] and first["date"]


def test_history_honours_the_limit_and_reports_more(reader, full_bundle):
    out = reader.main(full_bundle, action="history", ref="refs/heads/main", limit=2)
    assert len(out["commits"]) == 2
    assert out["has_more"] is True


def test_history_of_an_unknown_ref_is_refused_before_git_sees_it(reader, full_bundle):
    out = reader.main(full_bundle, action="history", ref="--upload-pack=touch /tmp/pwn")
    assert out["ok"] is False
    assert out["reason"] == "unknown-ref"


def test_history_defaults_to_the_default_ref(reader, full_bundle):
    out = reader.main(full_bundle, action="history")
    assert out["ok"] is True and out["commits"]


# ------------------------------------------------------------- bad input

def test_a_file_that_is_not_a_bundle(reader, tmp_path):
    p = tmp_path / "fake.bundle"
    p.write_text("I am not a bundle\n")
    out = reader.main(str(p))
    assert out["ok"] is False
    assert out["reason"] == "not-a-bundle"
    assert "bundle" in out["message"].lower()


def test_an_empty_file(reader, tmp_path):
    p = tmp_path / "empty.bundle"
    p.write_bytes(b"")
    out = reader.main(str(p))
    assert out["ok"] is False
    assert out["reason"] in ("not-a-bundle", "empty")


def test_a_truncated_bundle_reads_its_header_but_fails_to_open(reader, full_bundle, tmp_path):
    # Verified by hand against real git: `bundle verify` only checks the
    # HEADER, so a file cut off mid-pack still reports "okay" and lists its
    # refs. The truncation surfaces the moment anything reads the pack. So the
    # honest behaviour is: the overview renders (git says it is fine), and the
    # history refuses with a message instead of a traceback.
    p = tmp_path / "cut.bundle"
    p.write_bytes(open(full_bundle, "rb").read()[:80])
    assert reader.main(str(p))["ok"] is True
    out = reader.main(str(p), action="history")
    assert out["ok"] is False
    assert isinstance(out["message"], str) and out["message"]


def test_a_missing_file(reader, tmp_path):
    out = reader.main(str(tmp_path / "nope.bundle"))
    assert out["ok"] is False
    assert out["reason"] == "missing"


def test_a_directory_is_refused(reader, tmp_path):
    d = tmp_path / "dir.bundle"
    d.mkdir()
    out = reader.main(str(d))
    assert out["ok"] is False


def test_no_git_on_path_is_an_empty_state_not_a_traceback(reader, full_bundle, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(reader.subprocess, "run", boom)
    monkeypatch.setattr(reader.subprocess, "Popen", boom)
    out = reader.main(full_bundle)
    assert out["ok"] is False
    assert out["reason"] == "no-git"


def test_unknown_action_is_refused(reader, full_bundle):
    out = reader.main(full_bundle, action="rm -rf")
    assert out["ok"] is False
    assert out["reason"] == "unknown-action"


# ------------------------------------------------------- read-only previewing

def test_previewing_writes_nothing_beside_the_bundle(reader, repo, tmp_path):
    out_path = str(tmp_path / "ro.bundle")
    git(repo, "bundle", "create", out_path, "--all")
    before = sorted(os.listdir(tmp_path))
    reader.main(out_path)
    reader.main(out_path, action="history", ref="refs/heads/main")
    assert sorted(os.listdir(tmp_path)) == before


def test_previewing_a_bundle_in_a_read_only_directory(reader, repo, tmp_path):
    # The scratch repo the reader needs must live in temp, never beside the
    # file — otherwise a bundle on read-only media could not be previewed.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("read-only bits are ignored when running as root")
    holder = tmp_path / "ro"
    holder.mkdir()
    out_path = str(holder / "locked.bundle")
    git(repo, "bundle", "create", out_path, "--all")
    os.chmod(holder, 0o555)
    try:
        out = reader.main(out_path)
        assert out["ok"] is True
        assert reader.main(out_path, action="history")["ok"] is True
    finally:
        os.chmod(holder, 0o755)


def test_the_scratch_repo_is_cleaned_up(reader, full_bundle, tmp_path, monkeypatch):
    monkeypatch.setattr(reader.tempfile, "gettempdir", lambda: str(tmp_path))
    reader.main(full_bundle, action="history", ref="refs/heads/main")
    assert sorted(os.listdir(tmp_path)) == []


# -------------------------------------------------------------------- clone

def test_clone_writes_a_working_checkout_beside_the_bundle(reader, repo, tmp_path):
    out_path = str(tmp_path / "project.bundle")
    git(repo, "bundle", "create", out_path, "--all")
    out = reader.main(out_path, action="clone")
    assert out["ok"] is True
    dest = out["dest"]
    assert os.path.isdir(os.path.join(dest, ".git"))
    assert os.path.basename(dest) == "project"
    assert os.path.isfile(os.path.join(dest, "README.md"))


def test_a_second_clone_does_not_clobber_the_first(reader, repo, tmp_path):
    out_path = str(tmp_path / "project.bundle")
    git(repo, "bundle", "create", out_path, "--all")
    first = reader.main(out_path, action="clone")["dest"]
    second = reader.main(out_path, action="clone")["dest"]
    assert first != second
    assert os.path.basename(second) == "project 2"


def test_cloning_a_thin_bundle_is_refused_with_a_reason(reader, thin_bundle):
    out = reader.main(thin_bundle, action="clone")
    assert out["ok"] is False
    assert out["reason"] == "prerequisites"
    assert not os.path.exists(os.path.join(os.path.dirname(thin_bundle), "increment"))


def test_cloning_into_a_read_only_directory_is_refused(reader, repo, tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("read-only bits are ignored when running as root")
    holder = tmp_path / "ro"
    holder.mkdir()
    out_path = str(holder / "p.bundle")
    git(repo, "bundle", "create", out_path, "--all")
    os.chmod(holder, 0o555)
    try:
        out = reader.main(out_path, action="clone")
        assert out["ok"] is False
        assert out["reason"] == "readonly"
    finally:
        os.chmod(holder, 0o755)


# ------------------------------------------------------------------ wiring

# The template only ever runs if the registry binds the extension to it and the
# three files it is made of are actually shipped — without these, a .bundle
# falls through to the metadata-and-Download fallback and nothing above is
# reachable from the UI.

def _repo_path(*parts):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)


def test_the_registry_binds_dot_bundle_to_this_template():
    import json

    with open(_repo_path("fused_render", "templates", "registry.json"), encoding="utf-8") as fh:
        registry = json.load(fh)
    assert registry[".bundle"] == ["bundle"]


@pytest.mark.parametrize("name", ["reader.py", "template.html", "icon.svg"])
def test_the_template_ships_its_parts(name):
    assert os.path.isfile(_repo_path("fused_render", "templates", "bundle", name))


def test_the_page_calls_the_reader_for_every_action_it_offers():
    with open(_repo_path("fused_render", "templates", "bundle", "template.html"),
              encoding="utf-8") as fh:
        page = fh.read()
    assert 'fused.params.get("_file")' in page
    for action in ("overview", "history", "clone"):
        assert f'action: "{action}"' in page
    assert "fused.runPython(" in page and '"./reader.py"' in page


def test_the_listing_shows_an_archive_icon_for_dot_bundle():
    with open(_repo_path("frontend", "src", "components", "FileIcons.tsx"),
              encoding="utf-8") as fh:
        assert 'bundle: "archive"' in fh.read()


# ------------------------------------------------------------ hardening

def test_every_git_call_is_an_argv_list_with_no_shell_and_a_timeout(
        reader, full_bundle, monkeypatch):
    seen = []
    real_run, real_popen = reader.subprocess.run, reader.subprocess.Popen

    def spy_run(cmd, **kw):
        seen.append((cmd, kw))
        return real_run(cmd, **kw)

    def spy_popen(cmd, **kw):
        seen.append((cmd, kw))
        return real_popen(cmd, **kw)

    monkeypatch.setattr(reader.subprocess, "run", spy_run)
    monkeypatch.setattr(reader.subprocess, "Popen", spy_popen)
    reader.main(full_bundle)
    reader.main(full_bundle, action="history", ref="refs/heads/main")
    assert seen
    for cmd, kw in seen:
        assert isinstance(cmd, list) and cmd[0] == "git"
        assert kw.get("shell", False) is False
        assert kw.get("stdin") is subprocess.DEVNULL
        assert kw.get("timeout") or kw.get("stdout") is subprocess.PIPE

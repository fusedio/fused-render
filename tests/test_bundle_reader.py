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
import builtins
import importlib.util
import os
import subprocess
import sys

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


# ------------------------------------------------------- a chosen destination

# `dest` is the one piece of CLIENT input that decides where bytes land, so it
# is validated like any other write target: absolute, parent present and
# writable, never onto something that already exists, never onto a mount, and
# never inside the temp scratch tree this module rmtree's. The last two rules
# below are the load-bearing ones — the failure path deletes `dest`, so it must
# be impossible for `dest` to name anything the call did not create.

@pytest.fixture
def bundle(repo, tmp_path):
    out = str(tmp_path / "project.bundle")
    git(repo, "bundle", "create", out, "--all")
    return out


def test_clone_into_a_chosen_directory(reader, bundle, tmp_path):
    chosen = tmp_path / "elsewhere"
    chosen.mkdir()
    dest = str(chosen / "project")
    out = reader.main(bundle, action="clone", dest=dest)
    assert out["ok"] is True and out["dest"] == dest
    assert os.path.isfile(os.path.join(dest, "README.md"))


def test_clone_with_an_overridden_name(reader, bundle, tmp_path):
    dest = str(tmp_path / "my own name")
    out = reader.main(bundle, action="clone", dest=dest)
    assert out["ok"] is True
    assert out["name"] == "my own name"
    assert os.path.isdir(os.path.join(dest, ".git"))


def test_clone_still_derives_a_default_when_no_dest_is_given(reader, bundle, tmp_path):
    out = reader.main(bundle, action="clone")
    assert out["ok"] is True
    assert out["dest"] == str(tmp_path / "project")


@pytest.mark.parametrize("dest", ["   ", "relative/dir", "~/somewhere", 7])
def test_a_dest_that_is_not_an_absolute_path_is_refused(reader, bundle, dest):
    out = reader.main(bundle, action="clone", dest=dest)
    assert out["ok"] is False
    assert out["reason"] == "bad-dest"


@pytest.mark.parametrize("dest", ["", None])
def test_an_absent_dest_means_the_derived_default_not_a_refusal(reader, bundle, dest, tmp_path):
    # "" is the parameter's own default — an omitted `dest`, not a bad one — so
    # it keeps the pre-picker behaviour of deriving the sibling name.
    out = reader.main(bundle, action="clone", dest=dest)
    assert out["ok"] is True
    assert out["dest"] == str(tmp_path / "project")


def test_a_dest_that_already_exists_is_refused_and_left_alone(reader, bundle, tmp_path):
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "keep.txt").write_text("do not touch me\n")
    out = reader.main(bundle, action="clone", dest=str(taken))
    assert out["ok"] is False
    assert out["reason"] == "exists"
    assert (taken / "keep.txt").read_text() == "do not touch me\n"


def test_a_dest_whose_parent_is_missing_is_refused(reader, bundle, tmp_path):
    out = reader.main(bundle, action="clone", dest=str(tmp_path / "gone" / "here"))
    assert out["ok"] is False
    assert out["reason"] == "missing-parent"


def test_a_dest_whose_parent_is_a_file_is_refused(reader, bundle, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    out = reader.main(bundle, action="clone", dest=str(f / "under"))
    assert out["ok"] is False
    assert out["reason"] == "missing-parent"


def test_a_dest_in_a_read_only_parent_is_refused(reader, bundle, tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("read-only bits are ignored when running as root")
    holder = tmp_path / "ro"
    holder.mkdir()
    os.chmod(holder, 0o555)
    try:
        out = reader.main(bundle, action="clone", dest=str(holder / "here"))
        assert out["ok"] is False
        assert out["reason"] == "readonly"
    finally:
        os.chmod(holder, 0o755)


def test_a_mount_backed_dest_is_refused(reader, bundle, tmp_path, monkeypatch):
    # Same reasoning as /api/fs/compress: a clone's write pattern through the
    # rclone VFS is pathological, so it is refused rather than attempted.
    mnt = tmp_path / "mounts"
    mnt.mkdir()
    monkeypatch.setenv("FUSED_RENDER_MOUNTS_DIR", str(mnt))
    out = reader.main(bundle, action="clone", dest=str(mnt / "remote" / "here"))
    assert out["ok"] is False
    assert out["reason"] == "mount-unsupported"
    assert not (mnt / "remote").exists()


@pytest.fixture
def fresh_appenv():
    """Make the sys.path hop actually HAPPEN, whatever ran before in this worker.

    `appenv` is imported by NAME through a sys.path hop, so it lands in the
    process-global `sys.modules` under a name no package owns — and two other
    suites reach it through the STAGED core-templates copy under the test home
    (`test_app_git`'s agent commit turn, `test_canvas_template`'s mode order).
    Whichever ran first in this xdist worker wins the cache, and the assertion
    below then describes THEIR import rather than the reader's: the hop it is
    here to prove is short-circuited before it can be observed. Which suites
    share a worker is xdist's business and moves whenever tests are added, so
    this failed intermittently and on a different leg each time.

    Dropping the cached module and any staged `shared/` still on the path leaves
    the reader with no choice but to make the hop itself. Both go back
    afterwards: this isolates the test FROM the run, not the run from the test.
    """
    saved_mod = sys.modules.pop("appenv", None)
    saved_path = list(sys.path)
    sys.path[:] = [p for p in sys.path if ".core-templates" not in p]
    try:
        yield
    finally:
        sys.path[:] = saved_path
        if saved_mod is None:
            sys.modules.pop("appenv", None)
        else:
            sys.modules["appenv"] = saved_mod


def test_the_mount_check_reaches_shared_appenv_at_all(reader, bundle, tmp_path,
                                                      fresh_appenv):
    # `from appenv import is_mount_backed` (reader.py's _is_mount_backed) resolves
    # through a sys.path hop to `../shared`, not through a normal import, so a
    # type checker cannot see it and neither can a reader of the file. It is not
    # latent, though: a successful clone has to have gone through it, and the
    # module really is the shipped one next door.
    out = reader.main(bundle, action="clone", dest=str(tmp_path / "ok"))
    assert out["ok"] is True, out
    appenv = sys.modules["appenv"]
    assert os.path.realpath(appenv.__file__) == os.path.realpath(
        os.path.join(os.path.dirname(READER), "..", "shared", "appenv.py"))


def test_an_unreachable_appenv_refuses_the_clone_instead_of_crashing(
        reader, bundle, tmp_path, monkeypatch):
    # The fail-CLOSED branch, the one no happy path covers. A copy of the
    # template folder taken WITHOUT its `shared/` sibling (the degradation
    # test_template_appenv.py documents for the same helper) must produce the
    # readable "can't tell whether that folder is on a mount" refusal — not an
    # ImportError the page shows as a stuck spinner.
    real_import = builtins.__import__

    def no_appenv(name, *args, **kwargs):
        if name == "appenv":
            raise ImportError("no module named appenv")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "appenv", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_appenv)
    dest = tmp_path / "never"
    out = reader.main(bundle, action="clone", dest=str(dest))
    assert out["ok"] is False
    assert out["reason"] == "mount-unsupported"
    # The "cannot tell" wording specifically, not the "this IS a mount" wording —
    # tmp_path is not mount-backed, so a pass on the other message would mean the
    # test proved nothing about this branch.
    assert "Can't tell" in out["message"], out["message"]
    assert not dest.exists()


def test_a_dest_inside_the_scratch_tree_is_refused(reader, bundle, tmp_path, monkeypatch):
    # The scratch repo is rmtree'd when the call ends, so a clone placed inside
    # it would be deleted the moment it succeeded.
    scratch_root = tmp_path / "tmp"
    scratch_root.mkdir()
    monkeypatch.setattr(reader.tempfile, "gettempdir", lambda: str(scratch_root))
    seen = {}
    real_enter = reader._Scratch.__enter__

    def spy(self):
        seen["root"] = real_enter(self)
        return seen["root"]

    monkeypatch.setattr(reader._Scratch, "__enter__", spy)
    # Resolve the scratch path the same way the reader will, by running one
    # call first to learn it, then aiming the next clone inside it.
    reader.main(bundle, action="overview")
    out = reader.main(bundle, action="clone",
                      dest=os.path.join(seen["root"], "sneaky"))
    assert out["ok"] is False
    assert out["reason"] == "bad-dest"


def test_a_failed_clone_never_removes_a_directory_it_did_not_create(
        reader, bundle, tmp_path, monkeypatch):
    # The failure path rmtree's `dest`. If validation ever let a pre-existing
    # path through, that cleanup would delete the user's data — so the deletion
    # is guarded on this call having created the directory itself.
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "data.txt").write_text("irreplaceable\n")

    real_run = reader._run

    def fail_clone(args, cwd, **kw):
        if args and args[0] == "clone":
            # git "fails" without touching the destination at all.
            return "", "fatal: simulated failure", 128
        return real_run(args, cwd, **kw)

    monkeypatch.setattr(reader, "_run", fail_clone)
    out = reader.main(bundle, action="clone", dest=str(victim))
    assert out["ok"] is False
    assert (victim / "data.txt").read_text() == "irreplaceable\n"


def test_a_failed_clone_removes_the_partial_directory_it_did_create(
        reader, bundle, tmp_path, monkeypatch):
    dest = tmp_path / "half"
    real_run = reader._run

    def fail_clone(args, cwd, **kw):
        if args and args[0] == "clone":
            os.makedirs(os.path.join(str(dest), ".git"), exist_ok=True)
            return "", "fatal: simulated failure", 128
        return real_run(args, cwd, **kw)

    monkeypatch.setattr(reader, "_run", fail_clone)
    out = reader.main(bundle, action="clone", dest=str(dest))
    assert out["ok"] is False and out["reason"] == "git-failed"
    assert not dest.exists()


def test_clone_works_when_the_host_does_not_set_dunder_file(bundle, tmp_path):
    # The fused engine execs a reader with its own directory first on sys.path
    # but NO __file__. The mount check hops to `../shared` from this module's
    # location, so without rebuilding __file__ the first clone attempt died with
    # a bare NameError — which the page showed as a stuck "Cloning…". Found by
    # running the real app, so it gets a test that reproduces the host.
    src = open(READER, encoding="utf-8").read()
    # Annotated: a bare `{"__name__": "…"}` infers as dict[str, str], which makes
    # `module["main"]` a str and `module["main"](…)` unreadable as the call it is.
    # exec's globals is a namespace of arbitrary objects, so say so.
    module: dict[str, object] = {"__name__": "bundle_reader_no_file"}
    saved = list(sys.path)
    sys.path.insert(0, os.path.dirname(os.path.abspath(READER)))
    try:
        # Compiled under the REAL filename, as the engine does — the udf shim at
        # the bottom of the reader calls inspect.getsource(main), which needs the
        # code object to point at a file that exists. What the engine does NOT
        # give it is `__file__`, which is the whole point of this test.
        exec(compile(src, os.path.abspath(READER), "exec"), module)  # noqa: S102
        main = module["main"]
        assert callable(main), "the reader defines no main()"
        out = main(bundle, action="clone", dest=str(tmp_path / "engine-clone"))
    finally:
        sys.path[:] = saved
    assert out["ok"] is True, out
    assert os.path.isdir(str(tmp_path / "engine-clone" / ".git"))


def test_a_thin_bundle_is_refused_before_the_dest_is_touched(reader, thin_bundle, tmp_path):
    dest = tmp_path / "never"
    out = reader.main(thin_bundle, action="clone", dest=str(dest))
    assert out["ok"] is False and out["reason"] == "prerequisites"
    assert not dest.exists()


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


def test_the_page_asks_where_to_clone_through_the_shared_picker():
    # The picker is loaded from the /template-shared/ mount (never copied into
    # the template), and the folder it returns is what `dest` is built from —
    # without that the reader would silently fall back to its own sibling.
    page = _repo_path("fused_render", "templates", "bundle", "template.html")
    with open(page, encoding="utf-8") as fh:
        html = fh.read()
    assert '<script src="/template-shared/folder-picker.js">' in html
    assert "fusedFolderPicker.open(" in html
    assert "dest: choice.path" in html


def test_the_shared_picker_ships_and_exports_its_api():
    js_path = _repo_path("fused_render", "templates", "shared", "folder-picker.js")
    assert os.path.isfile(js_path)
    with open(js_path, encoding="utf-8") as fh:
        js = fh.read()
    assert "window.fusedFolderPicker" in js
    # Listing goes through the server, never a local scan — the rule that keeps
    # a mount-backed directory from being walked by the kernel.
    assert "/api/fs/list" in js


def test_the_listing_shows_an_archive_icon_for_dot_bundle():
    with open(_repo_path("frontend", "src", "platform", "ui", "FileIcons.tsx"),
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
        # argv[0] is the ABSOLUTE git path, and the absoluteness is the
        # point: CPython only reaches posix_spawn when
        # os.path.dirname(executable) is truthy, and a fork with libproj
        # resident SIGSEGVs before exec (tests/test_git_posix_spawn.py).
        # basename alone would pass a regression to a bare "git".
        assert isinstance(cmd, list)
        assert os.path.isabs(cmd[0])
        assert os.path.basename(cmd[0]) in ("git", "git.exe")
        # The other two thirds of the same rule — this module forked until
        # they were added, and this test passed the whole time.
        assert kw.get("close_fds") is False
        assert kw.get("cwd") is None
        assert kw.get("shell", False) is False
        assert kw.get("stdin") is subprocess.DEVNULL
        assert kw.get("timeout") or kw.get("stdout") is subprocess.PIPE

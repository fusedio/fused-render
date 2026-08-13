"""Tests for first-run onboarding (fused_render/shell/seed.py, D81): the
~/Documents/Fused workspace and its seeded examples.

Examples land under <fused_dir>/examples/<name>/ (not loose at the workspace
root) so they carry the "examples" tag in the Home apps grid, same as any
other <fused_dir>/<tag>/<project> folder.

FUSED_RENDER_DIR (the Fused dir) is redirected to a tmp dir so no test touches
a real dir.
"""
from fused_render.shell.seed import ensure_fused_dir


def _setup(tmp_path, monkeypatch):
    fdir = tmp_path / "Documents" / "Fused"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


# Every project folder the packaged seed ships (dot-metadata like .DS_Store on
# a dev machine is skipped by the seeder and must never land).
SEED_DIRS = ["ai_demo", "how_it_works", "sine", "tutorial"]


def test_seeds_examples_into_empty_dir(tmp_path, monkeypatch):
    fdir = _setup(tmp_path, monkeypatch)
    returned = ensure_fused_dir()

    assert returned == str(fdir)
    examples = fdir / "examples"
    # The packaged seed files land under examples/, each inside its own
    # subfolder — nothing loose at the workspace root.
    assert (examples / "sine" / "sine.html").is_file()
    assert (examples / "sine" / "sine.py").is_file()
    assert (examples / "ai_demo" / "ai_demo.html").is_file()
    assert (examples / "ai_demo" / "data.py").is_file()
    assert (examples / "how_it_works" / "demo.py").is_file()
    assert (examples / "how_it_works" / "explainer.html").is_file()
    assert (examples / "tutorial" / "index.html").is_file()
    assert (examples / "tutorial" / "hello.py").is_file()
    # Nothing spilled to the root: only the "examples" tag folder exists.
    assert sorted(p.name for p in fdir.iterdir()) == ["examples"]
    assert sorted(p.name for p in examples.iterdir()) == SEED_DIRS


def test_non_empty_dir_is_left_untouched(tmp_path, monkeypatch):
    fdir = _setup(tmp_path, monkeypatch)
    fdir.mkdir(parents=True)
    (fdir / "my_work.html").write_text("mine", encoding="utf-8")

    ensure_fused_dir()

    # Existing content preserved; no examples copied in over a user's own dir.
    assert (fdir / "my_work.html").read_text(encoding="utf-8") == "mine"
    assert not (fdir / "examples").exists()


def test_dir_with_only_ds_store_still_seeds(tmp_path, monkeypatch):
    # macOS drops .DS_Store into ~/Documents/Fused as soon as Finder looks at
    # it; hidden metadata must not count as user content blocking the seed.
    fdir = _setup(tmp_path, monkeypatch)
    fdir.mkdir(parents=True)
    (fdir / ".DS_Store").write_bytes(b"\x00")

    ensure_fused_dir()

    assert (fdir / "examples" / "sine" / "sine.html").is_file()
    assert (fdir / "examples" / "how_it_works" / "explainer.html").is_file()
    # The hidden file survives — seeding never deletes anything.
    assert (fdir / ".DS_Store").read_bytes() == b"\x00"


def test_partial_seed_leftover_is_cleaned_and_reseeded(tmp_path, monkeypatch):
    # An interrupted first run can strand a hidden ".examples.partial" staging
    # dir and leave the real examples/ missing. The next start must clear the
    # leftover and complete seeding (the partial must not wedge seeding off
    # forever).
    fdir = _setup(tmp_path, monkeypatch)
    fdir.mkdir(parents=True)
    partial = fdir / ".examples.partial"
    (partial / "sine").mkdir(parents=True)
    (partial / "sine" / "sine.html").write_text("half-copied", encoding="utf-8")

    ensure_fused_dir()

    # Leftover gone; examples fully (re)seeded; nothing else at the root.
    assert not partial.exists()
    assert (fdir / "examples" / "sine" / "sine.html").is_file()
    assert sorted(p.name for p in fdir.iterdir()) == ["examples"]


def test_idempotent_second_run_is_noop(tmp_path, monkeypatch):
    fdir = _setup(tmp_path, monkeypatch)
    ensure_fused_dir()

    # User edits a seeded example; a second startup must not re-seed or reset it.
    (fdir / "examples" / "sine" / "sine.html").write_text("edited", encoding="utf-8")
    ensure_fused_dir()

    assert (fdir / "examples" / "sine" / "sine.html").read_text(encoding="utf-8") == "edited"

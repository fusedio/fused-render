from scripts.hatch_build import _write_baked_ref


def test_bake_writes_and_registers_when_ref_set(tmp_path):
    (tmp_path / "fused_render").mkdir()
    build_data = {}
    _write_baked_ref(str(tmp_path), "foo", build_data)

    baked = tmp_path / "fused_render" / "_baked_branch.py"
    assert baked.read_text() == '_BAKED_REF = "foo"\n'
    assert build_data["artifacts"] == ["fused_render/_baked_branch.py"]


def test_bake_removes_stale_file_on_baseline(tmp_path):
    """A baseline build must clear a baked ref left by an earlier branch build,
    else _baked_ref() keeps loading it when FUSED_RENDER_BRANCH is unset.
    """
    fr = tmp_path / "fused_render"
    fr.mkdir()
    stale = fr / "_baked_branch.py"
    stale.write_text('_BAKED_REF = "old-branch"\n')

    build_data = {}
    _write_baked_ref(str(tmp_path), "", build_data)

    assert not stale.exists()
    assert "artifacts" not in build_data


def test_bake_baseline_noop_when_no_stale_file(tmp_path):
    (tmp_path / "fused_render").mkdir()
    build_data = {}
    _write_baked_ref(str(tmp_path), "", build_data)

    assert not (tmp_path / "fused_render" / "_baked_branch.py").exists()
    assert "artifacts" not in build_data

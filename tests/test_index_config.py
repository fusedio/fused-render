"""IndexConfig — the de-globalized engine configuration.

OpenIndex resolved the index dir and ignore list ONCE at import; the port must
re-resolve them per call, or a server that outlives an edit (or a test that
redirects FUSED_RENDER_HOME) keeps writing to the previous location.
"""
import json
import os

from fused_render.index import config as index_config
from fused_render.index.config import IndexConfig, load_config, save_config


def test_index_dir_follows_the_current_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "one"))
    first = load_config().dir
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "two"))
    second = load_config().dir
    assert first == str(tmp_path / "one" / "index")
    assert second == str(tmp_path / "two" / "index")


def test_index_dir_nests_under_a_branch(monkeypatch, tmp_path):
    from fused_render import _branch

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "featureX")
    # the ref is resolved once per process (_branch._CACHED_REF); clear it so
    # this test sees its own env rather than whatever imported first
    monkeypatch.setattr(_branch, "_CACHED_REF", None)
    assert load_config().dir == str(
        tmp_path / "home" / "branches" / "featurex" / "index")


def test_no_module_level_configuration_survives_import(monkeypatch, tmp_path):
    """A guard against the exact regression the port exists to prevent: the
    module must hold no frozen INDEX_DIR / IGNORE / NPROC."""
    for name in ("INDEX_DIR", "FILES_DIR", "IGNORE", "NPROC", "DIRS_PARQUET"):
        assert not hasattr(index_config, name)


def test_layout_paths_all_hang_off_the_configured_dir(tmp_path):
    cfg = IndexConfig(dir=str(tmp_path / "ix"))
    for p in (cfg.files_dir, cfg.dirs_parquet, cfg.partitions_json,
              cfg.fsevents_json, cfg.applied_ignore_json, cfg.config_json,
              cfg.runs_dir, cfg.scans_json):
        assert p.startswith(str(tmp_path / "ix") + os.sep)


def test_save_and_load_round_trips_ignore_and_roots(tmp_path):
    cfg = IndexConfig(dir=str(tmp_path / "ix"))
    cfg.ignore = ["node_modules", "  ", "node_modules", "#c"]
    cfg.roots = [str(tmp_path / "proj")]
    saved = save_config(cfg)
    assert saved.ignore == ["node_modules"]  # cleaned on the way in
    assert saved.roots == [str(tmp_path / "proj")]
    assert load_config(str(tmp_path / "ix")).ignore == ["node_modules"]


def test_corrupt_config_falls_back_to_defaults(tmp_path):
    d = tmp_path / "ix"
    d.mkdir()
    (d / "config.json").write_text("{not json", encoding="utf-8")
    cfg = load_config(str(d))
    assert "node_modules" in cfg.ignore
    assert cfg.roots == []


def test_to_dict_from_dict_round_trip_carries_the_store_location(tmp_path):
    cfg = IndexConfig(dir=str(tmp_path / "ix"), ignore=["a"], nproc=3)
    clone = IndexConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert clone.dir == cfg.dir
    assert clone.ignore == ["a"]
    assert clone.nproc == 3
    assert clone.rules.is_ignored("/x/a")


def test_rules_recompile_when_the_ignore_list_changes(tmp_path):
    # No cache reset at the call site: assigning `ignore` is the whole edit.
    cfg = IndexConfig(dir=str(tmp_path / "ix"), ignore=["a"])
    assert cfg.rules.is_ignored("/x/a")
    cfg.ignore = ["b"]
    assert not cfg.rules.is_ignored("/x/a")
    assert cfg.rules.is_ignored("/x/b")


def test_rules_recompile_when_the_ignore_list_is_mutated_in_place(tmp_path):
    cfg = IndexConfig(dir=str(tmp_path / "ix"), ignore=["a"])
    assert not cfg.rules.is_ignored("/x/b")
    cfg.ignore.append("b")
    assert cfg.rules.is_ignored("/x/b")


def test_unchanged_rules_are_compiled_once(tmp_path):
    cfg = IndexConfig(dir=str(tmp_path / "ix"), ignore=["a"])
    assert cfg.rules is cfg.rules

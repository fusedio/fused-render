"""The plugin contract: an `IndexKind` is a row shape plus one `extract`
function, registered by name. See fused_render/index/specs/index-plugins.md.

Host owns the walk, plugin owns the row: an `IndexKind` never scans a
directory tree itself, so nothing here exercises a filesystem walk — only
the shape (columns, extract signature) and the registry (register/get/
duplicate-rejection) a walker would call into.
"""
import pyarrow as pa
import pytest

from fused_render.index.kinds import Column, IndexKind, get, register, registered


def _noop_extract(path, st):
    return None


def test_column_rejects_an_unknown_type():
    with pytest.raises(ValueError):
        Column("size", "not-a-real-type")


def test_kind_rejects_duplicate_column_names():
    with pytest.raises(ValueError):
        IndexKind(
            name="dup",
            columns=(Column("name", "string"), Column("name", "string")),
            extract=_noop_extract,
            text_column="name",
        )


def test_kind_rejects_a_text_column_not_among_its_columns():
    with pytest.raises(ValueError):
        IndexKind(
            name="bad-text-col",
            columns=(Column("name", "string"),),
            extract=_noop_extract,
            text_column="title",
        )


def test_kind_builds_a_pyarrow_schema_from_its_columns():
    kind = IndexKind(
        name="widgets",
        columns=(
            Column("name", "string"),
            Column("size", "int64"),
            Column("depth", "int32"),
            Column("mtime", "float64"),
        ),
        extract=_noop_extract,
        text_column="name",
    )
    schema = kind.pa_schema(pa)
    assert schema.names == ["name", "size", "depth", "mtime"]
    assert schema.field("name").type == pa.string()
    assert schema.field("size").type == pa.int64()
    assert schema.field("depth").type == pa.int32()
    assert schema.field("mtime").type == pa.float64()


def test_register_and_get_round_trip():
    kind = IndexKind(
        name="_test_roundtrip",
        columns=(Column("name", "string"),),
        extract=_noop_extract,
        text_column="name",
    )
    register(kind)
    try:
        assert get("_test_roundtrip") is kind
        assert "_test_roundtrip" in registered()
    finally:
        # No unregister exists yet (nothing needs one); keep the registry
        # clean for other tests by replacing rather than leaking state.
        register(kind, replace=True)


def test_register_rejects_a_duplicate_name_by_default():
    kind = IndexKind(
        name="_test_dup",
        columns=(Column("name", "string"),),
        extract=_noop_extract,
        text_column="name",
    )
    register(kind)
    try:
        with pytest.raises(ValueError):
            register(kind)
    finally:
        register(kind, replace=True)


def test_register_replace_true_overwrites_silently():
    kind_a = IndexKind(
        name="_test_replace",
        columns=(Column("name", "string"),),
        extract=_noop_extract,
        text_column="name",
    )
    kind_b = IndexKind(
        name="_test_replace",
        columns=(Column("name", "string"), Column("size", "int64")),
        extract=_noop_extract,
        text_column="name",
    )
    register(kind_a)
    register(kind_b, replace=True)
    assert get("_test_replace") is kind_b


def test_get_raises_a_clear_error_for_an_unknown_kind():
    with pytest.raises(KeyError):
        get("_no_such_kind_ever_registered")


def test_registered_lists_known_kinds_sorted():
    kind = IndexKind(
        name="_test_sorted_b",
        columns=(Column("name", "string"),),
        extract=_noop_extract,
        text_column="name",
    )
    register(kind)
    try:
        names = registered()
        assert names == sorted(names)
        assert "_test_sorted_b" in names
    finally:
        register(kind, replace=True)

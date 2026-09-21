"""share_file_rules.resolve() against fixture rule dicts shaped like the real
catalog (see udfs/files/*/README.MD's `<!--fused:filePreview-->` blocks). No
network, no SDK — `resolve()` is pure. Locks in the four resolutions a reader
will question (SPEC artifact-fileudf.md §3) plus the ordering edge cases."""
from __future__ import annotations

import sys

from fused_render import share_file_rules as rules_mod


def rule(name, extensions=(), order=None, file_name=None, regex=None):
    return {
        "name": name,
        "token": f"UDF_{name}",
        "extensions": list(extensions),
        "file_name": file_name,
        "regex": regex,
        "order": order,
    }


CATALOG = [
    rule("DuckDB_Parquet", ["parquet", "pq", "gpq", "geoparquet"], order=1),
    rule("Metadata_Parquet", ["parquet", "pq", "gpq", "geoparquet"], order=2),
    rule("Pandas_Parquet", ["parquet", "pq", "gpq", "geoparquet"], order=2),
    rule("Preview_Parquet", ["parquet", "pq", "gpq", "geoparquet"], order=None),
    rule("Pandas_CSV", ["csv", "tsv"], order=1),
    rule("DuckDB_CSV", ["csv", "tsv"], order=2),
    rule("Text_File", ["md", "txt", "json", "geojson", "csv", "gpx", "kml", "tsv"], order=2),
    rule("Markdown_File", ["md", "markdown", "mdown", "mkd"], order=1),
    rule("DuckDB_JSON", ["json", "geojson"], order=3),
    rule("GeoPandas_GPX", ["gpx"], order=None),
    rule("GeoPandas_KML", ["kml"], order=None),
    rule("Empty_Extension_File", [""], order=0),
]


def test_parquet_resolves_to_duckdb_parquet():
    got = rules_mod.resolve("s3://bucket/data.parquet", CATALOG)
    assert got["name"] == "DuckDB_Parquet"


def test_csv_resolves_to_pandas_csv():
    got = rules_mod.resolve("/local/report.csv", CATALOG)
    assert got["name"] == "Pandas_CSV"


def test_md_resolves_to_markdown_file():
    got = rules_mod.resolve("/local/notes.md", CATALOG)
    assert got["name"] == "Markdown_File"


def test_json_resolves_to_text_file_not_duckdb_json():
    # Text_File's Menu order 2 beats DuckDB_JSON's 3.
    got = rules_mod.resolve("/local/data.json", CATALOG)
    assert got["name"] == "Text_File"


def test_unordered_rule_loses_to_an_ordered_one():
    got = rules_mod.resolve("/local/track.gpx", CATALOG)
    assert got["name"] == "Text_File"
    assert got["name"] != "GeoPandas_GPX"


def test_empty_extension_entry_matches_only_a_path_with_no_extension():
    got = rules_mod.resolve("/local/Makefile", CATALOG)
    assert got["name"] == "Empty_Extension_File"
    # A real extension must not accidentally satisfy an empty-string rule.
    got2 = rules_mod.resolve("/local/data.parquet", CATALOG)
    assert got2["name"] != "Empty_Extension_File"


def test_no_match_returns_none():
    assert rules_mod.resolve("/local/model.ipynb", CATALOG) is None


def test_file_name_constraint_beats_extension_match():
    catalog = CATALOG + [rule("Fused_Geopartitioned_Table", [], order=1, file_name="_sample")]
    got = rules_mod.resolve("/local/tables/_sample", catalog)
    assert got["name"] == "Fused_Geopartitioned_Table"


def test_regex_constraint_beats_extension_match():
    catalog = CATALOG + [
        rule("Special_Report", [], order=99, regex=r"report_\d+\.csv$"),
    ]
    got = rules_mod.resolve("/local/report_42.csv", catalog)
    assert got["name"] == "Special_Report"
    # A csv NOT matching the regex still falls through to the ordinary rules.
    got2 = rules_mod.resolve("/local/other.csv", catalog)
    assert got2["name"] == "Pandas_CSV"


def test_ties_at_equal_order_break_on_name():
    got = rules_mod.resolve("/local/data.tsv", CATALOG)
    # Pandas_CSV order=1 wins outright over DuckDB_CSV/Text_File order=2.
    assert got["name"] == "Pandas_CSV"


def test_regex_matches_the_basename_not_the_whole_path():
    # Finding 5: a regex must not fire because of the PARENT DIRECTORY name.
    # "census" is nowhere in the file name itself.
    catalog = CATALOG + [rule("Census_Viewer", [], order=1, regex=r"census")]
    got = rules_mod.resolve("/Users/me/census_data/notes.txt", catalog)
    assert got["name"] != "Census_Viewer"
    assert got["name"] == "Text_File"

    # The same regex DOES still win when the match is actually in the
    # basename — the predicate is not simply disabled.
    got2 = rules_mod.resolve("/Users/me/other_dir/census.txt", catalog)
    assert got2["name"] == "Census_Viewer"


def test_specificity_is_scored_by_what_matched_this_path_not_by_declaration():
    # Finding 6: General_Report declares BOTH a regex and extensions, but for
    # a plain "other.csv" its regex never matches — it must not outrank a
    # purpose-built, lower-`order` extension rule just because it "has" a
    # regex. Pandas_CSV (order=1, extension-only) must still win.
    catalog = CATALOG + [
        rule("General_Report", ["csv"], order=5, regex=r"report_\d+\.csv$"),
    ]
    got = rules_mod.resolve("/local/other.csv", catalog)
    assert got["name"] == "Pandas_CSV"

    # But when the regex DOES match this path, General_Report's specificity
    # correctly outranks the plain extension rules.
    got2 = rules_mod.resolve("/local/report_7.csv", catalog)
    assert got2["name"] == "General_Report"


def test_builtin_fused_rule_appended_only_when_uncovered():
    covered = rules_mod._with_builtin(CATALOG)  # 'fused' extension not in CATALOG
    names = [r["name"] for r in covered if "fused" in (r.get("extensions") or [])]
    assert names == ["Fused_App_File"]

    catalog_with_override = CATALOG + [rule("Custom_Fused_Viewer", ["fused"], order=1)]
    covered2 = rules_mod._with_builtin(catalog_with_override)
    fused_rules = [r["name"] for r in covered2 if "fused" in (r.get("extensions") or [])]
    assert fused_rules == ["Custom_Fused_Viewer"]


# ---------------------------------------------------------------------------
# build_rules() against the REAL SDK shape: `fused.api.get_udfs(whose=...)`
# returns a `UdfRegistry` — dict-like, `str -> Udf`, metadata on the Udf as
# an ATTRIBUTE. A fixture that hands `build_rules` a list of plain dicts
# does not exercise this at all (round 2: `dict(record)` on a bare name
# string raised `dictionary update sequence element #0 has length 1; 2 is
# required`, and even patched past that, `record.get("metadata")` silently
# missed the real attribute). These fixtures are built from a live catalog
# dump (`default/Markdown_File`, `default/Text_File`,
# `default/Fused_Geopartitioned_Table`) so they cannot drift back to a shape
# the SDK doesn't actually return.
# ---------------------------------------------------------------------------


class _FakeUdf:
    """Stands in for the SDK's `Udf`: metadata lives on `.metadata`, an
    attribute — never a dict key."""

    def __init__(self, name, metadata):
        self.name = name
        self.metadata = metadata


class _FakeUdfRegistry(dict):
    """Stands in for the SDK's `UdfRegistry`: dict-like (`str -> Udf`), and
    iterating it directly yields only the keys — same as the real thing,
    which is exactly what made the old `for record in records: dict(record)`
    traversal crash on a name string."""


class _FakeApi:
    def __init__(self, catalogs):
        self._catalogs = catalogs

    def get_udfs(self, whose):
        return self._catalogs.get(whose, _FakeUdfRegistry())


class _FakeFusedModule:
    def __init__(self, catalogs):
        self.api = _FakeApi(catalogs)


def _install_fake_fused(monkeypatch, community=None, team=None):
    catalogs = {
        "community": _FakeUdfRegistry(community or {}),
        "team": _FakeUdfRegistry(team or {}),
    }
    monkeypatch.setitem(sys.modules, "fused", _FakeFusedModule(catalogs))


def test_build_rules_traverses_a_udf_registry_not_a_list_of_dicts(monkeypatch):
    _install_fake_fused(monkeypatch, community={
        "default/Markdown_File": _FakeUdf("default/Markdown_File", {
            "fused:filePreview": True,
            "fused:filePreviewExtensions": ["md", "markdown", "mdown", "mkd"],
            "fused:filePreviewMenuOrder": "1",
            "fused:sharedToken": "UDF_Markdown_File",
        }),
    })
    built = rules_mod.build_rules()
    assert [r["name"] for r in built] == ["default/Markdown_File"]
    assert built[0]["token"] == "UDF_Markdown_File"
    # `fused:filePreviewMenuOrder` is a STRING in the real catalog ("1"); it
    # must come out coerced to an int so `_sort_key` can compare it.
    assert built[0]["order"] == 1
    assert built[0]["extensions"] == ["md", "markdown", "mdown", "mkd"]


def test_md_resolves_to_markdown_file_ahead_of_text_file_end_to_end(monkeypatch):
    # The acceptance case: .md must resolve to `default/Markdown_File`
    # (order 1), not `default/Text_File` (order 2), which also claims "md".
    _install_fake_fused(monkeypatch, community={
        "default/Markdown_File": _FakeUdf("default/Markdown_File", {
            "fused:filePreview": True,
            "fused:filePreviewExtensions": ["md", "markdown", "mdown", "mkd"],
            "fused:filePreviewMenuOrder": "1",
            "fused:sharedToken": "UDF_Markdown_File",
        }),
        "default/Text_File": _FakeUdf("default/Text_File", {
            "fused:filePreview": True,
            "fused:filePreviewExtensions": ["md", "txt", "json"],
            "fused:filePreviewMenuOrder": "2",
            "fused:sharedToken": "UDF_Text_File",
        }),
    })
    built = rules_mod.build_rules()
    got = rules_mod.resolve("/Users/me/.oh-my-zsh/README.md", built)
    assert got["name"] == "default/Markdown_File"
    assert got["token"] == "UDF_Markdown_File"


def test_build_rules_list_valued_file_name_matches_any_element(monkeypatch):
    # `fused:filePreviewFileName` is a LIST in the real catalog
    # (`["_sample"]` on `default/Fused_Geopartitioned_Table`), not the bare
    # string `_matches` used to assume.
    _install_fake_fused(monkeypatch, community={
        "default/Fused_Geopartitioned_Table": _FakeUdf(
            "default/Fused_Geopartitioned_Table", {
                "fused:filePreview": True,
                "fused:filePreviewFileName": ["_sample"],
                "fused:filePreviewMenuOrder": "1",
                "fused:sharedToken": "UDF_Fused_Geopartitioned_Table",
            }),
    })
    built = rules_mod.build_rules()
    assert built[0]["file_name"] == ["_sample"]
    got = rules_mod.resolve("/local/tables/_sample", built)
    assert got is not None
    assert got["name"] == "default/Fused_Geopartitioned_Table"
    # A path that merely shares the extension-less shape must not match.
    assert rules_mod.resolve("/local/tables/_other", built) is None


def test_build_rules_ignores_non_file_preview_udfs(monkeypatch):
    _install_fake_fused(monkeypatch, community={
        "default/Some_Other_Udf": _FakeUdf("default/Some_Other_Udf", {
            "some_other_key": True,
        }),
    })
    assert rules_mod.build_rules() == []


def test_build_rules_queries_both_team_and_community(monkeypatch):
    seen_whose = []
    community = _FakeUdfRegistry({
        "default/Markdown_File": _FakeUdf("default/Markdown_File", {
            "fused:filePreview": True,
            "fused:filePreviewExtensions": ["md"],
            "fused:filePreviewMenuOrder": "1",
        }),
    })

    class _RecordingApi(_FakeApi):
        def get_udfs(self, whose):
            seen_whose.append(whose)
            return super().get_udfs(whose)

    fake = _FakeFusedModule({"community": community, "team": _FakeUdfRegistry()})
    fake.api = _RecordingApi({"community": community, "team": _FakeUdfRegistry()})
    monkeypatch.setitem(sys.modules, "fused", fake)

    rules_mod.build_rules()
    assert set(seen_whose) == {"team", "community"}

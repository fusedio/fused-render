"""Source-contract test for the grid templates' param listener (D356).

The `duckdb` and `sqlite` grids both re-run their reader on a param change, and
both used to do it for EVERY param: `fused.params.onChange(load)`. In the file
view that is not their own URL — the content pane and the `claude` sidebar write
to the same shell URL, with no param boundary between them — so the sidebar's
`annotations` write, one per note the reader makes, re-ran the query. On a CSV it
also wiped the grid to "Loading…", because the keep-the-grid path needs a parquet
schema cache.

Pinned here rather than left to review because the regression is a one-line
revert that looks like a simplification, and its symptom shows up in a different
template's feature.
"""
import os
import re

import pytest

TEMPLATES = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates")

GRIDS = ["duckdb", "sqlite"]


@pytest.fixture(scope="module")
def sources():
    out = {}
    for name in GRIDS:
        with open(os.path.join(TEMPLATES, name, "template.html"),
                  encoding="utf-8") as handle:
            out[name] = handle.read()
    return out


@pytest.mark.parametrize("grid", GRIDS)
def test_param_listener_is_not_wired_straight_to_load(sources, grid):
    # The exact shape that made every annotation a reload.
    assert "params.onChange(load)" not in sources[grid]


@pytest.mark.parametrize("grid", GRIDS)
def test_listener_reloads_only_on_the_params_the_query_reads(sources, grid):
    source = sources[grid]
    # The watched set is the query's whole input: the file, the table, the page,
    # and the sort/filter the reader applies server-side. A param the query does
    # not read (`annotations`, `annmode`, `_side`, a hand-typed global) must not
    # be in it — that is the entire point of the guard.
    match = re.search(r"const QUERY_PARAMS = \[([^\]]*)\]", source)
    assert match, "no QUERY_PARAMS watch list"
    watched = re.findall(r'"([^"]+)"', match.group(1))
    assert watched == ["_file", "table", "offset", "sort", "filters"]
    # And the listener actually gates on it: a changed key reloads, an unchanged
    # one returns before load().
    body = re.search(
        r"fused\.params\.onChange\(\(\) => \{(.*?)\n    \}\);", source, re.S)
    assert body, "no guarded onChange listener"
    guard = body.group(1)
    assert "if (key === loadedParams) return;" in guard
    assert "load();" in guard


def test_sqlite_registers_after_its_default_stamps(sources):
    # sqlite normalises `sort`/`offset` into the URL at boot. Those writes are a
    # restatement of what the first load() already reads, so the listener has to
    # be registered AFTER them (and snapshot the params as they then stand),
    # otherwise the stamp spends a second load on the same query.
    source = sources["sqlite"]
    stamp = source.index('fused.params.set("offset", String(currentOffset())')
    listen = source.index("fused.params.onChange(() => {")
    assert stamp < listen

"""The plugin contract: an `IndexKind` is a row shape plus one `extract`
function, registered by name so the walker and the query layer can find it.

Host owns the walk, plugin owns the row. The engine already has an
incrementality/FSEvents/nice-throttling/cancel-flag walker (`index/scan.py`)
that nothing here should duplicate or bypass — a plugin never implements
`scan()`, never enumerates a directory, never sees a path the walker did not
already decide to visit. All a kind supplies is `extract(path, st)`: given
one file the host is ALREADY visiting, return a row (a dict of column name
to value) or `None` to say "not one of mine, skip it". This is what makes
trust decision #2 in SPEC-index-plugins.md enforceable rather than
advisory — a plugin cannot ask "what files exist under this root", only
"is THIS file, which the host chose to show me, one of mine" — and it is
why plugin code runs at INDEX TIME only: query time stays SQL over parquet,
never third-party Python on a keystroke (guarded_query.py's whole reason
to exist would be moot if a plugin could run during a query).

A kind's columns are declared, not inferred, so the store can build a
pyarrow schema and a stable parquet layout before the first row is ever
extracted — the same reason `store.schemas()` hardcodes the files/dirs
shape today. `text_column` names the one column query.py's ranking SQL
treats as the fuzzy-matchable name (the `nm` it already builds for the
files kind); every kind needs exactly one, or there is nothing to rank.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

# The store only ever needs these four physical types today (see
# store.schemas()): two string-ish/int-ish widths and no bool/list/struct.
# Keeping the set closed (rather than accepting any pyarrow type string)
# means a third-party kind can't declare a column the compaction SQL and
# guarded_query's empty-table stand-ins don't know how to handle.
_TYPES = {"string", "int64", "int32", "float64"}


@dataclass(frozen=True)
class Column:
    """One column of a kind's row shape. `type` is one of "string", "int64",
    "int32", "float64" — the physical types the store/compaction/guarded-query
    layers already know how to carry; anything else is rejected at
    declaration time rather than surfacing as a confusing DuckDB error deep
    in compaction."""

    name: str
    type: str

    def __post_init__(self) -> None:
        if self.type not in _TYPES:
            raise ValueError(
                f"Column {self.name!r} has type {self.type!r}; "
                f"must be one of {sorted(_TYPES)}"
            )


@dataclass(frozen=True)
class IndexKind:
    """A registered row shape plus its extractor.

    `extract(path, st)` is called by the host walker for one file at a time
    (`st` is the `os.stat_result` the walker already has, so a plugin never
    needs its own `stat` call) and must return either a dict matching
    `columns` by name, or None to decline the file. It must not raise for an
    ordinary "not mine" answer — only return None — the same degrade-safe
    posture as `exported_apps.py`: an index that cannot answer produces zero
    rows, never a crash that takes the whole scan down.
    """

    name: str
    columns: tuple[Column, ...]
    extract: Callable[[str, object], Optional[dict]]
    text_column: str

    def __post_init__(self) -> None:
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError(f"IndexKind {self.name!r} has duplicate column names: {names}")
        if self.text_column not in names:
            raise ValueError(
                f"IndexKind {self.name!r}'s text_column {self.text_column!r} "
                f"is not among its columns {names}"
            )

    def pa_schema(self, pa):
        """This kind's row shape as a pyarrow Schema, in declared column
        order — the schema `store.Sink` writes shards against and
        `guarded_query`'s empty-table stand-in mirrors."""
        type_map = {
            "string": pa.string(),
            "int64": pa.int64(),
            "int32": pa.int32(),
            "float64": pa.float64(),
        }
        return pa.schema([(c.name, type_map[c.type]) for c in self.columns])


_REGISTRY: dict[str, IndexKind] = {}


def register(kind: IndexKind, *, replace: bool = False) -> None:
    """Add `kind` to the registry. Raises ValueError on a name collision
    unless `replace=True` — registering is a deliberate, rare act (once at
    import time for a built-in kind, once on manifest-confirm for a
    third-party one), so a silent overwrite would most likely be a bug, not
    an intended upgrade."""
    if not replace and kind.name in _REGISTRY:
        raise ValueError(
            f"an IndexKind named {kind.name!r} is already registered "
            f"(pass replace=True to overwrite it deliberately)"
        )
    _REGISTRY[kind.name] = kind


def get(name: str) -> IndexKind:
    """The registered kind named `name`. Raises KeyError naming what IS
    registered, since an unregistered kind here means a caller asked for an
    index that was never wired up — a programming error to surface loudly,
    not a user-facing "index cannot answer" case (that posture belongs to
    query time, not to this lookup)."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"no IndexKind registered as {name!r}; registered: {sorted(_REGISTRY)}"
        ) from None


def registered() -> list[str]:
    """Names of every registered kind, sorted for stable listings."""
    return sorted(_REGISTRY)

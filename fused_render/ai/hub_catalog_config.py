"""`HubCatalogConfig` — where the on-device Hub catalog lives.

Mirrors `fused_render/index/config.py`'s split of "where things live" from
the engine that reads/writes them, for the same reason: this module is
imported by the search route, and a `manifest()` call on a machine with no
catalog yet should not pay a duckdb/pyarrow import (those stay inside
`hub_catalog.py`'s functions, not at this module's top).

Storage is `storage.home_dir()/hub_catalog`, the same branch-scoped,
FUSED_RENDER_HOME-aware home every other on-disk store in the app uses
(`index/config.py:index_dir`, `bench_store.py`, `footprints.py`).
"""
import os
from dataclasses import dataclass, field

from fused_render.shell import storage


def catalog_dir() -> str:
    """The hub catalog store for this home (FUSED_RENDER_HOME / branch aware)."""
    return os.path.join(storage.home_dir(), "hub_catalog")


@dataclass
class HubCatalogConfig:
    """Where the catalog lives. Build one with `load_config()` (a plain
    constructor call today — there is no user-editable knob yet, unlike
    `index.IndexConfig`'s ignore list — but the same shape keeps this module
    ready for one without a signature change at every call site)."""

    dir: str = field(default_factory=catalog_dir)

    # --- store layout --------------------------------------------------
    @property
    def manifest_json(self) -> str:
        return os.path.join(self.dir, "manifest.json")

    @property
    def pools_dir(self) -> str:
        return os.path.join(self.dir, "pools")

    @property
    def metadata_dir(self) -> str:
        return os.path.join(self.dir, "metadata")


def load_config(dir: str | None = None) -> HubCatalogConfig:
    return HubCatalogConfig(dir=dir or catalog_dir())

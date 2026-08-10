"""`IndexConfig` — every knob the index engine reads, resolved per call.

OpenIndex froze the index dir, the ignore list and the worker count in module
globals at import time. That is safe only because `fused.runPython` gives each
call its own process; inside this server the module is imported once and the
values would go stale the moment a user edited the ignore list or a test
redirected FUSED_RENDER_HOME. So there are no globals here: a config is built
from disk when it is needed, and travels to the detached worker (and from
there to its pool children) as JSON in the run's `spec.json`.

Storage is `storage.home_dir()/index`, which gives FUSED_RENDER_HOME
redirection and branch nesting for free — the same deal every other
persistent store in the app gets.
"""
import os
from dataclasses import dataclass, field

from fused_render.index.ignore import IgnoreRules, clean_patterns, default_ignore
from fused_render.shell import storage

# Rows per shard file written by a scan worker before it flushes.
SHARD_ROWS = 200_000
# Rows per compacted partition file (the unit partition pruning skips).
PART_ROWS = 500_000
# Split a pool task whose cached subtree is bigger than this, so one giant
# folder (~/Library) can't serialize the pool at the tail of a run.
SPLIT_DIRS = 4_000


def default_nproc() -> int:
    return max(2, min(10, os.cpu_count() or 4))


def index_dir() -> str:
    """The index store for this home (FUSED_RENDER_HOME / branch aware)."""
    return os.path.join(storage.home_dir(), "index")


@dataclass
class IndexConfig:
    """Where the index lives and how a scan behaves. Build one with
    `load_config()`; pass it explicitly to everything downstream."""

    dir: str = field(default_factory=index_dir)
    # RAW lines, exactly as the user typed them — comments and blanks
    # included. This is an authored document (the Preferences panel documents
    # `#` comments), so it is stored verbatim and parsed at use time by
    # `rules`; cleaning on the way in deleted the annotations on first save.
    ignore: list = field(default_factory=default_ignore)
    nproc: int = field(default_factory=default_nproc)
    shard_rows: int = SHARD_ROWS
    part_rows: int = PART_ROWS
    split_dirs: int = SPLIT_DIRS
    # Roots the startup scheduler rescans. Empty means "whatever the caller
    # asks for" — the server seeds it with its start_dir (see routers/index.py).
    roots: list = field(default_factory=list)

    def __post_init__(self):
        self._rules = None

    @property
    def rules(self) -> IgnoreRules:
        # Compiling the rules is not free, but the ignore list is editable at
        # runtime (POST /api/index/config) — so the cache is KEYED on the list
        # rather than invalidated by hand, and no caller can forget to reset it.
        # Keyed on the RAW lines, which over-invalidates on a comment edit and
        # never under-invalidates; the fingerprint below is the one that has to
        # be exact, and it comes from the cleaned patterns.
        key = tuple(self.ignore)
        if self._rules is None or self._rules[0] != key:
            self._rules = (key, IgnoreRules(clean_patterns(self.ignore)))
        return self._rules[1]

    # --- store layout (specs/index-store.md §1) ---------------------------
    @property
    def files_dir(self) -> str:
        return os.path.join(self.dir, "files")

    @property
    def dirs_parquet(self) -> str:
        return os.path.join(self.dir, "dirs.parquet")

    @property
    def partitions_json(self) -> str:
        return os.path.join(self.dir, "partitions.json")

    @property
    def fsevents_json(self) -> str:
        return os.path.join(self.dir, "fsevents.json")

    @property
    def applied_ignore_json(self) -> str:
        return os.path.join(self.dir, "ignore_applied.json")

    @property
    def config_json(self) -> str:
        return os.path.join(self.dir, "config.json")

    @property
    def scans_json(self) -> str:
        """Last successful scan per root — the startup debounce reads it."""
        return os.path.join(self.dir, "scans.json")

    @property
    def runs_dir(self) -> str:
        """Per-run scratch (spec, events, shards). Under the index dir rather
        than the system temp dir OpenIndex used, so a redirected home takes
        the runs with it and the shards land on the same filesystem as the
        index they are compacted into."""
        return os.path.join(self.dir, "runs")

    # --- worker transport --------------------------------------------------
    def to_dict(self) -> dict:
        """The subset a worker process needs to rebuild this config. Paths are
        included verbatim rather than re-derived in the child: the child may
        run under a different cwd, and two derivations of the store location
        is exactly how a scan ends up compacting into a directory nobody
        reads."""
        return {"dir": self.dir, "ignore": list(self.ignore), "nproc": self.nproc,
                "shard_rows": self.shard_rows, "part_rows": self.part_rows,
                "split_dirs": self.split_dirs}

    @classmethod
    def from_dict(cls, d: dict) -> "IndexConfig":
        known = {"dir", "ignore", "nproc", "shard_rows", "part_rows",
                 "split_dirs", "roots"}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def load_config(dir: str | None = None) -> IndexConfig:
    """The persisted config (`<index>/config.json`), falling back to defaults
    for anything absent or unparseable. A missing/corrupt file is not an
    error: the engine's whole configuration has usable defaults, and refusing
    to index because a JSON file got truncated would be worse than indexing
    with them."""
    cfg_dir = dir or index_dir()
    raw = storage.read_json(os.path.join(cfg_dir, "config.json"))
    if not isinstance(raw, dict):
        raw = {}
    cfg = IndexConfig(dir=cfg_dir)
    ignore = raw.get("ignore")
    if isinstance(ignore, list):
        # Verbatim: only the shape is enforced here, never the content.
        cfg.ignore = [str(p) for p in ignore]
    roots = raw.get("roots")
    if isinstance(roots, list):
        cfg.roots = [str(r) for r in roots if isinstance(r, str) and r.strip()]
    return cfg


def save_config(cfg: IndexConfig) -> IndexConfig:
    """Persist the user-editable half (ignore list + scan roots) and return
    the config as it now reads from disk."""
    storage.write_json(cfg.config_json,
                       {"ignore": list(cfg.ignore), "roots": list(cfg.roots)})
    return load_config(cfg.dir)

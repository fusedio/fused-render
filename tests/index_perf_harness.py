"""Reusable synthetic-corpus + real-scan + rank-latency harness.

Generalizes the pattern in
``test_index_rank_concurrency.py::test_rank_route_stays_fast_while_a_real_scan_is_running``:
build a synthetic tree on disk, prime a real (compacted) index over it, start
a real scan through the HTTP API, and sample ``/api/index/rank`` latency
while that scan is in flight. Two things use this:

- the concurrency regression above (an in-flight FULL scan must not starve
  interactive search), and
- a freshness-path variant that exercises the debounced on-demand scan
  workstream D made more eager, so a future loosening of its constants that
  starves interactive search fails here instead of shipping unnoticed.

Corpus size is a parameter, not a constant, precisely so the default stays
small enough to run in seconds under CI (see each call site for the size it
picks) while a large-corpus run stays available as a deliberate, opt-in
measurement rather than living in the default suite.

The default `build_tree` size also has to stay under `cfg.nproc * 24`
directories (`scan.py`'s threshold for fanning the walk out to a real
"spawn"-context `multiprocessing.Pool`, `default_nproc()` being
`max(2, min(10, cpu_count()))`): above it a default-suite test spawns real
child processes that each reimport duckdb/pyarrow/pandas, which is heavy
enough on a small, contended CI runner to starve or outright hang the
worker. The default size below keeps every default-suite caller entirely
in-process; only the `perf_large`-marked test asks for a tree big enough to
exercise the real pool fan-out.
"""
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from fused_render.index.config import IndexConfig
from fused_render.index.runner import canonical_root
from fused_render.index.store import Sink, compact


def build_tree(root, n_dirs=24, per_dir=150):
    """A real tree of `n_dirs * per_dir` files, big enough that a scan of it
    overlaps a burst of ranks, and — at the default size — small enough that
    the walk stays under the real multiprocessing pool's fan-out threshold
    (see this module's docstring) on any core count. `root` may be a `str`
    or `Path`; returned as the same `str` form the caller passed in, for
    canonicalization later."""
    root = str(root)
    Path(root).mkdir(parents=True, exist_ok=True)
    for d in range(n_dirs):
        sub = Path(root) / f"dir{d:03d}"
        sub.mkdir(parents=True, exist_ok=True)
        for i in range(per_dir):
            (sub / f"file{i:03d}_alpha.txt").write_text("x")
    return root


def prime_index(tmp_path, root, n=4000):
    """A real, compacted index over `root` so ranking has a full corpus to
    scan, without waiting on a real scan to build it first.

    Stored under `canonical_root(root)`, not the caller's raw `root`: the
    scan/rank routes canonicalize whatever root they are given before
    querying (platform.md §1), so a row filed under the un-normalized
    literal would leave `/api/index/rank` answering `covered: false` with no
    hits before the real scan below ever gets a chance to overlap it."""
    cfg = IndexConfig()
    shards = str(Path(tmp_path) / "prime-shards")
    Path(shards).mkdir(parents=True, exist_ok=True)
    sink = Sink(shards, "t", pa, pq, cfg.shard_rows)
    root = canonical_root(root)
    # The root's own dirs row is what `covered` is decided on — without it
    # the route answers `uncovered` with no hits and the timing means
    # nothing.
    sink.add(root, "s", ("sig", [], 0, 1_000_000_000, 0))
    per_dir = 100
    for d in range(n // per_dir):
        dirp = canonical_root(str(Path(root) / f"pre{d:04d}"))
        rows = []
        for i in range(per_dir):
            name = f"file{i:03d}_alpha.txt"
            rows.append((dirp + "/" + name, dirp, name, "txt",
                         10 + i, 100.0 + i))
        sink.add(dirp, "s", ("sig", rows, sum(r[4] for r in rows),
                             1_000_000_000, 0))
    sink.close()
    compact(cfg, root, shards, pa, pq)
    return cfg


@dataclass(frozen=True)
class LatencyReport:
    """p50/p95/max over a set of `/api/index/rank` latency samples, plus the
    ceiling assertion every caller ultimately wants."""

    samples: list = field(default_factory=list)

    @property
    def p50(self):
        return statistics.median(self.samples)

    @property
    def p95(self):
        if len(self.samples) < 2:
            return self.samples[0]
        # `statistics.quantiles` needs at least two points; n=100 gives a
        # percentile-indexed list so index 94 is the 95th percentile.
        return statistics.quantiles(self.samples, n=100)[94]

    @property
    def max(self):
        return max(self.samples)

    def assert_ceiling(self, ceiling):
        """The one assertion every call site wants: some overlap actually
        happened (an empty sample set proves nothing), and the worst
        latency observed stayed under `ceiling` seconds."""
        assert self.samples, "no rank request overlapped the scan"
        assert self.max < ceiling, (
            f"p50={self.p50:.3f}s p95={self.p95:.3f}s max={self.max:.3f}s "
            f">= ceiling {ceiling}s ({self.samples!r})")


def sample_rank_latencies(client, root, running, q="alpha", limit=50,
                           max_samples=15):
    """Poll `/api/index/rank` while `running()` says the scan it overlaps is
    still in flight, timing each call. Stops early once `max_samples` is hit
    or `running()` goes false, whichever comes first — the same shape as the
    concurrency regression this generalizes."""
    latencies = []
    for _ in range(max_samples):
        if not running():
            break
        t0 = time.perf_counter()
        resp = client.get("/api/index/rank",
                          params={"root": root, "q": q, "limit": limit})
        latencies.append(time.perf_counter() - t0)
        assert resp.status_code == 200, resp.text
        # Timing an answer the route declined to compute would measure
        # nothing: every request has to have taken the full ranking plan.
        body = resp.json()
        assert body["covered"] is True, body
        assert body["hits"], body
    return LatencyReport(latencies)

"""Hub catalog build worker: `python -m fused_render.ai.hub_catalog_worker
<build|delta> <capability> <catalog_dir> <formats_json>`.

Spawned by `hub_catalog_builder._run_pool_job` so a pool build's transient
memory (~100k raw Hub dicts and the fragmented heap they leave) is returned
to the OS when this process exits instead of pinning the server's footprint.
A module, not a script path, because a py2app bundle has no source file to
point at (same as `index/worker.py`).

Arguments are deliberately small — no rows, no token. The HF token is
resolved here through the builder's own `_token()` (env / hf cache), never
passed on argv. Protocol, on stdout (stderr is merged by the parent):
`@@pages N` after every Hub page, and one final `@@result {json}`. Exit is 0
only when a result was printed; any exception exits non-zero with the
traceback on stderr, having committed nothing (`PoolWriter` swaps a pool in
only on success).
"""
import json
import logging
import sys

from fused_render.ai import hub_catalog_builder as builder
from fused_render.ai.hub_catalog_config import HubCatalogConfig


class _ReportingCounter(list):
    """`[0]`-indexed page counter whose every bump is reported to the parent
    (the fetch loops do `page_counter[0] += 1`)."""

    def __init__(self):
        super().__init__([0])

    def __setitem__(self, index, value):
        super().__setitem__(index, value)
        print(f"{builder.PAGES_PREFIX}{value}", flush=True)


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[0] not in ("build", "delta"):
        print("usage: hub_catalog_worker <build|delta> <capability> <catalog_dir> "
              "<formats_json>", file=sys.stderr)
        return 2
    mode, capability, catalog_dir, formats_json = argv
    formats = tuple(json.loads(formats_json))
    cfg = HubCatalogConfig(dir=catalog_dir)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    counter = _ReportingCounter()
    if mode == "build":
        result = builder.build_capability_pool(
            cfg, capability, formats=formats, page_counter=counter)
    else:
        result = builder._refresh_delta_inprocess(
            cfg, capability, formats=formats, page_counter=counter)
    print(f"{builder.RESULT_PREFIX}{json.dumps(result)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

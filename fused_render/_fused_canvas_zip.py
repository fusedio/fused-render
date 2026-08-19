"""Write one canvas's exported TOML zip (raw bytes) to stdout.

Run as ``[sys.executable, _fused_canvas_zip.py, <collection_id>]`` by
canvases.py. Same bundle `fused canvas pull` extracts, but handed back as
bytes so the sync watcher can apply it SELECTIVELY (per-file three-way merge)
instead of the CLI's all-or-nothing ``--force`` extract.

FUSED_ENV is honored the same way the CLI does it (fused._env), so the zip
comes from the same environment the pull/push CLI calls target.
"""
import os
import sys

import fused

_env_name = os.environ.get("FUSED_ENV")
if _env_name:
    fused._env(_env_name)

from fused._global_api import get_api


def main() -> None:
    collection_id = sys.argv[1]
    payload = get_api().download_collection_toml_zip(collection_id)
    sys.stdout.buffer.write(payload)


if __name__ == "__main__":
    main()

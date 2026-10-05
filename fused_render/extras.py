"""Which optional feature groups this install has, and the reply when one is missing.

The base `pip install fused-render` carries what `fused-render open` needs to
render one app and run its `runPython` calls. Every other feature group is a
pyproject extra (`[index]`, `[data]`, `[desktop]`, `[hf]`, `[cloud]`,
`[fused]`, and `[all]` for every one of them). This module is where the server
asks whether a group is present:

* `available(extra)` answers from `importlib.util.find_spec` on the import
  names in `EXTRAS`, so asking never imports the package itself.
* `requires_extra(extra, feature)` is a FastAPI dependency. A route that
  declares it answers 503 with `{"error": ..., "missing_extra": extra}` when the
  group is absent, instead of reaching an `import duckdb` and 500-ing.
  `install_handler(app)` registers the handler that turns `MissingExtra` into
  that reply.
* `missing_for_serve()` lists the extras `fused-render serve` needs and this
  install lacks. `serve` is the desktop file explorer, and every panel of it
  leans on some extra, so it refuses to start without `[all]` rather than boot
  into a shell that errors in half its tabs.

Only import names are listed in `EXTRAS`, one or two per group, enough to tell
the group was installed. Platform-only members (pyobjc on macOS, dbus-fast on
Linux) are not in the list: their own modules already report `supported: false`
where they are absent, and `[desktop]` counts as present once zeroconf is.
"""

from __future__ import annotations

import importlib.util
from functools import lru_cache

# extra -> import names whose presence means the extra is installed.
EXTRAS: dict[str, tuple[str, ...]] = {
    "index": ("duckdb", "pyarrow", "watchfiles"),
    "data": ("pyarrow",),
    "desktop": ("zeroconf",),
    "hf": ("huggingface_hub",),
    "cloud": ("botocore", "google.auth"),
    "fused": ("boto3", "mcp", "anthropic"),
}

# What `fused-render serve` requires: every feature extra, i.e. `[all]`.
SERVE_EXTRAS: tuple[str, ...] = tuple(EXTRAS)


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        # A parent package that is itself missing raises from find_spec for a
        # dotted name ("google.auth" without "google").
        return False


@lru_cache(maxsize=None)
def available(extra: str) -> bool:
    return all(_importable(name) for name in EXTRAS[extra])


def install_hint(extra: str) -> str:
    return f"pip install 'fused-render[{extra}]'"


def message(extra: str, feature: str) -> str:
    return f"{feature} needs `{install_hint(extra)}`"


class MissingExtra(Exception):
    def __init__(self, extra: str, feature: str):
        super().__init__(message(extra, feature))
        self.extra = extra
        self.feature = feature


def payload(extra: str, feature: str) -> dict:
    return {"error": message(extra, feature), "missing_extra": extra}


def requires_extra(extra: str, feature: str):
    """A FastAPI dependency: 503 `missing_extra` unless `extra` is installed."""
    if extra not in EXTRAS:
        raise KeyError(extra)

    def _dependency() -> None:
        if not available(extra):
            raise MissingExtra(extra, feature)

    return _dependency


async def _missing_extra_handler(_request, exc: MissingExtra):
    from fastapi.responses import JSONResponse

    return JSONResponse(payload(exc.extra, exc.feature), status_code=503)


def install_handler(app) -> None:
    app.exception_handler(MissingExtra)(_missing_extra_handler)


def missing_for_serve() -> list[str]:
    return [extra for extra in SERVE_EXTRAS if not available(extra)]

# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see ../NOTICE.
# Injected shim: shadowing any real `openfused` package on sys.path so that
# `import openfused` inside user code always resolves to this thin client.
#
# Public surface:
#   openfused.project_root() -> str
#   openfused.asset_path(*parts) -> str
import os as _os


def project_root():
    """Return the absolute path to the current execution bundle's root directory.

    Reads ``OPENFUSED_PROJECT_ROOT``, which the backend exports into **every**
    call (no host round-trip — the value is already in the child's environment),
    and falls back to the process working directory, which is the same per-call
    sandbox dir. There is no "outside a project" mode: this always returns a path.
    """
    return _os.environ.get("OPENFUSED_PROJECT_ROOT") or _os.getcwd()


def asset_path(*parts):
    """Return an absolute path to a bundled asset under the project's ``assets/``.

    Use this instead of a relative ``./assets/...`` or ``__file__``-relative
    path: UDFs execute in a throwaway sandbox cwd, so only an absolute path
    anchored at the project root resolves reliably (``code run``, ``code test``
    and served routes alike). Example: ``pd.read_parquet(openfused.asset_path("data.parquet"))``.
    """
    return _os.path.join(project_root(), "assets", *parts)

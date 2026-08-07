"""Example runPython target: loads the full transcript of one Claude Code
session for the detail view. Stdlib only."""
from sessions import session_detail


def main(session: str = "") -> dict:
    if not session:
        return {"sessionId": "", "found": False, "turns": []}
    return session_detail(session)


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass

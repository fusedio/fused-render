"""Version-control feature endpoints (version-control.md).

The git layer's primitives live in lib.py; this module is the thin action
dispatcher the History tab and the status badge call.

main(action=...):
  log     -> {log: [{sha, date, message}]}
  status  -> {dirty, files}
  drift   -> {files, settings}          working-tree drift vs HEAD
  diff    -> {files, settings}          params: target (ref)  HEAD -> target
  commit  -> {ok, committed}            fold all drift into one commit
  restore -> {ok, sha}                  params: target (sha)  restore + forward-commit
"""
from . import lib


def main(action: str = "log", target: str = "") -> dict:
    if action == "log":
        return {"log": lib.log()}

    if action == "status":
        return lib.status()

    if action == "drift":
        return lib.drift_diff()

    if action == "diff":
        if not target:
            return {"error": "target required"}
        try:
            return lib.diff(target)
        except ValueError as e:
            return {"error": str(e)}

    if action == "commit":
        with lib.config_lock():
            committed = lib.commit("Commit working-tree changes")
        return {"ok": True, "committed": committed}

    if action == "restore":
        if not target:
            return {"ok": False, "error": "target required"}
        try:
            with lib.config_lock():
                sha = lib.restore(target)
            return {"ok": True, "sha": sha}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    return {"error": f"unknown action: {action}"}

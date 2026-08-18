"""runPython target for annotate/template.html: the view's revert surface
(SPEC §34, D194).

The URL `comments` param is the sole store the annotate view reads back —
comments live in the URL and nowhere else.

The version timeline and the restore itself live in `../shared/file_history.py`
(Claude Code's own checkpoint store, deliberately not git), and this module is
the bridge that turns every failure into an `{"error": ...}` dict, because
anything raised out of `main` becomes the red traceback overlay and "this file
has no history" is not an error.

Actions:
  main(action="history", file=..., enrich=False)
    -> file_history.timeline(...) — versions + current + revert selector + note
  main(action="revert_plan", file=..., version_id=None, enrich=False)
    -> what the write would do, for the confirm step (version_id=None means
       "the last change")
  main(action="revert", file=..., version_id=None, enrich=False)
    -> {"ok": True, "action": "restore"|"delete",
        "timeline": {...}, ...}   # the post-write timeline, so the panel does
                                  # not show the pre-revert position for the
                                  # length of a second round trip
"""
import os
import sys

# The fused engine execs this script without setting __file__; it puts the
# script's own directory first on sys.path, so rebuild __file__ from it. Under
# the built-in executor __file__ is already set, so this is a no-op.
if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "annotate.py")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "shared"))


# ------------------------------------------------------------- revert (§34)

def _file_history():
    """The shared reader.

    ImportError ONLY — that is the one condition this degrades over: a copy of
    this folder taken without its `shared/` sibling, where revert is simply not
    offered and nothing else in the view changes. A blanket `except Exception`
    also caught a SyntaxError or any other import-time bug inside
    `file_history.py` and reported it as "helper is not available", which reads
    as "you copied the folder wrong" and sends the reader to entirely the wrong
    place. Anything else now reaches `main`'s wrapper, which names the
    exception type.
    """
    import file_history
    return file_history


def _revert(file: str, version_id, confirm_unique: bool) -> dict:
    """Apply a revert the caller has already seen a plan for.

    Two refusals that exist because the confirm gate used to live ONLY in the
    page. `action="revert"` with no `version_id` performed the destructive write
    off its own freshly-computed choice, with no plan echo and no confirmation
    token, and the sole guard was a source grep over today's template — which
    pins the page, not this function, so any future second caller inherited an
    unguarded file-destroying entry point.

      * the plan's `id` must be echoed back. That is also a freshness check: a
        plan built against one disk state and applied against another is exactly
        how a user confirms one diff and gets a different one.
      * when the plan reports `unique_current` — the bytes on disk are in no
        checkpoint, so the write destroys the only copy — `confirm_unique` must
        be true. Deliberately NOT demanded for an ordinary step back, where
        nothing unrecorded is lost: a token the caller always has to pass is a
        token nobody reads.
    """
    fh = _file_history()
    if not isinstance(version_id, str) or not version_id:
        return {"error": "revert requires the version_id from a revert_plan "
                         "call — this action never chooses a target itself"}
    plan = fh.revert_plan(file, version_id)
    if not plan.get("ok"):
        return plan
    if plan.get("writable") is False:
        return {"error": "This file cannot be reverted: "
                         + (plan.get("writable_reason") or "it is not writable")}
    if plan.get("unique_current") and not confirm_unique:
        return {"error": "this file's current content is in no checkpoint, so "
                         "the revert would destroy the only copy — pass "
                         "confirm_unique=true once the user has confirmed",
                "plan": plan}
    res = fh.apply_revert(file, plan["id"])
    # The POST-write timeline, in the same response. The page used to follow every
    # revert with a second `history` call, and for that whole round trip the row
    # list went on showing the pre-revert position — precisely the window in which
    # the user is staring at it to find out whether the revert worked.
    #
    # ENRICHED unconditionally, like the plan and the write: this is what the panel
    # displays, and an unenriched timeline cannot see the did-not-exist boundary, so
    # adopting one would report `at_earliest` a step early. The caller's disclosure
    # state does not enter into it — that rule (`enrich` honoured for `history` and
    # nowhere else) is about what the button DOES, and this changes only what the
    # panel shows.
    #
    # Best-effort, and the field is simply ABSENT when it fails: the write already
    # landed and is already reported, so a failure to re-enumerate the store must
    # not turn a successful revert into an error. The page falls back to its own
    # `history` call, which reports its own failure in its own words.
    #
    # Absorbed for the USER, not for the log. With no trace at all, a timeline that
    # has started failing on every revert is indistinguishable from one that never
    # fails — the page falls back, the panel still paints, and the only symptom is a
    # round trip nobody can account for. stderr is where the engine already collects
    # a run's diagnostics, so naming the exception costs the user nothing.
    try:
        res["timeline"] = fh.timeline(file, enrich=True)
    except Exception as exc:
        print("annotate: post-revert timeline failed, the page will re-read it "
              "itself — %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    return res


def main(action: str = "history", file: str = "", version_id=None,
         enrich: bool = False, confirm_unique: bool = False) -> dict:
    # Every revert action answers with data, never an exception: a raised error
    # here reaches the page as the red traceback overlay, and "no history for
    # this file" / "read-only mount" / "stale version id" are all ordinary
    # states of this surface that the panel renders as text.
    if action in ("history", "revert_plan", "revert"):
        if not file:
            return {"error": "missing target file (no _file param?)"}
        try:
            if action == "revert":
                return _revert(file, version_id, bool(confirm_unique))
            fh = _file_history()
            if action == "history":
                # `enrich` is honoured HERE and nowhere else: the boot timeline
                # stays off the 5 MB transcripts, while the plan and the write
                # always enrich, so a disclosure widget cannot change what the
                # button DOES (only what the panel shows).
                return fh.timeline(file, enrich=bool(enrich))
            return fh.revert_plan(file, version_id)
        except ImportError:
            return {"error": "file history helper (../shared/file_history.py) "
                             "is not available"}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": f"unknown action: {action}"}

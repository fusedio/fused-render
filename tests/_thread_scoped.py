"""Thread-scoped filesystem traps for the "this code must not enumerate" tests.

Several suites assert that a code path never issues `os.scandir`/`os.listdir`/
`os.walk`/`glob` — the mount tests (a stray readdir drops an rclone mount), the
condition-gate tests (CT-12), the pathops tests. They do it the only way one
can: by patching the function on the `os` module, which is **process-wide**.

That is fine until something else in the process has a background thread. Under
the `fused-engine` CI job it does: the `fused` package starts an
`openfused-invoke-dispatcher` thread that polls its own request directory with
`pathlib.glob`, i.e. `os.scandir`. It ticks on its own schedule, so it lands
inside the patched window at random — and the trap blames the code under test
for a directory it never touched. Observed as both a hard failure
(`test_load_meta_consolidated_does_not_scandir_at_all` recording
`/tmp/openfused-invoke-*/requests`) and, in an earlier run, a
`PytestUnhandledThreadExceptionWarning` raised inside that thread.

Widening the trap's allow-list would be the wrong fix: it would have to name
another package's private temp paths, and it would go stale. What every one of
these assertions actually means is "**this** call must not enumerate" — a
property of one thread's work, not of the process. So the trap applies to the
thread that installed it and delegates everything else straight through.
"""
import threading


def this_thread_only(real, guard):
    """`guard`, but only for the installing thread; other threads get `real`.

    Capture happens at call time, so the owner is whichever thread builds the
    trap — which is the test thread, since these are installed by fixtures and
    test bodies rather than by imports.
    """
    owner = threading.get_ident()

    def wrapper(*args, **kwargs):
        if threading.get_ident() != owner:
            return real(*args, **kwargs)
        return guard(*args, **kwargs)

    return wrapper

"""runPython target for calls/template.html: read the app call log.

A thin op dispatcher over ``fused_render.calls``'s query helpers — the query
logic lives there so the template, the ``fused-render calls`` CLI, and any
future endpoint share one implementation rather than three that drift.

Op shape follows log_studio/reader.py (the other high-volume log viewer in the
tree): one helper, several ops, every op paged or pre-aggregated so none of
them ever loads the whole store. Bucketing/percentiles happen HERE, not in the
template, which is what keeps the charts fast on a big log — the template sees
one point per bucket, never 100k records.

Allowlisted for in-process execution (executor.INPROCESS_HELPERS, D72): it is
first-party, it never imports or executes user code, and its reads are bounded.
That removes the ~700 ms subprocess spawn from every poll, which is what makes
the live tail viable at all.

Ops:
  main(op="overview", page=..., since=..., ...)  -> counts, span, outcomes
  main(op="page", limit=100, cursor=...)         -> a page of records, newest first
  main(op="series", bucket_ms=60000, ...)        -> pre-bucketed chart points
  main(op="targets", ...)                        -> per-entrypoint rollup
  main(op="detail", call_id=...)                 -> one full record
  main(op="config")                              -> store location + capture state
"""
import time


def _filters(page, entrypoint, route, outcome, kind, since, until, failed, q, scope):
    """Normalise the shared filter arguments into calls._matches kwargs.

    ``since``/``until`` accept either an absolute epoch or a relative age in
    seconds (a negative value, or the string forms the CLI passes) — the
    template only ever sends absolute stamps, but a relative window is what a
    human or an agent types.
    """
    now = time.time()

    def stamp(value):
        if value in (None, "", 0):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        # A small number is an age in seconds ("last 5 minutes"), a large one is
        # an absolute epoch. The boundary is any plausible epoch: 10^9 is 2001.
        return now - value if value < 1_000_000_000 else value

    first_party = None
    if scope == "mine":
        first_party = False
    elif scope == "templates":
        first_party = True
    return {
        "page": page or None,
        "entrypoint": entrypoint or None,
        "route": route or None,
        "outcome": outcome or None,
        "kind": kind or None,
        "since": stamp(since),
        "until": stamp(until),
        "failed": bool(failed),
        "q": q or None,
        "first_party": first_party,
    }


def main(
    op: str = "overview",
    page: str = "",
    entrypoint: str = "",
    route: str = "",
    outcome: str = "",
    kind: str = "",
    since: float = 0,
    until: float = 0,
    failed: bool = False,
    q: str = "",
    scope: str = "",
    limit: int = 100,
    cursor: str = "",
    bucket_ms: int = 60_000,
    call_id: str = "",
):
    # Imported here, not at module scope: this module is loaded fresh per
    # evaluation by the in-process helper path, and a failed import should
    # surface as this op's error rather than at load time.
    from fused_render import calls

    if op == "config":
        return {
            "dir": calls.store_dir(),
            "enabled": calls.enabled(),
            "retention_days": calls.retention_days(),
            "files": len(calls.store_files()),
            "dropped": calls.dropped_count(),
        }

    if op == "detail":
        if not call_id:
            return {"error": "op=detail requires 'call_id'"}
        record = calls.detail(call_id)
        return {"record": record} if record else {"error": f"no record with call_id {call_id}"}

    filters = _filters(page, entrypoint, route, outcome, kind, since, until, failed, q, scope)

    if op == "overview":
        return calls.overview(**filters)
    if op == "page":
        return calls.query(limit=limit, cursor=cursor or None, **filters)
    if op == "series":
        return calls.series(bucket_ms=bucket_ms, **filters)
    if op == "targets":
        return calls.targets(**filters)
    return {
        "error": f"unknown op {op!r}; expected one of: "
                 "overview, page, series, targets, detail, config"
    }

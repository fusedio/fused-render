"""GitHub pull-request status (open / draft / merged / closed) for the Tasks
page's PR view and the chat's PR chip popover.

Asked of GitHub's GraphQL API with the token of the user's own `gh` login, a
batch of PRs per request, and cached here: a merged or closed PR never changes
state again, an open or draft one is re-asked after two minutes, and an answer
that failed (no token, offline, a PR the token cannot see) is retried after one.

Stdlib only. The token is never logged and never leaves this module except in
the one Authorization header.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request

from fused_render.copilot_health import gh_token

_REF_RE = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d{1,7})$")
_GRAPHQL_URL = "https://api.github.com/graphql"
_BATCH = 25
_MAX_REFS = 100
_TIMEOUT_S = 8
_TTL_LIVE_S = 120.0
_TTL_UNKNOWN_S = 60.0
_TOKEN_TTL_S = 300.0
_TOKEN_MISS_TTL_S = 30.0

_LOCK = threading.Lock()
# ref -> (expires_at, status | None); merged and closed never expire.
_CACHE: dict[str, tuple[float, str | None]] = {}
_TOKEN: tuple[float, str | None] | None = None
# ref -> head branch name, filled beside _CACHE (same lifetime in practice: a
# head ref can change while a PR is open, so it is refreshed with each ask).
_HEADS: dict[str, str | None] = {}


def reset_for_tests() -> None:
    global _TOKEN
    with _LOCK:
        _CACHE.clear()
        _HEADS.clear()
        _TOKEN = None


def parse_ref(ref: str) -> tuple[str, str, int] | None:
    """`owner/repo#n` -> `(owner, repo, n)`, None for anything else. The shape
    check is also what keeps a ref from smuggling GraphQL into the query."""
    m = _REF_RE.match(ref) if isinstance(ref, str) else None
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def _token() -> str | None:
    global _TOKEN
    now = time.time()
    with _LOCK:
        if _TOKEN is not None and _TOKEN[0] > now:
            return _TOKEN[1]
    token = gh_token()  # shells out to `gh`: not under the lock
    with _LOCK:
        _TOKEN = (now + (_TOKEN_TTL_S if token else _TOKEN_MISS_TTL_S), token)
    return token


def _status_of(pr) -> str | None:
    if not isinstance(pr, dict):
        return None
    state = pr.get("state")
    if state == "MERGED":
        return "merged"
    if state == "CLOSED":
        return "closed"
    if state == "OPEN":
        return "draft" if pr.get("isDraft") else "open"
    return None


def _query(batch: list[tuple[str, str, int]]) -> str:
    parts = [f"r{i}: repository(owner:{json.dumps(o)},name:{json.dumps(n)})"
             f"{{ pullRequest(number:{num}){{state isDraft headRefName}} }}"
             for i, (o, n, num) in enumerate(batch)]
    return "query{ " + " ".join(parts) + " }"


def _fetch_batch(batch: list[tuple[str, str, int]], token: str,
                 heads: list[str | None] | None = None) -> list[str | None]:
    req = urllib.request.Request(
        _GRAPHQL_URL, method="POST",
        data=json.dumps({"query": _query(batch)}).encode(),
        headers={"Authorization": f"bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "fused-render"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = (json.loads(resp.read()) or {}).get("data")
    except (OSError, ValueError):
        if heads is not None:
            heads.extend([None] * len(batch))
        return [None] * len(batch)
    data = data if isinstance(data, dict) else {}
    out = []
    for i in range(len(batch)):
        repo = data.get(f"r{i}")
        pr = repo.get("pullRequest") if isinstance(repo, dict) else None
        out.append(_status_of(pr))
        if heads is not None:
            name = pr.get("headRefName") if isinstance(pr, dict) else None
            heads.append(name if isinstance(name, str) and name else None)
    return out


def head_refs(refs: list[str]) -> dict[str, str]:
    """`{ref: head branch}` for the refs `fetch_statuses` has answered with a
    branch name; refs whose branch is unknown are left out."""
    with _LOCK:
        return {r: _HEADS[r] for r in refs if _HEADS.get(r)}


def fetch_statuses(refs: list[str]) -> dict[str, str | None]:
    """`{ref: status}` for each valid, distinct `owner/repo#n` (first 100).
    `status` is merged / closed / draft / open, or None when unknown. Invalid
    refs are left out of the answer."""
    wanted: list[str] = []
    for ref in refs:
        if parse_ref(ref) is not None and ref not in wanted:
            wanted.append(ref)
    wanted = wanted[:_MAX_REFS]
    now = time.time()
    out: dict[str, str | None] = {}
    misses: list[str] = []
    with _LOCK:
        for ref in wanted:
            hit = _CACHE.get(ref)
            if hit is not None and hit[0] > now:
                out[ref] = hit[1]
            else:
                misses.append(ref)
    if misses:
        token = _token()
        for i in range(0, len(misses), _BATCH):
            chunk = misses[i:i + _BATCH]
            got_heads: list[str | None] = []
            statuses = ([None] * len(chunk) if not token else
                        _fetch_batch([parse_ref(r) for r in chunk], token, got_heads))
            got_heads += [None] * (len(chunk) - len(got_heads))
            stamp = time.time()
            with _LOCK:
                for ref, status, head in zip(chunk, statuses, got_heads):
                    out[ref] = status
                    _HEADS[ref] = head
                    if status in ("merged", "closed"):
                        ttl = float("inf")
                    else:
                        ttl = _TTL_LIVE_S if status else _TTL_UNKNOWN_S
                    _CACHE[ref] = (stamp + ttl, status)
    return {ref: out[ref] for ref in wanted}

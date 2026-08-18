"""The explorer's fuzzy ranker, in Python — a faithful port, not a variant.

`frontend/src/platform/lib/fuzzy.ts` + `frontend/src/apps/explorer/listing/
search.ts` are the AUTHORITY, and they cannot be replaced by this module: the
in-folder search still ranks a live streamed walk in the browser, and only a
browser-side ranker can rank a stream as it arrives. So the same search box can
be answered by either ranker depending on whether the index covers the folder,
and if the two disagree the result order changes for reasons no user can see.

Parity is therefore pinned by a fixture generated from the JS side
(`tests/fixtures/rank-parity.json`, written by `bun scripts/gen-rank-fixture.ts`)
and asserted in BOTH languages — tests/test_index_rank.py here, and
frontend/src/apps/explorer/listing/rank-parity.test.ts there. Change the
ordering on one side only and one of them goes red.

Everything below therefore mirrors the JS line for line, comments included
where the reason is not visible in the code:

- two passes (forward greedy-earliest for the end, backward tighten from it),
- the substring fast path with `longest_run = len(q)`, which is the invariant
  that guarantees substring-over-fuzzy in `rank_compare`,
- +1 per char, +3 for a consecutive run, +5 for a segment start (tested on the
  ORIGINAL-case text so the camelCase hump survives lowercasing),
- the `max_span(n) = n * 3 + 8` refusal,
- name bonuses (+100 exact, +25 prefix), the 1/2/3 name tier, and the
  `longest_run desc, tier asc, score desc, depth asc, path` ordering.

ONE known divergence, deliberate: the JS final tie-break is
`Intl.Collator(sensitivity: "base")`, and this uses `str.lower()`. They agree
on every path in the fixture (which includes a case-only pair,
`notes/Alpha.txt` vs `notes/alpha.txt`) and on ASCII generally; a locale-aware
collation of accented or non-Latin paths could differ in the last tie-break
only. The fixture is the arbiter — if it ever disagrees, the fixture wins and
this note is what gets updated.
"""

# Chars that open a new "segment" in a path/name; a match right after one of
# these reads as the start of a word and scores higher.
SEPARATORS = frozenset("/.-_ ")


def max_span(query_length: int) -> int:
    """How far a `query_length`-char match may stretch, first char to last.

    Tuned against real paths, not derived (see fuzzy.ts for the measurements
    and for the named cost: a 3-char query used as word initials over a long
    prose title stops matching, and the obvious per-segment-start allowance was
    measured and is worse). Do not retune here — retune there and regenerate
    the fixture."""
    return query_length * 3 + 8


def _is_segment_start(text: str, i: int) -> bool:
    """Index 0, the char after a separator, or a camelCase hump. Uses the
    ORIGINAL-case text so the hump test survives the lowercasing done for
    matching."""
    if i == 0:
        return True
    prev = text[i - 1]
    if prev in SEPARATORS:
        return True
    return text[i].isupper() and text[i].isascii() and not (
        prev.isupper() and prev.isascii())


def fuzzy_match(query: str, text: str):
    """`{score, positions, longest_run}` for `query` against `text`, or None.

    None means either "not a subsequence at all" or "too spread out to mean
    anything" (max_span). `positions` are ascending indices into `text`."""
    if query == "":
        return {"score": 0, "positions": [], "longest_run": 0}
    q = query.lower()
    t = text.lower()
    sub = t.find(q)
    if sub != -1:
        # The substring branch stays AHEAD of everything below: longest_run =
        # len(q) is the maximum the subsequence branch can never reach, and
        # rank_compare orders on longest_run first. Its span is the query
        # length by construction, so the bound cannot apply to it.
        positions = []
        score = 0
        for ti in range(sub, sub + len(q)):
            positions.append(ti)
            score += 1
            if ti > sub:
                score += 3  # consecutive run
            if _is_segment_start(text, ti):
                score += 5  # landed on a word boundary
        return {"score": score, "positions": positions, "longest_run": len(q)}
    # Pass 1: does a subsequence exist, and where is the earliest it can end?
    qi = 0
    end = -1
    for ti, ch in enumerate(t):
        if qi >= len(q):
            break
        if ch == q[qi]:
            end = ti
            qi += 1
    if qi < len(q):
        return None  # ran out of text before matching every char
    # Pass 2: the same match, packed as far right as `end` allows. Guaranteed
    # to complete — pass 1's alignment is itself a witness that ends at `end`,
    # and binding later can only ever be easier.
    positions = [0] * len(q)
    qj = len(q) - 1
    ti = end
    while ti >= 0 and qj >= 0:
        if t[ti] == q[qj]:
            positions[qj] = ti
            qj -= 1
        ti -= 1
    if end - positions[0] + 1 > max_span(len(q)):
        return None
    # Scored over the TIGHTENED positions. Scoring pass 1's would have judged a
    # match nobody is going to see.
    score = 0
    run = 0
    longest_run = 0
    prev = -2
    for ti in positions:
        score += 1
        run = run + 1 if ti == prev + 1 else 1
        if run > longest_run:
            longest_run = run
        if ti == prev + 1:
            score += 3  # consecutive run
        if _is_segment_start(text, ti):
            score += 5  # landed on a word boundary
        prev = ti
    return {"score": score, "positions": positions, "longest_run": longest_run}


def query_wants_hidden(raw_query: str) -> bool:
    """A dot-leading query segment is explicit intent to SEE hidden entries.

    That makes ".py" work as an extension search without a second pass, and
    "env" deliberately not surface ".env"."""
    q = raw_query.strip()
    return q.startswith(".") or "/." in q


def is_hidden_rel(rel: str) -> bool:
    """An entry is hidden when any path segment is dot-leading."""
    return rel.startswith(".") or "/." in rel


def _name_tier(name: str, name_start: int, q: str, positions) -> int:
    """How much of the match landed on the entry's OWN name: 1 = the query is a
    substring of the name, 2 = the name matched only fuzzily, 3 = only ancestor
    directories matched. Derived from what the matcher already returned plus
    the lowercased name — matching a second time against the name alone would
    double the cost of the hot path."""
    if q in name:
        return 1
    if positions and positions[-1] < name_start:
        return 3
    return 2


def _depth_of(rel: str) -> int:
    return 1 + rel.count("/")


def _sort_key(hit: dict):
    """rank_compare as a key: longest_run desc, tier asc, score desc, depth
    asc, then a case-insensitive path compare.

    `tier` sits above `score` because scoring runs over the whole rel path, so
    a matching ancestor directory donates its score to every descendant. It
    sits BELOW `longest_run`, which is what already guarantees
    substring-over-fuzzy — that invariant must survive any change here."""
    return (-hit["longest_run"], hit["tier"], -hit["score"], hit["depth"],
            hit["rel"].lower())


def rank_entries(query: str, entries) -> list:
    """Score and order `entries` (dicts with `rel`, and whatever else they
    carry) against `query`, best first.

    Each returned hit is the entry's own keys plus `positions`, `score`,
    `longest_run`, `tier` and `depth`. An empty (or all-whitespace) query ranks
    nothing — a search box with nothing typed in it has no results, not all of
    them."""
    if not query.strip():
        return []
    q = query.lower()
    show_hidden = query_wants_hidden(query)
    hits = []
    for entry in entries:
        rel = entry["rel"]
        if not show_hidden and is_hidden_rel(rel):
            continue
        m = fuzzy_match(query, rel)
        if m is None:
            continue
        score = m["score"]
        name_start = rel.rfind("/") + 1
        name = rel[name_start:].lower()
        if name == q:
            score += 100
        elif name.startswith(q):
            score += 25
        hits.append({**entry, "positions": m["positions"], "score": score,
                     "longest_run": m["longest_run"],
                     "tier": _name_tier(name, name_start, q, m["positions"]),
                     "depth": _depth_of(rel)})
    hits.sort(key=_sort_key)
    return hits

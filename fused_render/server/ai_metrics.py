"""What `/api/ai` has done in THIS process: tokens, speed, failures (SPEC AI-12).

A local counter for a local question. `fused.ai` is the one place in this app
that spends model time — a page's chat box, the commit-message call, a
generation from a resident local model — and until now the only trace it left
was one line per call in the app log. "Is the thing I just wrote hammering the
model?", "how fast is this model on this laptop?" and "are my AI calls even
working?" had no answer at all.

Four counters, because volume alone answers only the first of those:

* **Tokens and completions**, bucketed over time — the graph.
* **Failures, by kind** — a page whose calls all time out has generated zero
  tokens, which is the same picture as a page nobody opened until the failures
  are counted beside them. A model that is merely still LOADING is not one of
  them: that call started a download and said so.
* **Seconds spent generating**, and the tokens/second that falls out of it —
  the number anybody choosing between two local models actually wants, and the
  explanation when a model on the CPU (AI-11b) "feels broken".
* **Which tier**, Claude or a local model, on the `/`-in-the-id seam AI-1
  already defines — because this page's whole subject is the local half, and a
  total that merged the two would answer neither question.

Three properties, and each one is a deliberate NON-feature:

* **In memory, in this process.** Nothing is written to disk and nothing
  survives a restart, so this is not a usage ledger, cannot be reconciled
  against a bill, and never becomes a file somebody has to prune. `since` in
  every snapshot says exactly which window the numbers cover, so a restarted
  server reads as "counting from 00:04" rather than as a machine that
  generated nothing today.
* **Bounded by construction.** A fixed ring of `BUCKETS` slots and at most
  `MAX_MODELS` per-model rows: the store is the same handful of kilobytes after
  one call and after a million, with no pruning pass to get wrong.
* **It counts what the CALLER was told.** `record()` takes the very `usage`
  dict the relay is about to put on the wire, so the graph can never disagree
  with the number the page read off its own response. A tier that reports no
  usage contributes a completion and no tokens rather than an estimate — this
  module never tokenizes anything itself, because a count derived from the text
  would be a different number wearing the same label.

What is NOT counted, deliberately: a completion nobody finished (a client that
went away mid-response) — only the terminal frame carries a token count, so
there is nothing honest to add; a request refused as malformed, which never
reached a model at all; and Claude Code sessions started elsewhere in the app
(the `claude` chat template, the task runner). Those are the user's own Claude
Code session spending the user's own budget through a CLI that reports it; this
file is about the API this app serves.
"""

from __future__ import annotations

import threading
import time

#: Bucket width. Ten seconds is the resolution a one-hour graph can actually
#: draw (360 columns is already more than a chart is wide) and it is coarse
#: enough that one prompt lands in one bar rather than smeared across ten.
BUCKET_S = 10
#: Retention. One hour of buckets, which is what makes the ring cheap: past
#: this the oldest slot is simply overwritten, so nothing ever grows and no
#: prune has to run.
WINDOW_S = 3600
BUCKETS = WINDOW_S // BUCKET_S
#: Longest window a snapshot may ask for, in minutes — the retention itself.
MAX_MINUTES = WINDOW_S // 60

#: Distinct models kept as their own NAMED row (the overflow row below is one
#: more). A model id reaches this store from `/api/ai`'s `model` parameter,
#: which any page may set to any Hub repo id, so the breakdown needs a ceiling
#: that does not depend on callers being reasonable. Past it everything folds
#: into one honest row rather than being dropped: the totals stay right either
#: way.
MAX_MODELS = 32
#: The name that row is kept under. A SPACE is what makes it safe: `_AI_MODEL_RE`
#: admits no whitespace, so no real model can ever be merged into the overflow
#: row by having been called the same thing.
OTHER_MODEL = "other models"

#: The two tiers `/api/ai` serves (AI-1). Not a guess about the id: the same
#: slash test the relay dispatches on, applied to the same string it dispatched.
CLAUDE, LOCAL = "claude", "local"


def _int(value) -> int | None:
    """`value` as a non-negative token count, or None if it isn't one.

    Bools are refused explicitly (`True` is an `int` in Python and would count
    as one token), and so is a negative — both mean the source reported
    something this module has no business summing.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _seconds(value) -> float | None:
    """`value` as a non-negative duration, or None. A zero is dropped too: it
    is a duration nothing could have taken, and it would make a tokens/second
    division either infinite or a lie."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not value > 0 or value != value:  # non-positive, or NaN
        return None
    return float(value)


class _Counts:
    """One row of the same counters, wherever it is being kept — a bucket, a
    model, a tier, the process.

    Two of the fields exist to keep a derived number honest:

    * `input_seen` tells "nobody reported input tokens" from "the input was
      zero tokens". A local worker reports the tokens it GENERATED and nothing
      else (SPEC AI-3's `/generate` contract), so a row that summed a missing
      input as 0 would state, in a table, that a local model read an empty
      prompt.
    * `timed_tokens` is the subset of `output_tokens` whose completion also
      reported a duration. Tokens/second divides those by `seconds` rather than
      dividing everything by what was timed — a cancelled local generation
      reports its tokens and no time, and counting them anyway would inflate
      the speed of the model it was cancelled on.
    """

    __slots__ = ("completions", "input_tokens", "output_tokens", "input_seen",
                 "failures", "seconds", "timed_tokens")

    def __init__(self):
        self.completions = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.input_seen = 0
        self.failures = 0
        self.seconds = 0.0
        self.timed_tokens = 0

    def add(self, input_tokens: int | None, output_tokens: int | None,
            seconds: float | None) -> None:
        self.completions += 1
        if input_tokens is not None:
            self.input_tokens += input_tokens
            self.input_seen += 1
        if output_tokens is not None:
            self.output_tokens += output_tokens
        if seconds is not None:
            self.seconds += seconds
            self.timed_tokens += output_tokens or 0

    def fail(self) -> None:
        self.failures += 1

    def merge(self, other: "_Counts") -> "_Counts":
        self.completions += other.completions
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.input_seen += other.input_seen
        self.failures += other.failures
        self.seconds += other.seconds
        self.timed_tokens += other.timed_tokens
        return self

    def copy(self) -> "_Counts":
        return _Counts().merge(self)

    def payload(self) -> dict:
        """Wire shape. `input_tokens` is null when no completion in this row
        ever reported one (see `input_seen`), and `tokens_per_second` is null
        when nothing in it was timed — both are absences, and an absence
        printed as 0 is an answer where there is none."""
        return {
            "completions": self.completions,
            "input_tokens": self.input_tokens if self.input_seen else None,
            "output_tokens": self.output_tokens,
            "failures": self.failures,
            "seconds": round(self.seconds, 2) if self.seconds else None,
            "tokens_per_second": (round(self.timed_tokens / self.seconds, 1)
                                  if self.seconds > 0 else None),
        }


class _Store:
    """The ring, its totals, and the one lock over both.

    Buckets are keyed by MONOTONIC time, not by the wall clock, and converted
    to wall clock only when a snapshot is taken. The clock on a laptop moves —
    an NTP correction, a lid opened in another timezone — and a wall-keyed ring
    answers that by putting a bucket in the future or by replaying an index it
    has already passed. Elapsed time is what a rate graph is made of, so the
    ring measures elapsed time and the axis labels are derived at read.

    `threading.Lock`, not an asyncio one: the Claude tier records from the event
    loop and the local tier records from the worker thread `_ai_relay` puts it
    on. The critical section is a handful of integer adds.
    """

    def __init__(self, monotonic=time.monotonic, wall=time.time):
        # The clocks are parameters so a test can walk the ring past its own
        # edge — an hour of buckets — without moving the machine's clock or
        # monkeypatching `time` out from under asyncio.
        self._monotonic = monotonic
        self._wall = wall
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            # A slot is [bucket key, counts]. The key says which lap of the ring
            # the counts belong to, so a slot from an hour ago is recognised as
            # stale on the next write instead of being added to.
            self._slots: list[list] = [[-1, _Counts()] for _ in range(BUCKETS)]
            self._models: dict[str, _Counts] = {}
            # Model name -> its tier, stamped when the row is created. None for
            # the overflow row, which is a mixture and names neither.
            self._model_tiers: dict[str, str | None] = {}
            self._tiers: dict[str, _Counts] = {CLAUDE: _Counts(), LOCAL: _Counts()}
            self._totals = _Counts()
            # Error TYPE -> count, since start. Unbounded on purpose and safe to
            # be: the keys are this server's own vocabulary (`ai_error`,
            # `timeout`, `ai_unavailable`), never a caller's string, so there is
            # nothing here for a page to grow.
            self._failure_types: dict[str, int] = {}
            self._last_completion: float | None = None
            self._started_mono = self._monotonic()
            self._started_wall = self._wall()

    # -- writing ------------------------------------------------------------

    def _rows(self, model: str):
        """The three rows one event lands in: its bucket, its model, its tier.
        Called under the lock."""
        # The TIER is read off the id the caller actually sent, before anything
        # below can rebind it. Past the cap `model` becomes `OTHER_MODEL`, which
        # has no slash — so a tier read after the fold would put every local
        # model past the cap in the Claude column, and get it wrong on exactly
        # the path the cap exists for.
        tier = self._tiers[LOCAL if "/" in model else CLAUDE]
        key = int((self._monotonic() - self._started_mono) // BUCKET_S)
        slot = self._slots[key % BUCKETS]
        if slot[0] != key:  # a slot from a previous lap: start it over
            slot[0], slot[1] = key, _Counts()
        row = self._models.get(model)
        if row is None:
            if len(self._models) >= MAX_MODELS:
                model = OTHER_MODEL
                row = self._models.get(model)
            if row is None:
                row = self._models[model] = _Counts()
                # Remembered per row rather than re-derived from the name at
                # read time, for the same reason: the overflow row's NAME
                # cannot answer the question, and it holds whatever mixture
                # arrived — so it claims no tier at all rather than the one its
                # placeholder id happens to look like.
                self._model_tiers[model] = (
                    None if model == OTHER_MODEL
                    else (LOCAL if "/" in model else CLAUDE))
        return slot[1], row, tier

    def record(self, model: str, usage: dict | None,
               seconds: float | None = None) -> None:
        """Count one finished completion, exactly as the caller was told it.

        `usage` is the relay's own `{input_tokens, output_tokens}` (the local
        tier's carries a `seconds`, and either token key may be missing or
        null). A completion with no countable numbers at all still counts as a
        completion: it happened, and a graph that dropped it would say the
        machine was idle while a model was talking.

        `seconds` is for the tier whose duration does NOT ride the wire: the
        Claude payload's `usage` is contractually exactly two token keys
        (`_ai_usage`, RH-11), so its duration is passed beside it rather than
        smuggled into a shape pages parse.
        """
        usage = usage if isinstance(usage, dict) else {}
        input_tokens = _int(usage.get("input_tokens"))
        output_tokens = _int(usage.get("output_tokens"))
        seconds = _seconds(seconds if seconds is not None
                           else usage.get("seconds"))
        with self._lock:
            for row in (*self._rows(model), self._totals):
                row.add(input_tokens, output_tokens, seconds)
            self._last_completion = self._wall()

    def record_failure(self, model: str, error_type: str) -> None:
        """Count one call that asked a model for text and got nothing back.

        Deliberately NOT counted as a completion: it generated no tokens, and
        folding it in would make "44 completions" a number that includes 3
        things that never answered. And deliberately NOT every non-2xx: a 409
        from a model that is still loading started the load it was supposed to
        start (AI-5), so it is not counted here at all — see AI-12b.
        """
        with self._lock:
            for row in (*self._rows(model), self._totals):
                row.fail()
            self._failure_types[error_type] = \
                self._failure_types.get(error_type, 0) + 1

    # -- reading ------------------------------------------------------------

    def snapshot(self, minutes: float) -> dict:
        """The last `minutes` as a DENSE series, plus totals since start.

        Dense — every bucket in the window, zeros included — because the gaps
        are the information: a chart handed only the buckets that had traffic
        would draw four prompts an hour apart as four adjacent bars.
        """
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            minutes = 15.0
        if not minutes == minutes:  # NaN compares false against itself
            minutes = 15.0
        minutes = max(1.0, min(float(MAX_MINUTES), minutes))
        wanted = max(1, min(BUCKETS, int(minutes * 60 // BUCKET_S)))

        now_wall = self._wall()
        with self._lock:
            elapsed = self._monotonic() - self._started_mono
            started_wall = self._started_wall
            last_completion = self._last_completion
            slots = {key: counts.copy()
                     for key, counts in self._slots if key >= 0}
            totals = self._totals.payload()
            tiers = {name: row.payload() for name, row in self._tiers.items()}
            models = [{"model": name,
                       "tier": self._model_tiers.get(name),
                       **row.payload()}
                      for name, row in self._models.items()]
            failures = [{"type": name, "count": count}
                        for name, count in self._failure_types.items()]
        current = int(elapsed // BUCKET_S)
        # Newest last, so the series reads left-to-right as time does. Nothing
        # before the process started (`k < 0`) is emitted: a zero bar there
        # would say this machine generated nothing at a time when this counter
        # did not exist, and the chart would rather be short than wrong.
        keys = [k for k in range(current - wanted + 1, current + 1) if k >= 0]
        window = _Counts()
        buckets = []
        for k in keys:
            counts = slots.get(k)
            if counts is not None:
                window.merge(counts)
            buckets.append({
                # `t` is the bucket's START, in epoch seconds, derived from the
                # elapsed distance to now rather than stored: see the class note
                # on why the ring itself is monotonic.
                "t": round(now_wall - (elapsed - k * BUCKET_S), 3),
                **(counts or _Counts()).payload(),
            })
        # Biggest generator first: the breakdown answers "what is spending this
        # machine's time", and that is a question about the top row. A model
        # that only ever failed still sorts in, below everything that produced
        # something — it is the row somebody is looking for.
        models.sort(key=lambda m: (-m["output_tokens"], -m["completions"],
                                   -m["failures"], m["model"]))
        failures.sort(key=lambda f: (-f["count"], f["type"]))
        return {
            "since": round(started_wall, 3),
            "now": round(now_wall, 3),
            "last_completion_at": (round(last_completion, 3)
                                   if last_completion else None),
            "bucket_seconds": BUCKET_S,
            "window_minutes": minutes,
            "retention_minutes": MAX_MINUTES,
            "totals": totals,
            "window": window.payload(),
            "tiers": tiers,
            "failure_types": failures,
            "models": models,
            "buckets": buckets,
        }


_STORE = _Store()


def record(model: str, usage: dict | None, seconds: float | None = None) -> None:
    """Count one finished `/api/ai` completion. Never raises — see AI-12."""
    try:
        _STORE.record(_name(model), usage, seconds)
    except Exception:  # pragma: no cover - a counter may not break a completion
        pass


def record_failure(model: str, error_type: str) -> None:
    """Count one `/api/ai` call that failed after reaching for a model."""
    try:
        _STORE.record_failure(_name(model), str(error_type or "error"))
    except Exception:  # pragma: no cover - same rule as record()
        pass


def _name(model) -> str:
    return model if isinstance(model, str) and model else "unknown"


def snapshot(minutes: float = 15) -> dict:
    return _STORE.snapshot(minutes)


def reset() -> None:
    """Forget everything counted so far. For tests; nothing in the app calls it
    (a process's numbers are the process's, and a Clear button would only make
    the graph lie about the window it labels itself with)."""
    _STORE.reset()

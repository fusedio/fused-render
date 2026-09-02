"""/api/ai-models/hub/* — models on the Hugging Face Hub that this machine can
actually run, told apart from the ones already on this disk.

The AI Models page (§37) answers "what did I already download". This answers the
other half — "what is there" — and the two are only useful *together*: the Hub
does not know your disk, and a browser tab open on huggingface.co cannot tell
you that the model you are looking at is already sitting in your cache, was last
read three weeks ago, and would cost nothing to open. Every result here is
cross-referenced against the local scan before it is returned, so a card can say
**downloaded**, **partly downloaded**, or **not downloaded, ~7.3 GB**.

**Every result is RUNNABLE HERE, and that constraint is the feature** (D313,
narrowed by D316). This search used to return whatever the Hub returned —
`sentence-transformers`, `bert-base-uncased`, a fill-mask model — over a page
that could load none of them, and it said so in a caption under the box:
"Search results are read-only". A browsing surface over tens of thousands of
repos in front of an app that runs about four kinds of model is not a feature
with a rough edge. So the constraint moved into the module: a row survives only
if

* its `pipeline_tag` maps, through the SAME table that decides whether a
  downloaded model gets a Load button (`registry.capability_for_task`), to a
  capability some registered runner serves. A repo with no pipeline tag at all
  is dropped too — we cannot promise something we cannot classify.
* it is not `private`. There is no step an ordinary account can take to reach
  one, so a card for it could never be actioned by the person reading it.
* its `library_name`, when it has one, is not a framework this app has no
  runner for at all. The tag above says what a model is FOR and is blind to
  what it is MADE OF, so a `text-to-image` repo of `.tflite` graphs passed it
  and arrived with a Download button in front of a load that could only fail.
  This is a NARROW test and stays narrow — see `_UNRUNNABLE_LIBRARIES`: it
  names formats with no path through any runner under any circumstances, and
  it is a denylist because the formats we DO read are open and
  community-labelled (one FLUX.2 klein loads from four different values of
  this field).

**The constraint is "an engine here can run it", not "nothing further is asked
of the user"** — that is D316's correction. Gated repos come BACK, carrying
their gate (`gated`: "auto" | "manual" | None), because a licence you accept by
signing in and clicking is a step the user can take, and several of the
best-known models on the Hub sit behind exactly one. The card says what is
needed instead of offering a button that 403s.

Every row therefore carries a non-null `capability`, which is exactly what the
page needs to hand to `POST /api/ai/runtime/download`. **The filter is by
CAPABILITY EXISTENCE, not by what resolves on this machine**: the Engines tab
lists all three capabilities on every platform, a runner that cannot run here
is a fact the download refuses with its own sentence, and making search results
depend on the host would mean the same query answered differently on two
machines for reasons neither user could see.

**Search is nevertheless a guarded POST, and the reason is worth stating.** The
app's rule is that reads are unguarded GETs (WF-5), because D36's protection is
the browser's own: a foreign page can fire a request but cannot read the reply.
That reasoning is about the RESPONSE. It says nothing about the REQUEST, and
search is the one read here that leaves the machine — it calls the Hub with the
user's token attached. Unguarded, a blind cross-origin GET could spend someone's
credential and their rate limit while learning nothing, which is a cost the
same-origin policy does not prevent. Rather than bolt a guard onto a GET and
leave a shape that contradicts the rule, the route takes the shape its effect
deserves: outward effect → POST → `X-Fused` (D36). `hub/tasks` remains a GET
beside it, because it is a static glossary and touches nothing.

**Why the server fetches, and not the page.** One place to hold the token, one
place to bound the timeout, one place to cache — and one place to audit what
this app sends to a third party. The page never talks to huggingface.co.

Three rules the outbound call follows:

* **The host is fixed.** Only the query string varies, so no request body can
  point this at another server. `HF_ENDPOINT` (the standard mirror override,
  which `huggingface_hub` honours) is the one exception: it comes from the
  user's own environment, and it is still checked to be an http(s) URL before it
  is used.
* **The token is hf's, not ours.** `hf_auth.token()` is `get_token()`, so this
  request carries whatever a `hf auth login` or the Preferences login button put
  in hf's own store — and nothing this app persists, because it persists none
  (D402).
* **The query is ENCODED, never concatenated.** `urlencode` builds it, so a
  search for `a&b=c` is a search, not a second parameter.
* **Every field is optional.** The Hub's list endpoint returns what it returns,
  and `expand[]` fields may be absent, renamed, or refused by an older
  deployment. Nothing here indexes blindly: a missing field is a field the card
  leaves out, never a 500.

**Sizes are estimates and say so.** `safetensors.parameters` is a dtype ->
count map, so the bytes are recovered by summing `count * bits / 8` — the same
arithmetic the model card does locally (HF-17), and the same `≈`. A repo
without safetensors metadata reports no size rather than a guessed one.

**And when there is no safetensors metadata, `hub/size` is the second ask —
one repo at a time, on purpose.** A GGUF, mflux or LoRA-only repo carries no
dtype map, so the arithmetic above has nothing to work from and the card shows
a dash, even though huggingface.co shows a real total on the model's own page.
That total is `usedStorage`, and it exists ONLY on the per-repo detail endpoint:
the list endpoint refuses `expand[]=usedStorage` with a 400 naming the fields it
does accept, so there is no way to fold it into the search. Getting it for a
page of results would therefore be one HTTP round trip PER ROW, on every
debounced keystroke — exactly the traffic the cache above exists to avoid. So it
is a separate route, and the page calls it lazily: only for a row that has no
estimate, and only once that card has actually scrolled into view. It is also a
DIFFERENT NUMBER and the page says so — the total of everything in the repo,
not the weights — because a tooltip claiming "computed from parameter counts"
over a figure that includes the tokenizer and three quantised copies would be a
sentence about work that never happened.

The TTL cache exists to be a good citizen: search-as-you-type would otherwise
put one request per keystroke on a public API. Identical queries inside the
window are answered from memory.
"""

from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Body, Header

from fused_render._view_url_codec import canonical_fs_path
from fused_render.ai import fit, footprints, hw_detect, speed
from fused_render.ai import tasks as ai_tasks
from fused_render.ai.registry import TEXT_GENERATION, available_runners, for_capability
from fused_render.ai.runners import formats
from fused_render.server.common import _error, _require_fused
from fused_render.ai.hub_cache import (
    _entry_is_dir,
    _quantization as _config_quantization_bits,
    _scan_repo,
    _unfinished_fetch,
    hub_cache_dir,
)

router = APIRouter()

_DEFAULT_ENDPOINT = "https://huggingface.co"

# Long enough that a burst of typing costs one request, short enough that a
# model published this morning shows up this morning.
_CACHE_TTL_S = 90.0
_CACHE_MAX = 64

# The Hub is a third party on the far side of someone's home connection. A
# search that has not answered in this long is a search the page should be told
# about, not one it should keep waiting on.
_TIMEOUT_S = 12.0

# What a card can show. Anything the deployment does not know how to expand is
# simply absent from the reply (see the module docstring).
#
# **`siblings` (D412) is the one entry that is not for the card at all — it
# is for `_model_row`'s own GGUF resolution.** Confirmed directly against the
# live API rather than assumed: the bare list endpoint returns NO `siblings`
# at all, passing `full` at ANY value (including `full=false`) turns them on
# as a side effect, and `expand[]=siblings` composed with the rest of this
# tuple returns the repo's COMPLETE file list in the LIST response itself —
# no per-repo follow-up request. That is what makes resolving a GGUF search
# result at ROW-CONSTRUCTION time (`formats.pick_gguf_file`) cheap: the data
# this needs is already in the payload this module was fetching anyway.
#
# **`gguf` (fix for code review finding 1, amending D637) is the same shape
# of free ride.** Live-verified composed with the rest of this tuple against
# both a single-file and a multi-quant GGUF repo (`hugging-quants/Llama-3.2-
# 1B-Instruct-Q4_K_M-GGUF`, `bartowski/Llama-3.2-1B-Instruct-GGUF`): the Hub
# returns `{"total": <param count>, "architecture": ..., "totalFileSize":
# ...}` for every repo that ships a `.gguf` at all, absent otherwise. `total`
# is the checkpoint's REAL parameter count read off the GGUF header itself —
# confirmed IDENTICAL (1,235,814,432) across three different quantizations
# of the same model, i.e. it does not change with quantization the way
# `totalFileSize` (the repo-wide byte total across every file the author
# shipped, the same "whole repo standing in for one file" shape `_quant`/
# `_estimated_bytes` already refuse for safetensors) does. `_model_row` uses
# `total` as `params` for a `file`-resolved row that has no safetensors
# metadata of its own — a GENUINE Hub-reported fact, not a guess from the
# repo's name — and never `totalFileSize`, which would reintroduce the exact
# repo-wide-total bug D638's own fix corrected for `usedStorage`.
_EXPAND = (
    "pipeline_tag", "downloads", "likes", "lastModified", "createdAt",
    "library_name", "gated", "private", "tags", "safetensors", "siblings",
    "config", "gguf",
)

# Sorts the page offers. Keyed so a client cannot pass an arbitrary sort field
# through to the Hub. `trending` -> `trendingScore` is verified live against the
# API. `fit` is deliberately NOT a key here — it is not a field the Hub has, so
# there is no wire value to map it to; `api_hub_search` special-cases it below,
# the same honesty `hubSearchView.ts` documents for its own page-only "size".
_SORTS = {
    "downloads": ("downloads", -1),
    "likes": ("likes", -1),
    "updated": ("lastModified", -1),
    "created": ("createdAt", -1),
    "trending": ("trendingScore", -1),
}

# Part 3's three explicit filters — "any"/unset is always the no-op default,
# so a page that never touches these controls behaves exactly as it did
# before they existed.
_FIT_LEVELS = frozenset({"easy", "tight", "any"})
_PARAMS_BANDS = frozenset({"under4b", "4to15b", "over15b", "any"})

# The one sort value this endpoint accepts that is not in `_SORTS`: "fit" asks
# for a candidate set the Hub CAN rank (downloads — the same honest default
# `size` uses on the frontend) and reorders it here, over `fit.verdict`'s own
# `score`, after the per-request join. See `api_hub_search`.
_FIT_SORT = "fit"

# The DEFAULT ranking (D639): one 0-100 number blending memory fit, a
# params-based capability proxy, speed, recency and popularity, plus a small
# on-disk bonus — see `_composite_score` and D639 for the full defense of
# the weights and the axes rejected. Like `_FIT_SORT`, not a `_SORTS` key:
# there is no Hub wire field for it either, so it asks for the same
# most-downloaded candidate set and reorders it here.
_BEST_SORT = "best"

# ---- D639's composite score: weights, defaults, and the axis curves -------
#
# Every weight below is a DELIBERATE choice, not a magic tuple — see D639 for
# the reasoning this comment only summarizes. They sum to 1.0 before the
# on-disk bonus, which is additive and outside the blend.
_WEIGHT_FIT = 0.35
_WEIGHT_CAPABILITY = 0.25
_WEIGHT_SPEED = 0.15
_WEIGHT_RECENCY = 0.15
_WEIGHT_POPULARITY = 0.10

# A small nudge for a model already on this disk (D639) — it costs nothing to
# open, so it earns a push toward the top of a tie, never enough on its own
# to out-rank a genuinely better-suited model that lives only on the Hub.
_ON_DISK_BONUS = 6.0

# D641: a row that only runs via CPU offload or CPU-only is a real cost the
# ranking must reflect — the speed axis (`_speed_score`) does NOT already
# cover this: it reads `speed.estimate_tok_s`'s `tokensPerSecond`, which is
# a MACHINE-WIDE backend guess (Metal/CUDA/CPU-ARM/…, `speed.py`'s own
# `backend_bucket`), not a per-row judgement of whether THIS repo's own
# footprint would spill out of fast memory on THIS machine — so without an
# explicit penalty here, two rows with the same speed estimate but
# different `runMode`s would tie on this axis despite one of them being
# visibly worse to actually use. Flat penalties, not a curve: this is a
# binary fact (offloaded or not), not a quantity with diminishing returns.
_CPU_OFFLOAD_PENALTY = 10.0
_CPU_ONLY_PENALTY = 20.0

# Defaults for a row with nothing to judge ONE axis by. Never 0 (reads as
# "definitely bad") and never the axis's own ceiling (reads as "definitely
# good") — "honest degrade to missing data" per the brief. Popularity is the
# one exception (see `_popularity_score`): a real absence of any download
# count is the worst case for a signal that measures nothing but downloads.
#
# **Every default here was re-checked against one question (code review
# finding 7): can this number ever score HIGHER than a real, honestly-
# measured value would for a genuinely capable/fast/recent row?** Only
# speed failed it. `_FIT_DEFAULT`/`_CAPABILITY_DEFAULT`/`_RECENCY_DEFAULT`
# each sit well below their own axis's ceiling and below what a
# comfortably-good real row scores (an "easy" fit is 100, a machine-
# saturating model's capability is ~86, a brand-new repo's recency is
# ~100) — a row with no evidence to judge those axes by never outranks one
# that is actually good on them, only ones that are actually bad, which is
# the intended "absence is not evidence of badness" reading. `_SPEED_
# DEFAULT` alone had a NAMED anchor to fail against: 70 (`_saturating`'s
# curve, corresponding to ~14.4 tok/s) sat ABOVE `_SPEED_CONVERSATIONAL_
# TOK_S` (12 tok/s) itself, so an unmeasured row scored higher than a real,
# measured, plainly-usable 8 tok/s model (score 48) ever could — absence
# beating weak-but-real evidence, not just beating bad evidence. Lowered to
# just AT the anchor (`_saturating(12, 12)` rounds to ~63.2): a row with no
# speed evidence now reads as "about as fast as barely-conversational",
# never faster than a model that is REALLY that fast.
_FIT_DEFAULT = 40.0
_CAPABILITY_DEFAULT = 30.0
_SPEED_DEFAULT = 63.0
_RECENCY_DEFAULT = 35.0

# Mirrors the frontend's identical anchor (`hubTableView.ts::SPEED_ANCHOR_
# PARAMS`, same value, same citation): below this many parameters,
# `speed.py`'s own bandwidth formula is documented as unvalidated (fixed
# per-call overhead dominates), so `speedLabel` renders the dash there
# rather than a number — and `_speed_score` (fix for finding 6) must not
# rank on a number the table itself refuses to print. Kept as a SEPARATE
# constant rather than importing the frontend's, because there is nothing
# in this backend module to import it FROM — see `_speed_score`'s own
# docstring for the bug this fixes.
_SPEED_ANCHOR_PARAMS = 1_000_000_000.0

# The capability axis turns `params` into a 0-100 score via a diminishing-
# returns curve anchored to what THIS machine could comfortably hold — so an
# 8B model on a 32GB Mac scores near its ceiling while a 137M model on the
# same machine scores near zero, without the axis needing to know anything
# about quality. The anchor assumes BF16 (2 bytes/param, the middle ground
# between full precision and a 4-bit quant) over `fit.COMFORT`'s own
# utilization ceiling — the SAME "comfortable" fraction `fit.verdict` scores
# 100 at, so this axis and the fit axis agree on what "comfortable" means
# rather than fighting over two different budgets.
_CAPABILITY_BYTES_PER_PARAM = 2.0
# No RAM reading at all (`fit.machine_ram_gb()` returned None or 0) — a
# reasonable mid-catalog anchor rather than refusing to rank the axis.
_CAPABILITY_DEFAULT_ANCHOR_PARAMS = 8_000_000_000.0
# `1 - exp(-k*x)` reaches ~86% of its ceiling at `x == 1` when `k == 2` — a
# model sitting exactly at the machine's own comfortable-capacity anchor
# should read as "near-saturated", not as "at the curve's inflection point".
_CAPABILITY_STEEPNESS = 2.0

# "Roughly conversational speed" (the brief's own words) — the tok/s past
# which more speed stops earning much on this axis. A UX anchor, not a
# hardware calibration constant, and deliberately not shared with anything
# in `speed.py`.
_SPEED_CONVERSATIONAL_TOK_S = 12.0

# Half-life, in days, for the recency decay (`0.5 ** (age_days / this)`). A
# repo exactly this old scores half of a brand-new one. One year: at four
# years old (`gpt2`, the screenshot's own example) that is `0.5**4 ≈ 6%` —
# visibly near the bottom with no hard cliff at an arbitrary birthday, which
# is the whole complaint about the old downloads-only ranking.
_RECENCY_HALF_LIFE_DAYS = 365.0

# The download count at which the (weak, log-scaled) popularity axis
# saturates near its ceiling — a handful of the most-downloaded repos on the
# Hub at any given time. Beyond this, "more downloads" stops being a
# meaningfully different fact for a TIEBREAK axis, which is all popularity
# is supposed to be here.
_POPULARITY_ANCHOR_DOWNLOADS = 5_000_000.0


def _saturating(value: float, scale: float, steepness: float = 1.0) -> float:
    """0-100 via `100 * (1 - exp(-steepness * value / scale))` — the one
    diminishing-returns curve shape every axis below shares. `<= 0` input or
    scale is a flat `0`, never a stray negative from floating point."""
    if value <= 0 or scale <= 0:
        return 0.0
    return 100.0 * (1.0 - math.exp(-steepness * value / scale))


def _capability_anchor_params(ram_gb: float | None) -> float:
    """How many BF16-equivalent parameters this machine could comfortably
    hold — the capability axis's own scale, not a size estimate anyone
    reads a number off of (that stays `fit.py`'s job, per row, off real
    evidence). See the constants above for the reasoning."""
    if not ram_gb or ram_gb <= 0:
        return _CAPABILITY_DEFAULT_ANCHOR_PARAMS
    comfortable_bytes = ram_gb * fit.GB_BYTES * fit.COMFORT
    return comfortable_bytes / _CAPABILITY_BYTES_PER_PARAM


def _capability_score(params: int | None, ram_gb: float | None) -> float:
    """The params-as-capability-proxy axis (D639) — never a guess when
    `params` is unknown, which is exactly the row shape D634/D636 already
    guard against inventing a number for."""
    if params is None or params <= 0:
        return _CAPABILITY_DEFAULT
    anchor = _capability_anchor_params(ram_gb)
    return _saturating(float(params), anchor, _CAPABILITY_STEEPNESS)


def _speed_score(speed_estimate: dict | None, params: int | None) -> float:
    """The speed axis. Saturates past `_SPEED_CONVERSATIONAL_TOK_S` by
    construction (`_saturating`), which is what stops a bandwidth formula's
    own known blind spot — an anchor-less sub-billion-parameter model's
    inflated tok/s (`speed.py:283`'s own documented gap) — from winning this
    axis outright: it saturates at the same ceiling a genuinely fast,
    correctly-modelled model already reaches, never above it.

    **`params` gates the SAME anchor `speedLabel` already refuses to print a
    number below** (fix for code review finding 6). Below `_SPEED_ANCHOR_
    PARAMS`, `speedTitle`'s own words are "a number here would not be a real
    estimate" — but this function used to score `tokensPerSecond` in
    unchanged regardless, so `_saturating(17_324.6, 12)` (the tiny CI-stub
    example the frontend's own doc cites) rounded to the axis's CEILING,
    100.0, while the cell right beside it showed a dash. One source of
    truth for "this estimate isn't real": a row below the anchor gets
    `_SPEED_DEFAULT` here too, the identical default an estimate-less row
    already gets, rather than trusting a number this app's own UI does not
    trust."""
    if params is not None and params < _SPEED_ANCHOR_PARAMS:
        return _SPEED_DEFAULT
    if not isinstance(speed_estimate, dict):
        return _SPEED_DEFAULT
    tok_s = speed_estimate.get("tokensPerSecond")
    if not isinstance(tok_s, (int, float)) or tok_s <= 0:
        return _SPEED_DEFAULT
    return _saturating(float(tok_s), _SPEED_CONVERSATIONAL_TOK_S)


def _recency_score(created: str | None) -> float:
    """The recency axis — exponential decay off `created` (ISO8601), or the
    default for a repo the Hub gave no `createdAt` for, or one this server
    could not parse. Never negative: a clock-skewed "future" timestamp is
    floored to age zero rather than scored ABOVE a brand-new repo."""
    if not created:
        return _RECENCY_DEFAULT
    try:
        parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _RECENCY_DEFAULT
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0)
    return 100.0 * (0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS))


def _popularity_score(downloads: int | None) -> float:
    """The popularity axis — log-scaled, and DELIBERATELY the one axis whose
    "nothing known" default is 0 rather than a middle value (D639): every
    other axis's default sits in the middle because the absence of evidence
    should not read as "definitely bad", but popularity is a WEAK signal by
    design (the brief's own instruction) — this axis measures downloads and
    nothing else, so a repo the Hub reports no count for genuinely has no
    popularity evidence to credit it with, unlike a missing `params` or
    `created` where the model still plainly exists and simply was not
    described."""
    if downloads is None or downloads <= 0:
        return 0.0
    return min(100.0, 100.0 * math.log1p(downloads) / math.log1p(_POPULARITY_ANCHOR_DOWNLOADS))


def _composite_raw_score(row: dict, ram_gb: float | None) -> float:
    """The composite blend BEFORE the `[0, 100]` clamp `_composite_score`
    applies for display — see that function for the full description of the
    five axes and the bonus/penalties below.

    **Kept separate from the clamp, and this is what the SORT must use**
    (fix for code review finding 8). `_ON_DISK_BONUS` and `_CPU_OFFLOAD_
    PENALTY`/`_CPU_ONLY_PENALTY` are flat adjustments that do not change one
    row's rank RELATIVE to another with the same blend — but clamping BEFORE
    comparing does: on a GPU-less machine every row takes the identical
    `_CPU_ONLY_PENALTY`, and a typical pre-penalty blend down the tail (say
    25-50) lands under 20 post-penalty, so `max(0.0, blended)` flattened a
    real slice of the tail to exactly 0.0 — a comparison the CLAMPED number
    can no longer make, silently falling the sort back to the Hub's own
    downloads order for every tied-at-zero row. `_ON_DISK_BONUS` does the
    same at the ceiling. The fix is to compare on THIS unclamped figure and
    only clamp the number actually shown."""
    fit_verdict = row.get("fit")
    fit_axis = (
        float(fit_verdict["score"])
        if isinstance(fit_verdict, dict) and isinstance(fit_verdict.get("score"), (int, float))
        else _FIT_DEFAULT
    )
    capability_axis = _capability_score(row.get("params"), ram_gb)
    speed_axis = _speed_score(row.get("speedEstimate"), row.get("params"))
    recency_axis = _recency_score(row.get("created"))
    popularity_axis = _popularity_score(row.get("downloads"))
    blended = (
        _WEIGHT_FIT * fit_axis
        + _WEIGHT_CAPABILITY * capability_axis
        + _WEIGHT_SPEED * speed_axis
        + _WEIGHT_RECENCY * recency_axis
        + _WEIGHT_POPULARITY * popularity_axis
    )
    if (row.get("local") or {}).get("state", "none") != "none":
        blended += _ON_DISK_BONUS
    run_mode = fit_verdict.get("runMode") if isinstance(fit_verdict, dict) else None
    if run_mode == "cpu-offload":
        blended -= _CPU_OFFLOAD_PENALTY
    elif run_mode == "cpu-only":
        blended -= _CPU_ONLY_PENALTY
    return blended


def _composite_score(row: dict, ram_gb: float | None) -> float:
    """`row["matchScore"]` (D639) — the composite 0-100 the DEFAULT sort
    ranks by and the merged Fit+Score cell renders, blending:

    * memory fit (`row["fit"]["score"]`, `fit.verdict`'s own 0-100) — the
      heaviest weight, because a row already carries this as a hard GATE
      elsewhere (`verdict: "no"` is dropped by default) and a row merely
      "tight" should still visibly rank below one that is "easy" rather
      than tying with it the way the pre-D639 fit-only sort did.
    * capability (`_capability_score`) — a params-based proxy for "how much
      model", scaled to what THIS machine can comfortably hold, so a
      machine that can run 8B stops surfacing 137M models ahead of it.
    * speed (`_speed_score`) — saturating past conversational pace, so an
      anchor-less tiny model's inflated tok/s cannot win outright.
    * recency (`_recency_score`) — exponential decay, so a 4-year-old repo
      sinks well below a current one with no hard cliff.
    * popularity (`_popularity_score`) — log-scaled and the LOWEST weight, a
      tiebreak rather than a ranking driver, per the brief's own instruction.

    Plus `_ON_DISK_BONUS` when this row is already on disk, MINUS
    `_CPU_OFFLOAD_PENALTY`/`_CPU_ONLY_PENALTY` when `fit.runMode` says this
    row would not run on the GPU/unified memory (D641 — the speed axis is a
    machine-wide backend guess, not a per-row judgement of THIS repo's own
    offload, so it does not already cover this). The blend is clamped to
    `[0, 100]` afterward: the bonus can push a near-ceiling row past 100 on
    its own, and a reader comparing this number to the 0-100 axes it is
    made of should never see it escape that range.

    **This is the DISPLAYED number only — `api_hub_search`'s own sort uses
    `_composite_raw_score` (unclamped) instead, see that function's own
    docstring for why (code review finding 8).**
    """
    return min(100.0, max(0.0, _composite_raw_score(row, ram_gb)))


_MAX_LIMIT = 60

# Longer than any real `org/name` and short enough that nothing hand-written
# turns into a long URL this server goes and fetches.
_MAX_ID_LEN = 200

# An unfiltered query is filtered HERE, so the Hub has to be asked for more rows
# than the page will show or a search for a common word would come back nearly
# empty after the supported-tag pass. Bounded, because this is somebody's home
# connection and a public API: four pages' worth, never more than _MAX_FETCH.
_OVERFETCH = 4
_MAX_FETCH = 200

# The tags a filter menu could offer are `ai/tasks.py`'s table, in its order —
# every `pipeline_tag` the Hub serves, vendored from `@huggingface/tasks`. This
# module used to keep its own hand-picked subset beside that; two lists of tags
# with different edit histories is exactly the drift `supported_tags` exists to
# prevent, and the local subset had already gone stale (it still offered
# `text2text-generation`, a tag the Hub retired and now returns nothing for).

# Bits per safetensors dtype, for turning a parameter count back into bytes.
# Deliberately the same table the model card uses on local files (HF-17): one
# model must not be 16GB on the Hub tab and 8GB on its card.
_DTYPE_BITS = {
    "U8": 8, "I8": 8, "F8_E4M3": 8, "F8_E5M2": 8,
    "U16": 16, "I16": 16, "F16": 16, "BF16": 16,
    "U32": 32, "I32": 32, "F32": 32,
    "U64": 64, "I64": 64, "F64": 64,
}

# The INTEGER dtypes, which store several packed weights per word in a
# quantized checkpoint (D634-amending finding, code review F2) — the same set
# `hub_cache._safetensors_params` already keys on for the identical reason on
# a downloaded model's own card. `_quant` must never report one of these as
# the model's quantization: it names the STORAGE CONTAINER (an MLX/GPTQ 4-bit
# checkpoint packs 8 weights into one `U32`), not the precision, and reporting
# it as one is a label as wrong in substance as guessing from the repo's name
# — the thing D634 exists to rule out. `_params` must not sum these RAW either
# — doing so counts storage slots, not weights, which is why a 27B MLX-4bit
# repo used to report 4.7B params (`_params_band` then misclassified it into
# "Under 8B"). See D636.
_PACKED_DTYPES = frozenset({"U8", "I8", "U16", "I16", "U32", "I32", "U64", "I64"})

# Hub `library_name` values NOTHING here can ever open, and the reason this is a
# DENYLIST rather than an allowlist.
#
# The tag filter above asks "is this KIND of model runnable"; it cannot see the
# FORMAT, so `litert-community/FLUX.2-klein-4B-LiteRT` — a `text-to-image` repo
# of `.tflite` graphs — arrived with a Download button in front of a load that
# could only fail. `grep -rn litert fused_render/` finds nothing, and no
# quantization, platform or wheel would change that.
#
# An allowlist keyed to the libraries the runners are BUILT on (diffusers,
# transformers, mlx) is the tempting version and it is wrong, because it is not
# what the repos we load actually report. Read off the Hub, not guessed: the
# FLUX.2 klein family alone loads today from `diffusers`
# (black-forest-labs/FLUX.2-klein-4B), `ggml`
# (unsloth/FLUX.2-klein-4B-GGUF, the recipe's quantized transformer),
# `diffusion-single-file` (mlx-community/FLUX.2-Klein-4B-4bit, the one repo
# `MFLUX_VARIANTS` names) and `mflux` (Runpod/FLUX.2-klein-4B-mflux-4bit) —
# four values for one model — while Whisper adds `ctranslate2` and `mlx`. A
# three-name allowlist would hide most of what works. The set of formats we
# read is open and community-labelled; the set we provably cannot is small and
# nameable, so the small one is the one written down.
#
# **What this claims and what it does not.** It claims only that the named
# framework has no runner in this app AT ALL — not that a repo passing it will
# load. It deliberately does NOT try to predict quantization, file layout or
# anything host-specific: that is D316's line, and a diffusers-tagged repo with
# a bespoke quantization still gets through and still fails, which is a
# different and harder problem. When in doubt a value is LEFT OUT: a card that
# should not be there is the status quo, and hiding a repo that would have
# loaded is a regression this filter must not introduce.
_UNRUNNABLE_LIBRARIES = frozenset({
    # Graph formats for other runtimes entirely. No runner imports any of them.
    "litert", "tflite", "coreml", "onnx", "openvino", "unity-sentis",
    "keras", "tf-keras",
    # Speech stacks that are not the two this app has. `nemo` used to need a
    # caveat here — `parakeet-mlx` read the MLX CONVERSION of a NeMo export
    # (`library_name: "mlx"`), never the archive itself — but D406 withdrew
    # that runner, so a raw `.nemo` archive (`library_name: "nemo"`) is
    # unloadable with no exception now.
    #
    # **Known, accepted gap left by that withdrawal (D406):** an MLX
    # CONVERSION of a Parakeet checkpoint reports `library_name: "mlx"`, which
    # is a format this app DOES run other things on (MLX LM, MLX FLUX, MLX
    # Whisper) — so it is not in this denylist and cannot be, without also
    # hiding every repo those runners actually load. Such a repo still passes
    # `supported_tags()` and appears in Hub search with a Download button;
    # `formats.py`'s trap catches it only AFTER download, once `config.json`
    # is on disk to read `target` from — the card then shows no engine tag and
    # no Load button, same as any other unloadable cached repo, but the
    # Download button upstream of that is honest-looking and wrong. Fixing it
    # would need a per-repo config fetch inside search results, which this
    # filter deliberately does not do (see "What this claims" above).
    "nemo", "espnet", "speechbrain", "k2",
    # Classical NLP toolkits, which publish under supported pipeline tags.
    "spacy", "fasttext", "flair", "stanza", "allennlp", "sklearn", "paddlenlp",
})

_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def hub_endpoint() -> str:
    """The Hub base URL. `HF_ENDPOINT` is the standard mirror override and is
    honoured, but only when it parses as an http(s) URL — an unset or malformed
    value falls back to the real Hub rather than being used."""
    raw = (os.environ.get("HF_ENDPOINT") or "").strip().rstrip("/")
    if not raw:
        return _DEFAULT_ENDPOINT
    parts = urlsplit(raw)
    if parts.scheme in ("http", "https") and parts.netloc:
        return raw
    return _DEFAULT_ENDPOINT


def _token() -> str | None:
    """The user's Hub token, if they have one, for gated repos and a higher rate
    limit. NEVER returned to the client, and never logged.

    `hf_auth.token()` — i.e. `huggingface_hub.get_token()` — rather than a
    resolution of its own (D402). The same credential decides this search and
    every model download, and a download resolves it by calling hf inside the
    worker; a second copy of the order here is how a page comes to report itself
    authenticated while the download beside it goes out anonymous. Read per
    request, so a login applies to the next search with no restart — and so that
    an OAuth token hf refreshed in place is the one that gets sent."""
    from fused_render.server.routers import hf_auth

    return hf_auth.token()


def supported_tags() -> tuple[str, ...]:
    """The Hub pipeline tags this app can download AND run, in menu order.

    Asked of `ai/tasks.py` rather than listed here, and that is the whole point
    of the split: it is the SAME table that decides whether a repo already on
    this disk gets a Load button, so a search result and a downloaded card
    cannot disagree about whether a kind of model is runnable — which they would
    the moment two hand-maintained lists drifted, and the drift would be
    invisible until a user downloaded 8GB of something that then refused to
    load.

    It follows that adding a runner for, say, text-to-speech makes that filter
    appear here with no edit to this module, and that removing one makes it
    vanish. `tests/test_hub_models.py` pins both directions.
    """
    return ai_tasks.supported_tags()


def _estimated_bytes(safetensors) -> int | None:
    """Bytes on disk, recovered from the dtype -> parameter-count map. None when
    the repo carries no safetensors metadata: a size we cannot compute is left
    out, never guessed from the parameter count alone."""
    if not isinstance(safetensors, dict):
        return None
    by_dtype = safetensors.get("parameters")
    if not isinstance(by_dtype, dict):
        return None
    total = 0
    for dtype, count in by_dtype.items():
        bits = _DTYPE_BITS.get(str(dtype).upper())
        if bits and isinstance(count, int) and count >= 0:
            total += count * bits // 8
    return total or None


def _quant(safetensors, file: str | None, config=None) -> str | None:
    """The row's quantization, when it is a MEASURED fact — never a guess from
    the repo's own NAME (the user's explicit complaint about llmfit's
    approach, and the reason this function exists at all).

    Three sources, in priority order, all real evidence:

    * a GGUF row's own resolved `file` — the quant token IN the filename
      llama.cpp's own ecosystem publishes it under (`formats.gguf_quant_token`),
      which is a real published fact about the one file this row would
      actually download. Wins outright when set (F3): `_model_row` clears
      `safetensors` for a `file`-resolved row precisely so a repo publishing
      BOTH formats cannot have its OTHER upload's dtype decide this row's
      label while the Download button fetches the GGUF.
    * `config`'s own `quantization`/`quantization_config` block (D636,
      amending D634) — MLX writes `quantization: {bits}`, transformers writes
      `quantization_config: {bits | load_in_4bit | load_in_8bit}`. This is the
      checkpoint's own declaration of its precision, read via the same
      `hub_cache._quantization` a downloaded model's card already trusts, so
      the two surfaces cannot label the same repo two different ways.
    * `safetensors.parameters` — the SAME dtype -> count map `_estimated_bytes`
      already sums, but **only a FLOAT dtype is reported this way.** An
      integer dtype (`_PACKED_DTYPES`) with no `config` evidence names a
      STORAGE CONTAINER, not a quantization — an MLX/GPTQ 4-bit checkpoint
      bit-packs eight weights into each `U32`, and `U32` is not "the
      quantization" any more than "gzip" would be. Among float dtypes, the one
      with the most BYTES (not the most parameters — a small embedding table
      at a different width must not decide the label for an otherwise-uniform
      model) is the one reported.

    `None` when nothing above has real evidence: an unquantifiable repo (no
    GGUF file, no declared config quantization, and either no safetensors
    metadata or only a packed integer dtype with nothing corroborating it)
    renders nothing rather than an inference.
    """
    if file:
        return formats.gguf_quant_token(file)
    if isinstance(config, dict):
        bits = _config_quantization_bits(config)
        if isinstance(bits, int) and bits > 0:
            return f"{bits}-bit"
    if isinstance(safetensors, dict):
        by_dtype = safetensors.get("parameters")
        if isinstance(by_dtype, dict):
            best_dtype, best_bytes = None, -1
            for dtype, count in by_dtype.items():
                dtype_u = str(dtype).upper()
                bits = _DTYPE_BITS.get(dtype_u)
                if not bits or not isinstance(count, int) or count < 0:
                    continue
                total_bytes = count * bits
                if total_bytes > best_bytes:
                    best_dtype, best_bytes = dtype_u, total_bytes
            if best_dtype is not None and best_dtype not in _PACKED_DTYPES:
                return best_dtype
    return None


def _params_band(params: int | None) -> str | None:
    """Which of Part 3's three size bands `params` falls in, or `None` for a
    row with no known parameter count — a band filter drops those rather than
    guessing which one they would belong to."""
    if params is None:
        return None
    if params < 4_000_000_000:
        return "under4b"
    if params <= 15_000_000_000:
        return "4to15b"
    return "over15b"


def _params(safetensors, config=None) -> int | None:
    """The repo's real parameter count — not the number of storage SLOTS a
    quantized checkpoint's dtype map counts (D636, code review F2).

    The Hub's own `safetensors.total` is the SAME undercount as summing
    `parameters` raw: verified live against `mlx-community/Qwen3.8-27B-4bit`
    (a declared 27B model), whose `total` (4,665,462,000) is exactly the sum
    of its `BF16` and `U32` element counts with NEITHER unpacked — i.e. the
    Hub does not adjust for packing either, so trusting `total` directly
    reproduces this bug rather than avoiding it. `total` is therefore only a
    fallback for the shape `parameters` cannot cover (present, but not a dict).

    When `config` declares a bit width (`_config_quantization_bits`, the same
    source `_quant` trusts), each PACKED dtype's count is expanded by how many
    that width of weights its storage width holds — `hub_cache._safetensors_params`'s
    own arithmetic, applied to the aggregate dtype->count map this endpoint
    gets instead of that function's per-tensor shapes. Without a declared bit
    width there is no honest way to un-pack a `U32` count, so it is counted as
    published (an undercount `_params_band`/callers must live with, the same
    as before this fix, rather than a guess at the packing ratio).
    """
    if not isinstance(safetensors, dict):
        return None
    by_dtype = safetensors.get("parameters")
    if isinstance(by_dtype, dict):
        quantized_bits = _config_quantization_bits(config) if isinstance(config, dict) else None
        counted = 0
        saw_any = False
        for dtype, count in by_dtype.items():
            if not isinstance(count, int) or count < 0:
                continue
            saw_any = True
            dtype_u = str(dtype).upper()
            if quantized_bits and dtype_u in _PACKED_DTYPES:
                bits = _DTYPE_BITS.get(dtype_u)
                per_word = (bits // quantized_bits) if bits else 0
                if per_word > 1:
                    count *= per_word
            counted += count
        if saw_any:
            return counted or None
    total = safetensors.get("total")
    if isinstance(total, int) and total > 0:
        return total
    return None


def _cached_dirs() -> dict[str, str]:
    """Repo id (`org/name`) -> cache folder name, for MODEL repos.

    One `scandir` of the cache root and nothing else — no `stat`, no walk, no
    reading of anyone's metadata. This runs on every search (HS-5: what is on
    this disk is never served stale), so it has to cost about nothing on a cache
    holding hundreds of repos.
    """
    dirs = {}
    try:
        entries = list(os.scandir(hub_cache_dir()))
    except OSError:
        return dirs
    for entry in entries:
        if not entry.name.startswith("models--"):
            continue  # datasets/spaces/.locks — this search is models only
        if not _entry_is_dir(entry):
            continue
        dirs["/".join(entry.name.split("--")[1:])] = entry.name
    return dirs


def _local_state(cache_dir: str, dirname: str | None) -> dict:
    """How ONE Hub result stands on this disk.

    Measuring is per RESULT, not per cached repo: a page shows at most a couple
    of dozen rows, and of those only the ones actually present cost a walk. The
    AI Models listing would answer this too, but it also reads every repo's
    model card, config and safetensors headers to say what each model is FOR —
    work no row here needs, and work a debounced keystroke must not pay for
    across an entire cache.

    `partial` is a real state, not a rounding of `downloaded`: an interrupted
    pull leaves bytes behind, and calling those "downloaded" would send someone
    to a model that cannot load.

    **The line is `_unfinished_fetch`, not "has at least one snapshot" (D424).**
    A snapshot directory is materialised FILE BY FILE — our own fetcher links
    each blob as it lands — so a repo whose first small file arrived before the
    user pressed ✕ had a revision, had no weights, and read as "downloaded"
    here. What answers honestly is the residue of the stopped fetch itself, and
    that reading lives in `ai_models` beside the listing's own, so this tab and
    that page cannot disagree about one folder.
    """
    if dirname is None:
        return {"state": "none"}
    repo_dir = os.path.join(cache_dir, dirname)
    scan = _scan_repo(repo_dir)
    return {
        "state": "partial" if _unfinished_fetch(repo_dir) else "downloaded",
        "size": scan.size,
        "files": scan.files,
        # Newest atime — "last read", the same measure the cached tab shows.
        "lastUsed": scan.atime or None,
        # Canonicalized like every other fs path the frontend gets, so it can go
        # straight to navigate(path, {isDir: true}).
        "path": canonical_fs_path(repo_dir),
        "dir": dirname,
    }


#: `base_model:<relation>:<id>` — the Hub's own tag naming what a repo was
#: derived from. `relation` is free text on the Hub's side, but the four
#: values every republish here actually uses are `quantized`, `finetune`,
#: `merge` and `adapter`; the parse itself does not narrow to that set — a
#: value the Hub adds later still groups, it would just group under a
#: relation label the frontend has not written a name for yet.
_BASE_MODEL_TAG_PREFIX = "base_model:"


def _base_model(tags) -> tuple[str | None, str | None]:
    """`(baseModel, relation)` parsed off a repo's own `tags`, or `(None,
    None)` when none of them says what this was derived from — a row
    standing alone, or a repo whose tags this server could not read at all
    (missing, not a list, or entries that are not strings).

    **Parsing only. The grouping RULE is the frontend's** (`hubFamilies.ts`)
    — this function's whole job is turning the Hub's own colon-delimited tag
    into two fields, never deciding which rows share a family or which one
    leads it. Mirrors `_gate`'s own shape: never absent from the row, so "no
    base" and "the Hub did not say" would be one field if this ever had a
    reason to conflate them — it does not, both read as `None` today, but the
    shape is deliberate rather than incidental.

    The FIRST matching tag wins where more than one exists (a repo cannot
    have two base models this table would agree on, and the Hub does not
    document what a second one would mean), and the base model id itself may
    contain colons in principle (an org or repo name never does on today's
    Hub, but nothing here assumes otherwise) — `partition`, not `split`, so
    only the first two colons are consumed and the id is whatever remains.

    **The relation-less form, `base_model:<id>` with no second colon, is a
    real tag shape** — it is what the Hub emits from a model card's own
    `base_model:` metadata when the card never set `base_model_relation:`,
    so treating it the same as a malformed tag (as an earlier version of this
    function did) silently ungrouped a large share of repos. When the
    remainder has no `:` at all, the whole remainder is the id and
    `relation` is `None` — the frontend keys a family on `baseModel` alone
    and never reads `relation`, so this costs nothing there. A malformed
    tag — an empty id either side of a colon that IS present — is still
    skipped.
    """
    if not isinstance(tags, list):
        return None, None
    for tag in tags:
        if not isinstance(tag, str) or not tag.startswith(_BASE_MODEL_TAG_PREFIX):
            continue
        rest = tag[len(_BASE_MODEL_TAG_PREFIX):]
        relation, sep, base_id = rest.partition(":")
        if not sep:
            # No second colon: the Hub's relation-less `base_model:<id>` form.
            if not relation:
                continue
            return relation, None
        if not relation or not base_id:
            continue
        return base_id, relation
    return None, None


def _gate(raw) -> str | None:
    """The Hub's `gated` field as one of None / "auto" / "manual".

    The Hub sends `False` for an open repo and the two strings for a gated one.
    Anything else truthy is read as "manual": that is the stricter of the two
    gates, and telling somebody a gate opens by signing in when it does not is
    worse than telling them to go and look.
    """
    if not raw:
        return None
    return "auto" if raw == "auto" else "manual"


def _model_row(raw: dict, cache_dir: str, dirs: dict[str, str],
               footprint_store: dict | None, hardware) -> dict | None:
    """One Hub result, joined to the local cache — or None for a row this app
    has no business offering.

    **Five ways to be dropped, and they are the search's whole contract**
    (D313, narrowed by D316, widened by D412). A row that reaches the page
    comes with a Download button or with the one sentence that says what to
    do first, so every one of these is the difference between an actionable
    card and one that apologises:

    * no id — a row the page could not act on at all.
    * a `pipeline_tag` no registered runner serves, or none at all. The tag is
      classified by `capability_for_task`, the same function the Local tab's
      Load button asks, so "searchable" and "loadable" cannot come apart.
    * `private` — visible only because this machine happens to hold a token
      that can see it. There is no step an ordinary account can take to reach
      one: no licence to accept, no queue to join, so a card for it could never
      be actioned by the person reading it.
    * a `library_name` in `_UNRUNNABLE_LIBRARIES` — the right KIND of model in
      a format nothing here reads. The tag says what a repo is FOR and cannot
      see what it is MADE OF, which is how a `.tflite` FLUX got a Download
      button; see that set for why it is a denylist and, importantly, for the
      much smaller thing it claims. A MISSING or non-string `library_name` is
      not a drop: the Hub often does not set it, and silence about the format
      is not evidence against it — only an explicit unrunnable value counts,
      the same way `_gate` reads only what the Hub actually said.
    * (D412) the capability's ACTIVE runner here declares a format tag
      (`Runner.hub_filter_tags`) and `formats.pick_gguf_file` finds nothing
      loadable among the repo's own `siblings` — see below for why this is
      the one drop reason that depends on the resolved runner rather than on
      capability existence alone, and is therefore the one exception to this
      module's "search does not depend on the host" rule.

    **(D638) A GGUF pick is not limited to the ACTIVE runner.** D412 reads as
    "the active runner decides", but a Mac with `mlx-text` active and
    `llamacpp-text` merely AVAILABLE (not preferred) was falling through
    every GGUF-only repo with `file` left `None` — no drop (mlx-text
    declares no format tag, so the D412 branch above never runs), but no
    resolution either, so `quant`/`params`/`estimatedSize`/`fit` were all
    `None` and the client's lazy per-file size lookup (keyed on `file`)
    never fired, leaving the repo's whole-repo `usedStorage` as the only
    number the page had — a multi-quant repo's TOTAL standing in for one
    file's size. So the GGUF pick is tried against the active runner FIRST
    (unchanged), and — only when the active runner declares no format tag at
    all — against the first OTHER runner this machine has AVAILABLE for the
    same capability that does declare one (`registry.available_runners`).
    Still no outbound Hub request: `pick_gguf_file` reads the same `siblings`
    already on the row. The drop rule above is UNCHANGED — it fires only when
    the ACTIVE runner itself needed a pick and found none; a repo the
    secondary runner also cannot resolve simply keeps `file=None`, same as
    before this fix, because the active runner never asked to be a gatekeeper
    for a format it does not speak.

    **`gated` is NOT a drop, and the distinction is the point** (D316). It was
    one, on the rule that every card must be downloadable — a rule drawn one
    step too tight. A gate you open by signing in and accepting a licence is
    not a repo nobody can have; several of the best-known models on the Hub sit
    behind exactly that, and a search that silently omitted them was answering
    a question nobody asked. The gate TRAVELS instead (`gated`: "auto",
    "manual" or None), so the card can say what is needed rather than offering
    a button that 403s. `manual` — the owner grants access by hand — is the one
    case that needs more than logging in, and the Hub does tell us, so it stays
    its own value; a truthy gate we do not recognise is read as `manual`, the
    stricter reading, because guessing "just sign in" about an unknown gate is
    the guess that wastes someone's afternoon.

    **Why the fifth drop is allowed to depend on the resolved runner, when
    every other check here is capability-only (D412).** `llamacpp-text`'s
    GGUF format and `mlx-text`'s safetensors are the first time one capability
    has had two genuinely different on-disk formats behind it — every earlier
    multi-runner capability's variants
    share a format, so `_UNRUNNABLE_LIBRARIES` never had to choose between
    them. A search result this machine's ACTIVE text-generation engine
    cannot resolve at all (a safetensors repo, while llamacpp is the engine
    in force) is not actionable HERE regardless of what a different engine
    elsewhere could do with it — the identical argument
    `_UNRUNNABLE_LIBRARIES` already makes about FORMAT, only now decided per
    machine because the format itself varies per machine. Two people running
    the same query see different results only after making different,
    VISIBLE choices in Preferences, never for a reason neither could see —
    which is what keeps this inside the spirit of "not by what resolves on
    this machine", even though it reads the resolved runner to answer it.
    """
    model_id = raw.get("id") or raw.get("modelId")
    if not isinstance(model_id, str) or not model_id:
        return None
    if raw.get("private"):
        return None
    reading = ai_tasks.classify(raw.get("pipeline_tag"))
    capability = reading.capability
    if capability is None:
        # HS-0: everything on this tab is runnable HERE. A ruled-out task and an
        # unrecognised one are both dropped, and now for stateable reasons —
        # `reading.support` says which, for a future face that wants to show
        # rather than hide them.
        return None
    task = reading.label
    library = raw.get("library_name") if isinstance(raw.get("library_name"), str) else None
    if library and library.lower() in _UNRUNNABLE_LIBRARIES:
        return None
    file = None
    runner = for_capability(capability)
    gguf_runner = None
    if runner is not None and "gguf" in runner.hub_filter_tags:
        gguf_runner = runner
    elif runner is not None:
        # D638: the active runner speaks no format this picker knows, but
        # another runner registered for the SAME capability may still be
        # able to load this repo — merely not preferred, not unavailable.
        # Only tried when the active runner itself declared no tag at all;
        # an active runner that DID declare one and found nothing already
        # took the drop branch above, and that verdict is the active
        # runner's alone to make (see the docstring's D638 paragraph).
        for candidate in available_runners(capability):
            if candidate.code != runner.code and "gguf" in candidate.hub_filter_tags:
                gguf_runner = candidate
                break
    if gguf_runner is not None:
        # The one runner-specific branch in this module (see the docstring's
        # last section) — `pick_gguf_file` is a GGUF-specific function, and
        # `hub_filter_tags` names the FILTER TAG generically but not the
        # picker that goes with it, since llama.cpp's is the only format
        # that needs one today. A future second format-specific runner would
        # need its own branch here, not a new item in `hub_filter_tags`.
        siblings = raw.get("siblings")
        names = ([s.get("rfilename") for s in siblings if isinstance(s, dict)]
                 if isinstance(siblings, list) else [])
        file = formats.pick_gguf_file(names)
        if file is None and gguf_runner is runner:
            # Only the ACTIVE runner's own inability to resolve a pick drops
            # the row (D412) — a secondary runner finding nothing is not a
            # verdict the active runner ever asked for.
            return None
    safetensors = raw.get("safetensors")
    config = raw.get("config")
    # F3 (code review): a `file`-resolved row (llama.cpp is the active text
    # engine and this repo also ships GGUF) downloads THAT file — a repo that
    # ALSO publishes safetensors is publishing a different upload the Download
    # button never touches. Reading size/params/quant off it here would make
    # every one of those fields describe the full-precision weights while the
    # button fetches a quantized GGUF, and — because `estimated_size` would
    # then be non-null — would suppress `HubResultRow`'s lazy per-file lookup
    # that would otherwise correct it (`wantsTotal` goes false). Treating
    # safetensors as absent for this row is what lets the file's own token
    # (`_quant`, below) and the file's own bytes (the lazy `hub/size` lookup,
    # once the fix for F1 keys it by `file` too) win instead, exactly as this
    # repo's Download button would.
    if file is not None:
        safetensors = None
    params = _params(safetensors, config)
    estimated_size = _estimated_bytes(safetensors)
    quant = _quant(safetensors, file, config)
    # `params` for a `file`-resolved (GGUF) row: the Hub's own `gguf`
    # metadata expand (`_EXPAND`, no extra request) reports the checkpoint's
    # REAL parameter count straight off the GGUF header — quantization-
    # invariant, unlike `estimatedSize` — and it costs nothing extra to keep.
    # `_capability_score` reads `params` directly with no bytes-per-param
    # conversion, so this figure alone cannot leak a memory verdict; it is
    # only ever a capability-axis input and a Params-column fact.
    if file is not None and params is None:
        gguf_meta = raw.get("gguf")
        gguf_total = gguf_meta.get("total") if isinstance(gguf_meta, dict) else None
        if isinstance(gguf_total, int) and gguf_total > 0:
            params = gguf_total
    # Fit/speed derivation for a GGUF row was DELETED (three code review
    # rounds each caught the same under-report class re-emerging — see the
    # DECISIONS.md entry recorded alongside this change). It is not a bug
    # this module can patch by recognising more quant tokens: `formats.
    # gguf_quant_token`'s regex resolves tokens (`Q8_K_XL`, `FP8`, `Q5_1`,
    # `IQ4_NL`, `Q4_1`, ...) that `fit._quant_key` has no bytes-per-param
    # entry for, and `_weight_bytes` silently falls back to
    # `DEFAULT_BYTES_PER_PARAM` (0.58, "4-bit-ish") for ANY unrecognised
    # string whenever there is no real `size_gb` to prefer — which is always
    # true here, since a `file`-resolved row has no safetensors-derived
    # `estimated_size`. A 30B `Q8_K_XL` file computes 30e9 x 0.58 = 17.4GB
    # against a real ~31.5GB and still reads "easy" on a 32GB machine. There
    # is no whitelist of tokens that makes `params x bytes-per-param` safe
    # in general — only the actual file's bytes do — so a GGUF row's `fit`
    # and `speedEstimate` are unconditionally `None` here. The client's lazy
    # per-file `hub/size` lookup, which fires anyway for every row without a
    # server-supplied `estimatedSize`, is the ONLY thing that ever produces
    # a memory verdict or a tok/s figure for such a row.
    size_gb = (estimated_size / fit.GB_BYTES) if estimated_size else None
    fit_verdict = (
        fit.verdict(capability, model_id, size_gb, params=params,
                    footprint_store=footprint_store, hardware=hardware)
        if file is None else None)
    speed_estimate = (
        speed.estimate_tok_s(size_gb, params=params, hardware=hardware)
        if file is None and capability == TEXT_GENERATION else None)
    created = raw.get("createdAt") if isinstance(raw.get("createdAt"), str) else None
    base_model, relation = _base_model(raw.get("tags"))
    return {
        "id": model_id,
        # Measured, never guessed from the repo's own name — see `_quant`'s
        # own docstring for the two sources this can come from and why a
        # third (name-matching) is deliberately not one of them.
        "quant": quant,
        # What this repo was derived from, and how — parsed off the Hub's own
        # `base_model:<relation>:<id>` tag, or (None, None) for a row standing
        # alone or one whose tags this server could not read. The GROUPING
        # rule that turns this into one row per family is the frontend's
        # (`hubFamilies.ts`) — this is the raw fact, not the judgement.
        "baseModel": base_model,
        "relation": relation,
        # {verdict, basis, footprintBytes, score, runMode} or None — the same
        # judgement `ai_runtime.describe_catalog` computes for a downloaded
        # model, over the SAME `fit.verdict` this app already trusts, so a
        # Hub row and a local card cannot disagree about what "fits" means.
        "fit": fit_verdict,
        # {tokensPerSecond, method, backend, bandwidthGbS, contextTokens,
        # calibrated, calibrationFactor} or None — text-generation only, the
        # same restriction `ai_runtime.describe_catalog` applies, because the
        # unit means nothing for the other three capabilities.
        "speedEstimate": speed_estimate,
        # ISO8601 or None. Already in `_EXPAND` and thrown away before this —
        # the field the "New" sort orders by but the page never drew.
        "created": created,
        "task": task,
        # The same sentence the local cards show on hover, so a task means the
        # same thing on both tabs or it means nothing.
        "taskHelp": ai_tasks.help_for(reading.tag),
        "pipelineTag": raw.get("pipeline_tag"),
        # Never null, by the drop rule above — it is what the page hands to
        # `POST /api/ai/runtime/download`, which needs to know which runner is
        # being asked for.
        "capability": capability,
        # The ONE file `pick_gguf_file` chose, for a GGUF row — None for
        # every other row. Carried so `POST /api/ai/runtime/download` can act
        # without re-deriving the pick from `siblings` a second time; the id
        # stays a bare repo id regardless (no `repo:file` grammar — see
        # `llama_text.py`'s own docstring for why one was rejected).
        "file": file,
        # None, "auto" or "manual" — never absent and never False, so the page
        # tests one field for "is there a gate and what kind". A missing key
        # would make "no gate" and "the Hub did not say" the same answer.
        "gated": _gate(raw.get("gated")),
        # Whatever the Hub said, minus the values dropped above — so the badge
        # on a card and the reason a card exists read off the same field.
        "library": library,
        "downloads": raw.get("downloads") if isinstance(raw.get("downloads"), int) else None,
        "likes": raw.get("likes") if isinstance(raw.get("likes"), int) else None,
        "updated": raw.get("lastModified") if isinstance(raw.get("lastModified"), str) else None,
        "params": params,
        "estimatedSize": estimated_size,
        "local": _local_state(cache_dir, dirs.get(model_id)),
        "url": f"{hub_endpoint()}/{model_id}",
    }


def _get(url: str) -> tuple[httpx.Response | None, str | None]:
    """One authenticated GET at the Hub: (response, error). Never raises — an
    unreachable Hub is a sentence on the page, not a 500 from this server.

    Shared by the two outbound calls this module makes, so a rate limit or a
    refused token reads the same whether the page was searching or asking one
    repo how big it is.
    """
    headers = {"Accept": "application/json"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.get(url, headers=headers, timeout=_TIMEOUT_S,
                             follow_redirects=True)
    except httpx.HTTPError as e:
        # Offline, DNS, TLS, timeout — all the same to the person looking at the
        # page, and all worth naming rather than spinning forever.
        return None, f"Could not reach {urlsplit(hub_endpoint()).netloc}: {e.__class__.__name__}"
    if response.status_code == 401 or response.status_code == 403:
        return None, ("The Hub refused this request. A private or gated repo needs a token — "
                      "sign in to Hugging Face in Preferences → AI, or set HF_TOKEN.")
    if response.status_code == 429:
        return None, "The Hub is rate-limiting this machine. Try again in a minute."
    if response.status_code >= 400:
        return None, f"The Hub answered {response.status_code}."
    return response, None


def _fetch(params: dict) -> tuple[list, str | None]:
    """The Hub's model list for one query: (rows, error)."""
    response, error = _get(f"{hub_endpoint()}/api/models?{urlencode(params, doseq=True)}")
    if error or response is None:
        return [], error
    try:
        payload = response.json()
    except ValueError:
        return [], "The Hub sent something that is not JSON."
    if not isinstance(payload, list):
        return [], "The Hub sent an unexpected reply."
    return payload, None


def _fetch_used_storage(model_id: str) -> tuple[int | None, str | None]:
    """One repo's total bytes on the Hub: (usedStorage, error).

    The DETAIL endpoint, which answers with an object rather than the list
    `_fetch` reads — and it is the only endpoint that will expand this field at
    all (see the module docstring). The id is quoted into the PATH with its
    slash intact and nothing else surviving, so a repo name carrying a `?`
    cannot become a second query parameter.

    Anything that is not a plain non-negative int is no answer: a string of
    digits, a float, a `True`. This route's only job is the total, so a repo the
    Hub does not measure reports None rather than falling back to the dtype map
    the search already tried.
    """
    url = (f"{hub_endpoint()}/api/models/{quote(model_id, safe='/')}"
           f"?{urlencode({'expand[]': 'usedStorage'})}")
    response, error = _get(url)
    if error or response is None:
        return None, error
    try:
        payload = response.json()
    except ValueError:
        return None, "The Hub sent something that is not JSON."
    if not isinstance(payload, dict):
        return None, "The Hub sent an unexpected reply."
    used = payload.get("usedStorage")
    if isinstance(used, bool) or not isinstance(used, int) or used < 0:
        return None, None
    return used, None


def _fetch_file_size(model_id: str, file: str) -> tuple[int | None, str | None]:
    """One named file's own bytes within a repo: (size, error) — the bytes
    the row's resolved GGUF `file` would actually add to disk, never the
    repo-wide total `_fetch_used_storage` answers.

    **`blobs=true` on the SAME detail endpoint**, not a second one — verified
    against `huggingface_hub`'s own `HfApi.model_info(files_metadata=True)`
    (`hf_api.py`: `if files_metadata: params["blobs"] = True`), which is what
    turns the bare-filename `siblings` the LIST endpoint already returns (see
    `_EXPAND`'s own docstring) into filename + size + LFS metadata on this
    per-repo endpoint. Still one GET, still one round trip.

    A file the Hub does not list among `siblings` (renamed since the search
    that resolved it, or simply wrong) reports no size rather than a stale
    guess. Anything that is not a plain non-negative int is the same "no
    answer" `_fetch_used_storage` already reads for `usedStorage`.
    """
    url = f"{hub_endpoint()}/api/models/{quote(model_id, safe='/')}?{urlencode({'blobs': 'true'})}"
    response, error = _get(url)
    if error or response is None:
        return None, error
    try:
        payload = response.json()
    except ValueError:
        return None, "The Hub sent something that is not JSON."
    if not isinstance(payload, dict):
        return None, "The Hub sent an unexpected reply."
    siblings = payload.get("siblings")
    if not isinstance(siblings, list):
        return None, None
    for sibling in siblings:
        if not isinstance(sibling, dict) or sibling.get("rfilename") != file:
            continue
        size = sibling.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return None, None
        return size, None
    return None, None


def _cached(key: tuple):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    return None


def _store(key: tuple, value: dict) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            # Oldest first — this is a politeness cache, not a correctness one,
            # so the cheapest eviction that bounds memory is the right one.
            for stale, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[: _CACHE_MAX // 2]:
                _cache.pop(stale, None)
        _cache[key] = (time.monotonic(), value)


@router.post("/api/ai-models/hub/search")
def api_hub_search(body: dict = Body(default={}), x_fused: str | None = Header(default=None)):
    """Hub models matching a query, each told apart from the local cache.

    **A POST, and guarded, even though it reads nothing on this machine.** Every
    other read in the app is an unguarded GET (WF-5), because D36's protection
    is the browser's: a foreign page can fire the request but cannot read the
    reply. That argument covers the RESPONSE and says nothing about the REQUEST,
    and this is the one read that leaves the machine — it makes an outbound call
    to the Hub carrying the user's token. A blind cross-origin GET could
    therefore spend someone's credential and their rate limit without ever
    seeing an answer.

    So the shape follows the rule rather than the rule acquiring an exception:
    a request with an outward effect is a POST, and POSTs carry `X-Fused` (D36).
    `hub/tasks` stays a GET beside it — it is a static glossary and touches
    nothing. Still NOT authentication (D3 stands); it only forces a preflight
    that a cross-origin caller cannot satisfy.

    Sync `def` on purpose: it makes one bounded outbound request and one cache
    walk, so FastAPI runs it in the threadpool rather than parking the event
    loop behind someone else's network.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    q, task = body.get("q"), body.get("task")
    # D639: the composite match score, not raw downloads, is the default —
    # see that decision for why downloads-first rewards age and CI traffic
    # over usefulness. `downloads` stays a fully explicit choice, unchanged.
    sort = body.get("sort") or _BEST_SORT
    limit = body.get("limit")
    query = (q or "").strip()[:120] if isinstance(q, str) else ""
    task_filter = (task or "").strip()[:60] if isinstance(task, str) else ""
    include_unfit = bool(body.get("includeUnfit"))
    # Part 3's three explicit filters. All server-side, and for the SAME
    # reason `includeUnfit` already is (see the `fetch` comment below): each
    # one only removes rows AFTER the Hub's own answer, so filtering them
    # client-side over an already-truncated page would under-fill it exactly
    # the way the unfit drop used to before that bug was fixed.
    fit_level = (body.get("fitLevel") or "any").strip() if isinstance(body.get("fitLevel"), str) else "any"
    # Capped like `q`/`task` above (code review F6) — a real quant token
    # (`Q4_K_M`, `4-bit`, a dtype name) is short, so an uncapped string here
    # was never functional, only an unbounded value nothing else in this
    # section had.
    quant_filter = (body.get("quant") or "").strip().upper()[:40] if isinstance(body.get("quant"), str) else ""
    params_band = (body.get("paramsBand") or "any").strip() if isinstance(body.get("paramsBand"), str) else "any"
    # Publisher/org is the one exception: `author` is a real Hub query
    # parameter (verified against `huggingface_hub.HfApi.list_models`'s own
    # `params["author"] = author`), so this narrows the WIRE request itself
    # rather than the join — the Hub does the filtering, before this route's
    # own overfetch multiplier even applies. Capped (code review F6): unlike
    # `q`/`task`/`file`, this had no length limit at all before this fix, and
    # it is the one of these four that goes straight onto the OUTBOUND Hub
    # URL as `params["author"]` — a hand-written megabyte-long value became a
    # megabyte-long URL this server would actually go and fetch, and would
    # sit in the cache key forever within the TTL. No real Hub org name
    # approaches `_MAX_ID_LEN`, the same cap `file`/a repo id already use.
    publisher = body.get("publisher")
    publisher = publisher.strip()[:_MAX_ID_LEN] if isinstance(publisher, str) and publisher.strip() else None
    if sort not in _SORTS and sort not in (_FIT_SORT, _BEST_SORT):
        return _error(f"unknown sort {sort!r}", status=400)
    if fit_level not in _FIT_LEVELS:
        return _error(f"unknown fitLevel {fit_level!r}", status=400)
    if params_band not in _PARAMS_BANDS:
        return _error(f"unknown paramsBand {params_band!r}", status=400)
    # A task nothing here can run is refused rather than searched for. The menu
    # only offers supported tags, so reaching this is either a stale page or a
    # hand-written request — and answering it with an empty grid would look
    # like "the Hub has no summarization models" rather than "this app does not
    # run them".
    if task_filter and task_filter not in supported_tags():
        return _error(f"nothing here runs {task_filter!r}", status=400)
    try:
        count = 24 if limit is None else max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        return _error("limit must be a number", status=400)

    # "fit" is not a Hub field: the candidate set the Hub is asked for is the
    # same honest default `size` uses on the frontend — most-downloaded — and
    # this route reorders it below, over `fit.verdict`'s own score, once every
    # row's fit is known.
    sort_field, direction = _SORTS[sort] if sort in _SORTS else _SORTS["downloads"]
    # With a task filter AND `includeUnfit`, the Hub already returns only rows
    # we keep and nothing here drops any more of them, so asking for `count`
    # is asking for what will be shown. In every OTHER case something between
    # here and the reply can still throw rows away: WITHOUT a task filter, the
    # supported-tag pass runs here and throws most of a page away (a search
    # for "small" sorted by downloads is mostly embedding models); and by
    # DEFAULT (`includeUnfit` false) the verdict:"no" drop below removes rows
    # a task filter alone cannot see coming (fit is a fact about this
    # machine, not about the pipeline tag). Either reason over-fetches, or
    # the reply truncates to fewer than `count` rows with headroom left
    # unused on the Hub's own answer.
    #
    # Part 3's fitLevel/quant/paramsBand filters widen the same problem: each
    # one runs in this same post-join, pre-truncation section (below) and can
    # throw away MORE of the candidate set than the unfit drop alone would —
    # a `quant=Q4_K_M` filter over a page of mostly-BF16 results, say. Any of
    # them being active forces the full overfetch multiplier, the same as an
    # unfiltered query already gets by default.
    extra_filters_active = fit_level != "any" or bool(quant_filter) or params_band != "any"
    fetch = (count if task_filter and include_unfit and not extra_filters_active
             else min(count * _OVERFETCH, _MAX_FETCH))
    params: dict[str, object] = {
        "sort": sort_field,
        "direction": direction,
        "limit": fetch,
        "expand[]": list(_EXPAND),
    }
    if query:
        params["search"] = query
    if publisher:
        params["author"] = publisher
    # (D412) When a task filter is set AND the runner actually serving that
    # capability HERE declares a format tag (`Runner.hub_filter_tags`), the
    # Hub is asked to AND it onto the pipeline-tag filter already sent —
    # confirmed live that multiple `filter=` values are ANDed, and the Hub
    # request already uses `urlencode(..., doseq=True)` below, so a list
    # value here is not a new parameter shape. Without a task filter there is
    # no single capability to resolve a runner for (a bare keyword search
    # spans every supported tag at once), so this narrowing is skipped and
    # `_model_row`'s own per-row check is the only gate — the same
    # two-layer shape the pipeline-tag filter itself already has.
    extra_tags: tuple[str, ...] = ()
    if task_filter:
        filter_capability = ai_tasks.capability_for_tag(task_filter)
        filter_runner = for_capability(filter_capability) if filter_capability else None
        if filter_runner is not None:
            extra_tags = filter_runner.hub_filter_tags
        params["filter"] = [task_filter, *extra_tags] if extra_tags else task_filter

    # `extra_tags` is part of the cache key because it is part of the
    # ANSWER: a preference switched live (CT-5, no restart needed) changes
    # which runner serves the capability and therefore what this narrows to,
    # so the SAME query/task/sort/count must not be served from a cache
    # entry built under a different engine choice. `fetch` is in the key for
    # the same reason: it is exactly how many raw rows the cached payload
    # holds, and `include_unfit` (the only other thing `fetch` depends on
    # besides `task_filter`, already in the key) can flip within the TTL —
    # toggling it off after an on request must not hand back the smaller
    # `count`-sized payload the "on" request fetched, or the verdict:"no"
    # drop below runs with no headroom left to backfill from.
    # `publisher` joins the key for the identical `extra_tags` reason: it
    # changes the WIRE request (`params["author"]`), so two different values
    # are genuinely two different Hub answers. `fit_level`/`quant_filter`/
    # `params_band` do NOT need to — they never reach the Hub, they run fresh
    # every request in the post-join section below exactly like
    # `include_unfit` already does, and their only effect on what gets FETCHED
    # is already captured by `fetch`'s own value, which is already here.
    key = (hub_endpoint(), query, task_filter, sort, count, bool(_token()),
           extra_tags, fetch, publisher)
    payload = _cached(key)
    if payload is None:
        rows, error = _fetch(params)
        if error:
            # Not a 5xx: the request was fine, the far side was not, and the
            # page has a sentence to show for it.
            return {"models": [], "error": error, "query": {
                "q": query, "task": task_filter, "sort": sort, "limit": count}}
        payload = {"raw": rows}
        _store(key, payload)

    # The JOIN is deliberately outside the cache: the Hub's answer is stable for
    # the TTL, but what is on this disk changes the moment someone deletes a
    # model, and a stale "downloaded" badge would send them to a folder that is
    # no longer there. It can afford to run every time because it is scoped to
    # the rows being returned — one scandir of the cache root, then a measure of
    # only the handful of repos that turned out to be present.
    cache_dir = hub_cache_dir()
    dirs = _cached_dirs()
    # Read ONCE per request, exactly like `ai_runtime.py:906-923` — both are a
    # `storage.read_json` open plus a parse (`hw_detect.cached_hardware()` also
    # re-checks machine identity), and this join answers as many rows as the
    # Hub sent back before truncation. `_model_row` threads both straight
    # through to `fit.verdict`/`speed.estimate_tok_s` rather than letting
    # either call resolve its own reading per row.
    footprint_store = footprints.load_store()
    hardware = hw_detect.cached_hardware()
    # `_model_row` is also the supported-tag filter (see its docstring): a row
    # this app could not download and run comes back None and never reaches the
    # page. Both the "cannot run here" drop below and the fit reorder run
    # BEFORE truncation, so `limit` keeps meaning "rows you will be shown"
    # rather than "rows the Hub was asked for".
    models = [row
              for row in (_model_row(r, cache_dir, dirs, footprint_store, hardware)
                          for r in payload["raw"] if isinstance(r, dict))
              if row is not None]

    # D639: every row gets a `matchScore` regardless of which sort was asked
    # for — the merged Fit+Score cell renders it on every row, not only when
    # ranking by it. Computed once per request off a single `machine_ram_gb()`
    # reading (already `lru_cache`d, like `fit.verdict`'s own per-row reads
    # of the same value), never per-row.
    #
    # `raw_scores` (fix for code review finding 8) keeps the UNCLAMPED blend
    # beside the displayed, clamped `matchScore` — keyed by `id(row)` rather
    # than written onto the row itself, so it never reaches the JSON reply
    # (an internal sort key, not a wire field). See `_composite_raw_score`'s
    # own docstring for why sorting on the clamped number ties a real slice
    # of a GPU-less machine's tail at exactly 0.0.
    ram_gb = fit.machine_ram_gb()
    raw_scores: dict[int, float] = {}
    for row in models:
        raw_score = _composite_raw_score(row, ram_gb)
        raw_scores[id(row)] = raw_score
        row["matchScore"] = round(min(100.0, max(0.0, raw_score)), 1)

    if sort == _BEST_SORT:
        # Descending composite score, on the UNCLAMPED figure (finding 8) —
        # stable sort keeps the Hub's own most-downloaded order as the
        # tie-break, same guarantee `_FIT_SORT` documents below.
        models.sort(key=lambda row: raw_scores.get(id(row), 0.0), reverse=True)
    elif sort == _FIT_SORT:
        # Descending score, nulls (nothing to judge) sorted last — `sort` is
        # Python's own stable sort, so ties (including every null-fit row
        # among themselves) keep the Hub's own most-downloaded ordering as
        # the tie-break, the same guarantee `bySizeAscending` documents for
        # the frontend's own page-side sort. Sorted BEFORE the unfit drop
        # below so the "would have been shown" window it counts against, and
        # the page actually returned, agree on one order.
        models.sort(key=lambda row: (row.get("fit") or {}).get("score", -1.0),
                    reverse=True)

    # Part 3's three explicit filters — run BEFORE the unfit-hide-by-default
    # block below, so a reader who has already narrowed to (say) "easy fits
    # under 4B" sees `hiddenUnfit` counted against THAT window, not the wider
    # one they asked to leave behind. A row with nothing to judge a filter
    # against (no fit, no quant, no params) is dropped rather than kept: these
    # are the reader's own explicit ask, unlike the unfit-by-default hide
    # below, which carries the on-disk exemption because IT is a default
    # nobody asked for.
    if fit_level != "any":
        allowed = {"easy"} if fit_level == "easy" else {"easy", "tight"}
        models = [row for row in models if (row.get("fit") or {}).get("verdict") in allowed]
    if quant_filter:
        models = [row for row in models if (row.get("quant") or "").upper() == quant_filter]
    if params_band != "any":
        models = [row for row in models if _params_band(row.get("params")) == params_band]

    def _on_disk(row: dict) -> bool:
        return (row.get("local") or {}).get("state", "none") != "none"

    # A `verdict: "no"` row is a fact about THIS MACHINE's memory, not about
    # how popular or well-classified the model is — dropped by default so a
    # search does not fill a page with models nothing here could hold, but
    # never silently: `hiddenUnfit` says how many, and `includeUnfit` asks for
    # them back. A row already on this disk — downloaded, or a fetch still in
    # flight — is NEVER dropped by this filter regardless of verdict: this
    # search's local join exists so someone can find a model they already
    # have (HubResults.tsx's own header comment), and a 70B repo pulled
    # months ago must still turn up when its name is searched, verdict or no.
    if include_unfit:
        hidden_unfit = 0
    else:
        # Counted only against the WINDOW this page would have shown absent
        # any hiding (`models[:count]`, in the order fixed above), not the
        # whole overfetched candidate set behind it. A search can fetch up to
        # `_OVERFETCH`x a page's worth just to backfill after this drop, and
        # counting hidden rows across that entire buffer reports a number far
        # bigger than un-hiding could ever add back to THIS page — 96 rows
        # fetched behind a 24-row page reading "71 hidden" when un-hiding only
        # ever adds a handful. This is therefore a floor on the true count
        # across the query, not an exact total: rows past the window are
        # never inspected.
        window = models[:count]
        hidden_unfit = sum(
            1 for row in window
            if (row.get("fit") or {}).get("verdict") == "no" and not _on_disk(row))
        models = [row for row in models
                  if (row.get("fit") or {}).get("verdict") != "no" or _on_disk(row)]

    models = models[:count]
    return {
        "models": models,
        "query": {"q": query, "task": task_filter, "sort": sort, "limit": count},
        "endpoint": hub_endpoint(),
        "authenticated": bool(_token()),
        "hiddenUnfit": hidden_unfit,
    }


@router.post("/api/ai-models/hub/size")
def api_hub_size(body: dict = Body(default={}), x_fused: str | None = Header(default=None)):
    """One repo's size on the Hub, for a card the dtype map could not measure
    — the repo's TOTAL by default, or one named FILE's own bytes when the
    caller already knows which single file a row would download.

    **Guarded POST for exactly the reason search is** — see `api_hub_search`:
    the cost of this request is in the REQUEST, not the reply, because it leaves
    the machine carrying the user's Hub token. Nothing about it being a "read"
    changes that, so it takes the same shape rather than the rule acquiring a
    second exception.

    **One repo per call, and the page asks lazily.** The Hub only expands
    `usedStorage` (or, for a named `file`, per-file sizes via `blobs=true`) on
    the per-repo detail endpoint, so a page of two dozen results is two dozen
    round trips — which is why this is not folded into search and why the
    frontend calls it only for a row with no estimate whose card has actually
    scrolled into view (see the module docstring).

    **`usedStorage` is NOT the search's `estimatedSize` and must not be
    presented as one**: it is everything in the repo — tokenizer, configs,
    every quantised copy the author published — rather than the weights a load
    would read. This is exactly why a GGUF row must NOT be sized off it: the
    row already knows (`_model_row`'s own `file`, via `formats.pick_gguf_file`)
    which ONE file it would actually download, and passing that back as `file`
    here gets that file's own bytes off the SAME endpoint instead of the whole
    repo's total.

    **`fit`/`speedEstimate` ride the SAME round trip, when they can be judged
    at all.** `_model_row` cannot compute either for a GGUF row during SEARCH
    — there is no safetensors dtype map to size it from, and resolving this
    very lookup per row inside a search reply would be exactly the per-row Hub
    round trip the module docstring forbids. But this lookup already happens,
    lazily, once a card scrolls into view — so the verdict is judged off
    whatever size THIS call resolved, at no extra cost, rather than left null
    forever. Two conditions, both load-bearing: a `capability` must be given
    (the caller's own row says what ladder to judge against — a bare byte
    count is not enough), and a `file`-specific size must have resolved (the
    repo-wide `usedStorage` total is deliberately NOT judged: it counts every
    quantization the author published, not the weights a load would read, so
    judging fit off it would be more likely wrong than showing nothing).
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    model_id = body.get("id")
    if not isinstance(model_id, str):
        return _error("id must be a string", status=400)
    model_id = model_id.strip()
    # `org/name`, and nothing else becomes a request. The Hub's own shape, so a
    # malformed id is a client bug rather than a URL this server goes and fetches.
    parts = model_id.split("/")
    if len(parts) != 2 or not all(parts) or len(model_id) > _MAX_ID_LEN:
        # Echoed back so a caller can see WHICH id was refused, truncated so a
        # megabyte of junk in the body is not a megabyte of error message.
        return _error(f"{model_id[:80]!r} is not a repo id of the form org/name", status=400)
    raw_file = body.get("file")
    file = raw_file.strip() if isinstance(raw_file, str) and raw_file.strip() else None
    if file is not None and len(file) > _MAX_ID_LEN:
        return _error("file is too long", status=400)
    raw_capability = body.get("capability")
    capability = raw_capability if isinstance(raw_capability, str) and raw_capability else None

    key = ("size", hub_endpoint(), model_id, file, bool(_token()))
    payload = _cached(key)
    if payload is None:
        if file:
            size, error = _fetch_file_size(model_id, file)
            if error:
                # Not a 5xx, and not cached: the request was fine, the far side
                # was not, and the next card into view should find out for itself.
                return {"id": model_id, "usedStorage": None, "fileSize": None,
                        "fit": None, "speedEstimate": None, "error": error}
            payload = {"usedStorage": None, "fileSize": size}
        else:
            used, error = _fetch_used_storage(model_id)
            if error:
                return {"id": model_id, "usedStorage": None, "fileSize": None,
                        "fit": None, "speedEstimate": None, "error": error}
            payload = {"usedStorage": used, "fileSize": None}
        _store(key, payload)

    # Judged FRESH every request, never cached alongside the raw byte count —
    # the same reason `api_hub_search`'s own join runs outside its cache:
    # hardware and the footprint store can change between two requests for the
    # same repo within the TTL, and baking a verdict into the cached payload
    # would let one go stale under the other.
    fit_verdict = None
    speed_estimate = None
    if file and capability and isinstance(payload.get("fileSize"), int):
        size_gb = payload["fileSize"] / fit.GB_BYTES
        footprint_store = footprints.load_store()
        hardware = hw_detect.cached_hardware()
        fit_verdict = fit.verdict(capability, model_id, size_gb, params=None,
                                  footprint_store=footprint_store, hardware=hardware)
        if capability == TEXT_GENERATION:
            speed_estimate = speed.estimate_tok_s(size_gb, params=None, hardware=hardware)

    return {"id": model_id, "usedStorage": payload["usedStorage"],
            "fileSize": payload.get("fileSize"), "fit": fit_verdict,
            "speedEstimate": speed_estimate, "error": None}


@router.get("/api/ai-models/hub/tasks")
def api_hub_tasks():
    """The task filters the page offers: the Hub's tag, our label for it, and
    the sentence explaining what it means.

    **Only the ones something here can run** (D313). The menu used to list
    every tag the Hub recognises — twenty-six of them, of which this app could
    load four — so the filter that looked most like the point of the feature
    was mostly a list of ways to get results with no working button.

    The candidate TAGS are listed in this module because they are Hub
    vocabulary and this is the module that talks to the Hub — a filter is only
    useful if the far side recognises it. Which of them survives is
    `supported_tags`, i.e. the registry's answer. The LABEL and the sentence
    come from the shared glossary, so a filter named "text generation" here
    means exactly what a downloaded model labelled "text generation" means on
    the other tab.

    Deriving the tags by reversing the glossary is the tempting version and it
    is wrong: several labels there ("image generation", "video generation",
    "audio generation") are our READING of a diffusers pipeline or an
    architecture suffix, not tags anyone publishes under, and a filter built
    from one would quietly return nothing.
    """
    rows = []
    for tag in supported_tags():
        rows.append({"tag": tag,
                     "label": ai_tasks.label_for(tag),
                     "help": ai_tasks.help_for(tag)})
    return {"tasks": rows}

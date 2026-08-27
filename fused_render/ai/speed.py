"""A tok/s speed estimate, with a recorded basis, and this machine's own
local calibration of it (SPEC AI-21, D524, D525).

`fit.py` answers "will this model fit"; this module answers "how fast, once
it does" — a second, independent estimate over the same inputs (a weight
size, this machine's own hardware), kept in its own module rather than
folded into `fit.py` because a footprint and a throughput are different
questions with different failure modes: a wrong footprint estimate reads as
"will not fit" on a model that would have run, a wrong speed estimate reads
as "slow" on a model that was fine — bad in different ways, worth being able
to reason about (and get wrong) independently.

**The formula** (SPEC item 9): `tok/s ~= bandwidth_GB_s / weight_gb *
BANDWIDTH_FRACTION`, the same bandwidth-bound-decode approximation the
comparative study this build derives from uses for its own equivalent
estimate — a memory-bandwidth-bound decode loop reads roughly one full pass
over the resident weights per token, so tokens per second scales with how
many WEIGHT-sized reads the memory bus can complete per second. `weight_gb`
is `fit.weight_bytes`'s own figure (SPEC item 4's `params x bpp` estimate
when the quantization is recognized, `size_gb` otherwise, D522's
precedence) — the bytes actually read per token, not `fit.footprint_bytes`'s
full figure, which also adds the KV term and the flat runtime overhead: real
memory the model occupies, but not bandwidth the decode loop repeatedly
reads through.

**When bandwidth is unknown** (`hw_detect.HardwareInfo.bandwidth_gb_s` is
`None` — an unrecognized GPU name, or no cached hardware reading at all),
this falls back to a flat per-backend tok/s constant (`PER_BACKEND_TOK_S`) —
SPEC item 9's own table, `CUDA=220, Metal+MLX=250, Metal(other)=160,
ROCm=180, SYCL=100, CPU-ARM=90, CPU-x86=70`. Which backend is inferred from
whatever this machine's cache already knows (`backend_bucket` — Apple
unified memory, else the primary GPU's vendor by name, else CPU
architecture) rather than the runner that will actually load a given model:
no caller in this codebase threads an actual runner code through to this
module today, and a coarse machine-level guess, HONESTLY labelled by its own
`method`/`backend` fields, is a better answer than inventing a runner-aware
wiring this build was not asked to build. Two backends in SPEC item 9's own
table are consequently unreachable by this module's inference alone:
`sycl` (no Intel GPU probe exists anywhere in `hw_detect.py`) and
`metal-other` (a non-MLX Metal backend — this codebase's own registry
resolves `mlx-text` ahead of every other text runner on Apple Silicon, per
D416, so nothing here would ever actually run one). Both stay in the table
for a future caller that DOES know the real runner and can look either one
up directly; this is a documented, known gap, not an oversight.

**Reads only `hw_detect.cached_hardware()`, never `detect_hardware()`/
`refresh_hardware()`** — the same boundary `fit.py` keeps (`hw_detect.py`'s
own docstring: "the ONLY function `fit.py` and `benchmark.py` may call").
This module is a third reader of that same cache, never a second prober; a
subprocess spawn on a route the AI Models picker polls is exactly what
`hw_detect`'s split exists to prevent. `estimate_tok_s`'s own `hardware=`
parameter (mirroring `fit.verdict`'s identical `_NOT_GIVEN`-sentinel shape)
is what lets a caller answering MANY catalog entries in one request read
that cache ONCE and thread it through every call — see that function's own
docstring for why this is per-request threading, never a process-wide
`functools.lru_cache`.

**Local calibration** (SPEC item 11): `median(measured_tok_s /
uncalibrated_estimate_tok_s)` over this machine's own `bench_store.py`
TEXT_GENERATION runs, anchored only on "non-trivial" models
(`params_b >= 1.0 and not is_moe` — the comparative study's own anchor rule,
llmfit's `params_b >= 1.0 and not is_moe`), clamped to `[CALIBRATION_MIN,
CALIBRATION_MAX] = [0.05, 3.0]`, and persisted to `~/.fused-render/
ai_speed_calibration.json` in the `bench_store.py`/`footprints.py` store
idiom. `estimate_tok_s` multiplies the uncalibrated figure by the stored
factor whenever one exists.

**Idempotence (SPEC item 11's own required property) holds by
CONSTRUCTION, not by an explicit "divide the old factor out" step.**
`recalibrate` computes every ratio against `_uncalibrated`'s raw formula —
NEVER against `estimate_tok_s`'s own (possibly already-calibrated) output —
so the currently-stored factor never enters the computation of its
replacement in the first place. There is nothing to divide out because it
was never let in: calling `recalibrate()` twice over the identical evidence
(the same `bench_store` runs, the same cached hardware) produces the
identical factor both times, rather than compounding a `0.6` into `0.36`
the way it would if the second call's ratios were taken against an estimate
that already had the first `0.6` baked in. `test_ai_speed.py::
test_recalibrate_is_idempotent_across_repeated_calls` pins this directly.
"""
from __future__ import annotations

import os
import platform
import statistics
import time
from typing import Any, Callable

from fused_render.ai import bench_store, catalog, fit, hw_detect, registry
from fused_render.shell import storage

VERSION = 1

#: What "the caller did not pass a hardware reading" looks like — the exact
#: sentinel shape `fit.verdict`'s own `hardware=` parameter uses (code
#: review: `fit._select_pool` used to call `hw_detect.cached_hardware()`
#: itself on every `verdict()` call, a fresh `storage.read_json` open and
#: JSON parse per catalog ROW; `estimate_tok_s` had the IDENTICAL bug on a
#: different call path and was left unfixed because this is a different
#: module). Distinct from `None`, because `hw_detect.cached_hardware()`
#: legitimately returns `None` (no probe has ever run, or the cache file is
#: corrupt) and that answer has to be usable AS a reading ("no hardware
#: known") rather than read as "go look it up yourself". `Any`-typed for the
#: identical pyright reason `fit._NOT_GIVEN`'s own comment gives: the
#: parameter below is annotated `HardwareInfo | None` (its real contract
#: once past the `is _NOT_GIVEN` check), and an inferred-`object` default
#: would otherwise widen that annotation in a way pyright cannot narrow back
#: down from the `is` check alone.
_NOT_GIVEN: Any = object()

#: SPEC item 9's own multiplier — a bandwidth-bound decode loop does not
#: sustain the theoretical peak of the memory bus (contention with the
#: allocator, non-weight traffic, imperfect overlap of compute and memory
#: fetch), so the raw `bandwidth / weight_gb` reading is scaled down to a
#: figure closer to what real hardware sustains. Matches the comparative
#: study's own equivalent constant for the identical formula shape.
BANDWIDTH_FRACTION = 0.55

#: SPEC item 9's own fallback table — tok/s assumed for a backend when this
#: machine's cached hardware reports no bandwidth figure for the device it
#: found (an unrecognized GPU name) or reports no GPU at all. See the module
#: docstring for which two of these seven this module's OWN inference can
#: never actually select (`sycl`, `metal-other`) and why they stay regardless.
PER_BACKEND_TOK_S: dict[str, float] = {
    "cuda": 220.0,
    "metal-mlx": 250.0,
    "metal-other": 160.0,
    "rocm": 180.0,
    "sycl": 100.0,
    "cpu-arm": 90.0,
    "cpu-x86": 70.0,
}

#: Substrings (matched case-insensitively against a `GpuDevice.name`) that
#: name an NVIDIA or AMD card — the same coarse, name-based approach
#: `hw_detect._apply_unified_override` already uses for its own vendor
#: detection, reused here rather than plumbing a vendor field through
#: `HardwareInfo` for the one other place that would read it.
_NVIDIA_NAME_MARKERS = ("nvidia", "geforce", "rtx", "gtx", "tesla", "quadro", "titan")
_AMD_NAME_MARKERS = ("amd", "radeon", "rx 7", "rx 6", "rx 5", "mi300", "mi250")


def backend_bucket(hardware: hw_detect.HardwareInfo | None, *,
                   is_apple_unified: bool) -> str:
    """Which `PER_BACKEND_TOK_S` row applies when bandwidth is unknown —
    also the label recorded on every estimate's `backend` field regardless
    of whether the bandwidth or the constant path was actually taken, so a
    reader can tell WHAT this machine was judged as even on the `bandwidth`
    path.

    Apple unified memory wins outright (SPEC's own `Metal+MLX` row is what
    every Apple-Silicon text/image/embedding runner in this codebase's
    registry resolves to ahead of anything else, D416) — checked first and
    unconditionally, matching `_select_pool`'s own Apple-first ordering.
    Off Apple, the primary cached GPU's name decides NVIDIA vs. AMD; no GPU,
    or a name neither vendor's markers match, falls to this machine's CPU
    architecture. See the module docstring for the two rows
    (`sycl`/`metal-other`) this inference can never reach on its own.
    """
    if is_apple_unified:
        return "metal-mlx"
    if hardware is not None and hardware.gpus:
        name = hardware.gpus[0].name.lower()
        if any(marker in name for marker in _NVIDIA_NAME_MARKERS):
            return "cuda"
        if any(marker in name for marker in _AMD_NAME_MARKERS):
            return "rocm"
    arch = platform.machine().lower()
    return "cpu-arm" if arch in ("arm64", "aarch64") else "cpu-x86"


def _uncalibrated(weight_gb: float, hardware: hw_detect.HardwareInfo | None,
                  is_apple_unified: bool) -> tuple[float, str, float | None, str]:
    """`(tokens_per_second, method, bandwidth_gb_s_used, backend)` — the raw
    formula, with NO calibration factor applied. Split out from
    `estimate_tok_s` so `recalibrate` can compute each historical run's
    ratio against exactly this, and never against a figure that might
    already carry a calibration factor — see the module docstring's
    idempotence argument."""
    backend = backend_bucket(hardware, is_apple_unified=is_apple_unified)
    bandwidth = hardware.bandwidth_gb_s if hardware is not None else None
    if isinstance(bandwidth, (int, float)) and not isinstance(bandwidth, bool) and bandwidth > 0:
        return bandwidth / weight_gb * BANDWIDTH_FRACTION, "bandwidth", bandwidth, backend
    return PER_BACKEND_TOK_S[backend], "backend-constant", None, backend


def estimate_tok_s(size_gb: float | None = None, *, params: float | str | None = None,
                   quantization: str | None = None,
                   hardware: hw_detect.HardwareInfo | None = _NOT_GIVEN) -> dict | None:
    """`{tokensPerSecond, method, backend, bandwidthGbS, contextTokens,
    calibrated, calibrationFactor}`, or `None` when even the weight size is
    unknown — mirrors `fit.verdict`'s own "`None` when nothing is known"
    contract rather than a fabricated number.

    Every field beyond `tokensPerSecond` states the BASIS of the number
    (SPEC item 9's own requirement, mirroring the shape of the comparative
    study's `EstimateBasis`) so a caller can render an honest "how sure is
    this" alongside the figure rather than a bare tok/s: `method` is
    `"bandwidth"` when this machine's cached hardware reported a real
    figure for its device, `"backend-constant"` when it fell back to
    `PER_BACKEND_TOK_S`; `bandwidthGbS` is the actual number used on the
    `bandwidth` path, `None` on the constant path; `contextTokens` is the
    same `KV_CACHE_CONTEXT_TOKENS` (8192) `fit.py`'s own KV term assumes —
    this formula does not model context-length pressure on bandwidth at
    all (a steady-state decode assumption), so surfacing the shared
    constant states what context this whole family of estimates implicitly
    assumes rather than leaving it unstated; `calibrated`/
    `calibrationFactor` say whether, and by how much, this machine's own
    measured history adjusted the raw formula.

    `size_gb`/`params`/`quantization` are the same three fields
    `fit.verdict` itself takes, fed straight to `fit.weight_bytes` — a
    caller with a catalog entry in hand passes it exactly the way it
    already does for `fit.verdict`.

    `hardware` — a caller's own `hw_detect.cached_hardware()` reading —
    exists for the exact reason, and in the exact shape, `fit.verdict`'s own
    `hardware=` parameter does (code review): this function used to call
    `hw_detect.cached_hardware()` itself, a `storage.read_json` open plus a
    JSON parse EVERY call, and `ai_runtime.describe_catalog` calls this once
    per `text-generation` catalog entry on a route the model picker polls —
    the identical cost `fit.verdict` was already fixed for, on a different
    call path that got missed the first time because `speed.py` is a
    separate module. Left unpassed (the default), this reads
    `hw_detect.cached_hardware()` itself — correct for a one-off lookup, and
    what every test and single-estimate caller still gets; passed
    explicitly, a caller answering MANY entries in one request reads the
    cache once and threads the SAME reading through every `estimate_tok_s`
    call, each one at most as fresh as that one read — `footprint_store`'s
    and `fit.verdict`'s own `hardware=` shape, not a new convention for the
    same idea. Deliberately NOT a process-wide `functools.lru_cache`:
    `hw_detect.start_hardware_refresh()` rewrites `ai_hardware.json` on a
    6-hour tick (an eGPU plugged in mid-session), and a permanently memoized
    reading would never see that — per-request threading is what lets a
    LATER request still pick up a change a cache would have hidden.
    """
    weight = fit.weight_bytes(size_gb, params, quantization)
    # MoE (code review): decode only streams the ACTIVE experts, not the
    # whole checkpoint — `weight` above is `fit.weight_bytes`'s TOTAL-param
    # figure, correct for a footprint question but wrong for THIS one, the
    # exact reason `recalibrate` already excludes MoE rows from its own
    # ratio rather than trust this formula's total-weight reading for them
    # (`_is_moe`, this module). `fit.parse_active_params` answers the
    # active-per-token count when the catalog's `params` string names one
    # (`"8B (~1B active)"`); `None` for a dense model, which leaves `weight`
    # exactly as computed above. `fit.weight_bytes`'s own recognized-quant-
    # vs-real-`size_gb` precedence does not apply here: `size_gb` is the
    # TOTAL checkpoint's resident size and has no "active-only" reading to
    # fall back to, so a recognized quantization's bytes-per-param is used
    # directly against the active count.
    active_params = fit.parse_active_params(params)
    if active_params is not None and active_params > 0:
        weight = active_params * fit.quant_bytes_per_param(quantization)
    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
        return None
    weight_gb = weight / fit.GB_BYTES
    resolved_hardware = hw_detect.cached_hardware() if hardware is _NOT_GIVEN else hardware
    is_apple = fit.is_apple_silicon()
    base, method, bandwidth, backend = _uncalibrated(weight_gb, resolved_hardware, is_apple)
    factor = stored_calibration_factor()
    tokens_per_second = base * factor if factor is not None else base
    return {
        "tokensPerSecond": tokens_per_second,
        "method": method,
        "backend": backend,
        "bandwidthGbS": bandwidth,
        "contextTokens": fit.KV_CACHE_CONTEXT_TOKENS,
        "calibrated": factor is not None,
        "calibrationFactor": factor,
    }


# --------------------------------------------------------------- SPEC item 11

#: SPEC item 11's own clamp — a machine with almost no real measurements
#: (one wildly fast or slow outlier run) must not scale every OTHER
#: estimate by an order of magnitude off a single anomalous data point,
#: and a factor of exactly 0 would silently zero out every future estimate
#: rather than merely distrusting the raw formula.
CALIBRATION_MIN = 0.05
CALIBRATION_MAX = 3.0

#: llmfit's own anchor rule, restated here for `recalibrate`'s use: a model
#: under a billion parameters is dominated by fixed per-call overhead
#: (Python dispatch, a short KV cache, graph setup) that the bandwidth
#: formula does not model at all, so its measured tok/s is not evidence
#: about how well the FORMULA predicts a real, weight-bandwidth-bound load —
#: including it would calibrate against the wrong regime.
CALIBRATION_MIN_PARAMS_B = 1.0


def _path() -> str:
    return os.path.join(storage.home_dir(), "ai_speed_calibration.json")


def stored_calibration_factor() -> float | None:
    """The last `recalibrate()`'s persisted factor, or `None` — no file yet,
    a corrupt one, or a store that has never anchored on anything. The same
    "absent/corrupt reads as no observation" contract every other store
    under `~/.fused-render` gives (`footprints.read`, `bench_store.read`)."""
    data = storage.read_json(_path())
    if not isinstance(data, dict):
        return None
    factor = data.get("factor")
    if not isinstance(factor, (int, float)) or isinstance(factor, bool):
        return None
    return float(factor)


def _catalog_entry(model_id: str) -> dict | None:
    """The curated entry for `model_id`, scanning every RUNNER's resolved
    list — `recalibrate`'s default lookup, since a `bench_store` run only
    records a bare model id and needs the curated `size_gb`/`params`/
    `quantization` triple to compute an uncalibrated estimate to compare
    against. `None` for a repo nobody curated (a cached, uncurated model a
    benchmark was run against) — that run simply cannot anchor calibration,
    the same way it cannot feed `fit.py`'s `download` rung's `params x bpp`
    estimate either.

    **`catalog.for_runner(code)`, not a raw scan of `catalog.SUGGESTIONS`**
    (Bugbot review) — the raw table is the BUILT-IN curation only,
    bypassing the `~/.fused-render/models.json` overlay (`catalog_overlay.
    apply`, SPEC AI-25) that `estimate_tok_s`'s own live estimate already
    goes through by way of `ai_runtime.describe_catalog`. A user-corrected
    `size_gb`/`params`/`quantization` was therefore honoured by the
    ESTIMATE this factor is meant to correct but ignored when COMPUTING
    that factor — the stored calibration then scaled every future estimate
    against a weight size it was never actually derived from. Iterated over
    `catalog.SUGGESTIONS`'s own KEYS (the canonical runner codes this table
    is already keyed by) rather than a fresh registry query, since those
    keys are exactly the codes `for_runner` needs and nothing here reads
    hardware availability — a runner this machine cannot currently run
    still curates entries worth calibrating against, the same way `bench_
    store`'s own history can include a run made on a different machine.
    """
    for code in catalog.SUGGESTIONS:
        for entry in catalog.for_runner(code):
            if entry.get("id") == model_id:
                return entry
    return None


def _is_moe(params: object) -> bool:
    """Whether a catalog `params` string names a Mixture-of-Experts row —
    the same `"(~<active>B active)"` qualifier `fit.parse_params`'s own
    docstring already special-cases, checked here by the same substring
    (`"active"`) rather than a second regex, since the two call sites
    agree on exactly what marks the string, not on what to DO once it does."""
    return isinstance(params, str) and "active" in params.lower()


def recalibrate(*, runs: list[dict] | None = None,
                catalog_lookup: "Callable[[str], dict | None] | None" = None) -> float | None:
    """Recompute the calibration factor from this machine's own measured
    history and persist it — SPEC item 11. Returns the new factor, or
    whatever was already stored (unchanged) when nothing in `runs` could
    anchor a computation, per `test_recalibrate_with_no_anchors_leaves_the_
    stored_value_untouched` — an empty result must not silently reset a real
    factor back to "no calibration" the next time a benchmark tab happens to
    have nothing new to report.

    `runs` defaults to `bench_store.read()` (every stored benchmark run);
    `catalog_lookup` defaults to `_catalog_entry`. Both are overridable —
    the same dependency-injection shape `fit.footprint_bytes`'s own
    `footprint_store` parameter uses — so a test can drive this against
    synthetic evidence without a real `~/.fused-render` catalog or store on
    disk.

    Only `TEXT_GENERATION` runs are eligible: `benchmark.py`'s other three
    capabilities report `secondsPerStep`/`realtimeFactor`/`textsPerSecond`,
    not a tok/s figure this formula's `measured/estimated` ratio could ever
    compare against. Within those, a run must have `ok: True` (a failed run
    measured nothing), a real `metrics.tokensPerSecond`, a curated catalog
    entry (`catalog_lookup` answers one), and pass the anchor rule
    (`CALIBRATION_MIN_PARAMS_B` and not `_is_moe`) before its ratio counts.
    """
    entries = bench_store.read() if runs is None else runs
    lookup = catalog_lookup or _catalog_entry
    hardware = hw_detect.cached_hardware()
    is_apple = fit.is_apple_silicon()
    ratios: list[float] = []
    for run in entries:
        if not isinstance(run, dict) or run.get("capability") != registry.TEXT_GENERATION:
            continue
        if not run.get("ok"):
            continue
        measured = (run.get("metrics") or {}).get("tokensPerSecond")
        if not isinstance(measured, (int, float)) or isinstance(measured, bool) or measured <= 0:
            continue
        model_id = run.get("model")
        if not isinstance(model_id, str):
            continue
        entry = lookup(model_id)
        if not isinstance(entry, dict):
            continue
        params_raw = entry.get("params")
        if _is_moe(params_raw):
            continue
        params_val = fit.parse_params(params_raw)
        if params_val is None or params_val < CALIBRATION_MIN_PARAMS_B * 1e9:
            continue
        weight = fit.weight_bytes(entry.get("size_gb"), params_raw, entry.get("quantization"))
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
            continue
        base, _method, _bandwidth, _backend = _uncalibrated(
            weight / fit.GB_BYTES, hardware, is_apple)
        if base <= 0:
            continue
        ratios.append(measured / base)
    if not ratios:
        return stored_calibration_factor()
    factor = statistics.median(ratios)
    factor = max(CALIBRATION_MIN, min(CALIBRATION_MAX, factor))
    storage.write_json(_path(), {
        "version": VERSION,
        "factor": factor,
        "computedAt": time.time(),
        "sampleCount": len(ratios),
    })
    return factor

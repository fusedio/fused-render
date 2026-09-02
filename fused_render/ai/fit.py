"""Will this model FIT on this machine? — fused_render/ai/fit.py (SPEC AI-16,
AI-16b, AI-16c, AI-19, D497, D520, D521, D522, D541).

`ai_runtime._fit_verdict` used to be handed `size_gb` and asked a memory
question, which conflates two quantities that coincide only for a
single-checkpoint text model: `ltx-video` runs `DistilledPipeline
(low_memory=True)`, which FREES the transformer and the Gemma text encoder
between stages — its peak is one STAGE, its download (AI-11a) is every byte
of TWO repos — so a 28.5GB constant answered "Likely too big for this
machine" on a machine where it demonstrably renders. A cached, uncurated repo
was worse: its `size_gb` comes from bytes on DISK including every revision
the cache holds, a figure that drifts further from memory the longer the
cache lives.

**The verdict is computed here, not by the router, which is a view.** Over
the best FOOTPRINT available, on a precedence ladder that degrades to
today's behaviour rather than replacing it — a model nobody has run yet is
judged exactly as it was before this module existed:

    measured   footprints.py, keyed <capability>/<model_id> — this model has
               RUN here and this is what it cost
    declared   an optional `resident_gb` on a curated catalog entry — the
               curator (or the runner's own docstring) knows the envelope
    download   `size_gb` (or, since AI-19, a quantization-aware estimate
               built from `params` + a KV-cache term when the caller has
               them) — nothing MEASURED is known, so this rung is honest
               about being a guess

`None` when even `size_gb` is missing, unchanged: AI-11a's rule that an
unknown size is a dash and never a guess governs the verdict too.
`resident_gb` is optional and additive, the shape AI-11i/AI-11j already
established for `recommended`/`acceptsImage` — a curator MAY answer, and
absence falls through rather than meaning anything.

`verdict()` returns `{verdict, basis, footprintBytes, score, runMode}` or
`None` — never a bare string, so the page can word a MEASURED verdict as a
fact rather than a guess (AI-16c) instead of hedging every answer the same
way, and can render a continuous bar (AI-19) instead of only a three-way
badge.

**AI-19's one coherent change to this module** (SPEC items 3-7 of the fit/
categorization/download overhaul): a flat runtime-overhead constant, a
quantization-aware weight-size table, a KV-cache term, VRAM-vs-RAM pool
selection with a run-mode concept, and a continuous Gaussian fit score that
replaces the old `EASY_FRACTION` cliff. All four land ONLY on the `download`
rung's arithmetic — `measured` and `declared` are already real, observed
numbers (a resident-set sample, a curator's own envelope), and adding a
flat overhead or a computed KV term on TOP of an observation that already
includes whatever overhead and cache the model actually used would double
count it. The `download` rung is the one place this module has ever had to
GUESS, and it is the one AI-19 makes a better guess.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import functools
import math
import os
import re
import struct
import sys
from typing import Any

from fused_render.ai import footprints, hw_detect

#: `catalog.py`'s unit for `size_gb`, and the same unit a curator writes
#: `resident_gb` in — decimal GB, matching `modelSize.ts`'s own
#: `CATALOG_GB_BYTES`. Mixing this with a binary (1024-based) reading would be
#: the same ~7% drift that module's own header explains.
GB_BYTES = 1e9

#: OS + browser + this server (SPEC AI-16b). Subtracted from total RAM before
#: any fraction is taken, so the budget SCALES: on a 16GB machine this leaves
#: a correct 8GB for everything else; on a 64GB machine the old 50%-of-total
#: rule left 32GB unusable for no stated reason, which is the defect headroom
#: thresholds exist to fix.
RESERVE_BYTES = 8e9

#: Apple's own documented meaning of `iogpu.wired_limit_mb == 0`: no explicit
#: limit has been set, so the kernel enforces its DEFAULT ceiling, which is
#: roughly this fraction of total RAM.
_DEFAULT_WIRED_FRACTION = 0.75


# --------------------------------------------------------------- SPEC item 3
#: A loaded runtime is never JUST the weights: CUDA/Metal context, cuBLAS/
#: MPS scratch buffers, the allocator's own bookkeeping, and (for a hybrid/
#: Mamba model) fixed recurrent state that is not context-scaled KV all cost
#: real memory before a single token is generated. `estimate_memory_gb_with_
#: kv`'s own comment in the comparative study this build derives from calls
#: this out by name; without it, a `download`-rung estimate at exactly a
#: machine's usable budget reads as "fits" right up until the runtime itself
#: pushes it over. Added to the `download` rung ONLY — see the module
#: docstring for why `measured`/`declared` do not get a second helping of it.
RUNTIME_OVERHEAD_BYTES = 0.5 * GB_BYTES


# --------------------------------------------------------------- SPEC item 4
#: Real-world bytes-per-parameter, NOT `bits_per_weight / 8`. A quantization
#: format's on-disk (and resident) size is bits-per-weight PLUS whatever the
#: format spends on block scales/zero-points/outlier handling — GGUF's
#: `Q4_K_M`, for instance, is nominally "4-bit" but costs 0.58 bytes/param
#: (4.64 bits) once its per-block fp16 scale factors are counted, not the
#: naive 0.5 a bits/8 reading would give. This table is the fix for the bug
#: class `catalog.py:52-58` documents: an 18.6GB actual download hand-curated
#: as 2.6GB for eighteen months, because nothing ever multiplied a real
#: parameter count by a real per-format byte cost — `size_gb` was typed by
#: a human reading a HEADLINE quant label, not computed.
#:
#: Keys are `_normalize_quant`'s output — see that function for the string
#: shapes this is meant to match (`catalog.py`'s free-text `quantization`
#: display strings, e.g. `"MLX 4-bit"`, `"GGUF Q4_K_M"`).
QUANT_BYTES_PER_PARAM: dict[str, float] = {
    "f32": 4.0,
    "f16": 2.0,
    "bf16": 2.0,
    "q8_0": 1.05,
    "q6_k": 0.80,
    "q5_k_m": 0.68,
    "q4_k_m": 0.58,
    "q4_0": 0.58,
    "q3_k_m": 0.48,
    "q2_k": 0.37,
    "mlx_8bit": 1.0,
    "mlx_4bit": 0.55,
    "awq_4bit": 0.5,
    "gptq_4bit": 0.5,
    "awq_8bit": 1.0,
    "gptq_8bit": 1.0,
}

#: What an unrecognised or absent quantization string costs — the same
#: figure `QUANT_BYTES_PER_PARAM["q4_k_m"]` gives, on the reasoning that
#: 4-bit-ish quantization is by far the most common shape a curated or
#: cached entry actually ships (SPEC item 4's own text names this default).
#: Never silently 0 or `bits/8`: an unknown quant format is not evidence a
#: model costs NOTHING, and the fallback still has to be a real number this
#: module can add a KV term and overhead to.
DEFAULT_BYTES_PER_PARAM = 0.58

#: `_normalize_quant`'s output alphabet, most-specific-first the same way
#: `hw_detect._BANDWIDTH_TABLE` orders its own substring keys — matched
#: against a string with every non-alphanumeric character stripped, so
#: `"MLX 4-bit"`, `"mlx-4bit"` and `"MLX_4_BIT"` all normalize identically.
#: `awq`/`gptq` are checked together (both are integer-quantization schemes
#: this codebase does not currently distinguish a byte cost for) rather than
#: given four separate table rows for the same two numbers.
_QUANT_TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"fp32|f32"), "f32"),
    (re.compile(r"bf16"), "bf16"),
    (re.compile(r"fp16|f16"), "f16"),
    (re.compile(r"q8_?0|q80"), "q8_0"),
    (re.compile(r"q6_?k|q6k"), "q6_k"),
    (re.compile(r"q5_?k_?m|q5km"), "q5_k_m"),
    (re.compile(r"q4_?k_?m|q4km"), "q4_k_m"),
    (re.compile(r"q4_?0|q40"), "q4_0"),
    (re.compile(r"q3_?k_?m|q3km"), "q3_k_m"),
    (re.compile(r"q2_?k|q2k"), "q2_k"),
    (re.compile(r"mlx.*8[- _]?bit|8[- _]?bit.*mlx"), "mlx_8bit"),
    (re.compile(r"mlx.*4[- _]?bit|4[- _]?bit.*mlx"), "mlx_4bit"),
    (re.compile(r"(awq|gptq).*4[- _]?bit|4[- _]?bit.*(awq|gptq)"), "awq_4bit"),
    (re.compile(r"(awq|gptq).*8[- _]?bit|8[- _]?bit.*(awq|gptq)"), "awq_8bit"),
)


def _quant_key(quantization: str | None) -> str | None:
    """`quantization` (a free-text display string, e.g. `catalog.py`'s
    `"MLX 4-bit"` / `"GGUF Q4_K_M"`) normalized down to a `QUANT_BYTES_PER_
    PARAM` key, or `None` when nothing recognisable is in it — a caller
    reads `None` as "use `DEFAULT_BYTES_PER_PARAM`", the same "absence
    falls through" contract every other optional field on this module's
    surface already keeps.

    Regexes, not a plain `key in normalized` substring table: `awq`/`gptq`
    both need "AND a bit-width" (an AWQ model with no bit-width in its label
    must not silently match `awq_4bit`), which a flat substring table cannot
    express without also matching every OTHER pattern's tokens loosely.
    """
    if not isinstance(quantization, str) or not quantization:
        return None
    normalized = re.sub(r"[^a-z0-9]", "", quantization.lower())
    for pattern, key in _QUANT_TOKEN_PATTERNS:
        if pattern.search(normalized):
            return key
    return None


def quant_bytes_per_param(quantization: str | None) -> float:
    """`QUANT_BYTES_PER_PARAM[_quant_key(quantization)]`, or
    `DEFAULT_BYTES_PER_PARAM` when the string is absent or unrecognised."""
    key = _quant_key(quantization)
    if key is None:
        return DEFAULT_BYTES_PER_PARAM
    return QUANT_BYTES_PER_PARAM.get(key, DEFAULT_BYTES_PER_PARAM)


#: `k`/`m`/`b`/`t` — the units actually spread across `catalog.py`'s
#: `params` field (`grep -oP '"params": "[^"]*"' catalog.py | sort -u`:
#: everything from `"39M"` to `"27B"`). No `k` or `t` row exists in the
#: catalog TODAY, but the pattern costs nothing extra to make complete
#: rather than silently failing the day a sub-1M embedding row or a
#: trillion-param entry is added.
_PARAMS_UNIT_MULTIPLIER: dict[str, float] = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}

#: The first `<number><unit>` token in a `catalog.py` `params` string —
#: `"1.2B"`, `"39M"`, and (deliberately, see `parse_params`) the LEADING
#: figure of `"8B (~1B active)"`. `\b` at the end so `"27Bxyz"` cannot
#: match, though nothing in the catalog is shaped like that today.
_PARAMS_VALUE_PATTERN = re.compile(r"([\d.]+)\s*([kmbt])\b", re.IGNORECASE)

#: The parenthetical `"(~<number><unit> active)"` qualifier `parse_params`
#: deliberately skips (see its own docstring: it parses the LEADING, total
#: figure for a memory/footprint question) — the ACTIVE-per-token figure,
#: for a compute/bandwidth question instead. `\(` anchors it to the
#: parenthetical specifically, so a hypothetical future string naming an
#: active count OUTSIDE parens would not silently match here.
_PARAMS_ACTIVE_PATTERN = re.compile(
    r"\(~?([\d.]+)\s*([kmbt])\b[^)]*active\)", re.IGNORECASE)


def parse_active_params(params: float | int | str | None) -> float | None:
    """The ACTIVE-per-token parameter count out of an MoE `params` display
    string (`"8B (~1B active)"` -> `1e9`) — the figure `speed.py`'s
    bandwidth-bound decode formula needs, as opposed to `parse_params`'s
    TOTAL (resident) figure that `fit.py`'s footprint/weight-size arithmetic
    correctly keeps using (all experts are resident in memory even though
    only some stream per token — see that function's own docstring for why
    it deliberately parses the leading, total number).

    `None` for a dense model's string (no `"(...active)"` qualifier to
    parse — the caller falls back to `parse_params`'s total, which for a
    dense checkpoint already equals the active count) and for anything that
    is not a string at all (a bare numeric `params` never carries this
    qualifier, by construction — `parse_params` accepts it directly with no
    string to search)."""
    if not isinstance(params, str) or not params:
        return None
    match = _PARAMS_ACTIVE_PATTERN.search(params.lower())
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value * _PARAMS_UNIT_MULTIPLIER[match.group(2).lower()]


def parse_params(params: float | int | str | None) -> float | None:
    """A raw parameter COUNT — the unit `_weight_bytes` multiplies by
    bytes-per-param — out of whatever `params` actually is: a number
    already (returned as-is, so a future caller that HAS a real count in
    hand never has to stringify it first), or one of `catalog.py`'s
    free-text `params` display strings (`"1.2B"`, `"39M"`, `"8B (~1B
    active)"`, `"4B effective"`). `None` when nothing usable is in it —
    the same "absence falls through" contract `_quant_key` gives
    `quantization`, and `_weight_bytes` reads a `None` here exactly like a
    `None` `params` argument: fall back to `size_gb`.

    **Two qualifiers need an explicit decision, not a regex that happens to
    fire on them:**

    `"8B (~1B active)"` (SPEC-MoE rows — `LiquidAI/LFM2.5-8B-A1B-*`) states
    BOTH a total parameter count and an active-per-token count. Those two
    numbers mean genuinely different things for memory: an MoE checkpoint's
    inactive experts are ordinary tensors on disk and in memory (the
    catalog's own note on that row: "MoE experts are ordinary tensors...
    the whole [checkpoint] is fetched and resident, and the win is
    arithmetic per token, not bytes") — only COMPUTE and memory BANDWIDTH
    scale with the active count, not resident weight bytes. A
    footprint estimate is a memory question, so this parses the LEADING
    (total) figure and drops the parenthetical deliberately — `_PARAMS_
    VALUE_PATTERN.search` already returns the first match in the string,
    which is the total by construction of how every MoE row here is
    written (`"<total>B (~<active>B active)"`), so no special-casing is
    needed to get the right one; this paragraph exists so that "which
    number, and why" is written down rather than left as an accident of
    regex ordering.

    `"4B effective"` (`gemma-4-e4b-it-*` — Gemma's own "E4B" naming, a
    MatFormer/nested-submodel scheme) is the OPPOSITE situation: the string
    gives exactly one number, and that number is a compute-quality-parity
    figure, not a parameter count — the checkpoint's real resident size is
    measurably larger than a literal 4B (checked against this build's own
    catalog rows: `gemma-4-e4b-it-4bit`'s curated `size_gb` is 5.2 at MLX
    4-bit, `4e9 x 0.55 GB_BYTES = 2.2e9` bytes — under HALF the curated
    figure, while treating the row as an ~8B checkpoint lands within
    single-digit percent, see the verification below). Unlike the MoE form,
    there is no second number in the string to fall back to — parsing "4B
    effective" as a literal 4B would be exactly the wrong figure for a
    resident-weight estimate, so this returns `None` for ANY string
    containing "effective" rather than silently taking the one number that
    happens to be there, and `_weight_bytes` falls back to the curated
    `size_gb` honestly instead.
    """
    if isinstance(params, bool):
        return None
    if isinstance(params, (int, float)):
        return float(params) if params > 0 else None
    if not isinstance(params, str) or not params:
        return None
    lowered = params.lower()
    if "effective" in lowered:
        return None
    match = _PARAMS_VALUE_PATTERN.search(lowered)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value * _PARAMS_UNIT_MULTIPLIER[match.group(2).lower()]


def _weight_bytes(size_gb: float | None, params: float | str | None,
                  quantization: str | None) -> float | None:
    """The `download` rung's weight-size estimate — `params x bytes-per-
    param` when a real parameter COUNT is known AND the quantization string
    was actually RECOGNISED in `QUANT_BYTES_PER_PARAM`, else the old plain
    `size_gb x GB_BYTES` reading, so a curated row whose `params` cannot be
    parsed (or was never supplied) keeps exactly today's behaviour (SPEC
    item 4's own closing line: "keep `size_gb` working as an
    override/fallback").

    **A DEFAULTED bpp does not outrank a real `size_gb`.** `quant_bytes_per_
    param` silently answers `DEFAULT_BYTES_PER_PARAM` for a quantization
    string this table has no row for — a caller-facing convenience so the
    function never returns nothing, but internally that default carries NO
    evidentiary basis for what this specific checkpoint actually costs.
    `size_gb`, when a curated row has one, is a REAL number (this build's
    own verification confirmed it for both rows this rule was built to
    catch — `prism-ml/Ternary-Bonsai-27B-mlx-2bit`'s catalog.py comment
    states its 6.1GB is "what the completed download MEASURES on disk",
    and `tonera/FLUX.2-klein-4B-int8-diffusers`'s is "the whole repo" per
    Hub metadata). Preferring an unconditional `params x bpp` produced
    15.66GB for the first (2.57x its real 6.1GB — pushes a machine's
    verdict toward tight/no for a model that comfortably fits) and 2.32GB
    for the second (0.28x its real 8.2GB — under-reports by 3.5x, the more
    dangerous direction of the two). Both were a defaulted-bpp artifact,
    not evidence about either checkpoint, and `size_gb` was right both
    times — the same "measured beats guessed" precedence this module's own
    `measured`/`declared`/`download` ladder is built on, one level down.

    A defaulted guess is used ONLY when there is no `size_gb` to prefer —
    a guess still beats nothing, but never beats a real number."""
    valid_size_gb = size_gb if (isinstance(size_gb, (int, float))
                                and not isinstance(size_gb, bool) and size_gb > 0) else None
    parsed = parse_params(params)
    recognized = _quant_key(quantization) is not None
    if parsed is not None and parsed > 0 and (recognized or valid_size_gb is None):
        return parsed * quant_bytes_per_param(quantization)
    if valid_size_gb is not None:
        return valid_size_gb * GB_BYTES
    return None


# --------------------------------------------------------------- SPEC item 5
#: llama.cpp/Ollama's own default — not a model's *advertised* maximum
#: context, which routinely runs into six figures and would wildly
#: overestimate the KV-cache term for the context length almost nobody
#: actually opens a session at. Matches the comparative study's own
#: `DEFAULT_ESTIMATION_CTX`.
KV_CACHE_CONTEXT_TOKENS = 8192

#: Bytes per cached scalar, keyed by the KV cache's OWN quantization — a
#: runtime may cache K/V at a lower precision than the weights (llama.cpp's
#: `--cache-type-k`), so this is deliberately a separate axis from
#: `QUANT_BYTES_PER_PARAM`. `None`/absent defaults to fp16, the precision
#: every runner in this codebase caches at EXCEPT `llamacpp-text`/
#: `llamacpp-text-vulkan`, whose loader (`llama_text.load()`, by way of
#: `_kv_cache_kwargs`) tries a q8_0 cache before fp16 at every rung of its
#: offload schedule — `ai_runtime._kv_geometry_kwargs` is what tells this
#: module's `kv_dtype` kwarg to say `"q8_0"` for those two runners and leave
#: every other one on this default.
KV_BYTES_PER_ELEMENT: dict[str, float] = {
    "fp16": 2.0,
    "bf16": 2.0,
    "fp8": 1.0,
    "q8_0": 1.0,
    "q4_0": 0.5,
}
DEFAULT_KV_BYTES_PER_ELEMENT = 2.0

#: A layer whose `layer_types`/`layers_block_type` string contains one of
#: these substrings holds fixed-size recurrent state (Mamba/SSM/linear-
#: attention/short-conv), not a context-scaled KV cache — Jamba, Zamba,
#: Nemotron-H and similar hybrids interleave these with real attention
#: layers, and `hub_metadata.py`'s own docstring states why counting them
#: overcounts a hybrid's KV cache: "a flat `num_hidden_layers` overcounts...
#: by however many Mamba layers it has, which carry no KV cache at all."
#: A sliding-window attention layer (`"sliding_attention"`) is NOT excluded
#: here — it still caches real K/V, just window-bounded, so it is closer to
#: this formula's assumption than to a Mamba layer's "no cache at all".
_NON_CACHING_LAYER_MARKERS = ("mamba", "linear", "recurrent", "ssm", "conv",
                              "gated_delta", "short_conv")


def _is_full_attention_layer(layer_type: str) -> bool:
    lowered = layer_type.lower()
    if "attention" not in lowered:
        return False
    return not any(marker in lowered for marker in _NON_CACHING_LAYER_MARKERS)


def _full_attention_layer_count(num_hidden_layers: int | None,
                                layer_types: list | None) -> int | None:
    """How many of a model's layers actually carry a context-scaled KV
    cache — every layer when `layer_types` is absent (the plain dense-
    transformer case this module has always assumed), or only the ones
    `_is_full_attention_layer` recognises when a hybrid/Mamba config
    publishes a layer-type list. `None` when neither is known — the KV
    term is skipped entirely rather than guessed (see `_kv_cache_bytes`)."""
    if isinstance(layer_types, list) and layer_types:
        return sum(1 for entry in layer_types
                   if isinstance(entry, str) and _is_full_attention_layer(entry))
    if isinstance(num_hidden_layers, (int, float)) and not isinstance(num_hidden_layers, bool) \
            and num_hidden_layers > 0:
        return int(num_hidden_layers)
    return None


def _kv_cache_bytes(*, num_hidden_layers: int | None = None,
                    num_key_value_heads: int | None = None,
                    num_attention_heads: int | None = None,
                    head_dim: int | None = None,
                    hidden_size: int | None = None,
                    layer_types: list | None = None,
                    kv_dtype: str | None = None,
                    context_tokens: int = KV_CACHE_CONTEXT_TOKENS) -> float:
    """`2 * n_kv_heads * head_dim * ctx * bytes_per_element * n_full_
    attention_layers` — the K and V caches together (the leading 2), in raw
    bytes. `0.0`, never a guess, when the geometry needed to compute it is
    missing: `head_dim` (read verbatim from `hub_metadata.get()`'s `headDim`
    when the Hub published it, else DERIVED here from `hidden_size /
    num_attention_heads` — `hub_metadata.py`'s own docstring assigns that
    derivation to this module rather than guessing at harvest time) and a
    full-attention layer count are both load-bearing; without either, this
    is 0 rather than a number built on a fabricated layer count or head
    width.

    `num_key_value_heads` falls back to `num_attention_heads` then a flat
    `8` (SPEC item 5) ONLY once `head_dim` and the layer count are already
    known — grouped-query attention (far more common than plain MHA in any
    model published since 2023) makes `num_attention_heads` alone a
    meaningfully worse guess for `n_kv_heads`, but the `8` floor is still a
    better answer than silently returning 0 for the vast majority of
    checkpoints that do not publish `num_key_value_heads` explicitly.
    """
    n_layers = _full_attention_layer_count(num_hidden_layers, layer_types)
    if not n_layers:
        return 0.0
    resolved_head_dim = head_dim
    if not isinstance(resolved_head_dim, (int, float)) or resolved_head_dim <= 0:
        if isinstance(hidden_size, (int, float)) and hidden_size > 0 \
                and isinstance(num_attention_heads, (int, float)) and num_attention_heads > 0:
            resolved_head_dim = hidden_size / num_attention_heads
        else:
            return 0.0
    n_kv_heads = num_key_value_heads or num_attention_heads or 8
    bytes_per_element = KV_BYTES_PER_ELEMENT.get(
        (kv_dtype or "").lower(), DEFAULT_KV_BYTES_PER_ELEMENT)
    return 2 * n_kv_heads * resolved_head_dim * context_tokens * bytes_per_element * n_layers


def weight_bytes(size_gb: float | None, params: float | str | None,
                 quantization: str | None) -> float | None:
    """Public wrapper over `_weight_bytes` — the same weight-size figure the
    `download` rung's own arithmetic uses (SPEC item 4, and D522's "measured
    beats guessed" precedence over it), exposed so a caller OUTSIDE this
    module can read a weight-size estimate without depending on a private
    name or re-deriving the recognized-quant-vs-real-`size_gb` precedence a
    second time. `speed.py` (SPEC AI-21) is the first such caller — a tok/s
    estimate needs the bytes actually moved per token, which is the WEIGHT
    size alone, not `footprint_bytes`'s full figure (which also adds the KV
    term and the flat runtime overhead — real memory the model occupies, but
    not bandwidth the decode loop repeatedly reads through)."""
    return _weight_bytes(size_gb, params, quantization)


def is_apple_silicon() -> bool:
    """Whether the Apple-Silicon wired-memory ceiling is readable on this
    machine — the exact fact `verdict()` already computes as `is_apple =
    wired_limit is not None` before selecting a pool. Exposed as its own
    public function so a second module (`speed.py`, SPEC AI-21's backend
    inference) reads the SAME probe this module already trusts, rather than
    re-deriving "is this Apple Silicon" from a fresh sysctl call or a
    `platform.machine() == "arm64"` guess that could disagree with it — an
    Intel Mac, for instance, has no wired limit to read and must answer
    `False` here exactly as `verdict()` already treats it."""
    return _wired_limit_mb() is not None


def _download_tier_bytes(size_gb: float | None, params: float | str | None,
                         quantization: str | None, *, kv_kwargs: dict) -> float | None:
    """The `download` rung's full estimate — weight size (SPEC item 4) plus
    the KV-cache term (SPEC item 5, `0.0` when the geometry to compute it is
    absent) plus the flat runtime overhead (SPEC item 3). `None` only when
    even the weight size is unknown (no `params`, no `size_gb`) — the same
    "nothing to guess from" case `footprint_bytes` has always answered
    `None` for."""
    weight = _weight_bytes(size_gb, params, quantization)
    if weight is None:
        return None
    return weight + _kv_cache_bytes(**kv_kwargs) + RUNTIME_OVERHEAD_BYTES


@functools.lru_cache(maxsize=1)
def machine_ram_gb() -> float | None:
    """Total physical memory in decimal GB, or None where it cannot be read.

    Moved here from `ai_runtime.py` unchanged (AI-16's own text: "`ram` stays
    `_machine_ram_gb`'s stdlib reading — decimal GB, cached forever") — this
    module is where the headroom arithmetic that consumes it now lives, and a
    router importing a private name out of another router would be the wrong
    direction of dependency. Stdlib only — psutil lives in the runner venvs,
    not the server's own environment (AI-2's rule). `sysconf` covers macOS and
    Linux; Windows answers through `GlobalMemoryStatusEx`. Cached forever: the
    machine's RAM does not change under a running server, and this is read
    per catalog request.
    """
    try:
        if hasattr(os, "sysconf") and os.sysconf_names.get("SC_PHYS_PAGES"):
            return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9
    except (ValueError, OSError):
        pass
    if sys.platform == "win32":  # pragma: no cover - the Windows branch
        # A literal `sys.platform == "win32"` check, not a `hasattr`/try
        # probe: pyright narrows `ctypes.windll` (only declared in typeshed
        # under this exact guard) the same way `index/worker.py`'s own
        # `GetProcessMemoryInfo` read already does — off Windows this whole
        # branch is eliminated by static platform inference, so the
        # attribute is never even type-checked, not just never executed.
        try:
            class _MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                            ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                            ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                            ("ullAvailExtendedVirtual", ctypes.c_uint64)]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / 1e9
        except Exception:  # noqa: BLE001 - a failed read must not be fatal
            pass
    return None


@functools.lru_cache(maxsize=1)
def _libc():
    """The C library handle, bound ONCE and reused for the life of the
    process — never a fresh `ctypes.CDLL(...)` per call.

    Code review caught what the docstring below originally elided: the ~2µs
    round trip was measured against an ALREADY-BOUND handle exactly like this
    one, not against `ctypes.util.find_library("c")` plus a fresh `CDLL(...)`
    per call, which is a real `dlopen` and is not free — a curated catalog
    plus cached repos is dozens of catalog entries per `GET /api/ai/catalog`,
    which the picker polls, so "cheap per call" only holds once the handle
    itself is not being re-resolved on every one of them.

    `None` off Darwin or if the library cannot be found — `_wired_limit_mb`
    degrades to "unreadable" exactly as it already does for every other
    failure mode, and that must cost the GATE, never the verdict.
    """
    if sys.platform != "darwin":
        return None
    path = ctypes.util.find_library("c")
    if not path:
        return None
    try:
        return ctypes.CDLL(path, use_errno=True)
    except OSError:
        return None


def _wired_limit_mb() -> int | None:
    """`iogpu.wired_limit_mb`, read via `ctypes.sysctlbyname` — None off
    Darwin, or if the read fails for any reason (SPEC AI-16b).

    **A `ctypes` libc call, not a subprocess.** AI-6 refuses `nvidia-smi`/
    `rocminfo` on this same per-catalog-request path because a cold spawn is
    50-500ms; measured directly on this machine (2026-08-26, Apple Silicon,
    against an already-bound handle — see `_libc`), one read-the-size-then-
    read-the-value round trip is ~2µs — a `sysctl` subprocess was never
    actually necessary here, which is worth writing down since the spec that
    asked for this probe expected one. The VALUE itself is read fresh on
    every call rather than cached (unlike the handle): `iogpu.wired_limit_mb`
    can change on a running machine, and AI-6's rule for a device probe
    applies just as well to a memory ceiling — a cached refusal or a cached
    stale reading that outlives a real change is the wrong direction to
    optimise in, and at ~2µs a call this one costs nothing to keep fresh.

    `0` is a REAL answer, Apple's own documented meaning of "no explicit
    limit — the kernel enforces its default, roughly 75% of RAM" — not
    "unset". Only a failed read (wrong platform, the sysctl name does not
    exist, a `ctypes`-level error) is `None`, and `None` must cost the GATE
    below, never the verdict — an unreadable wired limit is not evidence a
    model does not fit.
    """
    libc = _libc()
    if libc is None:
        return None
    try:
        name = b"iogpu.wired_limit_mb"
        size = ctypes.c_size_t(0)
        if libc.sysctlbyname(name, None, ctypes.byref(size), None, 0) != 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctlbyname(name, buf, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value == 4:
            return struct.unpack("<i", buf.raw)[0]
        if size.value == 8:
            return struct.unpack("<q", buf.raw)[0]
        return None  # an unexpected width — answer honestly with "unknown"
    except Exception:  # noqa: BLE001 - a probe must never break the catalog route
        return None


def _wired_limit_bytes(ram_gb: float) -> float | None:
    """The Apple-Silicon hard ceiling in bytes, or None where it does not
    apply (off Darwin) or cannot be read. See `_wired_limit_mb`."""
    limit_mb = _wired_limit_mb()
    if limit_mb is None:
        return None
    if limit_mb <= 0:
        return ram_gb * GB_BYTES * _DEFAULT_WIRED_FRACTION
    return limit_mb * 1024 * 1024


#: What "the caller did not pass a footprint store" looks like — distinct
#: from `None`, because `footprints.load_store()` legitimately returns `None`
#: (no file yet, a corrupt one, a different machine's) and that answer has to
#: be usable AS a store (meaning "nothing measured") rather than being read as
#: "go look it up yourself".
#:
#: Typed `Any` rather than left inferred: `footprint_store`'s own parameter
#: below is annotated `dict | None` (its REAL contract once past the `is
#: _NOT_GIVEN` check), and an inferred-`object` default would otherwise
#: widen that annotation to `dict | None | object` — the exact narrowing
#: gap that made `peak_from_store(footprint_store, ...)` a type error
#: (`object` is not `dict | None`) despite the `is` check right above it
#: already ruling `_NOT_GIVEN` out at runtime. `Any` here is the same
#: "trust the sentinel contract, not the inferred type" typing every
#: sentinel-default parameter needs — pyright cannot see that `is
#: _NOT_GIVEN` and `is dict-or-None` are the only two states this value
#: ever holds without being told the parameter's real type directly.
_NOT_GIVEN: Any = object()


def footprint_bytes(capability: str, model_id: str, size_gb: float | None = None,
                    resident_gb: float | None = None, *,
                    footprint_store: dict | None = _NOT_GIVEN,
                    quantization: str | None = None,
                    params: float | str | None = None,
                    num_hidden_layers: int | None = None,
                    num_key_value_heads: int | None = None,
                    num_attention_heads: int | None = None,
                    head_dim: int | None = None,
                    hidden_size: int | None = None,
                    layer_types: list | None = None,
                    kv_dtype: str | None = None) -> tuple[float | None, str | None]:
    """The best footprint available for `<capability>/<model_id>`, and which
    rung of the ladder it came from — `(bytes, basis)`, or `(None, None)`.

    Precedence: measured (this machine, this run) > declared (a curator's
    `resident_gb`) > download (SPEC AI-19: `params x bytes-per-param` +
    KV-cache + runtime overhead when the geometry is known, `size_gb` alone
    otherwise). The first rung that answers wins outright — this is NOT an
    average or a "prefer the largest", because a measured number is strictly
    better evidence than a guess about the same model, whichever guess is
    bigger.

    `footprint_store` — from `footprints.load_store()` — lets a caller
    answering MANY entries in one request (SPEC AI-16, code review) skip a
    fresh disk read and machine-identity check per entry. Left unpassed (the
    default), this does its own single `footprints.read` — correct for a
    one-off lookup, and what every test and single-verdict caller still gets.

    Every keyword from `quantization` on is optional and additive, the same
    "a caller MAY answer, and absence falls through" shape `resident_gb`
    already has. `params`/`quantization` feed the weight-size estimate
    (SPEC item 4) — `ai_runtime.describe_catalog` passes both straight off
    a curated entry's `catalog.py` fields (`entry.get("params")`,
    `entry.get("quantization")`), so this rung is LIVE for every curated
    row that has one; a cached (uncurated) entry has neither and gets
    exactly the pre-AI-19 `download` reading, plus the flat overhead (SPEC
    item 3) that now applies to every `download`-rung estimate regardless
    of how much else is known. `params` accepts `catalog.py`'s free-text
    display string directly (`"1.2B"`, `"8B (~1B active)"`) — `parse_params`
    is what turns it into a raw count, or answers `None` and falls back to
    `size_gb` when it cannot (see that function's own docstring for the
    two qualifier forms that need a deliberate decision, not a lucky
    regex). `num_hidden_layers` through `kv_dtype` feed the KV-cache term
    (SPEC item 5) — see `_kv_cache_bytes` for what each one means and what
    it falls back to. `num_hidden_layers` through `layer_types` are the same
    field NAMES `hub_metadata.get()` returns (minus its `numHiddenLayers`-
    style camelCase), so `ai_runtime._kv_geometry_kwargs` passes that dict
    through with a straight `**`-unpack; `kv_dtype` has no `hub_metadata`
    counterpart, so that same caller supplies it itself, from the runner the
    catalog row will actually load through rather than from anything
    harvested (`_KV_DTYPE_RUNNERS`).
    """
    if footprint_store is _NOT_GIVEN:
        measured = footprints.read(capability, model_id)
    else:
        measured = footprints.peak_from_store(footprint_store, capability, model_id)
    if measured is not None:
        return measured, "measured"
    if isinstance(resident_gb, (int, float)) and not isinstance(resident_gb, bool) and resident_gb > 0:
        return resident_gb * GB_BYTES, "declared"
    download = _download_tier_bytes(size_gb, params, quantization, kv_kwargs={
        "num_hidden_layers": num_hidden_layers,
        "num_key_value_heads": num_key_value_heads,
        "num_attention_heads": num_attention_heads,
        "head_dim": head_dim,
        "hidden_size": hidden_size,
        "layer_types": layer_types,
        "kv_dtype": kv_dtype,
    })
    if download is not None:
        return download, "download"
    return None, None


# --------------------------------------------------------------- SPEC item 6
#: A discrete GPU's pool that a footprint fits WITHOUT spilling to system
#: RAM at all — the load never touches host memory for weights, so this is
#: the only run mode a wired-limit-style hard ceiling would even apply to
#: off Apple Silicon.
RUN_MODE_GPU = "gpu"

#: A footprint that exceeds a discrete GPU's VRAM but fits once system RAM
#: is added to the pool — some layers resident in VRAM, the rest offloaded
#: to host memory (llama.cpp's `--n-gpu-layers`, `device_map="auto"`-style
#: sharding). Slower than a pure GPU run, but a real, commonly-used path,
#: which is why this module reports it as its OWN mode rather than folding
#: it into a plain "no" the way a naive VRAM-only check would.
RUN_MODE_CPU_OFFLOAD = "cpu-offload"

#: No usable discrete GPU was detected at all — the footprint is judged
#: against system RAM alone, exactly as this module did before AI-19.
RUN_MODE_CPU_ONLY = "cpu-only"


def _select_pool(footprint: float, usable_ram_bytes: float, *,
                 is_apple_unified: bool,
                 hardware: hw_detect.HardwareInfo | None) -> tuple[float, str]:
    """The memory pool a footprint is actually judged against, and the run
    mode that pool implies — SPEC item 6.

    **Apple Silicon's pool stays system RAM**, unconditionally: unified
    memory has no separate VRAM carveout to select between, and the wired-
    limit hard ceiling (checked before this function ever runs — see
    `verdict()`) is already the machine-specific cap that matters there.
    This is the one property AI-19 explicitly must NOT regress — the
    comparative study this build derives from detects a VRAM-alike figure
    on Apple and then never actually uses it as a ceiling; this codebase's
    wired-limit read is real evidence and stays load-bearing.

    Off Apple, `hardware` — a caller's own `hw_detect.cached_hardware()`
    reading, see `verdict()`'s docstring for why this function does not
    call it itself — answers whether a discrete, non-unified GPU exists.
    No cache yet, no GPU, or a reported VRAM of `0` all read the same way:
    judge against RAM, `cpu-only` — a machine hw_detect has not gotten to
    yet must not be silently treated as GPU-less FOREVER, but it also must
    not be treated as having a GPU it has not confirmed, so the safe
    default on "we don't know" is the one this module has always used. A
    non-Apple UNIFIED-memory device (Strix Halo, Grace/DGX Spark —
    `hw_detect._apply_unified_override`'s own cases) draws from system RAM
    exactly like Apple Silicon does, so it is treated the same way here:
    pool stays RAM, mode is `gpu` (the accelerator IS doing the work; the
    pool just is not a separate one).

    A DISCRETE GPU's pool is its VRAM alone when the footprint fits inside
    it (`gpu`), or VRAM-plus-usable-RAM when it does not but the combined
    pool would (`cpu-offload`) — the combined figure is what an offloading
    runtime can actually draw from, not a second, smaller ceiling that would
    report "no" for a load real software runs today.
    """
    if is_apple_unified:
        return usable_ram_bytes, RUN_MODE_GPU

    if hardware is None or not hardware.gpus or hardware.total_vram_gb <= 0:
        return usable_ram_bytes, RUN_MODE_CPU_ONLY

    if any(gpu.unified_memory for gpu in hardware.gpus):
        return usable_ram_bytes, RUN_MODE_GPU

    vram_bytes = hardware.total_vram_gb * GB_BYTES
    if footprint <= vram_bytes:
        return vram_bytes, RUN_MODE_GPU
    return vram_bytes + usable_ram_bytes, RUN_MODE_CPU_OFFLOAD


# --------------------------------------------------------------- SPEC item 7
#: The utilization ratio (`footprint / pool`) up to which a fit score holds
#: a flat 100 — SPEC item 7's own figure. Below this, "how much headroom is
#: left" is not worth distinguishing further: a model using 30% of the pool
#: and one using 65% are both comfortably running, and the old
#: `EASY_FRACTION = 0.6` cliff scored 59% and 61% as two different
#: verdicts for no reason a user could act on.
COMFORT = 0.70

#: Falloff width for the Gaussian past `COMFORT` — SPEC item 7's own figure.
#: Chosen (in the comparative study this derives from) so the curve replaces
#: the old step function's discontinuity ("79% scored 100, 81% scored 70")
#: with a smooth ease-down instead of a second cliff at a different ratio.
SIGMA = 0.20


def _fit_score(footprint: float, pool: float) -> float:
    """`100` at or below `COMFORT` utilization, easing down via a one-sided
    Gaussian past it, `0` once the footprint exceeds the pool outright —
    SPEC item 7. This is the continuous replacement for the old
    `EASY_FRACTION = 0.6` step function: the old rule scored every ratio
    below 60% identically (100, in the score-shaped sense) and every ratio
    from 60% to 100% identically too ("tight"), which is exactly the cliff
    a UI progress bar cannot render honestly — two footprints one byte
    apart on either side of 60% used to look nothing alike.

    `required > available` is a hard `0`, not left to the exponential's own
    decay: the tail of a Gaussian never reaches exactly zero, and a
    footprint that provably does not fit must not be permitted a small
    positive score no matter how close a huge `SIGMA` might otherwise place
    it to zero.
    """
    if pool <= 0 or footprint > pool:
        return 0.0
    ratio = footprint / pool
    z = max(0.0, (ratio - COMFORT) / SIGMA)
    return 100.0 * math.exp(-0.5 * z * z)


def verdict(capability: str, model_id: str, size_gb: float | None = None,
           resident_gb: float | None = None, *,
           footprint_store: dict | None = _NOT_GIVEN,
           hardware: hw_detect.HardwareInfo | None = _NOT_GIVEN,
           quantization: str | None = None,
           params: float | str | None = None,
           num_hidden_layers: int | None = None,
           num_key_value_heads: int | None = None,
           num_attention_heads: int | None = None,
           head_dim: int | None = None,
           hidden_size: int | None = None,
           layer_types: list | None = None,
           kv_dtype: str | None = None) -> dict | None:
    """`{verdict, basis, footprintBytes, score, runMode}`, or `None` when
    nothing is known — SPEC AI-16, AI-16c, AI-19.

    `verdict` is "easy" | "tight" | "no", now DERIVED from the continuous
    `score` (SPEC item 7) rather than computed by its own separate fraction
    check: `score == 100` (at or under `COMFORT` utilization) is "easy",
    `0 < score < 100` is "tight", `score == 0` (the footprint does not fit
    the selected pool at all) is "no". `score` itself (0-100) is exposed
    alongside the three-way verdict so the page can render a bar instead of
    only a badge.

    `runMode` (SPEC item 6) says HOW the footprint would run — `"gpu"`,
    `"cpu-offload"`, or `"cpu-only"` — over whichever pool (VRAM, a combined
    VRAM+RAM offload budget, or plain system RAM) `_select_pool` judged the
    verdict against. On Apple Silicon the pool stays system RAM exactly as
    it always has, and `runMode` reads `"gpu"` since Metal is unified-memory
    accelerated compute, not a separate carveout to distinguish from RAM.

    On Apple Silicon a footprint past the wired-memory ceiling is "no"
    regardless of the headroom arithmetic — MLX cannot allocate past it no
    matter how much of the reserve is unused. **Checked first, and entirely
    independent of `_select_pool`** — the ceiling is a real, machine-read
    number and stays load-bearing exactly as SPEC AI-19's own instructions
    require, whichever pool the rest of this function would have selected.

    A MEASURED "no" is reachable and is not a contradiction: the footprint
    store only ever holds models that ran, but the budget here is what is
    left after the reserve, so a model measured above it ran while nothing
    else was competing for memory. `basis` is what lets a reader (AI-16c)
    tell that apart from a guess.

    `footprint_store` and every keyword from `quantization` on are threaded
    straight through to `footprint_bytes` — see its own docstring.

    `hardware` — a caller's own `hw_detect.cached_hardware()` reading —
    exists for the identical reason `footprint_store` does (code review):
    `_select_pool` used to call `hw_detect.cached_hardware()` itself,
    which is a `storage.read_json` open plus a JSON parse EVERY time this
    function runs, and a catalog request answers dozens of entries through
    this same function on a route the model picker polls — the exact cost
    `machine_ram_gb` is `lru_cache`d to avoid, just not paid off here yet.
    Unlike `machine_ram_gb`, a PERMANENT cache is the wrong fix: `hw_detect
    .start_hardware_refresh()` rewrites `ai_hardware.json` on a 6-hour tick
    (an eGPU plugged in mid-session, say), and a `functools.lru_cache`d
    reading would never see that — the exact staleness the background
    refresh exists to prevent. So this is a PER-REQUEST reading threaded
    through, `footprint_store`'s own shape: left unpassed (the default),
    this calls `hw_detect.cached_hardware()` itself — correct for a
    one-off lookup, and what every test and single-verdict caller still
    gets, and still the ONLY `hw_detect` call this module makes (the
    boundary `tests/test_ai_hw_detect.py::
    test_fit_module_only_reads_the_cache_never_the_probe` pins) — passed
    explicitly, a caller answering MANY entries in one request reads the
    cache once and threads the SAME reading through every `verdict()` call,
    each one at most as fresh as that one read, exactly like
    `footprint_store`.
    """
    footprint, basis = footprint_bytes(
        capability, model_id, size_gb, resident_gb,
        footprint_store=footprint_store, quantization=quantization, params=params,
        num_hidden_layers=num_hidden_layers, num_key_value_heads=num_key_value_heads,
        num_attention_heads=num_attention_heads, head_dim=head_dim,
        hidden_size=hidden_size, layer_types=layer_types, kv_dtype=kv_dtype)
    if footprint is None:
        return None
    ram_gb = machine_ram_gb()
    if ram_gb is None or ram_gb <= 0:
        return None
    ram_bytes = ram_gb * GB_BYTES

    wired_limit = _wired_limit_bytes(ram_gb)
    is_apple = wired_limit is not None
    if is_apple and footprint > wired_limit:
        return {"verdict": "no", "basis": basis, "footprintBytes": footprint,
                "score": 0.0, "runMode": RUN_MODE_GPU}

    resolved_hardware = hw_detect.cached_hardware() if hardware is _NOT_GIVEN else hardware
    usable = max(0.0, ram_bytes - RESERVE_BYTES)
    pool, run_mode = _select_pool(footprint, usable, is_apple_unified=is_apple,
                                  hardware=resolved_hardware)

    score = _fit_score(footprint, pool)
    if score >= 100.0:
        result = "easy"
    elif score > 0.0:
        result = "tight"
    else:
        result = "no"
    return {"verdict": result, "basis": basis, "footprintBytes": footprint,
            "score": score, "runMode": run_mode}


# ---------------------------------------------------------- item 14 wiring
def available_budget_bytes(*, hardware: hw_detect.HardwareInfo | None = _NOT_GIVEN
                           ) -> float | None:
    """The largest footprint this machine could plausibly hold, in bytes —
    the ONE reusable memory-budget notion this module already computes
    (`RESERVE_BYTES`, the Apple wired-limit ceiling, VRAM-vs-RAM pool
    selection via `_select_pool`), exposed for a caller that needs a
    budget BEFORE it has a specific footprint to judge — a GGUF file
    picker (`formats.select_gguf_recipe`) deciding WHICH file to fetch in
    the first place, not `verdict()`'s own job of judging one footprint
    already chosen.

    **Reusing this rather than a second computation is the point.** Two
    independent "how much memory do I have" answers that could disagree —
    this function computing one figure, `verdict()` computing a different
    one for the SAME machine — would be strictly worse than the single
    hard-coded recipe table item 14 replaces: a picker that fetches by ONE
    budget and a fit badge that judges by ANOTHER would show a model as
    both "the one we picked for your machine" and "tight"/"no" in the
    same breath. So this shares every input `verdict()` does: the wired-
    limit ceiling on Apple Silicon (checked first, exactly like `verdict()`
    — MLX cannot allocate past it regardless of the pool arithmetic below),
    and `_select_pool` off Apple.

    **The COMBINED VRAM+RAM offload ceiling, not VRAM alone, on a discrete
    GPU.** `_select_pool` normally picks between the two by comparing a
    SPECIFIC footprint against VRAM — but a budget computed before any
    footprint exists has nothing to compare, so this asks for the ceiling
    `_select_pool` would report for a footprint too large to fit VRAM by
    itself (`math.inf`), which is exactly its COMBINED-pool branch: the
    true maximum this machine could ever hold via `llama_text._offload_
    schedule`'s existing CPU-offload backoff. A caller that then picks a
    file BELOW even the VRAM-alone figure loses nothing — `_select_pool`'s
    branches only change how much of that pool is GPU-resident, not
    whether the bytes fit somewhere on the machine at all, which is the
    only question a pre-footprint budget can honestly answer.

    `hardware` mirrors `verdict()`'s own keyword exactly: a caller
    answering many entries in one request threads ONE `hw_detect.cached_
    hardware()` reading through every call; a lone caller (the default)
    reads the cache itself, the same `_NOT_GIVEN` sentinel shape `verdict()`
    already uses.

    `None` when RAM itself cannot be read — the same "nothing to judge
    against" answer `verdict()` gives for the identical reason.
    """
    ram_gb = machine_ram_gb()
    if ram_gb is None or ram_gb <= 0:
        return None
    ram_bytes = ram_gb * GB_BYTES
    wired_limit = _wired_limit_bytes(ram_gb)
    is_apple = wired_limit is not None
    usable = max(0.0, ram_bytes - RESERVE_BYTES)
    resolved_hardware = hw_detect.cached_hardware() if hardware is _NOT_GIVEN else hardware
    pool, _run_mode = _select_pool(math.inf, usable, is_apple_unified=is_apple,
                                   hardware=resolved_hardware)
    if is_apple:
        pool = min(pool, wired_limit)
    return pool

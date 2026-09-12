// One row per hit's cell values, as pure derivations — read by
// `HubSearchScreen.tsx`'s `HitRow`, tested here for the reason
// `hubSearchView.ts` already is: no DOM harness exists in this repo by
// design, so the part with a rule in it lives in a module that can be
// driven.
//
// **Every cell whose source can be absent renders a dash, and that is the
// whole discipline this module exists to enforce.** A wrong number is a
// louder mistake than a dash — the plan's own example is llmfit's `search`
// table, whose Score/tok/s/Runtime/Mode/Mem% columns are filled entirely
// with `-` because nothing there ever distinguished "we do not know" from
// "the answer is zero". This module never renders `0` for something merely
// unmeasured.
//
// The family/hoist/dense-table half of this module (`hoistValue`,
// `familyHoist`, `occupiedColumns`, `speedLabel`, `familyDisplay`,
// `capabilityHint`, and friends) was deleted alongside `HubResultsTable.tsx`
// and `hubFamilies.ts`: the two-pane port's search screen (`HubSearchScreen`)
// draws one row per repo with no family grouping and no dense `<table>`, so
// there is nothing left to hoist a column out of or collapse a family into.
import type { AiFitVerdict, HubMatchAxis } from "@platform/lib/api";
import { formatParams, timeAgo } from "@platform/lib/format";

/** The dash every absent cell in this table shows — one glyph, so a reader's
 *  eye can learn it once rather than per column. */
const DASH = "—";

// ---------------------------------------------------------------------------
// D780/D781/D782 — the merged Match (Fit+Score) cell.
//
// Before D780, "Fit" and "Score" were two renderings of the SAME memory-only
// number, which is why a capable machine's table showed an identical bar and
// "100" on every row. D780 makes SCORE a composite (`HubModel.matchScore`,
// server-computed) that blends memory fit with capability, speed, recency
// and popularity. The cell prints that composite as a single number, coloured
// by the memory verdict — the two facts stay distinct (a number's magnitude
// and its colour can disagree, and that disagreement is itself informative:
// a red 76 ranks well on everything except this machine's memory) without
// needing two separate marks to carry them.

/** What one row's merged Match cell renders — the printed number, the
 *  verdict its colour is drawn from, and (D782) a visible "offload" suffix
 *  for a row that would not run on the GPU/unified memory. `verdict` is
 *  `"unknown"` for a row with no fit verdict to judge at all — a fourth,
 *  neutral state distinct from "no" (which means "judged, and it does not
 *  fit"). */
export interface MatchCell {
  /** The printed number, or the dash when `matchScore` is absent. */
  scoreText: string;
  /** The memory verdict the printed number is coloured by — independent of
   *  the number's own magnitude (see this section's own doc). */
  verdict: AiFitVerdict["verdict"] | "unknown";
  /** D782: MODE was cut as its own column — on Apple Silicon it is a
   *  structural constant (`fit.py`'s own doc: unified memory always reads
   *  "gpu"), and where it DOES vary it is derived from the same footprint
   *  arithmetic the fit verdict already is, so a separate column was a
   *  coarser restatement of this one. What survives is visible, not just a
   *  hover fact: a non-GPU run mode is a real cost a reader should see
   *  without hovering, so it prints beside the score as a muted suffix —
   *  never a colour change, since colour here already carries the memory
   *  verdict and must not carry two meanings on top of each other. `null`
   *  for "gpu" (the overwhelmingly common, unremarkable case) or no fit at
   *  all. */
  offloadLabel: "offload" | "CPU only" | null;
}

/** Which basis a shown fit verdict rests on — the SAME three-way ladder
 *  `AiFitVerdict.basis` already carries (`measured`/`declared`/`download`,
 *  see `fitNote.ts`'s own copy table for the established wording), or
 *  `null` when there is no fit at all. Read straight off the wire rather
 *  than re-derived: there is no fourth state, "estimated", for a GGUF row's
 *  server-side params x bytes-per-param guess — that guess would
 *  under-report real memory footprints for quant tokens `fit._quant_key`
 *  cannot classify (see the DECISIONS.md entry), so there is no guess to
 *  distinguish from a measurement. Every fit a row can show comes from
 *  `fit.verdict` itself — either computed at search time (never for a GGUF
 *  row) or by the lazy per-file `hub/size` lookup — and `fit.verdict`
 *  already states which rung of its own ladder it used. */
export type MatchFitBasis = AiFitVerdict["basis"] | null;

// D1266+1 (item 4): the row's number and its tooltip both used to round
// `matchScore` independently (`Math.round` in two places) — harmless while
// the two call sites agreed, but a trap the moment they didn't. One helper,
// used by both `matchCell` and `matchRowTip`, makes "same integer" structural
// rather than a coincidence of two copies of the same one-liner.
export function matchScoreInt(matchScore: number | null | undefined): number | null {
  return typeof matchScore === "number" ? Math.round(matchScore) : null;
}

export function matchCell(
  fit: AiFitVerdict | null,
  matchScore: number | null | undefined,
  stale = false,
): MatchCell {
  const scoreInt = matchScoreInt(matchScore);
  const scoreText = !stale && scoreInt != null ? scoreInt.toString() : DASH;
  const verdict = fit?.verdict ?? "unknown";
  const offloadLabel = fit?.runMode === "cpu-offload" ? "offload" : fit?.runMode === "cpu-only" ? "CPU only" : null;
  return { scoreText, verdict, offloadLabel };
}

const VERDICT_SENTENCE: Record<AiFitVerdict["verdict"], string> = {
  easy: "comfortably fits this machine's memory",
  tight: "would be a squeeze on this machine's memory",
  no: "will not fit this machine's memory",
};

/** The merged cell's hover text — has to explain BOTH facts the cell's one
 *  number carries (D781): what the composite is made of, and what its
 *  colour means, PLUS the run mode D782 folded in here once Mode stopped
 *  being its own column. The cell itself is terse by design — a bare number
 *  in a verdict colour — so this hover is where all of that detail lives. */
export function matchTitle(
  fit: AiFitVerdict | null,
  matchScore: number | null | undefined,
  stale = false,
  fitBasis: MatchFitBasis = null,
): string {
  const scoreText = stale
    ? "Match score not shown: this repo's memory fit was just corrected from a fuller size lookup, and the " +
      "score above has not been recomputed against it yet."
    : typeof matchScore === "number"
      ? `Match score ${Math.round(matchScore)}/100 — blends memory fit, how much of this machine's capacity ` +
        "the model's size uses, estimated speed, how recently it was published, and popularity, with a small " +
        "bonus if it is already on this disk."
      : "Match score is unavailable — nothing here to rank this repo by yet.";
  const verdictText = fit?.verdict ? VERDICT_SENTENCE[fit.verdict] : "memory fit for this repo is unknown";
  const modeText =
    fit?.runMode === "cpu-offload"
      ? " Runs via CPU offload: part of the model spills out of fast memory, which costs real speed."
      : fit?.runMode === "cpu-only"
        ? " Runs on the CPU only — no GPU or unified-memory path was available to judge it against."
        : fit?.runMode === "gpu"
          ? " Runs on the GPU (Apple's unified memory counts as this too)."
          : "";
  // A row's fit can rest on different rungs of `fit.verdict`'s own ladder
  // (`AiFitVerdict.basis` — see `fitNote.ts`'s copy table for the
  // established wording this mirrors) — a real runtime measurement, or a
  // real-but-unmeasured figure judged from the repo's own reported size.
  // There is no "guess in flight" state — every fit comes straight off the
  // wire — so the hover only ever distinguishes "this actually ran here"
  // from "judged, not run".
  const basisText =
    fitBasis === "measured"
      ? " This fit is measured from real memory usage recorded when this model ran on this machine."
      : fitBasis != null
        ? " This fit is judged from this repo's own reported size — not yet measured by an actual run here."
        : "";
  return `${scoreText} Number colour: ${verdictText}.${modeText}${basisText}`;
}

// ---------------------------------------------------------------------------
// D1245/D1246/D1267 — the search hit row's own short `data-tip` popover.
//
// `matchTitle` above is a full paragraph meant for a native `title=`; this is
// the terse replacement `HubSearchScreen.tsx`'s hit row actually shows. The
// first cut (D1245/D1246) was itself still a paragraph — "lost points on
// speed (no reliable estimate for this size), model size (9.1B — this
// machine could run more) and popularity (14K downloads)" reads as noise in
// a small popover. D1267 cuts it to two short lines, ~90 characters total:
// a loss line naming at most the two biggest axes with no parentheticals,
// and a fit line stating the verdict and the GB numbers (rounded to one
// decimal) in one breath. The per-axis raw numbers (downloads, tok/s, age)
// this used to spell out are dropped rather than relocated — there is no
// drawer slot for them, and the loss line's job is now "which two axes cost
// the most", not "the full ledger".

/** One short, parenthetical-free word naming an axis — the vocabulary the
 *  loss line draws from. `fit`/`onDisk` never appear here: fit gets its own
 *  dedicated second line (the verdict + GB numbers), and the on-disk bonus
 *  is not a loss. */
function axisShortName(axis: HubMatchAxis["axis"]): string {
  switch (axis) {
    case "popularity":
      return "popularity";
    case "recency":
      return "recency";
    case "capability":
      return "size";
    case "speed":
      return "speed";
    case "runMode":
      return "offload";
    default:
      return "fit";
  }
}

/** "a" / "a, then b" — the loss line's own join rule, capped at two items by
 *  the caller. */
function joinWithThen(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items[0]}, then ${items[1]}`;
}

/** A GB figure rounded to one decimal, for the fit line — never the raw
 *  float a server-side blend can hand back. */
function round1(n: number): number {
  return Math.round(n * 10) / 10;
}

/** The fit line: verdict word, plus the GB numbers when there is a footprint
 *  to report. `"Memory not measured"` is the honest fourth state — no
 *  verdict at all, nothing to squeeze a number out of. */
function fitLine(fit: AiFitVerdict | null, footprintGb: number | null, poolGb: number | null): string {
  if (!fit?.verdict) return "Memory not measured";
  const fg = footprintGb != null ? round1(footprintGb) : null;
  const pg = poolGb != null ? round1(poolGb) : null;
  if (fit.verdict === "easy") {
    return fg != null && pg != null ? `Fits easily · ${fg} of ${pg} GB` : "Fits easily";
  }
  if (fit.verdict === "tight") {
    return fg != null ? `Tight fit · needs ~${fg}${pg != null ? ` of ${pg}` : ""} GB` : "Tight fit";
  }
  return fg != null ? `Won't fit · needs ${fg} GB` : "Won't fit";
}

/** The search hit row's `data-tip` text (D1245/D1246, cut down by D1267) —
 *  two short lines: "Match N · lost most on A, then B" (silent when every
 *  axis scored full marks, or when there is no breakdown to judge losses
 *  from), and a fit line giving the verdict its own words plus the GB
 *  numbers — the fix for two rows both showing "84" with a different bar
 *  colour reading as a bug rather than two independent facts that happen to
 *  total the same. Reads `matchScoreInt` (item 4) so this NEVER disagrees
 *  with the cell's own printed number. */
export function matchRowTip(
  fit: AiFitVerdict | null,
  matchScore: number | null | undefined,
  breakdown: HubMatchAxis[] | null | undefined,
): string {
  const scoreInt = matchScoreInt(matchScore);
  const scoreText = scoreInt != null ? scoreInt.toString() : DASH;
  const entries = breakdown ?? [];

  const losses = entries
    .filter((e) => e.axis !== "onDisk" && e.axis !== "fit" && e.lost > 0.05)
    .sort((a, b) => b.lost - a.lost)
    .slice(0, 2)
    .map((e) => axisShortName(e.axis));

  let line1 = `Match ${scoreText}`;
  if (losses.length > 0) {
    line1 += ` · lost most on ${joinWithThen(losses)}`;
  } else if (entries.length > 0) {
    line1 += " · full marks";
  }

  const fitEntry = entries.find((e) => e.axis === "fit");
  const line2 = fitLine(fit, fitEntry?.footprintGb ?? null, fitEntry?.poolGb ?? null);

  return `${line1}\n${line2}`;
}

/** "18d ago", or the dash when the Hub did not say (or said something this
 *  page cannot parse) — `created` is an ISO8601 string or null, and
 *  `timeAgo` wants epoch SECONDS, so the one unit conversion lives here
 *  rather than at the column that reads it. */
export function ageLabel(created: string | null): string {
  if (!created) return DASH;
  const ms = Date.parse(created);
  if (!Number.isFinite(ms)) return DASH;
  return timeAgo(ms / 1000) ?? DASH;
}

/** The row's measured quantization (`HubModel.quant`, server-derived — see
 *  `hub_models.py`'s own `_quant`), or the dash. Deliberately a pass-through
 *  with no formatting rule of its own: the wire value IS the label
 *  (`BF16`, `Q4_K_M`, …), and inventing a second vocabulary here would be
 *  exactly the kind of guess this column exists to refuse. */
export function quantLabel(quant: string | null): string {
  return quant ?? DASH;
}

/** Item A (per-variant download): the row's "✓ Downloaded" caption, once the
 *  on-disk file might not be the DEFAULT variant a plain row-level Download
 *  would have fetched. `model.local.file` names whichever single GGUF is
 *  actually on disk (`hub_models.py::_local_state`'s own "exactly one, or
 *  null" rule — an ambiguous multi-file cache reads the same as none here,
 *  matching that same refusal-to-guess); `model.file` is the file a plain
 *  download would pick. When they differ, the caption names the count of
 *  variants this repo offers alongside the quant actually downloaded, so a
 *  reader is not told "Downloaded" for a file that quietly is not the one
 *  they would get by pressing Download again. Callers pass `null`
 *  `variantCount`/`quant` when the row has none (a non-GGUF format) and get
 *  the plain default caption back. */
export function downloadedVariantLabel(model: {
  variantCount: number | null;
  variants: { file: string; quant: string | null }[] | null;
  file: string | null;
  quant: string | null;
  localFile: string | null;
}): string {
  const isNonDefault =
    model.localFile != null && model.file != null && model.localFile !== model.file;
  if (!isNonDefault || !model.variantCount || model.variantCount <= 1) {
    return model.quant ? `${model.quant} downloaded` : "Downloaded";
  }
  const variant = model.variants?.find((v) => v.file === model.localFile);
  const quant = variant?.quant ?? model.quant;
  return quant
    ? `${model.variantCount} variants · ${quant} downloaded`
    : `${model.variantCount} variants downloaded`;
}

/** Whether a variant row's Download button should show at all.
 *
 *  Item 2 (code review): `HubModel.variants[].downloadable` is documented on
 *  `api.ts` as absent for a response shape that PREDATES the field (a
 *  replayed cached search response) — so it must be treated as optional,
 *  never as a required boolean. Treating `undefined` the same as `false`
 *  (which a bare truthiness check on the field does, since `undefined` is
 *  falsy) would hide the Download button on EVERY variant of a stale cached
 *  response, not just the sharded ones `downloadable: false` is meant to
 *  flag. The correct read is "downloadable unless the server said
 *  otherwise": only an explicit `false` (a shard the server actually
 *  checked and rejected) hides the button. */
export function variantIsDownloadable(v: { downloadable?: boolean }): boolean {
  return v.downloadable !== false;
}

/** Downloads, compacted the same way the rest of the page counts things
 *  (`formatParams`'s own K/M/B steps) — or the dash for a repo the Hub
 *  reported no count for. Never a bare `0`: an uncounted repo is not
 *  evidence of zero downloads. */
export function popLabel(downloads: number | null): string {
  if (downloads === null || downloads === undefined) return DASH;
  const compact = formatParams(downloads);
  return compact || String(downloads);
}

/** Splits a Hub repo id into its owner and its own name — the owner is
 *  everything before the last `/` (`null` when the id has none, the Hub's
 *  legacy canonical models like `gpt2`), and the name is the remainder.
 *
 *  A curated card can drop the owner (`RepoCard.tsx`'s `modelName`) because
 *  each card names ONE model a reader already trusts is genuine — the owner
 *  is a fact its subtitle states once, not something two cards ever need to
 *  be told apart by. A search table has no such guarantee: the same repo
 *  NAME can be uploaded by many different owners — a publisher's own
 *  weights alongside every mirror and re-upload of them — and those rows
 *  land side by side, often with identical params/quant/size. The owner is
 *  frequently the ONLY fact that tells a genuine upload from a mirror
 *  apart, so this table can never discard it the way a card does. */
export function splitRepoId(id: string): { owner: string | null; name: string } {
  const cut = id.lastIndexOf("/");
  return cut === -1 ? { owner: null, name: id } : { owner: id.slice(0, cut), name: id.slice(cut + 1) };
}

/** `params` formatted the same compact way the rest of the page counts
 *  parameters, or the dash for a repo with none. */
export function paramsLabel(params: number | null): string {
  if (params === null || params === undefined) return DASH;
  return formatParams(params) || DASH;
}

/** The mockup's own glyph ladder for a search hit's match cell (item C) —
 *  ● easy / ▲ tight / ■ no / ? unknown. `matchCell`'s `verdict` already
 *  carries the same four-way state (including the "unknown" case a plain
 *  `AiFitVerdict["verdict"]` cannot express), so this is a second, tiny pure
 *  function rather than folding a glyph into `MatchCell` itself — the glyph
 *  is presentation for one specific screen (the dense hit row), while
 *  `MatchCell` is shared by every place a match score renders. */
export function verdictGlyph(verdict: AiFitVerdict["verdict"] | "unknown"): string {
  switch (verdict) {
    case "easy":
      return "●";
    case "tight":
      return "▲";
    case "no":
      return "■";
    default:
      return "?";
  }
}

// ---------------------------------------------------------------------------
// SPEC docs/HUB_CATALOG_SPEC.md item 2 — the on-device catalog build banner.
//
// A capability pane's FIRST search always serves live Hub results while its
// pool builds behind it (or sits out a 429 backoff) — this is the one-line,
// non-modal text for that state, read from the search response's
// `poolState`/`poolPagesDone`. `null` means "no banner" (poolState is
// "ready" or "none" — the pane is either already on the fast catalog path or
// has never tried to build one for this capability/no-capability search).

/** Seconds until `until` (a `blockedUntil` epoch-seconds deadline) reads as
 *  a short clock time, or "a bit" if it has already passed / is absent —
 *  never a negative or nonsensical duration. */
function untilLabel(blockedUntil: number | null | undefined, nowMs: number): string {
  if (typeof blockedUntil !== "number") return "shortly";
  const seconds = Math.round(blockedUntil - nowMs / 1000);
  if (seconds <= 0) return "shortly";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  return `${minutes}m`;
}

/** The banner text for the current `poolState`, or `null` for no banner.
 *  `nowMs`: caller-supplied `Date.now()` so this stays a pure function
 *  testable without faking the clock globally. */
export function poolBuildBanner(
  poolState: "ready" | "building" | "blocked" | "none" | undefined,
  poolPagesDone: number | null | undefined,
  blockedUntil: number | null | undefined,
  nowMs: number,
): string | null {
  if (poolState === "building") {
    const pages = typeof poolPagesDone === "number" ? poolPagesDone : 0;
    return (
      `Building the full catalog for this capability (${pages} page${pages === 1 ? "" : "s"} so far)… ` +
      `showing live Hub results until it finishes.`
    );
  }
  if (poolState === "blocked") {
    const when = untilLabel(blockedUntil, nowMs);
    return `Hub rate limit hit; the full catalog resumes after ${when}. Showing live results.`;
  }
  return null;
}

// ---------------------------------------------------------------------------
// The animated first-run build card (replaces the plain `poolBuildBanner`
// text while `poolState === "building"`). Two small pure helpers live here so
// the timing-free parts of the card stay testable; the actual hold-then-fade
// on completion is a timer in the component and isn't covered here.

/** The progress row's page count, or a "still connecting" placeholder before
 *  the first page has landed — `poolPagesDone` is 0/null/undefined on a pane
 *  that has only just started polling. */
export function pagesFetchedLabel(poolPagesDone: number | null | undefined): string {
  if (!poolPagesDone) return "connecting to the Hub…";
  return `${poolPagesDone} page${poolPagesDone === 1 ? "" : "s"} fetched`;
}

// ---------------------------------------------------------------------------
// Round 7 — the "v2 staged status" wait state, replacing the plain
// "Asking {host}…" line a FIRST search (no rows on the pane yet) used to
// show for the whole round trip. Approved from
// `hub-wait-variants.html`'s own `v2` block: one centred stage line + sub-
// caption + sweep bar + a three-dot "Hub · Size · Rank" step row.
//
// The three stages are honest about what is and is not known: "Hub" is the
// only one gated on the real network call — there is no timer that ever
// advances it, only the response actually arriving. Once the response lands,
// "Size" and "Rank" play in quick, fixed succession (the component's own
// ~250ms-each timers) because both are genuinely instantaneous client-side
// work by then — the server already ranked and sized every row before it
// answered. What is pure and testable here is the per-stage copy and the
// slow-line text; the phase clock itself is a component-owned timer chain
// (mirroring `nextPoolPhase`'s own split above).

/** One of the three stages the wait block ever names — `"hub"` is the only
 *  one a pane can sit in for an unbounded time; `"size"`/`"rank"` are each a
 *  fixed ~250ms beat played once the response has actually arrived. */
export type HubWaitStage = "hub" | "size" | "rank";

/** Facts the "Sizing…" sub-caption can fold in when the component already
 *  has them — it does not today (round 7), so every caller sees the plain
 *  fallback, but the shape exists so a future caller wiring in real
 *  memory/runner facts is a one-line change here rather than a new
 *  function. */
export interface HubWaitFacts {
  ramGb: number | null;
  runnerCount: number | null;
}

const NO_HUB_WAIT_FACTS: HubWaitFacts = { ramGb: null, runnerCount: null };

/** The stage line + sub-caption for one of the three wait stages — copy
 *  lifted verbatim from the approved mockup's own `stage()` text map.
 *
 *  `poolReady` is the honesty fix from the user's D1273 feedback: when the
 *  pool for this capability is (or was last seen) "ready", the search runs
 *  entirely against the local DuckDB pool — zero Hub requests — so the
 *  "hub" stage must not claim to be asking the Hub anything. Defaults to
 *  `false` (the pre-existing "Asking {host}" copy) so every other call site
 *  and the pre-existing tests keep behaving exactly as before. */
export function hubWaitStageText(
  stage: HubWaitStage,
  host: string,
  poolReady: boolean = false,
  facts: HubWaitFacts = NO_HUB_WAIT_FACTS,
): { line: string; sub: string } {
  if (stage === "hub") {
    if (poolReady) {
      return {
        line: "Searching the local catalog",
        sub: "every model on the Hub this Mac can run, already on disk",
      };
    }
    return { line: `Asking ${host}`, sub: "one request, then everything else runs here" };
  }
  if (stage === "size") {
    const sub =
      facts.ramGb != null && facts.runnerCount != null
        ? `${round1(facts.ramGb)} GB of unified memory, ${facts.runnerCount} runner${
            facts.runnerCount === 1 ? "" : "s"
          } installed`
        : "sized against this Mac's memory";
    return { line: "Sizing each model for this Mac", sub };
  }
  return { line: "Ranking for this Mac", sub: "memory fit first, then speed, freshness, popularity" };
}

/** The amber "still waiting" line a pane shows once stage `"hub"` has held
 *  for 12s or more, with a live seconds counter — never claims progress
 *  that hasn't happened, only names how long the wait has been.
 *
 *  Same `poolReady` honesty fix as `hubWaitStageText`: a ready-pool search
 *  never talks to the Hub at all, so the slow line must not blame it —
 *  "ranking" is what could plausibly still be slow client-side. Defaults to
 *  `false` to keep the pre-existing copy/tests unchanged. */
export function hubWaitSlowLabel(seconds: number, poolReady: boolean = false): string {
  if (poolReady) return `Still ranking. ${seconds} s and counting.`;
  return `The Hub is slow right now. ${seconds} s and counting.`;
}

/** Whether a response that arrived `elapsedMs` after the request went out is
 *  fast enough that the wait block must never have flashed on screen at
 *  all — under 400ms, per the approved design ("If the response arrives
 *  within 400 ms of the request, skip the block entirely"). */
export function shouldSkipHubWait(elapsedMs: number): boolean {
  return elapsedMs < 400;
}

/** The build card's own tiny state machine: `"hidden"` (nothing to show),
 *  `"building"` (shimmer + page count), `"done"` (the success beat that
 *  holds briefly before the caller collapses it back to `"hidden"`).
 *
 *  Deliberately ignorant of time — the 2.5s hold and the fade-out are a
 *  `setTimeout` in the component, which is the only part of this that
 *  can't be driven as a pure function. What IS pure, and what bit us before
 *  in earlier "banner" work, is the transition rule: `"done"` must only ever
 *  be reached by *leaving* `"building"`, never by a pane that opened on an
 *  already-`"ready"` pool — such a pane has nothing to celebrate. */
export function nextPoolPhase(
  phase: "hidden" | "building" | "done",
  poolState: "ready" | "building" | "blocked" | "none" | undefined,
): "hidden" | "building" | "done" {
  if (poolState === "building") return "building";
  if (phase === "done") return "done"; // the hold — the component's timer clears this
  if (poolState === "ready") return phase === "building" ? "done" : "hidden";
  return "hidden";
}


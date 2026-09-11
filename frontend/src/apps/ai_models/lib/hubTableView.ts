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

export function matchCell(
  fit: AiFitVerdict | null,
  matchScore: number | null | undefined,
  stale = false,
): MatchCell {
  const scoreText = !stale && typeof matchScore === "number" ? Math.round(matchScore).toString() : DASH;
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
// D1245/D1246 — the search hit row's own short `data-tip` popover.
//
// `matchTitle` above is a full paragraph meant for a native `title=`; this is
// the terse replacement `HubSearchScreen.tsx`'s hit row actually shows (see
// its own comment on why: a 60-word tooltip in a small unstyled native
// popup read as ugly noise). It answers the user's two real complaints
// (D1246): "why did THIS row lose points" (the loss list, biggest first,
// capped at three, each with its own number — never a repeat of the axes
// this row scored full marks on), and "why do two 84s look different"
// (the closing sentence ALWAYS separates colour from score in plain words,
// regardless of whether fit made the loss list).

/** "3y old"/"2mo old"/"18d old" — the same coarse-bucket rounding `ageLabel`
 *  reads off a full ISO date, but starting from the already-computed
 *  `ageDays` a `HubMatchAxis` breakdown entry carries (D1245) so this tip
 *  never has to re-parse `created` itself. */
function agePhrase(ageDays: number): string {
  if (ageDays < 30) return `${Math.max(1, Math.round(ageDays))}d old`;
  if (ageDays < 365) return `${Math.max(1, Math.round(ageDays / 30))}mo old`;
  return `${Math.max(1, Math.round(ageDays / 365))}y old`;
}

/** One short phrase naming an axis and the raw number behind it — the
 *  vocabulary `matchRowTip`'s loss list draws from. Never invents a number
 *  the axis entry did not carry (e.g. a speed axis with no real tok/s
 *  estimate says so honestly rather than printing one). */
function axisLossPhrase(entry: HubMatchAxis): string {
  switch (entry.axis) {
    case "popularity":
      return `popularity (${popLabel(entry.downloads ?? null)} downloads)`;
    case "recency":
      return `recency (${entry.ageDays != null ? agePhrase(entry.ageDays) : "age unknown"})`;
    case "capability":
      return `model size (${paramsLabel(entry.params ?? null)} — this machine could run more)`;
    case "speed":
      return entry.tokensPerSecond != null
        ? `speed (~${Math.round(entry.tokensPerSecond)} tok/s)`
        : "speed (no reliable estimate for this size)";
    case "runMode":
      return entry.runMode === "cpu-offload"
        ? "runs via CPU offload"
        : "runs on the CPU only";
    case "fit":
    default:
      return "memory fit";
  }
}

/** "a" / "a and b" / "a, b and c" — the loss list's own join rule, never an
 *  Oxford comma since the list is capped at three. */
function joinWithAnd(items: string[]): string {
  if (items.length === 0) return "";
  if (items.length === 1) return items[0];
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** The search hit row's `data-tip` text (D1245/D1246) — leads with the
 *  score, names only the axes that actually cost THIS row points (biggest
 *  loss first, capped at three, silent about any axis that scored full
 *  marks), mentions the on-disk bonus when it applied, and ALWAYS closes
 *  by separating the row's colour (memory fit alone) from its score,
 *  with fit's own footprint-vs-pool numbers — the fix for two rows both
 *  scoring 84 with different bar colours reading as a bug rather than
 *  two independent facts that happen to total the same. `breakdown`
 *  absent or empty (an older response, or nothing to explain) still
 *  renders a short, honest tip rather than an empty one. */
export function matchRowTip(
  fit: AiFitVerdict | null,
  matchScore: number | null | undefined,
  breakdown: HubMatchAxis[] | null | undefined,
): string {
  const scoreText = typeof matchScore === "number" ? Math.round(matchScore).toString() : DASH;
  const entries = breakdown ?? [];

  const bonus = entries.find((e) => e.axis === "onDisk" && e.gained > 0);
  const losses = entries
    .filter((e) => e.axis !== "onDisk" && e.lost > 0.05)
    .sort((a, b) => b.lost - a.lost)
    .slice(0, 3)
    .map(axisLossPhrase);

  let tip = `Match ${scoreText}/100`;
  if (losses.length > 0) {
    tip += ` — lost points on ${joinWithAnd(losses)}.`;
  } else if (entries.length > 0) {
    tip += " — full marks on every axis.";
  } else {
    tip += ".";
  }
  if (bonus) {
    tip += ` +${Math.round(bonus.gained)} for already being on disk.`;
  }

  const verdictText = fit?.verdict ? VERDICT_SENTENCE[fit.verdict] : "unknown — nothing to judge fit by yet";
  const fitEntry = entries.find((e) => e.axis === "fit");
  const footprintGb = fitEntry?.footprintGb ?? null;
  const poolGb = fitEntry?.poolGb ?? null;
  const fitNumbers =
    footprintGb != null
      ? ` (needs ~${footprintGb}GB${poolGb != null ? ` of this machine's ~${poolGb}GB` : ""})`
      : "";
  tip += ` Colour shows memory fit, not this score: ${verdictText}${fitNumbers}.`;
  return tip;
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


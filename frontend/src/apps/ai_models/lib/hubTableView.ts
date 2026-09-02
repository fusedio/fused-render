// The dense results table's cell values, as pure derivations — read by
// `HubResultsTable.tsx`, tested here for the reason `hubSearchView.ts` and
// `hubFamilies.ts` already are: no DOM harness exists in this repo by design,
// so the part with a rule in it lives in a module that can be driven.
//
// **Every cell whose source can be absent renders a dash, and that is the
// whole discipline this module exists to enforce.** A table is denser than a
// card, so a wrong number is louder — the plan's own example is llmfit's
// `search` table, whose Score/tok/s/Runtime/Mode/Mem% columns are filled
// entirely with `-` because nothing there ever distinguished "we do not know"
// from "the answer is zero". The two failure modes this module is built to
// keep apart: an unmeasured size must never render `0 GB`, and a null
// `speedEstimate` must never render `0 tok/s` — both would be a LOUDER wrong
// answer than the dash they replace.
import type { AiFitVerdict, AiSpeedEstimate, HubModel } from "@platform/lib/api";
import { formatParams, timeAgo } from "@platform/lib/format";
import type { HubFamily } from "./hubFamilies";

/** The dash every absent cell in this table shows — one glyph, so a reader's
 *  eye can learn it once rather than per column. */
const DASH = "—";

// ---------------------------------------------------------------------------
// D639/D640/D641 — the merged Match (Fit+Score) cell.
//
// Before D639, "Fit" and "Score" were two renderings of the SAME memory-only
// number, which is why a capable machine's table showed an identical bar and
// "100" on every row. D639 makes SCORE a composite (`HubModel.matchScore`,
// server-computed) that blends memory fit with capability, speed, recency
// and popularity; D640 folds the two columns into one now that they carry
// different facts: bar LENGTH and the printed number are the composite, bar
// COLOUR (and the glyph's SHAPE — see below) stay the memory verdict.

/** What one row's merged Match cell renders — the bar/number pair, the
 *  verdict's colour+shape, and (D641) a visible "offload" suffix for a row
 *  that would not run on the GPU/unified memory. `dot` is `"unknown"` for a
 *  row with no fit verdict to judge at all — a fourth, neutral state
 *  distinct from "no" (which means "judged, and it does not fit"). */
export interface MatchCell {
  /** Bar fill width, 0-100 — the composite score, or 0 when there is none to
   *  show (never a bare fallback to the memory score alone: a row with no
   *  `matchScore` at all is a row this table has nothing to rank it by). */
  percent: number;
  /** The printed number, or the dash when `matchScore` is absent. */
  scoreText: string;
  dot: AiFitVerdict["verdict"] | "unknown";
  /** D641: MODE was cut as its own column — on Apple Silicon it is a
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
 *  than re-derived: this module used to infer a fourth state, "estimated",
 *  for a GGUF row's server-side params x bytes-per-param guess — that guess
 *  was deleted entirely (the derived-fit feed under-reported real memory
 *  footprints for quant tokens `fit._quant_key` cannot classify; see the
 *  DECISIONS.md entry recorded alongside this change), so there is no guess
 *  left to distinguish from a measurement. Every fit a row can show now
 *  comes from `fit.verdict` itself — either computed at search time (never
 *  for a GGUF row any more) or by the lazy per-file `hub/size` lookup — and
 *  `fit.verdict` already states which rung of its own ladder it used. */
export type MatchFitBasis = AiFitVerdict["basis"] | null;

/** The fit actually shown for a row — pulled out of `HubResultsTable.tsx`
 *  (code review finding 2) so the precedence rule is a pure function this
 *  file's own test suite can drive, matching every other rule in this
 *  module.
 *
 *  **A judged override only wins when the lookup that produced it could
 *  actually judge anything.** `api_hub_size` (`hub_models.py`) computes a
 *  fit/speed verdict ONLY when it was asked with a `file` — a lookup made
 *  with `file: null` (any row `HubResultsTable` did not resolve a GGUF pick
 *  for) always answers `fit: null`, not because there was nothing to fit,
 *  but because that shape of request never judges at all. `hubSize.ts`'s
 *  `lookupTotalSize` caches that `null` the same way it would cache a real
 *  "asked, and there was nothing to judge" answer (`knownFit`'s own pinned
 *  contract, `hubSize.test.ts:295`) — the two are indistinguishable from the
 *  cache alone. Without this `file` gate, a row that already carries a real
 *  `basis: "measured"` verdict from `footprint_store` (a model already on
 *  disk, scored at search time) would have that verdict WIPED by the
 *  never-judges `null` the moment its `file`-less lookup resolves — the dot
 *  drops to "unknown" and the tok/s blanks for a row nothing was ever wrong
 *  with. So the override is only honoured when `file !== null`: exactly the
 *  shape of request `api_hub_size` can answer with a real verdict for. */
export function resolveFit(
  modelFit: AiFitVerdict | null,
  fitOverride: AiFitVerdict | null | undefined,
  file: string | null,
): AiFitVerdict | null {
  return file !== null && fitOverride !== undefined ? fitOverride : (modelFit ?? null);
}

/** `resolveFit`'s sibling for the speed estimate riding the same lazy
 *  lookup — identical precedence, same reason. */
export function resolveSpeed(
  modelSpeed: AiSpeedEstimate | null,
  speedOverride: AiSpeedEstimate | null | undefined,
  file: string | null,
): AiSpeedEstimate | null {
  return file !== null && speedOverride !== undefined ? speedOverride : (modelSpeed ?? null);
}

/** Which basis the fit actually being shown rests on — `matchTitle`'s hover
 *  text. Reads `AiFitVerdict.basis` straight off the resolved verdict rather
 *  than re-deriving it from `wantsTotal`/`fitOverride`: with the derived-fit
 *  guess deleted (see `MatchFitBasis`'s own doc), the wire value is already
 *  the honest answer, and inferring a second one risks disagreeing with it. */
export function matchFitBasis(fit: AiFitVerdict | null): MatchFitBasis {
  return fit?.basis ?? null;
}

/** Whether `matchScore` was computed against a fit this row is no longer
 *  showing — `matchCell`/`matchTitle`'s `stale` parameter, pulled out as its
 *  own pure function (code review finding 3) for the same reason
 *  `resolveFit` was: a rule worth a test belongs in a module that can be
 *  driven, not inline in a component with no DOM harness to exercise it.
 *
 *  **True when the lazy lookup handed back a verdict that actually differs
 *  from the one `matchScore` was computed against** — not merely "the
 *  lookup has answered". Compares verdicts, not just nullness: `modelFit ==
 *  null` alone (the server scored `matchScore` against `_FIT_DEFAULT` with
 *  nothing real to judge by) still counts, but so would a same-shaped
 *  correction that changed the verdict outright, if one were ever possible.
 *  `fitOverride` resolving to `null` (`knownFit`'s own pinned contract for
 *  "asked, and there was nothing to judge") is not a correction of
 *  anything, so it never marks stale. When genuinely stale, the bar/number
 *  fall back to the dash/empty state (never a second, possibly-also-wrong
 *  number invented to fill the gap) so the colour/shape the corrected fit
 *  earns has nothing beside it to contradict it — this is what let an
 *  EARLIER bug show a GGUF row's green "easy" dot beside a ~40%-long bar and
 *  a low number, while the hover claimed the number "blends memory fit". */
export function isMatchScoreStale(
  modelFit: AiFitVerdict | null,
  fitOverride: AiFitVerdict | null | undefined,
): boolean {
  return fitOverride != null && (modelFit == null || fitOverride.verdict !== modelFit.verdict);
}

export function matchCell(
  fit: AiFitVerdict | null,
  matchScore: number | null | undefined,
  stale = false,
): MatchCell {
  const percent = !stale && typeof matchScore === "number" ? matchScore : 0;
  const scoreText = !stale && typeof matchScore === "number" ? Math.round(matchScore).toString() : DASH;
  const dot = fit?.verdict ?? "unknown";
  const offloadLabel = fit?.runMode === "cpu-offload" ? "offload" : fit?.runMode === "cpu-only" ? "CPU only" : null;
  return { percent, scoreText, dot, offloadLabel };
}

const VERDICT_SENTENCE: Record<AiFitVerdict["verdict"], string> = {
  easy: "comfortably fits this machine's memory",
  tight: "would be a squeeze on this machine's memory",
  no: "will not fit this machine's memory",
};

/** The merged cell's hover text — has to explain BOTH encodings the cell
 *  carries (D640): what the composite number is made of, and what the bar's
 *  colour+shape mean, PLUS the run mode D641 folded in here once Mode
 *  stopped being its own column. */
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
  // Code review finding (2): a row's fit can rest on different rungs of
  // `fit.verdict`'s own ladder (`AiFitVerdict.basis` — see `fitNote.ts`'s
  // copy table for the established wording this mirrors) — a real runtime
  // measurement, or a real-but-unmeasured figure judged from the repo's own
  // reported size. There is no "guess in flight" state any more (the
  // derived-fit estimate this used to describe was deleted), so the hover
  // only ever distinguishes "this actually ran here" from "judged, not run".
  const basisText =
    fitBasis === "measured"
      ? " This fit is measured from real memory usage recorded when this model ran on this machine."
      : fitBasis != null
        ? " This fit is judged from this repo's own reported size — not yet measured by an actual run here."
        : "";
  return `${scoreText} Bar colour and glyph: ${verdictText}.${modeText}${basisText}`;
}

// ---------------------------------------------------------------------------
// D641 — Task and Capability were the same fact twice (`text generation` /
// `text-generation`) on every row this table has ever shown for a single
// capability. The visible column is now keyed on `capability` — the value
// the download path and runner resolution actually act on — with the Hub's
// own `task` label folded into the hover ONLY where it genuinely differs,
// so a real discrepancy stays inspectable rather than silently dropped.

/** The hover note for the merged Task/Capability cell — present only when
 *  the Hub's own task label and this app's capability slug actually
 *  disagree (they are usually the same string, `"text-generation"` twice,
 *  which is the bug this column collapse fixes). */
export function capabilityHint(model: Pick<HubModel, "capability" | "task">): string | undefined {
  if (model.task && model.task !== model.capability) {
    return `The Hub's own task label for this repo is "${model.task}".`;
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// D640 — hoisting a value that is identical across the whole result set out
// of every row's cell and into one summary line above the table. A value
// repeated on 21 of 21 rows is noise in a cell and information in a header.
//
// D661 narrowed this to UNANIMITY ONLY, after two rounds each contradicted
// the other's direction: round 1 computed presence over primaries only (the
// summary said "all BF16" while an expanded disclosure showed `Q4_K_M`);
// round 2 shared one primaries+variants set for BOTH presence and the hoist,
// but let a strong (80%) MAJORITY still drive both `isMajorityValue`'s
// full-weight cell styling and a "mostly X" summary line — so three visible
// primary rows all reading `BF16` could sit under a "mostly Q4_K_M" summary
// decided entirely by fifteen variants nobody had opened the disclosure to
// see. There is no majority hoist any more: `hoistValue` only ever returns a
// value when the ENTIRE set (primaries and variants) agrees, so the summary
// can never state a fact contradicted by a row that's actually on screen.
// The muted-common-value styling a majority used to buy is now a SEPARATE,
// column-local concept (`majorityValue`) that only ever affects which cell
// is de-emphasized, never whether the column exists or what the header says.

/** One column's hoisted fact: the single value EVERY row in the set shares.
 *  Returned only when the whole set is unanimous (`hoistValue`) — the
 *  column is then DROPPED entirely (`columnVisible`) and the value is
 *  stated once in the summary line instead of on every row. */
export interface Hoist {
  value: string;
}

/** The threshold (design review, 2026-09-02) a value's SHARE of a column
 *  must clear to be muted as "the common case" (`majorityValue`,
 *  `isMajorityValue`) — a purely cosmetic de-emphasis, never a fact this
 *  column's header or the summary line above it claims. 80%: high enough
 *  that muting still tracks the row a reader is actually looking at most of
 *  the time, without pretending a bare majority makes the minority beneath
 *  notice. */
export const HOIST_MAJORITY = 0.8;

/** Whether every value in the set is the SAME known value — the only case
 *  this column's fact is ever hoisted out of the table. A `null` anywhere
 *  in the set breaks unanimity outright, same as a differing value would:
 *  "we don't know for one row" is exactly the case the column exists to
 *  show, never something to explain away by treating the known majority as
 *  the whole truth. There is no partial/majority result here any more —
 *  see `majorityValue` for the separate, purely cosmetic concept that used
 *  to live in this function. */
export function hoistValue(values: readonly (string | null)[]): Hoist | null {
  if (values.length === 0) return null;
  const first = values[0];
  if (first === null) return null;
  for (const v of values) {
    if (v !== first) return null;
  }
  return { value: first };
}

/** The value most rows in the set share, when it clears `HOIST_MAJORITY` —
 *  used ONLY to mute that value's cells (`isMajorityValue`) so the rows
 *  that disagree with it are what catch the eye. Never used to hide a
 *  column or to put a value in the summary line: those both require full
 *  unanimity (`hoistValue`) now, and a column that is visible (i.e. NOT
 *  unanimous) may still have a common value worth de-emphasizing without
 *  the header claiming it as fact. `null` values are excluded from the
 *  count itself (an unknown row is not evidence FOR any value) but still
 *  count in the denominator, so a column half full of unknowns cannot
 *  read as 80%-common off the known half alone. */
export function majorityValue(values: readonly (string | null)[]): Hoist | null {
  if (values.length === 0) return null;
  const counts = new Map<string, number>();
  for (const v of values) {
    if (v === null) continue;
    counts.set(v, (counts.get(v) ?? 0) + 1);
  }
  let modal: string | null = null;
  let modalCount = 0;
  for (const [v, c] of counts) {
    if (c > modalCount) {
      modal = v;
      modalCount = c;
    }
  }
  if (modal === null || modalCount === 0) return null;
  if (modalCount / values.length >= HOIST_MAJORITY) return { value: modal };
  return null;
}

/** Whether a column should exist in the table AT ALL — presence, not just
 *  cell content (fix for a half-applied hoist: a fully-hoisted column used
 *  to leave every cell blank while the `<th>` and every `<td>` still
 *  rendered, which is a labelled column stating nothing 21 times over).
 *
 *  HIDDEN in two cases: every row agrees (`hoist` non-null — by
 *  construction that means unanimous, already stated once in the summary
 *  line — repeating it as a column of identical text would be the exact
 *  noise this whole redesign removes), or NOTHING is known at all (every
 *  value `null`) — a column of nothing but dashes states nothing either,
 *  so it is not worth its width.
 *
 *  SHOWN in every other case — including a strong majority short of full
 *  agreement: `hubTableView`'s cell rule for a shown column always prints
 *  the row's own real value (see `isMajorityValue` for the muted/full-weight
 *  split) — it never blanks a majority row's cell while the column is
 *  visible, because that produced a real ambiguity a reviewer caught live:
 *  a blank cell and a genuine dash (unknown) are two different facts, and
 *  rendering both as "no visible text" left a reader with no way to tell
 *  "unremarkable, matches the summary" from "nobody knows".
 *
 *  **`values` must cover every row the table can DISPLAY, primaries and
 *  variants alike** (code review finding), and so must `hoist` itself —
 *  both are built from the same set in `familyHoist`, so this function's
 *  own re-check of `values` can never disagree with what `hoist` was
 *  computed from. */
export function columnVisible(hoist: Hoist | null, values: readonly (string | null)[]): boolean {
  const known = values.filter((v): v is string => v !== null);
  if (known.length === 0) return false;
  return hoist === null;
}

/** Whether ONE row's value is the column's majority value (`majorityValue`)
 *  — purely a STYLING signal (de-emphasize the value most rows already
 *  share) and never a reason to omit the text: the cell always prints
 *  `value` (or the dash for a genuinely unknown one) regardless of this
 *  function's answer. Callers pass `majorityValue`'s result here, never
 *  `hoistValue`'s — a unanimous column is not rendered at all
 *  (`columnVisible`), so there is no cell left to style, and `hoistValue`
 *  no longer has a non-unanimous "majority" shape to hand this function. */
export function isMajorityValue(value: string | null, majority: Hoist | null): boolean {
  return !!majority && value !== null && value === majority.value;
}

/** The one line above the table naming whatever the result set UNANIMOUSLY
 *  agrees on (D640, narrowed by D661) — task/capability and quant are the
 *  two candidates left after D641 folded Mode into the Match cell's own
 *  hint. Size, Params, tok/s, Pop. and New are never hoisted: they are the
 *  columns a reader compares row-to-row on a RANKED list, so even a
 *  coincidental cluster must stay visible per row.
 *
 *  There is no "mostly X" clause any more (D661): a hoist that is anything
 *  short of unanimous is `null` by construction (`hoistValue`), so this
 *  function only ever states a value it can back with full agreement across
 *  every row it was computed from — never a claim decided by rows a reader
 *  cannot currently see. `null` when there is nothing to say (no rows yet,
 *  or neither column reached unanimity). */
export function hoistSummary(count: number, capabilityHoist: Hoist | null, quantHoist: Hoist | null): string | null {
  if (count <= 0) return null;
  const noun = count === 1 ? "model" : "models";
  const head = capabilityHoist ? `${count} ${capabilityHoist.value} ${noun}` : `${count} ${noun}`;
  if (!quantHoist) return head;
  return `${head} · all ${quantHoist.value}`;
}

/** Everything `HubResultsTable` needs to draw the Task/Capability and Quant
 *  columns' shared state — pulled out of the component (code review finding
 *  4) so the "one value set feeds both presence and the summary" rule is a
 *  pure function this file's own test suite can drive, the way every other
 *  rule in this module already is.
 *
 *  **The whole fix, in one sentence: `capabilityHoist`/`quantHoist`, their
 *  majority counterparts, and the `values` `columnVisible` re-checks against
 *  are all built from the SAME rows** — `allRows`, every family's primary
 *  AND every one of its variants, whether or not a disclosure is currently
 *  open. `count` (fed to `hoistSummary`) is `allRows.length` too, not
 *  `families.length` (D661's fix to a denominator mismatch a code review
 *  caught: the OLD call passed `families.length` — the number of top-level
 *  rows — as the count a hoist claim was stated "about", while the hoist
 *  itself was already being decided over the larger primaries+variants set;
 *  a claim and the count attached to it must describe the same rows, or a
 *  reader has no way to know how many models the stated fact actually
 *  covers). */
export function familyHoist(families: readonly HubFamily[]): {
  capabilityHoist: Hoist | null;
  quantHoist: Hoist | null;
  capabilityMajority: Hoist | null;
  quantMajority: Hoist | null;
  summary: string | null;
  showTask: boolean;
  showQuant: boolean;
} {
  const allRows = families.flatMap((f) => [f.primary, ...f.variants]);
  const allCapabilityValues = allRows.map((m) => m.capability);
  const allQuantValues = allRows.map((m) => m.quant);
  const capabilityHoist = hoistValue(allCapabilityValues);
  const quantHoist = hoistValue(allQuantValues);
  return {
    capabilityHoist,
    quantHoist,
    capabilityMajority: majorityValue(allCapabilityValues),
    quantMajority: majorityValue(allQuantValues),
    summary: hoistSummary(allRows.length, capabilityHoist, quantHoist),
    showTask: columnVisible(capabilityHoist, allCapabilityValues),
    showQuant: columnVisible(quantHoist, allQuantValues),
  };
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

/** Below this many parameters, `speed.py`'s own bandwidth formula is
 *  documented as UNVALIDATED (`speed.py:283`'s own anchor rule, llmfit's
 *  `params_b >= 1.0 and not is_moe`): a sub-billion-parameter model's tok/s
 *  is dominated by fixed per-call overhead the formula does not model at
 *  all, so a number it produces down there is not evidence of anything.
 *  This table has no MoE flag to check the other half of that rule against
 *  (a Hub search row carries no such fact), so the params-only half is what
 *  it can honestly apply. */
const SPEED_ANCHOR_PARAMS = 1_000_000_000;

/** tok/s, scaled to how many digits a reader actually needs — or the dash,
 *  never "0 tok/s", which reads as a measurement rather than as "nobody
 *  knows".
 *
 *  **The bug this fixes:** `tiny-Qwen2ForCausalLM-2.5` (2M parameters) shows
 *  a confident five-digit `17.3k` — a number the formula's own documented
 *  anchor rule (see `SPEED_ANCHOR_PARAMS`) says it has no business
 *  producing for anything this small. Below the anchor the honest render is
 *  the dash, with the reason in the hover (`speedTitle`) — a formatting fix
 *  cannot repair a number that should not have been shown at all. At or
 *  above the anchor, three precision bands: under 10 keeps one decimal (a
 *  real distinction at conversational speeds), 10-999 rounds to a whole
 *  token, and 1000+ compacts to `formatParams`'s own K-step so the column
 *  stays a fixed few characters wide. */
export function speedLabel(speed: AiSpeedEstimate | null, params: number | null): string {
  if (params !== null && params < SPEED_ANCHOR_PARAMS) return DASH;
  if (!speed || typeof speed.tokensPerSecond !== "number" || !Number.isFinite(speed.tokensPerSecond)) {
    return DASH;
  }
  const tps = speed.tokensPerSecond;
  if (tps < 10) return tps.toFixed(1);
  if (tps < 1000) return Math.round(tps).toString();
  return `${(tps / 1000).toFixed(1)}k`;
}

/** The tok/s cell's hover — states the anchor-rule reason for the dash when
 *  that is why it is one, rather than leaving a reader to guess whether
 *  "unknown" means "not measured" or "not modelled at this size". */
export function speedTitle(params: number | null): string | undefined {
  if (params !== null && params < SPEED_ANCHOR_PARAMS) {
    return (
      "Not estimated: below one billion parameters, tok/s is dominated by fixed per-call overhead " +
      "the bandwidth formula does not model, so a number here would not be a real estimate."
    );
  }
  return undefined;
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

/** How many OTHER repos this family folds in — the dash for a family that is
 *  just the one repo, never a bare `0`: the column states how much was
 *  collapsed, and "nothing was collapsed" is a fact worth a dash rather than
 *  a number that reads as a measurement of something. */
export function variantLabel(variantCount: number): string {
  return variantCount > 0 ? String(variantCount) : DASH;
}

/** The Model column's two lines: the repo a Download button on this row would
 *  actually act on, and — only where a base model is known — what it was
 *  grouped under.
 *
 *  **The row's NAME is always the primary's own id, never the base model.**
 *  An earlier version of this function named the row by `baseModel` when one
 *  was known, with the primary's id as a muted second line — readable, but
 *  wrong on three counts a table full of Download buttons cannot afford: the
 *  base model may not be a repo this app can even run (it is not necessarily
 *  among the rows `_model_row` let through, which is exactly why a variant
 *  can still be grouped under a base that "never appeared" —
 *  `hubFamilies.ts`'s own case for it), truncating two DIFFERENT base models
 *  that happen to share a repo name (`Qwen/Qwen3-8B` vs `unsloth/Qwen3-8B`)
 *  renders an identical name for two unrelated families, and — the sharpest
 *  version of the same mistake — identity, the href, and the download action
 *  must all name the same repo or a click acts on something other than what
 *  was read. So `name` is `family.primary.id`, matching the href and the
 *  download action exactly; `baseModel` is the family's grouping fact, shown
 *  as secondary context ("grouped under …") only when one is known.
 */
export function familyDisplay(family: HubFamily): { name: string; baseModel: string | null } {
  return { name: family.primary.id, baseModel: family.primary.baseModel };
}

/** `params` formatted the same compact way the rest of the page counts
 *  parameters, or the dash for a repo with none. */
export function paramsLabel(params: number | null): string {
  if (params === null || params === undefined) return DASH;
  return formatParams(params) || DASH;
}

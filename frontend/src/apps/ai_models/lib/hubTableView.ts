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
import type { AiFitVerdict, AiSpeedEstimate } from "@platform/lib/api";
import { formatParams, timeAgo } from "@platform/lib/format";
import type { HubFamily } from "./hubFamilies";

/** The dash every absent cell in this table shows — one glyph, so a reader's
 *  eye can learn it once rather than per column. */
const DASH = "—";

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

/** The Fit column's two readings of one number — the continuous bar and the
 *  three-way dot — bundled together because a cell that drew one without the
 *  other would be answering half the question the column exists for. `null`
 *  when nothing was judged (no safetensors size, no params): a bar drawn at
 *  0% would read as "does not fit" rather than "unknown", which is a
 *  different and false claim. */
export function fitCell(fit: AiFitVerdict | null): { percent: number; dot: AiFitVerdict["verdict"] } | null {
  if (!fit || typeof fit.score !== "number") return null;
  return { percent: fit.score, dot: fit.verdict };
}

/** One decimal of tok/s, or the dash — never "0 tok/s", which reads as a
 *  measurement rather than as "nobody knows". */
export function speedLabel(speed: AiSpeedEstimate | null): string {
  if (!speed || typeof speed.tokensPerSecond !== "number") return DASH;
  return speed.tokensPerSecond.toFixed(1);
}

/** The three run modes `fit.verdict` can report, in words a reader chose
 *  rather than the wire's hyphenated strings — the dash when there is no fit
 *  to read one off of at all. */
export function runModeLabel(runMode: AiFitVerdict["runMode"] | undefined): string {
  switch (runMode) {
    case "gpu":
      return "GPU";
    case "cpu-offload":
      return "CPU offload";
    case "cpu-only":
      return "CPU only";
    default:
      return DASH;
  }
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

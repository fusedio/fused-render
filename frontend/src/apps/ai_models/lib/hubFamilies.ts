// Collapsing a Hub search's quant/finetune republishes back to one row per
// model family — a pure grouping rule, tested directly here for the reason
// `hubSearchView.ts` already is: the rule is testable and the JSX that draws
// its output is not.
//
// **The signal is the Hub's `base_model:<relation>:<id>` tag** (parsed
// server-side, `hub_models.py::_base_model`), and the module accepts every
// relation it names — `quantized`, `finetune`, `merge`, `adapter` — rather
// than narrowing to `quantized` alone. In the wild sample that motivated this
// build, `quantized` dominates, but the MLX ports actually on this machine
// (`gemma-3-12b-it-4bit`, `ltx-2.3-mlx-q4`) mostly declare `finetune`, and
// keying on one relation would split exactly the families this exists to
// join.
//
// **A row keeps its identity even when its base model never appeared in the
// same page of results** — dropped upstream by D313, or simply outside the
// query's match. The variant still names what it came from, and grouping it
// under that id (rather than its own) is what lets a second variant of the
// SAME base, arriving on a later page or a different sort, land in the same
// family rather than starting a new one.
import type { HubModel } from "@platform/lib/api";

export interface HubFamily {
  /** `baseModel` when known, else the lone member's own id — stable across
   *  re-renders of the SAME result set, which is what a React `key` needs. */
  key: string;
  /** The best-fitting member (then most-downloaded, then the server's own
   *  ranking as the final tie-break) — the row a family's single line draws. */
  primary: HubModel;
  /** Every other member, same ordering rule, for the "N variants" affordance. */
  variants: HubModel[];
}

/** Descending: higher fit score first (a model with no fit — nothing to
 *  judge — sorts behind one that has any real score, including "no"'s 0),
 *  then higher downloads, then leaves ties exactly where they were.
 *
 *  `Array.prototype.sort` has been a STABLE sort in every engine this app
 *  ships on for years (the ECMA-262 requirement since ES2019), so a
 *  comparator that returns 0 for a tie is enough to keep the server's own
 *  ranking as the last word — no index bookkeeping needed here. */
function byFitThenDownloads(a: HubModel, b: HubModel): number {
  const fitA = a.fit?.score ?? -1;
  const fitB = b.fit?.score ?? -1;
  if (fitA !== fitB) return fitB - fitA;
  const dlA = a.downloads ?? -1;
  const dlB = b.downloads ?? -1;
  return dlB - dlA;
}

/** Every result, collapsed to one family per model — primary chosen by fit,
 *  variants ordered the same way, an untagged row standing alone.
 *
 *  **Families are positioned at their PRIMARY's index in `models`, not at
 *  whichever member first appeared.** A family draws its primary's row —
 *  size, downloads, age, everything a column shows comes off that one
 *  member — so that is also the member whose position in an already-sorted
 *  `models` (`bySizeAscending`, the Hub's own `downloads`/`trending` order,
 *  `sort=fit`'s reorder) has to decide where the family lands. Positioning by
 *  first-appearance instead let a family sit at a NON-primary variant's
 *  index while showing the primary's value there — the Size column visibly
 *  not ascending under a size sort, because the row drawn at the "smallest
 *  so far" position could be showing a bigger primary. Ordering by the
 *  primary's own index instead makes the family order exactly as sorted as
 *  `models` itself was, for whatever key it was sorted by, with no need to
 *  special-case which sort is in force.
 */
export function groupIntoFamilies(models: readonly HubModel[]): HubFamily[] {
  const buckets = new Map<string, HubModel[]>();
  const indexOf = new Map<HubModel, number>();

  models.forEach((model, i) => {
    indexOf.set(model, i);
    const key = model.baseModel ?? model.id;
    let bucket = buckets.get(key);
    if (!bucket) {
      bucket = [];
      buckets.set(key, bucket);
    }
    bucket.push(model);
  });

  const families = Array.from(buckets.entries(), ([key, group]) => {
    const sorted = group.slice().sort(byFitThenDownloads);
    return { key, primary: sorted[0], variants: sorted.slice(1) };
  });
  families.sort((a, b) => indexOf.get(a.primary)! - indexOf.get(b.primary)!);
  return families;
}

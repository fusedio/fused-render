// The number beside a search result's name: which figure it is, what it
// measures, and how the second one gets asked for.
//
// There are TWO sizes on this page and they are not the same measurement.
//
//   * `estimatedSize` — the weights, recovered by the server from the dtype →
//     parameter-count map the Hub publishes. Free: it rides the search reply.
//   * `usedStorage` — the Hub's total for the whole repo: every file in it,
//     tokenizer and configs and every quantised copy the author shipped. Costs
//     ONE REQUEST PER REPO, because the Hub refuses to expand the field on its
//     list endpoint (see `routers/hub_models.py`).
//
// The second exists because the first is often absent: a GGUF, mflux or
// LoRA-only repo publishes no dtype map at all, so the card showed a dash while
// the model's own page on the Hub showed a real total. It is a FALLBACK, asked
// for only when there is no estimate and only for a card actually on screen —
// a page of two dozen results must not become two dozen outbound calls on a
// debounced keystroke.
//
// And because the two measure different things, the tooltip has to say WHICH
// one it is showing. A total that includes three quantised copies described as
// "computed from the parameter counts" would be a sentence about work that
// never happened.
//
// Here rather than in the component for the reason `discoverView.ts` is: there
// is no DOM harness in this repo by design, so the part with a rule in it lives
// in a module that can be driven. What stays in `HubCard` is the
// IntersectionObserver — the one piece that genuinely needs a DOM node.
import type { HubModel } from "@platform/lib/api";
import { getHubModelSize } from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";

/** What the size cell reads, or null for the dash. `total` is the fallback,
 *  once it has resolved; it is ignored when there is a real estimate, so a repo
 *  that publishes weights metadata always shows the weights figure. */
export function hubSizeLabel(model: HubModel, total: number | null): string | null {
  if (model.estimatedSize) return `≈${formatSize(model.estimatedSize)}`;
  if (total !== null) return `≈${formatSize(total)}`;
  return null;
}

/** What that number MEASURES, on hover. Three states, because there are three
 *  different true things to say — and the third has to stay true while a lazy
 *  lookup simply has not fired yet, so it does not claim the size "can't be
 *  computed" about a card nobody has scrolled to. */
export function hubSizeTitle(model: HubModel, total: number | null): string | undefined {
  if (model.estimatedSize) {
    // The "≈" is doing real work: the bytes are recovered from the dtype →
    // parameter-count map the Hub publishes, which is the weights and not the
    // tokenizer, configs or extra formats sitting beside them.
    return (
      `≈${formatSize(model.estimatedSize)} of weights, computed from the parameter counts the Hub ` +
      "publishes. Other files in the repo are not included."
    );
  }
  if (total !== null) {
    return (
      `≈${formatSize(total)} — the Hub's total for this repo: every file in it, not just the ` +
      "weights a load would read. This repo publishes no safetensors metadata, so there is no " +
      "weights-only figure to show."
    );
  }
  return "This repo publishes no safetensors metadata on the Hub, so there is no size to show yet.";
}

/** Totals already resolved, for the page's lifetime. Null is an ANSWER — the
 *  Hub does not measure every repo — so `has` is the question to ask, not
 *  truthiness. Re-sorting or re-filtering brings the same cards back into view,
 *  which must not cost a second round trip. */
const resolved = new Map<string, number | null>();

/** Lookups in flight. React 18 runs an effect twice in strict mode, and an
 *  IntersectionObserver can report the same card again before state settles;
 *  either would be a duplicate request to a third party. */
const inFlight = new Map<string, Promise<number | null>>();

/** The total already known for this repo, or `undefined` if nobody has asked.
 *  Lets a card render the right number on its very first paint rather than
 *  flashing a dash for a repo the page measured a scroll ago. */
export function knownTotalSize(id: string): number | null | undefined {
  return resolved.has(id) ? (resolved.get(id) ?? null) : undefined;
}

/** This repo's total bytes, asked once however many cards want it.
 *
 *  A failure resolves to null rather than rejecting: the card keeps its dash,
 *  and a size nobody asked out loud for is not worth a banner. Cached either
 *  way — a Hub that has no number for this repo will not have one a scroll
 *  later, and the server does not cache its own errors, so a genuine outage
 *  costs one attempt per card rather than one per scroll.
 */
export function lookupTotalSize(
  id: string,
  fetchSize: (id: string) => Promise<{ usedStorage: number | null }> = getHubModelSize,
): Promise<number | null> {
  const known = resolved.get(id);
  if (resolved.has(id)) return Promise.resolve(known ?? null);
  const running = inFlight.get(id);
  if (running) return running;
  const lookup = fetchSize(id)
    .then((r) => (typeof r.usedStorage === "number" ? r.usedStorage : null))
    .catch(() => null)
    .then((bytes) => {
      resolved.set(id, bytes);
      inFlight.delete(id);
      return bytes;
    });
  inFlight.set(id, lookup);
  return lookup;
}

/** Tests only: the caches outlive a component, which is the point of them. */
export function _forgetTotalSizes(): void {
  resolved.clear();
  inFlight.clear();
}

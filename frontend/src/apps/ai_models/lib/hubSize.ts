// The number beside a search result's name: which figure it is, what it
// measures, and how the second one gets asked for. Read by the Local tab's
// search face (local/RecommendedCard.tsx's `HubResultCard`).
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
// LoRA-only repo publishes no dtype map at all, so the card showed a dash
// where the model's own page on the Hub shows a real number. It is a
// FALLBACK, asked for only when there is no estimate and only for a card
// actually on screen — a page of two dozen results must not become two dozen
// outbound calls on a debounced keystroke.
//
// But the fallback only ever stands in for THIS row's size when the row names
// the one file it measures (`HubModel.file`, a GGUF row's resolved
// quantization). Without a `file`, the total the Hub hands back is repo-WIDE —
// every quantization the author shipped, summed — and showing that sum next
// to one row's name is a wrong number wearing a "≈": a 9.1B-parameter model
// reading out at 154 GB, seventeen bytes a parameter, because the figure on
// screen was several sibling files' worth of weights. `hubSizeLabel` and
// `hubSizeBytes` render nothing rather than that number.
//
// And because the two measure different things, the tooltip has to say WHICH
// one it is showing. A total that includes three quantised copies described as
// "computed from the parameter counts" would be a sentence about work that
// never happened.
//
// Here rather than in the component for the reason `hubSearchView.ts` is: there
// is no DOM harness in this repo by design, so the part with a rule in it lives
// in a module that can be driven. What stays in `HubResultCard` is the
// IntersectionObserver — the one piece that genuinely needs a DOM node.
import type { AiFitVerdict, AiSpeedEstimate, HubModel } from "@platform/lib/api";
import { getHubModelSize } from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";

/** What the size cell reads, or null for the dash. `total` is the fallback,
 *  once it has resolved; it is ignored when there is a real estimate, so a repo
 *  that publishes weights metadata always shows the weights figure.
 *
 *  `total` is trusted ONLY when `model.file` names the one file it measures —
 *  a GGUF row whose own resolved quantization is what `lookupTotalSize` was
 *  asked about. Without a `file`, `total` is the Hub's repo-WIDE `usedStorage`:
 *  every file in the repo, tokenizer and configs and every quantization the
 *  author shipped, not this row's own size. For a multi-quant repo that sum
 *  can run to hundreds of gigabytes over what any one of its rows would
 *  actually download — a fabricated ~17 bytes/param on a 9.1B model is that
 *  bug, not a rounding error. An honest dash beats a confident wrong number,
 *  so an unscoped total renders nothing rather than standing in for a size it
 *  was never a measurement of. */
export function hubSizeLabel(model: HubModel, total: number | null): string | null {
  if (model.estimatedSize) return `≈${formatSize(model.estimatedSize)}`;
  if (total !== null && model.file) return `≈${formatSize(total)}`;
  return null;
}

/** The bytes a SIZE SORT ranks a result by: whatever figure the card is showing.
 *
 *  **The same precedence as `hubSizeLabel`, and that is the whole point.** The
 *  number beside a name is the only evidence a reader has that a size sort
 *  worked, so ordering the grid by a different measurement — the Hub's repo
 *  total, say, while the card shows the weights estimate — produces a column of
 *  numbers that does not ascend. Correct, invisible, and indistinguishable from a
 *  broken sort. Mixing the two measurements in one ordering is the cost, and it
 *  is a cost this cell already pays: it holds either figure, says which on hover,
 *  and hedges both with `≈`.
 *
 *  It also means a sort asks the Hub for exactly the repos a card would have
 *  asked about anyway — the ones with no estimate — instead of one request per
 *  result.
 *
 *  `undefined` (nobody has asked, or asking failed) is passed through rather than
 *  flattened to null: the sort keeps those apart from "the Hub has no total for
 *  this repo" only in as much as neither is a number, but the caller deciding
 *  what to measure needs the distinction.
 *
 *  Same `model.file` gate as `hubSizeLabel`, for the same reason: an unscoped
 *  `total` is a repo-wide sum, not this row's size, and a sort that ranked by
 *  it while the cell shows a dash is the "does not ascend" failure this
 *  function's own docstring warns about — just invisible instead of loud. */
export function hubSizeBytes(
  model: Pick<HubModel, "estimatedSize" | "file">,
  total: number | null | undefined,
): number | null | undefined {
  if (model.estimatedSize) return model.estimatedSize;
  return model.file ? total : total === undefined ? undefined : null;
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
  if (total !== null && model.file) {
    // A GGUF row: `total` here is the ONE file `formats.pick_gguf_file`
    // resolved, not the repo-wide total — see `hub_models.py`'s own
    // `_fetch_file_size`. Saying so, and naming the file, matters because a
    // tooltip that names a size without naming the file would read as the
    // repo total (every quantization the author published) and claim a
    // much bigger download than this row would actually make.
    return (
      `≈${formatSize(total)} — the size of \`${model.file}\`, the specific quantization this row ` +
      "would download. This repo publishes no safetensors metadata, so there is no weights-only " +
      "figure to show."
    );
  }
  // `total` resolved but names no file: it is the Hub's repo-WIDE total —
  // every quantization the author shipped, summed — not a size this row
  // itself would download. Saying so as a size, even hedged with "≈", would
  // read as this row's own size: a 9.1B model reading ~17 bytes/param because
  // the number on screen is several sibling files' worth of weights. There
  // is no honest number for this row, so there is no number.
  return "This repo publishes no safetensors metadata on the Hub, so there is no size to show yet.";
}

/** The cache key for one repo's size lookup. **Must include `file`**: the
 *  server's answer depends on it — a `null` `file` gets
 *  the repo-WIDE `usedStorage` total with no fit/speed at all, while a named
 *  `file` gets that ONE file's own bytes plus a fit verdict judged off it
 *  (see `lookupTotalSize`'s own docstring). Two callers ask about the same
 *  repo with different `file` values — `HubResults.tsx`'s page-level Size
 *  sort always asks with `null` (`measureSizes`), while a table row asks with
 *  its own resolved `model.file` (`HubResultsTable.tsx`) — and keying on `id`
 *  alone let whichever answer landed first (typically the sort's `null`-file,
 *  repo-wide answer, since it fires with no viewport gate) satisfy BOTH: a
 *  row would then read `estimatedSize`-shaped title text over the OTHER
 *  measurement, and never re-ask with its own `file` because `resolved` said
 *  the id was already known. */
function _sizeKey(id: string, file: string | null): string {
  // A NUL cannot appear in either a repo id or a filename, so this cannot
  // collide two different (id, file) pairs into one string. Written as an
  // escape, not the byte itself, so the file stays text to every tool that
  // reads it (git calls a file with a raw NUL in it binary and stops
  // diffing it).
  return `${id}\u0000${file ?? ""}`;
}

/** Totals already ANSWERED, for the page's lifetime. Null is an answer — the
 *  Hub does not measure every repo — so `has` is the question to ask, not
 *  truthiness. Re-sorting or re-filtering brings the same cards back into view,
 *  which must not cost a second round trip.
 *
 *  A failed lookup is NOT an answer and never lands here: the server returns
 *  200 with an `error` field on a Hub-side failure and deliberately does not
 *  cache it, so caching it on this side would turn one 429 into a permanent
 *  dash for that repo.
 *
 *  Keyed by `_sizeKey(id, file)`, not `id` alone — see that function's own
 *  docstring for why. */
const resolved = new Map<string, number | null>();

/** The fit/speed judgement that rode along with the size — see
 *  `lookupTotalSize`'s own docstring for why this can only ever be non-null
 *  for a row whose lookup was asked with a `file` (a GGUF row). Populated in
 *  the SAME pass as `resolved`, never separately: they are one answer to one
 *  question, and reading one without the other would let a card show a size
 *  and a stale (or missing) fit from two different requests. Keyed like
 *  `resolved`. */
const resolvedFit = new Map<string, AiFitVerdict | null>();
const resolvedSpeed = new Map<string, AiSpeedEstimate | null>();

/** Lookups in flight. React 18 runs an effect twice in strict mode, and an
 *  IntersectionObserver can report the same card again before state settles;
 *  either would be a duplicate request to a third party. Keyed like `resolved`
 *  — a page-level `null`-file lookup for a repo and a row's own `file`-scoped
 *  lookup for the SAME repo are two different in-flight requests, not one. */
const inFlight = new Map<string, Promise<number | null>>();

/** The total already known for this repo (and this `file`), or `undefined` if
 *  nobody has asked — or if the asking failed, which is not something known.
 *  Lets a card render the right number on its very first paint rather than
 *  flashing a dash for a repo the page measured a scroll ago, and lets it
 *  tell "the Hub said no number" (cached null) from "the ask did not get
 *  through" (undefined).
 *
 *  `file` must be the SAME value the caller will pass to `lookupTotalSize` —
 *  a page-level sort asking about the repo total (`file: null`) and a row
 *  asking about its own resolved file are different questions with different
 *  answers (see `_sizeKey`). */
export function knownTotalSize(id: string, file: string | null = null): number | null | undefined {
  const key = _sizeKey(id, file);
  return resolved.has(key) ? (resolved.get(key) ?? null) : undefined;
}

/** The fit verdict that rode along with `id`'s (`file`-scoped) size lookup,
 *  or `undefined` if nothing has answered for this repo/file yet. Only ever
 *  populated for a lookup made WITH a `file` and a `capability` (see
 *  `lookupTotalSize`) — every other repo answers `undefined` forever, which a
 *  reader of this function should read the same as "nothing to show",
 *  identical to `HubModel.fit` being null from the search reply itself. */
export function knownFit(id: string, file: string | null = null): AiFitVerdict | null | undefined {
  const key = _sizeKey(id, file);
  return resolvedFit.has(key) ? (resolvedFit.get(key) ?? null) : undefined;
}

/** `knownFit`'s sibling for the speed estimate that rides the same lookup. */
export function knownSpeedEstimate(id: string, file: string | null = null): AiSpeedEstimate | null | undefined {
  const key = _sizeKey(id, file);
  return resolvedSpeed.has(key) ? (resolvedSpeed.get(key) ?? null) : undefined;
}

/** This repo's size, asked once however many cards want it — the repo-wide
 *  total by default, or one named FILE's own bytes when `file` is given (a
 *  GGUF row's own resolved `HubModel.file`): summing sibling sizes across an
 *  entire GGUF repo (every quantization variant) is the bug this fixes, and
 *  the resolved file is the one this row would actually download.
 *
 *  **Fit/speed ride the SAME request when `file` is given.** `_model_row`
 *  cannot judge fit for a GGUF row during search (no dtype map to size it
 *  from, and a per-row Hub call inside a search reply is exactly what the
 *  server refuses to do), but this lazy per-repo lookup already costs one
 *  round trip — so the verdict comes back on it for free rather than staying
 *  null forever. Callers that want that ride-along pass a `fetchSize` bound
 *  to their row's own `capability` (see `HubResultsTable.tsx`); callers that
 *  only care about the byte total (the page-level size SORT) can leave it at
 *  the default, which asks for a size with no capability and gets none back.
 *
 *  A failure resolves to null rather than rejecting: the card keeps its dash,
 *  and a size nobody asked out loud for is not worth a banner. But it is NOT
 *  remembered — an answer is remembered, a failure is only reported. The server
 *  goes out of its way not to cache its own Hub errors so that the next card
 *  can find out for itself; caching them here would undo exactly that, and a
 *  transient 429 would read as "this repo has no size" until the tab closed.
 *  Whether anything asks again is the caller's business (see `HubResultCard`); this
 *  only promises not to stand in the way.
 */
export function lookupTotalSize(
  id: string,
  file: string | null,
  fetchSize: (
    id: string,
    file?: string,
  ) => Promise<{
    usedStorage: number | null;
    fileSize?: number | null;
    fit?: AiFitVerdict | null;
    speedEstimate?: AiSpeedEstimate | null;
    error?: string;
  }> = getHubModelSize,
): Promise<number | null> {
  const key = _sizeKey(id, file);
  if (resolved.has(key)) return Promise.resolve(resolved.get(key) ?? null);
  const running = inFlight.get(key);
  if (running) return running;
  const lookup = fetchSize(id, file ?? undefined)
    .then((r) => {
      // 200 with an `error` is how the server reports a Hub-side failure — a
      // rate limit, an unreachable Hub, a reply it could not read. It looks
      // like "no number for this repo" and means something else entirely.
      if (r.error) return null;
      const bytes = file
        ? (typeof r.fileSize === "number" ? r.fileSize : null)
        : (typeof r.usedStorage === "number" ? r.usedStorage : null);
      resolved.set(key, bytes);
      resolvedFit.set(key, r.fit ?? null);
      resolvedSpeed.set(key, r.speedEstimate ?? null);
      return bytes;
    })
    .catch(() => null)
    // Whichever way it went, the key stops being in flight — a rejected
    // lookup that stayed here would wedge the repo/file as forever pending.
    .finally(() => {
      inFlight.delete(key);
    });
  inFlight.set(key, lookup);
  return lookup;
}

/** Tests only: the caches outlive a component, which is the point of them. */
export function _forgetTotalSizes(): void {
  resolved.clear();
  resolvedFit.clear();
  resolvedSpeed.clear();
  inFlight.clear();
}

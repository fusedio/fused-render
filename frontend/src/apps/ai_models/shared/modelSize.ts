// Which size a model card shows, when there are two of them and they disagree.
//
// There are two numbers describing one download and the page used to show both
// at once:
//
//   * ADVERTISED — `size_gb` from `fused_render/ai/catalog.py`, a hand-written
//     approximate constant, documented as approximate, thirty-odd of them;
//   * ACTUAL — the job row's `total`, summed by the worker from the live listing
//     for exactly the files it is fetching (`_repo_files` → `_total_bytes`),
//     with `done` clamped to it.
//
// So a card could read `~64 GB` beside `68 GB / 68 GB` and be describing one
// download: the row right, the constant stale. Read as a download overrunning
// its own size, which is the one thing it cannot do.
//
// The rule, in one place rather than at each of the seven call sites: **once a
// running job for this model reports a real total, that total IS the size.**
// The catalog figure is a pre-download budget hint and nothing more — worth
// showing before the pull starts, never worth contradicting the fetcher with.
//
// Here rather than in a component for the reason `hubSize.ts` gives: there is
// no DOM harness in this repo by design, so the part with a rule in it lives in
// a module that can be driven.
import { formatSize } from "@platform/lib/format";
import { isRunning, type Job } from "@platform/lib/jobs";

/** The live total for this model, or null when there is nothing better than the
 *  catalog's figure to show.
 *
 *  Three conditions, and each one is a case that really happens. RUNNING,
 *  because a finished row keeps its total and a card would then quote a size
 *  measured for a pull that is over — including a CANCELLED one, which never
 *  measured the whole repo at all. A POSITIVE number, because `total` is null
 *  for every phase that does not know its size (a venv build, a weight load)
 *  and 0 is not a size. And `unit === "bytes"`, because `total` is only a byte
 *  count while the row is a download: the same field counts steps on an image
 *  job and seconds on a transcription, and `formatSize(16)` of those would read
 *  "16 B". */
export function liveModelTotal(job: Job | undefined): number | null {
  if (!job || !isRunning(job)) return null;
  if (job.unit !== "bytes") return null;
  return typeof job.total === "number" && job.total > 0 ? job.total : null;
}

/** What the size cell reads: the live total, else the catalog's figure, else the
 *  em-dash this page has always shown for an unmeasured model — an unknown size
 *  is a dash and never a guess. */
export function modelSizeLabel(sizeGb: number | null | undefined, job?: Job): string {
  const live = liveModelTotal(job);
  if (live !== null) return formatSize(live);
  return sizeGb === null || sizeGb === undefined ? "—" : `${sizeGb} GB`;
}

/** The size cell for a card whose FALLBACK figure is not the catalog's — the
 *  Hub search results, which measure a repo from the Hub's own metadata
 *  (`hubSize.ts`) and are a third number again. Null means "nothing to
 *  override", so the caller keeps whatever it worked out.
 *
 *  Same rule, one implementation: a card drawing `ModelProgress` beside a size
 *  must not name a size the progress row disagrees with, whichever table the
 *  other number came from. The title is spelled out here rather than at the call
 *  site because it is the answer to "why did this number just change" — the
 *  estimate was replaced by what the fetcher is actually pulling. */
export function liveSizeOverride(job?: Job): { text: string; title: string } | null {
  const live = liveModelTotal(job);
  if (live === null) return null;
  return {
    text: formatSize(live),
    title: `${formatSize(live)} — the size this download is actually fetching, from the job itself.`,
  };
}

/** The same figure for the places that write it into a sentence — a tooltip, a
 *  button label, the download hint — where "nothing to say" has to be null
 *  rather than a dash, so the caller can leave the phrase out entirely.
 *
 *  `approx` is what the caller should print before the number: the catalog's
 *  constant is approximate and says so ("~"), and the fetcher's own total is
 *  not. A measured 68 GB dressed as "~68 GB" would keep the hedge the live
 *  number exists to remove. */
export function modelSizeHint(
  sizeGb: number | null | undefined,
  job?: Job,
): { text: string; approx: boolean } | null {
  const live = liveModelTotal(job);
  if (live !== null) return { text: formatSize(live), approx: false };
  if (sizeGb === null || sizeGb === undefined) return null;
  return { text: `${sizeGb} GB`, approx: true };
}

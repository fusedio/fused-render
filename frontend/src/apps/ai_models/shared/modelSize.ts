// Which size a model card shows, when there are two of them and they disagree.
//
// There are two numbers describing one download and the page used to show both
// at once:
//
//   * ADVERTISED — `size_gb` from `fused_render/ai/catalog.py`, a hand-written
//     approximate constant, documented as approximate, thirty-odd of them, in
//     DECIMAL GB and covering every byte the download fetches across every repo
//     it touches;
//   * ACTUAL — the job row's `total`, summed by the worker from the live listing
//     for exactly the files the CURRENT fetch is pulling.
//
// So a card could read `~64 GB` beside `68 GB / 68 GB` and be describing one
// download: the row right, the constant stale. Read as a download overrunning
// its own size, which is the one thing it cannot do.
//
// Two things make that comparison harder than it looks, and both are handled
// here rather than at the seven call sites:
//
// **The units differ.** `size_gb` is decimal (4.62e9 bytes → 4.6); `formatSize`
// is base-1024 with a "GB" label, which is what every other size on this page
// uses — the progress row's own `68 GB / 68 GB` included, and `hubSizeLabel`
// converts the Hub's decimal bytes the same way. So the catalog figure is
// converted to BYTES and formatted like everything else. The visible cost is
// that a card reads 4.3 GB where `catalog.py` writes 4.6: the alternative was a
// cell in decimal GB beside a progress row in binary GB, which is the same
// "two numbers, one download" defect this module exists to remove.
//
// **A row's total is one PHASE, not necessarily the whole download.** A single
// download can be several sequential fetches with a scoped total each —
// `torch_image.py`'s GGUF recipe pulls an allow-listed snapshot and then a
// quantized transformer out of a second repo — so a bare phase total can be a
// fraction of what the download will really cost. So for a PHASE total the
// rule is not "the live total wins": it is that **the number shown never
// understates**. A live total LARGER than the advertised figure is a stale
// constant and the row is right; a live total SMALLER is either a phase of a
// multi-part download or a conservative constant, and in both cases quoting
// it would promise a download cheaper than it is.
//
// **`total_scope` (SPEC AI-5n, D498) says whether a row's total is the WHOLE
// download**, and when it is, the never-understate hedge is unnecessary and
// actively wrong: `worker_base.download_plan` sums every phase of a
// multi-repo download into one grand total BEFORE a byte moves, so that
// total is never a fraction the way a bare phase total can be. A live
// "download"-scoped total therefore WINS OUTRIGHT — including over a
// catalog constant that happens to be stale HIGH, which the never-understate
// rule could never correct (it only ever raises what it shows). A "phase"
// total — the shape every reporter has always sent, before this — keeps the
// never-understate rule exactly as it always worked, so a single-repo runner
// is correct without migrating anything.
//
// Here rather than in a component for the reason `hubSize.ts` gives: there is
// no DOM harness in this repo by design, so the part with a rule in it lives in
// a module that can be driven.
import { formatSize } from "@platform/lib/format";
import { isRunning, type Job } from "@platform/lib/jobs";

/** `catalog.py`'s unit, named rather than inlined: `size_gb` is decimal GB, and
 *  a 1024-based reading of it is a figure ~7% out. */
const CATALOG_GB_BYTES = 1e9;

/** The advertised download in bytes, or null when nobody recorded one. */
export function catalogSizeBytes(sizeGb: number | null | undefined): number | null {
  return typeof sizeGb === "number" ? sizeGb * CATALOG_GB_BYTES : null;
}

/** The live total for this model's current fetch, or null when there is none.
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

/** The bytes to show for this model and whether they came from the live row.
 *  The one place the rule lives; see the header for why a "download"-scoped
 *  total wins outright while a "phase"-scoped one only ever raises what is
 *  shown (never-understate). */
function shown(
  sizeGb: number | null | undefined,
  job?: Job,
): { bytes: number; live: boolean } | null {
  const advertised = catalogSizeBytes(sizeGb);
  const live = liveModelTotal(job);
  if (live !== null && job?.total_scope === "download") {
    return { bytes: live, live: true };
  }
  if (live !== null && (advertised === null || live > advertised)) {
    return { bytes: live, live: true };
  }
  return advertised === null ? null : { bytes: advertised, live: false };
}

/** What the size cell reads: the figure from `shown`, else the em-dash this page
 *  has always shown for an unmeasured model — an unknown size is a dash and
 *  never a guess. */
export function modelSizeLabel(sizeGb: number | null | undefined, job?: Job): string {
  const figure = shown(sizeGb, job);
  return figure === null ? "—" : formatSize(figure.bytes);
}

/** The same figure for the places that write it into a sentence — a tooltip, a
 *  button label, the download hint — where "nothing to say" has to be null
 *  rather than a dash, so the caller can leave the phrase out entirely.
 *
 *  `approx` is what the caller should print before the number: the catalog's
 *  constant is approximate and says so ("~"), and a figure the download itself
 *  reported is not. A measured total dressed as "~68 GB" would keep the hedge
 *  the live number exists to remove. */
export function modelSizeHint(
  sizeGb: number | null | undefined,
  job?: Job,
): { text: string; approx: boolean } | null {
  const figure = shown(sizeGb, job);
  return figure === null ? null : { text: formatSize(figure.bytes), approx: !figure.live };
}

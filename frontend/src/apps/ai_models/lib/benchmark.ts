// What the Benchmark tab SAYS about a run, separated from what draws it — the
// same split, and for the same reason, as `engines.ts` and `aiModelGroups.ts`
// beside it (SPEC AI-14).
//
// The rendering is a table, a row of numbers and a polyline. The places this
// feature can actually be WRONG are all in here, and none of them is visible in
// a screenshot:
//
//   * **Which number is the primary one, and which way is better.** Three of
//     the four capabilities are "bigger is faster"; image generation reports
//     seconds per step, where bigger is slower. A shared "up is green" rule
//     would state the opposite of the truth on one section in four.
//   * **null vs 0.** The server's standing rule is that a metric it did not
//     measure is null, never zero (ai/benchmark.py). A renderer doing
//     `value || "—"` turns a genuine 0 into a dash; one doing `value ?? 0`
//     turns "this runner does not report tokens" into "this model generated
//     nothing". Both look fine until the day a number matters.
//   * **The workload-revision seam.** Runs measured under different revisions
//     of a workload are not comparable, so a delta across one would be a
//     fabricated regression. `latestByModel` refuses to draw it.
//
// Nothing here invents a label or a byte formatter: capability names come from
// `capabilityLabel`, sizes from `formatSize`, and the section order from the
// Local tab's own `CAPABILITY_ORDER`. A second copy of any of those is how one
// page comes to disagree with itself.
import type { AiBenchmarkMetrics, AiBenchmarkRun, AiBenchmarkWorkload } from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";
import { CAPABILITY_ORDER } from "@apps/ai_models/lib/aiModelGroups";

/** What a run under this capability actually DOES, in one sentence — server
 *  fact, never a frontend guess (D483). The Benchmark tab's only other words
 *  about the workload were the tab subtitle ("a fixed workload per
 *  capability, timed on this machine"), which never said WHAT the fixed
 *  work actually was; a reader had no way to learn that a text run decodes
 *  128 tokens greedily, or that a speech run transcribes a 30-second tone,
 *  without reading `ai/benchmark.py` themselves.
 *
 *  Built from `workload.params` — the SAME object the server sends, built by
 *  `Workload.as_dict()` — never a second, hand-copied table of those facts
 *  on this side: `tests/test_ai_benchmark_workload_note.py` pins every
 *  param name used below against the server's own `WORKLOADS` so a new
 *  param added there fails a test here rather than drifting silently, the
 *  same class of guard D470 already gives `_IMAGE_WIRE_KEYS`/
 *  `_TRANSCRIBE_WIRE_KEYS`.
 *
 *  **`prompt` and `texts` are read but never echoed verbatim** — a reader
 *  comparing two models needs to know the prompt/texts are FIXED, not the
 *  words themselves, which would turn one line into a paragraph for no
 *  comparison anybody is making. `_CONTENT_PARAMS_WITH_NO_LITERAL_ECHO` in
 *  that same drift test is the explicit, narrow exemption for those two —
 *  every OTHER param name in `workload.params` must appear literally
 *  somewhere in the strings this function returns.
 *
 *  Returns null for a capability this function does not know how to
 *  describe (there is currently one for one — every capability with a
 *  workload at all has a case below) rather than a generic fallback
 *  sentence that would say nothing. */
export function workloadNote(capability: string, workload: AiBenchmarkWorkload | null): string | null {
  if (!workload) return null;
  const p = workload.params;
  const provenance = `${workload.name} · rev ${workload.revision}`;
  switch (capability) {
    case "text-generation":
      return `Decodes ${p.maxTokens} tokens from a fixed prompt, greedy (temperature ${p.temperature}) — ${provenance}.`;
    case "text-to-image":
      // `steps` is pinned in `params` like every other field here
      // (ai/benchmark.py) — every model renders at the same step count, which
      // is what makes total render time comparable across models.
      return `Renders a fixed ${p.width}×${p.height} prompt at ${p.steps} steps, guidance ${p.guidance}, seed ${p.seed} — ${provenance}.`;
    case "automatic-speech-recognition":
      return `Transcribes ${p.audioSeconds}s of a generated ${p.toneHz} Hz tone at ${p.sampleRate} Hz — ${provenance}.`;
    case "embeddings":
      return `Encodes ${p.batch} fixed texts as one batch — ${provenance}.`;
    default:
      return null;
  }
}

/** What an unmeasured number renders as. One constant, so a table cell, a
 *  summary line and a chart caption cannot each pick a different dash. */
export const DASH = "—";

/** A metric this frontend can read off a run — either a key inside
 *  `run.metrics` (capability-specific), or one of the two universal facts that
 *  live on the run record ITSELF rather than in `metrics`: peak memory and
 *  load time. One union rather than two separate lookup paths through the
 *  rest of this file, so a caller comparing "is this the selected metric"
 *  never has to know which of the two shapes it came from. */
export type MetricKey = keyof AiBenchmarkMetrics | "peakResidentBytes" | "loadSeconds";

export interface MetricSpec {
  key: MetricKey;
  /** For a column heading, a chart's y axis, or a `<select>` option. */
  label: string;
  unit: string;
  /** Whether a bigger number is a faster model. False for seconds per step,
   *  load time and memory — a chart, a unit badge or a leaderboard bar drawn
   *  on any of those must read "smaller is better" or it states the opposite
   *  of the truth. */
  higherIsBetter: boolean;
  /** Decimal places. Rates get one; a ratio people read as "40× faster than
   *  listening to it" gets one too, and nothing here gets more — a benchmark
   *  repeated twice does not agree to three. Unused for `peakResidentBytes`,
   *  which is formatted through `formatSize` instead of digits-and-a-unit. */
  digits: number;
}

/** Every metric this frontend can plot or rank by, per capability — the
 *  primary one FIRST (the section's chart and row default to it, and
 *  `primaryMetric` below is just `[0]`), then whichever of the capability's
 *  OTHER recorded numbers are worth a second look.
 *
 *  **Only genuine PERFORMANCE numbers are listed — never a workload
 *  parameter echoed back on the record.** `steps`, `width`, `height`, `dim`,
 *  `batch` and `audioSeconds` are all real keys `ai/benchmark.py`'s measure
 *  functions return, and all excluded here: they are the FIXED WORKLOAD
 *  describing itself, constant across every run of a given model (or every
 *  run of any model, for the audio/embedding ones), so charting one over time
 *  is a flat line with nothing to say — the exact "noise dressed as data"
 *  problem model size has, applied to a number that at least LIVES on the
 *  run record. `outputTokens` is excluded for the same reason on a fixed
 *  `maxTokens` workload: a healthy run always reports the same count, and it
 *  is a completion count, not a speed.
 *
 *  `peakResidentBytes` and `loadSeconds` are appended to every list rather
 *  than declared per capability, because both are universal — every run
 *  measures memory, and every run attempts a load — but PHYSICALLY live on
 *  `AiBenchmarkRun` itself rather than inside `metrics` (see `MetricKey`).
 *
 *  Declared here rather than inferred from which keys a run happens to carry:
 *  every capability reports several metrics, so "the first non-null one" would
 *  silently change what a chart means the day a runner starts reporting one
 *  more. Keyed by the server's capability constants (ai/registry.py).
 */
const METRICS: Record<string, MetricSpec[]> = {
  "text-generation": [
    { key: "tokensPerSecond", label: "Throughput", unit: "tok/s", higherIsBetter: true, digits: 1 },
    { key: "ttftMs", label: "Time to first token", unit: "ms", higherIsBetter: false, digits: 0 },
    { key: "promptTokensPerSecond", label: "Prompt read", unit: "tok/s", higherIsBetter: true, digits: 1 },
    { key: "peakResidentBytes", label: "Peak memory", unit: "", higherIsBetter: false, digits: 0 },
    { key: "loadSeconds", label: "Load time", unit: "s", higherIsBetter: false, digits: 1 },
  ],
  "text-to-image": [
    // Total seconds IS the primary now. It was not, while the step count was
    // per-model: the two could not be compared, so per-step stood in. The
    // workload fixes steps for every model (ai/benchmark.py), which makes the
    // wall-clock figure directly comparable AND the one a person actually
    // waits — while per-step, which divides a total that includes fixed text
    // encoding and VAE decode, is the derived number rather than the honest
    // one. Kept as its own series because it still separates a slow step from
    // a slow load.
    { key: "totalSeconds", label: "Total render", unit: "s", higherIsBetter: false, digits: 1 },
    { key: "secondsPerStep", label: "Per step", unit: "s/step", higherIsBetter: false, digits: 2 },
    { key: "peakResidentBytes", label: "Peak memory", unit: "", higherIsBetter: false, digits: 0 },
    { key: "loadSeconds", label: "Load time", unit: "s", higherIsBetter: false, digits: 1 },
  ],
  "automatic-speech-recognition": [
    { key: "realtimeFactor", label: "Speed", unit: "× realtime", higherIsBetter: true, digits: 1 },
    { key: "totalSeconds", label: "Decode time", unit: "s", higherIsBetter: false, digits: 1 },
    { key: "peakResidentBytes", label: "Peak memory", unit: "", higherIsBetter: false, digits: 0 },
    { key: "loadSeconds", label: "Load time", unit: "s", higherIsBetter: false, digits: 1 },
  ],
  "embeddings": [
    { key: "textsPerSecond", label: "Throughput", unit: "texts/s", higherIsBetter: true, digits: 1 },
    { key: "peakResidentBytes", label: "Peak memory", unit: "", higherIsBetter: false, digits: 0 },
    { key: "loadSeconds", label: "Load time", unit: "s", higherIsBetter: false, digits: 1 },
  ],
};

/** The primary metric for a capability, or null when this frontend does not
 *  know the capability. The FIRST entry in `METRICS`' list — the chart and the
 *  row default to it, and the metric `<select>` opens on it.
 *
 *  Null rather than a fallback: a capability added server-side should render as
 *  a section with a run table and no chart, which is honest, rather than as a
 *  chart of whichever number happened to be first.
 */
export function primaryMetric(capability: string): MetricSpec | null {
  return METRICS[capability]?.[0] ?? null;
}

/** Every metric the SELECTOR should offer for a capability, filtered to the
 *  ones at least one run actually measured — a metric where every run is
 *  `null` (a capability whose runs are all warm, say, for `loadSeconds`) is
 *  not worth a dead option in the dropdown.
 *
 *  Falls back to the FULL declared list when `runs` cannot answer the
 *  question either way: an empty list (nothing recorded yet, or every
 *  recorded run happened to measure nothing) must not strand the selector
 *  with zero options, which is a worse failure than offering one that turns
 *  out empty.
 */
export function availableMetrics(capability: string, runs: AiBenchmarkRun[]): MetricSpec[] {
  const specs = METRICS[capability] ?? [];
  if (runs.length === 0) return specs;
  const measured = specs.filter((spec) => runs.some((run) => metricValueForSpec(run, spec) !== null));
  return measured.length > 0 ? measured : specs;
}

/** The metric the URL's `?benchMetric=` names, when it is one this capability
 *  actually offers — `defaultMetric`'s pick (the first of `specs`, i.e. the
 *  primary) otherwise. Same forgiving posture as `resolveCapability`: a stale
 *  or foreign key falls through to the default rather than rendering nothing. */
export function resolveMetric(specs: MetricSpec[], param: string | null): MetricSpec | null {
  if (param) {
    const found = specs.find((spec) => spec.key === param);
    if (found) return found;
  }
  return specs[0] ?? null;
}

/** A number, or null when it was not measured. `undefined` (the key absent for
 *  this capability) and `null` (the runner did not report it) collapse to the
 *  same answer, because both mean "no measurement" to a reader. */
function metricValue(run: AiBenchmarkRun, key: keyof AiBenchmarkMetrics): number | null {
  const value = run.metrics[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** A run's value for ANY `MetricSpec` — capability metric or one of the two
 *  universal fields — or null when `spec` itself is null (an unknown
 *  capability) or the value was not measured. The one place that has to know
 *  `peakResidentBytes`/`loadSeconds` live outside `run.metrics`; every other
 *  function in this file reads a metric through here rather than re-deciding
 *  where on the record it lives. */
export function metricValueForSpec(run: AiBenchmarkRun, spec: MetricSpec | null): number | null {
  if (!spec) return null;
  if (spec.key === "peakResidentBytes") return run.peakResidentBytes;
  if (spec.key === "loadSeconds") return run.loadSeconds;
  return metricValue(run, spec.key);
}

/** The run's primary metric, or null. Null for a failed run (no metrics at
 *  all), for an unknown capability, and for a runner that did not report it. */
export function primaryValue(run: AiBenchmarkRun): number | null {
  return metricValueForSpec(run, primaryMetric(run.capability));
}

/** `n` at `digits` places with trailing zeros trimmed — "42.1", "0", "3".
 *
 *  Trimmed because the alternative reads as false precision: "42.0 tok/s"
 *  claims a tenth the second run will not reproduce.
 *
 *  Exported (not just an internal helper) because the chart's end-labels and
 *  y-axis ticks need the SAME trimming rule the table and the row use — a
 *  second copy is how an axis comes to show "42.0" beside a row that shows
 *  "42.1 tok/s · loaded in 42".
 */
export function formatNumber(value: number, digits: number): string {
  return String(Number(value.toFixed(digits)));
}

/** `value` with its unit, or a dash. Explicit `null` check, NEVER a falsy one:
 *  a measured 0 is a real (and interesting) result. */
function withUnit(value: number | null, unit: string, digits: number): string {
  if (value === null) return DASH;
  // Seconds are the one unit whose OWN magnitude can put a real measurement
  // at zero decimal places away from disappearing — see `formatDuration`.
  if (unit === "s") return formatDuration(value, digits);
  return `${formatNumber(value, digits)} ${unit}`;
}

/** A duration in SECONDS, formatted so a genuinely sub-second measurement
 *  stays legible rather than rounding to "0s".
 *
 *  **The bug this fixes**: `whisper-tiny.en-8bit`'s `totalSeconds` is a real
 *  0.022–0.035 across five runs — a fast model doing a fast job, not a
 *  missing measurement — but at this capability's one decimal place
 *  (`digits`), `formatNumber` rounds every one of them to "0.0", trimmed to
 *  the bare, misleading "0" (`formatNumber`'s own trailing-zero trim, correct
 *  everywhere else, is what turns "0.0" into "0" here). A reader sees "0s"
 *  and reasonably reads it as "not measured" or "instant", neither of which
 *  is true.
 *
 *  Below one second, this reports MILLISECONDS instead, at whole-number
 *  precision — `digits` (built for the second-scale range) does not apply
 *  there; a sub-second value is precise enough in whole ms that a fraction of
 *  one would be false precision anyway. At or above one second it is
 *  unchanged: `formatNumber(seconds, digits)` at the metric's own precision,
 *  exactly as before — `loadSeconds`' own 1.0–24.6 s range never crosses the
 *  ms threshold and reads exactly as it always has.
 *
 *  One function behind `withUnit`, so every caller — the leaderboard
 *  headline, a bar's own end-value label, the Details line, and both charts'
 *  axis ticks (`axisTickLabel` reuses it below) — reports the same number the
 *  same way; a second copy is how one of them comes to still print "0s" the
 *  day this one is fixed.
 */
export function formatDuration(seconds: number, digits: number): string {
  if (Math.abs(seconds) < 1) return `${formatNumber(seconds * 1000, 0)} ms`;
  return `${formatNumber(seconds, digits)} s`;
}

/** `value` as `spec` reports it — "42.1 tok/s", "5.2 GB", or a dash.
 *
 *  `peakResidentBytes` is special-cased to the platform's own byte formatter
 *  rather than `digits`-and-a-unit: "5872000000 bytes" is not a number anyone
 *  reads, and `formatSize` is the one place this app turns a byte count into
 *  "5.2 GB" — a second copy here is how a memory chart comes to disagree with
 *  the Local tab's own sizes.
 */
export function formatMetricSpecValue(value: number | null, spec: MetricSpec): string {
  if (value === null) return DASH;
  if (spec.key === "peakResidentBytes") return formatSize(value);
  return withUnit(value, spec.unit, spec.digits);
}

/** The section's own metric badge — unit and, where it matters, DIRECTION —
 *  never the metric's own name (the `<select>` right beside this badge
 *  already shows that; repeating it here is exactly the duplicated-ink this
 *  file has already cut once, see `ComparisonChart`'s own header comment).
 *
 *  "lower is better" is stated ONLY for a `!higherIsBetter` metric, and
 *  NEVER the mirror phrase for a higher-is-better one — driven off the
 *  metric's own `higherIsBetter` flag, not a hardcoded list of metric keys,
 *  so a lower-is-better metric added later gets the cue automatically rather
 *  than needing to be remembered. The reason for the asymmetry: both the
 *  comparison chart's bars and the leaderboard's own rank order already read
 *  correctly for a higher-is-better metric under the ordinary "longer bar /
 *  bigger number wins" habit — labelling that case too would be noise that
 *  makes the one case actually worth flagging (a SHORTER bar winning) stop
 *  standing out. Said ONCE, where the metric is chosen — folded into the select's
 *  own option text by `metricOptionLabel` and read from there by both
 *  instruments below it (the comparison chart and the per-model trend chart
 *  both invert the same way) rather than drawn again beside either. The share
 *  card (`shareCard.ts`) calls this directly, since a PNG leaving this app has
 *  no select to carry it.
 */
export function metricUnitAndCue(metric: MetricSpec): string {
  const parts: string[] = [];
  if (metric.unit) parts.push(metric.unit);
  if (!metric.higherIsBetter) parts.push("lower is better");
  return parts.join(" · ");
}

/** One `<option>` in the Metric select: the metric's name with its unit and
 *  direction cue in parentheses behind it — "Speed (× realtime)", "Peak memory
 *  (lower is better)", "Throughput (tok/s)".
 *
 *  **This replaced a separate badge beside the select**, and the reason is the
 *  transcription unit: `× realtime` is a multiplier SUFFIX, which parses only
 *  when it trails a number ("1.4× realtime"). Alone in a bordered pill next to
 *  a control it stops being a unit and becomes a stray operator — it read as
 *  the dismiss ✕ of a removable filter chip, which is a shape this app uses
 *  elsewhere for exactly that meaning (the Tasks filter pills). Inside the
 *  option it is back to trailing words that give it a subject.
 *
 *  It also answers the metric whose unit is EMPTY: `Peak memory` formats
 *  through the byte formatter and carries no unit string, so the badge there
 *  drew a chip containing nothing but "lower is better".
 *
 *  A metric with neither a unit nor a cue (none today — every
 *  higher-is-better metric here has a unit) gets no parenthetical rather than
 *  an empty pair of brackets. */
export function metricOptionLabel(metric: MetricSpec): string {
  const suffix = metricUnitAndCue(metric);
  return suffix ? `${metric.label} (${suffix})` : metric.label;
}

/** The primary metric as the page shows it — "42.1 tok/s", or a dash. */
export function formatPrimary(run: AiBenchmarkRun): string {
  const metric = primaryMetric(run.capability);
  if (!metric) return DASH;
  return formatMetricSpecValue(metricValueForSpec(run, metric), metric);
}

/** Seconds to load, or a dash for a warm run. */
export function formatLoad(run: AiBenchmarkRun): string {
  return withUnit(run.loadSeconds, "s", 1);
}

/** Resident bytes through the platform's own byte formatter, or a dash. */
export function formatMemory(run: AiBenchmarkRun): string {
  return run.peakResidentBytes === null ? DASH : formatSize(run.peakResidentBytes);
}

/** The one-line reading of a run — what the model's row says.
 *
 *  **Unmeasured parts are OMITTED rather than dashed.** A row of five dashes
 *  says nothing five times; the primary metric is the exception and always
 *  keeps its slot, because a benchmark that produced no number is itself the
 *  news. A failed run replaces the line entirely with why: the numbers that
 *  would have gone here do not exist, and a line of dashes reads as a page bug
 *  rather than as an out-of-memory.
 */
export function summaryLine(run: AiBenchmarkRun): string {
  if (!run.ok) return `Failed — ${failureReason(run)}`;
  const parts: string[] = [formatPrimary(run)];
  const ttft = metricValue(run, "ttftMs");
  if (ttft !== null) parts.push(`TTFT ${formatNumber(ttft, 0)} ms`);
  const steps = metricValue(run, "steps");
  if (steps !== null) parts.push(`${formatNumber(steps, 0)} steps`);
  if (run.peakResidentBytes !== null) parts.push(formatMemory(run));
  if (run.loadSeconds !== null) parts.push(`loaded in ${formatLoad(run)}`);
  if (run.device) parts.push(run.device);
  return parts.join(" · ");
}

/** The org half of a model id, dropped — "whisper-large-v3-mlx" rather than
 *  "mlx-community/whisper-large-v3-mlx". The compact row scans down a column
 *  of these, and the org prefix is what makes every row in one section start
 *  with the same six characters; the full id still lives in the row's title
 *  attribute for the reader who needs to tell two orgs' same-named repo apart. */
export function shortModelName(model: string): string {
  const slash = model.lastIndexOf("/");
  return slash === -1 ? model : model.slice(slash + 1);
}

/** Why a failed run failed, or a fallback when the server did not say. Its own
 *  function (not inlined at each call site) because `summaryLine` and the
 *  row's details expander both need the identical fallback wording — a second
 *  copy is how one comes to say "unknown error" while the other says nothing
 *  at all. */
export function failureReason(run: AiBenchmarkRun): string {
  return run.error ?? "no reason given";
}

/** The compact row's ONE line: the primary metric and memory, nothing else.
 *
 *  Load time, device and TTFT used to be crammed into this same line — see
 *  `summaryLine`, which still carries them for the details expander and the
 *  archive table — and a failed run's full `RuntimeError` paragraph used to go
 *  here too, dominating the section. Both were the verbosity this function
 *  exists to cut: a failed run says just "Failed", with the reason a click
 *  away in `rowDetail`/`failureReason` rather than the row's headline.
 *
 *  **`metric` is the SELECTED one, not necessarily the primary** — the
 *  leaderboard now reads by whichever metric the picker is on, so the row it
 *  leads with has to be that metric's value, not always throughput. Memory
 *  rides along as a second fact UNLESS memory itself is what is selected,
 *  which would otherwise print it twice.
 */
export function rowHeadline(run: AiBenchmarkRun, metric: MetricSpec | null): string {
  if (!run.ok) return "Failed";
  if (!metric) return DASH;
  const parts: string[] = [formatMetricSpecValue(metricValueForSpec(run, metric), metric)];
  if (metric.key !== "peakResidentBytes" && run.peakResidentBytes !== null) {
    parts.push(formatMemory(run));
  }
  return parts.join(" · ");
}

/** What `rowHeadline` left out of a SUCCESSFUL run — TTFT, step count, load
 *  time, device — or null when there is nothing left over, which tells the
 *  row not to draw an expander for a run whose whole story is one number.
 *
 *  A failed run's detail is its error text, read through `failureReason`
 *  instead: that is a different KIND of fact (why it broke, not what else it
 *  measured), so this function returns null for one rather than folding both
 *  into one string a caller would have to re-parse to tell apart.
 *
 *  `metric` again decides what NOT to repeat: TTFT and load time drop out of
 *  this line exactly when one of them is the thing the headline is already
 *  showing.
 */
export function rowDetail(
  run: AiBenchmarkRun,
  metric: MetricSpec | null,
  expectedDevice: string | null = null,
): string | null {
  if (!run.ok) return null;
  const parts: string[] = [];
  const ttft = metricValue(run, "ttftMs");
  if (ttft !== null && metric?.key !== "ttftMs") parts.push(`TTFT ${formatNumber(ttft, 0)} ms`);
  const steps = metricValue(run, "steps");
  if (steps !== null) parts.push(`${formatNumber(steps, 0)} steps`);
  if (run.loadSeconds !== null && metric?.key !== "loadSeconds") {
    parts.push(`loaded in ${formatLoad(run)}`);
  }
  // The device is USUALLY the same string for every model on one machine —
  // repeating it on every row is noise, so it is DROPPED whenever it matches
  // `expectedDevice` (the capability's own common device — `commonDevice`
  // below computes it, and `BenchmarkTab.tsx` is the one caller that knows
  // it). It is KEPT when it differs: a runner falling back to CPU for one
  // model while everything else in the section runs on `mps` is exactly the
  // fact a reader wants surfaced, and that is the outlier this rule exists to
  // preserve rather than delete along with the repetition. Defaults to null
  // (never matches a real device string) so a caller with no opinion about
  // what is "expected" gets the old, always-shown behaviour.
  if (run.device && run.device !== expectedDevice) parts.push(run.device);
  return parts.length > 0 ? parts.join(" · ") : null;
}

export interface BenchmarkDelta {
  /** Signed percentage change in the primary metric, latest against previous. */
  percent: number;
  /** Whether that change is an IMPROVEMENT — which is not the sign of
   *  `percent`: on an image section a negative change is the good one. */
  better: boolean;
  previous: AiBenchmarkRun;
}

export interface ModelLatest {
  model: string;
  /** The newest run for this model, failed or not — the page shows the most
   *  recent truth, and hiding a failure behind an older success would state
   *  that the model still works. */
  latest: AiBenchmarkRun;
  delta: BenchmarkDelta | null;
}

/** The device most of a capability's benchmarked models last ran on — the
 *  "expected" device `rowDetail` compares each model's own device against to
 *  decide whether it is worth printing.
 *
 *  On one machine every model almost always reports the SAME device (`mps`,
 *  `cuda`, `cpu`) — the hardware does not change per model — which is exactly
 *  why the per-model detail line drops it by default. But "almost always" is
 *  not "always": a runner that falls back to CPU for one model while every
 *  other model in the section runs on `mps` is a genuinely interesting fact,
 *  and the model whose device does not match this one is the outlier that
 *  fact belongs to.
 *
 *  A plurality vote across every LATEST run in the section, not a strict
 *  unanimous check — one outlier must not blank the "expected" device for
 *  everyone else, which a `some row disagrees -> no consensus` rule would do.
 *  Ties and an all-null section fall back to null, in which case `rowDetail`
 *  shows every model's device: with no majority to call "expected", nothing
 *  is an outlier either.
 */
export function commonDevice(rows: ModelLatest[]): string | null {
  const counts = new Map<string, number>();
  for (const row of rows) {
    const device = row.latest.ok ? row.latest.device : null;
    if (device) counts.set(device, (counts.get(device) ?? 0) + 1);
  }
  let best: string | null = null;
  let bestCount = 0;
  // A strict `count > bestCount` alone picks whichever device the Map
  // happens to iterate first among ties — insertion order, i.e. which model
  // reported it first — and never actually returns null for one, despite the
  // doc above promising it. `tied` tracks whether the CURRENT leader shares
  // its count with another device, and is cleared the moment a later device
  // strictly exceeds it (a real majority, not a tie).
  let tied = false;
  for (const [device, count] of counts) {
    if (count > bestCount) {
      best = device;
      bestCount = count;
      tied = false;
    } else if (count === bestCount) {
      tied = true;
    }
  }
  return tied ? null : best;
}

/** Runs for one capability, oldest first. Order is the store's own append
 *  order re-established by `startedAt`, because a caller may have concatenated
 *  a fresh run onto a fetched list. */
export function runsFor(runs: AiBenchmarkRun[], capability: string): AiBenchmarkRun[] {
  return runs.filter((r) => r.capability === capability).sort((a, b) => a.startedAt - b.startedAt);
}

/** The newest run per model, with the delta against the last COMPARABLE one.
 *
 *  Comparable means: the same model, an earlier run, the same workload
 *  revision, and a metric that was actually measured on both. Each of those
 *  drops a specific wrong answer —
 *
 *  * a different revision measured different work, so the difference is not the
 *    model's (this is the seam `revision` exists for);
 *  * a failed or unmeasured run has no number, and treating its absence as a
 *    zero would report a 100% improvement out of nowhere.
 *
 *  **`metric` defaults to each run's own primary** (omit it, or pass `null`,
 *  and every model is scored on `primaryMetric(latest.capability)` — the
 *  original behaviour). Passing an explicit metric scores every model on THAT
 *  one instead, which is what the leaderboard now does once a reader picks a
 *  different metric than the default: the delta shown beside a bar must be a
 *  delta IN the thing the bar is ranking by, or the percentage and the ranking
 *  would be reporting two different measurements under one row.
 *
 *  Models come back in first-appearance order, which is the order the caller's
 *  list already has them in — the section's own model list decides layout, not
 *  this function.
 */
export function latestByModel(runs: AiBenchmarkRun[], metric?: MetricSpec | null): ModelLatest[] {
  const byModel = new Map<string, AiBenchmarkRun[]>();
  for (const run of [...runs].sort((a, b) => a.startedAt - b.startedAt)) {
    const list = byModel.get(run.model);
    if (list) list.push(run);
    else byModel.set(run.model, [run]);
  }
  const rows: ModelLatest[] = [];
  for (const [model, history] of byModel) {
    const latest = history[history.length - 1]!;
    const spec = metric ?? primaryMetric(latest.capability);
    const value = metricValueForSpec(latest, spec);
    let delta: BenchmarkDelta | null = null;
    if (value !== null) {
      for (let i = history.length - 2; i >= 0; i--) {
        const candidate = history[i]!;
        if (candidate.workload.revision !== latest.workload.revision) continue;
        const before = metricValueForSpec(candidate, spec);
        if (before === null || before === 0) continue;
        const percent = ((value - before) / before) * 100;
        const higherIsBetter = spec?.higherIsBetter ?? true;
        delta = { percent, better: higherIsBetter ? percent >= 0 : percent <= 0, previous: candidate };
        break;
      }
    }
    rows.push({ model, latest, delta });
  }
  return rows;
}

export interface SeriesPoint {
  /** The run's position in this capability's whole history — a SHARED x axis,
   *  so two models' runs interleave in the order they were actually taken
   *  rather than each restarting at zero. */
  x: number;
  y: number;
  run: AiBenchmarkRun;
}

export interface Series {
  model: string;
  points: SeriesPoint[];
}

/** One polyline per model over a set of runs, plus the y domain.
 *
 *  **Scoped to ONE model's history in practice** — the trend chart's whole
 *  redesign point (a comparison across models belongs to the leaderboard, not
 *  a shared timeline, see `BenchmarkTab.tsx`) — but this function stays
 *  generic over `run.model` rather than assuming one, the same way it always
 *  has: nothing here breaks if a caller ever hands it more than one model's
 *  runs again.
 *
 *  A run with no value for `metric` contributes no point: the alternative is
 *  plotting a zero, which on a throughput chart is a visible, believable claim
 *  that the model produced nothing. `metric` defaults to each run's own
 *  primary, same as `latestByModel`.
 *
 *  The domain starts at **zero** rather than at the smallest value. A rate axis
 *  cropped to its own range turns a 3% difference into a chart where one point
 *  looks twice another — the classic misleading axis, and this chart's entire
 *  job is an honest one.
 */
export function chartSeries(runs: AiBenchmarkRun[], metric?: MetricSpec | null): {
  series: Series[];
  yMin: number;
  yMax: number;
} {
  const ordered = [...runs].sort((a, b) => a.startedAt - b.startedAt);
  const series: Series[] = [];
  const index = new Map<string, Series>();
  let yMax = 0;
  ordered.forEach((run, x) => {
    const spec = metric ?? primaryMetric(run.capability);
    const y = metricValueForSpec(run, spec);
    if (y === null) return;
    let entry = index.get(run.model);
    if (!entry) {
      entry = { model: run.model, points: [] };
      index.set(run.model, entry);
      series.push(entry);
    }
    entry.points.push({ x, y, run });
    if (y > yMax) yMax = y;
  });
  // `yMax` accumulated rather than `Math.max(...ys)`: over an empty list that
  // spreads to -Infinity, which renders as an SVG with NaN coordinates and no
  // error anywhere.
  return { series, yMin: 0, yMax };
}

export interface AxisTick {
  value: number;
  /** Through `formatNumber`, so a gridline reads "25" beside a row that reads
   *  "25 tok/s" — never "25.0" beside it. */
  label: string;
}

/** `value` formatted the way ITS OWN chart tick should read.
 *
 *  A byte-valued metric (only `peakResidentBytes` today) goes through
 *  `formatSize` — the identical formatter every leaderboard row's own memory
 *  figure and every comparison-chart bar's own end-value label already use —
 *  because the STORED unit (bytes) is never what a reader reads an axis in;
 *  "294748160" is not a number anyone reads as memory, where "281 MB" is.
 *  Every other metric already stores its own display unit, so a tick is just
 *  its rounded number, unlabelled — the unit itself is stated once already,
 *  by the section's metric badge and by every row's own value, and repeating
 *  it on every gridline would be the wrong kind of literal.
 *
 *  **One function for every axis this tab draws** (`yAxisTicks` below and
 *  `niceAxisTicks`), so a byte-valued metric added after this one gets the
 *  fix for free rather than needing its own axis to remember it — the bug
 *  this exists to prevent was exactly a chart that forgot, and reformatted a
 *  raw byte count as a bare number.
 */
function axisTickLabel(value: number, metric: MetricSpec): string {
  if (metric.key === "peakResidentBytes") return formatSize(value);
  // A seconds-valued tick goes through the SAME ms/s switch every other
  // duration reading does (`formatDuration`) — a nice round step on a
  // sub-second domain (0.022 s peak, say) is a nice round number of
  // MILLISECONDS, and a bare "0" tick repeated four times is the exact bug
  // this whole file exists to prevent, just on an axis instead of a row.
  if (metric.unit === "s") return formatDuration(value, metric.digits);
  return formatNumber(value, metric.digits);
}

/** `count + 1` evenly spaced gridlines from 0 to `yMax`, the chart's own
 *  domain — not a "nice round number" scale that would need to EXTEND the
 *  domain past the tallest point to land on one. Equal division always ends
 *  exactly on `yMax`, so the top gridline IS the peak, and the chart does not
 *  need a separate "peak N unit" caption to say so.
 *
 *  Empty over an empty domain: a scale with a top of zero is not a scale for
 *  anything, and the chart already draws nothing in that case (see
 *  `BenchmarkChart`).
 *
 *  Used by `ModelTrendChart`, against its own `paddedAxisMax` domain — see
 *  `niceAxisTicks` below for the COMPARISON chart's own, differently-shaped
 *  axis (round numbers past the raw peak, no padding).
 */
export function yAxisTicks(yMax: number, metric: MetricSpec, count = 4): AxisTick[] {
  if (yMax <= 0) return [];
  const ticks: AxisTick[] = [];
  for (let i = 0; i <= count; i++) {
    const value = (yMax * i) / count;
    ticks.push({ value, label: axisTickLabel(value, metric) });
  }
  return ticks;
}

/** A run's date as a short, LOCALE label — "Aug 12", never a full timestamp.
 *  The chart's x axis used to say only "oldest"/"newest" because the runs are
 *  not evenly spaced in time (see `BenchmarkChart`'s own comment on this); a
 *  couple of these at the ends and the middle name WHEN without claiming an
 *  even spacing the axis does not have. */
export function formatRunDate(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

/** A run's time of day — "14:22" — for the chart's x axis when a DATE tick
 *  would say the same thing three times over (see `chartAxisTicks`). No
 *  seconds: this is "roughly when in the day", not a precise timestamp — the
 *  archive table's `toLocaleString()` is where a reader who wants the exact
 *  second should look. */
export function formatRunTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export interface ChartAxisTicks {
  ticks: { x: number; label: string }[];
  /** The single date every TIME tick shares, or null when the ticks are
   *  already dates and repeating one in a caption would say nothing new. */
  dateCaption: string | null;
}

/** The chart's x-axis ticks — first run, the middle one, the last (never a
 *  claim of even spacing the runs don't have, see `BenchmarkChart`) — as
 *  DATES when they span more than a day, or as TIMES with the shared date
 *  stated once in `dateCaption` when they don't.
 *
 *  **This is the fix for a real bug**: four runs taken minutes apart within
 *  one day used to draw three identical "22 Aug" ticks, which tells a reader
 *  nothing about when, within that day, each run happened — the axis is
 *  supposed to distinguish points, and three copies of one date distinguishes
 *  none of them. The threshold is the runs' OWN span (oldest to newest, under
 *  24 hours), not a fixed rule, because "the same day" is a fact about the
 *  data, not a property of the chart.
 *
 *  A single run still gets a DATE, never a time: a lone time of day with no
 *  date on screen anywhere names a moment with nothing to anchor it to, which
 *  is a worse answer than the date alone already was.
 */
export function chartAxisTicks(runs: AiBenchmarkRun[]): ChartAxisTicks {
  if (runs.length === 0) return { ticks: [], dateCaption: null };

  const indices =
    runs.length === 1
      ? [0]
      : runs.length === 2
        ? [0, 1]
        : [0, Math.floor((runs.length - 1) / 2), runs.length - 1];

  const span = runs[runs.length - 1]!.startedAt - runs[0]!.startedAt;
  // **`span < 24h` alone is not "the same day" — it is only "the same day
  // MOST of the time".** A real run of this bug: 23:02 one evening to 14:07
  // the next afternoon is under 24 hours elapsed but crosses midnight, so the
  // old rule drew time-only ticks ("11:02 PM" / "01:50 PM") under a single
  // "Aug 22" caption that was wrong for the later points — they happened on
  // Aug 23. `sameCalendarDay` checks the actual local dates of the first and
  // last tick, not just the elapsed duration between them; when they differ,
  // this falls through to dated ticks (each stating its OWN date, correctly)
  // exactly as it already does for a multi-day span.
  const useTime =
    runs.length > 1 &&
    span < 24 * 60 * 60 &&
    sameCalendarDay(runs[0]!.startedAt, runs[runs.length - 1]!.startedAt);

  const ticks = indices.map((i) => ({
    x: i,
    label: useTime ? formatRunTime(runs[i]!.startedAt) : formatRunDate(runs[i]!.startedAt),
  }));

  return { ticks, dateCaption: useTime ? formatRunDate(runs[0]!.startedAt) : null };
}

/** Whether two epoch-seconds timestamps fall on the same LOCAL calendar day —
 *  not just within 24 hours of each other, which a pair straddling midnight
 *  can satisfy while still being two different dates. */
function sameCalendarDay(a: number, b: number): boolean {
  const da = new Date(a * 1000);
  const db = new Date(b * 1000);
  return (
    da.getFullYear() === db.getFullYear() &&
    da.getMonth() === db.getMonth() &&
    da.getDate() === db.getDate()
  );
}

/** Truncate `text` to at most `maxLength` characters, eliding the MIDDLE
 *  rather than the tail — "whisper-lar…v3-mlx" instead of "whisper-large-v3…".
 *
 *  **Tail-ellipsis is wrong here specifically because model names that share
 *  a long common prefix differ at the END**: `whisper-large-v3-mlx` and
 *  `whisper-large-v3-turbo` are identical for their first 17 characters, so a
 *  trailing "…" throws away exactly the four-or-so characters that tell them
 *  apart, and a leaderboard whose rows read the same is a leaderboard that
 *  answers nothing. Keeping both ends costs the finer detail in the MIDDLE of
 *  a name instead, which is `-large-v3-` here — legible from context (the
 *  head names the family, the tail names the variant) in a way an eaten
 *  variant suffix is not.
 *
 *  A no-op when the text already fits, or when `maxLength` is too small to
 *  hold a head, a tail and the ellipsis character usefully (2 or fewer) —
 *  returning the untruncated text is a more honest failure than a string that
 *  is mostly ellipsis.
 */
export function middleEllipsis(text: string, maxLength: number): string {
  if (text.length <= maxLength || maxLength <= 2) return text;
  const keep = maxLength - 1; // one character spent on the ellipsis itself
  const head = Math.ceil(keep / 2);
  const tail = Math.floor(keep / 2);
  return text.slice(0, head) + "…" + text.slice(text.length - tail);
}

/** The page's reading order for a set of capabilities.
 *
 *  `CAPABILITY_ORDER` is imported from the Local tab's grouping rather than
 *  re-declared: the two tabs of one page must not disagree about where
 *  Embeddings goes. A capability neither list knows sorts after the known ones,
 *  in the order it arrived — the same rule `aiModelGroups.rank` follows, so a
 *  capability added server-side appears here instead of vanishing.
 */
/** Which of `all` a Run press can actually measure — `workloadCapabilities`
 *  is `AiBenchmarkHistory`'s own field, exactly `benchmark.WORKLOADS`' keys
 *  server side. Narrower than the registry's full capability list: video
 *  generation is the first capability the registry knows that has no fixed
 *  workload (`benchmark.NO_WORKLOAD_YET`) — a real one would mean a multi-GB,
 *  minutes-long render behind every press — so once any video model lands on
 *  disk it would otherwise join `all` (the union with `repos.map(...)`) and
 *  render a leaderboard section whose only Run outcome is a 400 toast.
 *
 *  Filtered against the SERVER's list rather than a capability named here by
 *  hand, so a future workload lights this section back up with no frontend
 *  edit — the same "server stays the source of truth" argument the video
 *  traits payload (`VideoStage.tsx`) already makes for a different gap.
 */
export function benchmarkableCapabilities(
  all: string[],
  workloadCapabilities: string[],
): string[] {
  const known = new Set(workloadCapabilities);
  return all.filter((capability) => known.has(capability));
}

export function orderCapabilities(capabilities: string[]): string[] {
  const rank = (key: string) => {
    const known = CAPABILITY_ORDER.indexOf(key);
    return known === -1 ? Number.MAX_SAFE_INTEGER : known;
  };
  return capabilities
    .map((capability, arrived) => ({ capability, arrived }))
    .sort((a, b) => rank(a.capability) - rank(b.capability) || a.arrived - b.arrived)
    .map((entry) => entry.capability);
}

/** Which capability currently has a benchmark in flight, and on which model.
 *
 *  Keyed by CAPABILITY because that is the unit the server serialises on: it
 *  holds one resident model per capability, so a second run on the same
 *  capability would evict the first's model mid-measurement (a 409), while a run
 *  on a different capability is explicitly permitted.
 */
export type RunsInFlight = Record<string, string>;

export interface RunButtonState {
  /** THIS model's run is the one in flight. */
  busy: boolean;
  /** The button cannot be pressed — because this model is running, or because
   *  another model of the SAME capability is. */
  blocked: boolean;
  label: string;
  title: string;
}

/** What one model's Run button says and whether it can be pressed.
 *
 *  **Scoped to the capability, and that is the whole point of the function.**
 *  The first cut held a single page-level "a benchmark is running" flag and
 *  passed it to every section, so starting a text benchmark greyed out image,
 *  speech and embeddings under the tooltip "Another benchmark is running for
 *  this capability" — a sentence that was false, over an action the server
 *  permits (`routers/ai_benchmark._claim` is per capability, pinned by
 *  `test_a_different_capability_may_run_alongside`). The UI was strictly more
 *  restrictive than the rule it was supposedly reflecting, which is the worst
 *  kind of guess: indistinguishable from a real constraint.
 *
 *  A pure function here rather than a ternary in the JSX for this file's
 *  standing reason — the sentence is where this can be wrong, and a screenshot
 *  of a greyed-out button does not reveal which rule greyed it.
 */
export function runButtonState(
  capability: string,
  model: string,
  inFlight: RunsInFlight,
  hasHistory: boolean = false,
): RunButtonState {
  const holder = inFlight[capability];
  const busy = holder === model;
  return {
    busy,
    blocked: holder !== undefined,
    label: busy ? "Running…" : hasHistory ? "Run again" : "Run benchmark",
    title: busy
      ? "This benchmark is running — it takes minutes"
      : holder !== undefined
        // Names the run that is blocking: the reader's next question is "by
        // what", and a button that cannot answer it reads as broken.
        ? `Waiting for the ${holder} benchmark to finish`
        : "Run the fixed workload for this capability",
  };
}

/** `mm:ss`, floored, never negative — the same shape `TranscribeStage.tsx`'s
 *  own `clock()` uses, so the app states elapsed time one way. Negative would
 *  only happen from a clock skew between "when the click fired" and "now",
 *  and floored to 0 rather than shown as a nonsense negative count. */
function benchClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** The busy row's own text: phase, then elapsed time — never an invented
 *  percentage (`styles/ai-models.css`'s own rule, and the busy row's previous
 *  static "this takes minutes" is exactly the silence that rule exists to
 *  replace).
 *
 *  **`stillLoading` is the AI runtime's OWN answer, not a guess.** `run()`
 *  opens its measurement row only once `_load_to_ready` returns
 *  (`ai/benchmark.py`), so the runtime's `loaded[]` entry for this model is
 *  the one honest signal for "which phase is this, right now" — a client-side
 *  timer cannot know when the load actually finished, and guessing from
 *  elapsed time alone would show "Measuring" for a cold model still pulling
 *  gigabytes. `useAiRuntime` is already polled by the page (`lib/aiRuntime.ts`)
 *  for exactly this table, so this reuses that poll rather than adding a
 *  second one.
 *
 *  Once loading ends, the label switches to a ticking elapsed clock from the
 *  moment the button was pressed (`startedAt`, `now` in epoch milliseconds) —
 *  real, measured time, matching the row `_MeasurementRow` now keeps open on
 *  the server for exactly this phase. */
export function busyRowText(
  stillLoading: boolean,
  startedAt: number,
  now: number,
): string {
  if (stillLoading) return "Loading weights into memory…";
  return `Measuring — ${benchClock((now - startedAt) / 1000)}`;
}

/** What to tell the user when a run came back stopped rather than measured.
 *
 *  **The cause could be the run's own row now, or something else entirely, and
 *  the note does not guess which.** A benchmark's own measurement row
 *  (`ai/benchmark.py`'s `_MeasurementRow`) offers a ✕ during the timed
 *  phase — but a run that ends `cancelled` could just as well have been
 *  stopped by something ELSE reaching the model it was using, since one
 *  resident model per capability, shared by the whole app, makes that
 *  possible too. `fused.ai.cancel()` from any open page does it; so does the
 *  ✕ on the model's own LOAD row while it was still coming up, and so does
 *  the ✕ on a queued-transcription row a speech benchmark can inherit. The
 *  client cannot tell those apart, so the wording names the SHAPE of the cause
 *  and offers the commonest one as an example rather than asserting it — an
 *  earlier draft said "most likely fused.ai.cancel()" and was simply wrong for
 *  the ✕ cases.
 *
 *  Without this the failure was silent in the worst way: nothing appended, no
 *  error set, the button quietly re-enabled — so somebody who pressed Run and
 *  waited several minutes was given no signal at all that their run had died,
 *  and would reasonably conclude the button was broken.
 *
 *  Deliberately NOT worded as a failure. A failed run is a fact about the model
 *  and is kept in the history; a stopped one is a fact about the app and is not,
 *  and "failed" would collapse the two — which is the whole distinction the
 *  server draws by answering with no `run` at all.
 */
export function stoppedNote(model: string): string {
  return (
    `The ${model} benchmark was stopped before it finished — the model it was ` +
    `using was cancelled elsewhere in the app (for example by ` +
    `fused.ai.cancel() on another page, or a ✕ on its row in the download ` +
    `manager). Nothing was recorded; press Run again to retry.`
  );
}

/** How far a bar reaches, 0..1, where 1 is the SECTION'S best model — never an
 *  absolute fraction of the raw number. `higherIsBetter` decides which
 *  direction "best" points: on a higher-is-better metric the winner has the
 *  biggest number, so `value / best` already lands the winner at 1; on
 *  seconds-per-step the winner has the SMALLEST number, so the ratio is
 *  inverted (`best / value`) — otherwise the fastest model in the section
 *  would draw the shortest bar, which is backwards for a leaderboard whose
 *  whole point is "longer bar, better model" on every section alike.
 *
 *  Guards a degenerate all-zero section (every model measured literally
 *  nothing) rather than dividing zero by zero into `NaN`, which SVG/CSS widths
 *  render as nothing sized rather than as an error. */
function barFraction(value: number, best: number, higherIsBetter: boolean): number {
  if (value <= 0 && best <= 0) return higherIsBetter ? 0 : 1;
  if (higherIsBetter) return best <= 0 ? 0 : Math.min(1, value / best);
  return value <= 0 ? 1 : Math.min(1, best / value);
}

export interface LeaderboardRow {
  model: string;
  /** The same `ModelLatest` the caller passed in — untouched, just reordered
   *  and given a bar. */
  row: ModelLatest | null;
  /** 0..1 against the section's best measured model, or null when there is
   *  nothing to bar: no run yet, or the latest run failed or measured
   *  nothing. A bar of length 0 would read as "measured, and terrible" —
   *  which is a different fact from "never run" or "broke". */
  barFraction: number | null;
}

/** One capability's models, ranked best-first for the leaderboard.
 *
 *  **Takes the metric DIRECTLY rather than a capability** — the leaderboard
 *  ranks by whatever the reader selected, memory or load time included, not
 *  always the capability's primary. A caller wanting the old default passes
 *  `primaryMetric(capability)`.
 *
 *  Three groups, in this fixed order — measured, then failed, then never
 *  benchmarked — because they are different KINDS of "nothing to compare",
 *  and a plain sort by value would either crash on the ones with no number or
 *  silently treat "no run yet" as tied with "measured zero". Within the
 *  measured group, order is by `metric` in whichever direction is actually
 *  better for it (see `barFraction`); the other two groups keep the input
 *  order, because there is no value to rank them by.
 *
 *  A null `metric` (an unknown capability, or nothing left to select from)
 *  draws no bars and ranks nothing — the same "no guessed number" posture
 *  `primaryMetric` and `chartSeries` already take.
 */
export function leaderboard(
  metric: MetricSpec | null,
  entries: { model: string; row: ModelLatest | null }[],
): LeaderboardRow[] {
  if (!metric) return entries.map((e) => ({ ...e, barFraction: null }));

  const measured: { entry: (typeof entries)[number]; value: number }[] = [];
  const failed: (typeof entries)[number][] = [];
  const never: (typeof entries)[number][] = [];
  for (const entry of entries) {
    if (!entry.row) {
      never.push(entry);
      continue;
    }
    const value = metricValueForSpec(entry.row.latest, metric);
    if (value === null) failed.push(entry);
    else measured.push({ entry, value });
  }

  measured.sort((a, b) =>
    metric.higherIsBetter ? b.value - a.value : a.value - b.value,
  );
  const best = measured.length > 0 ? measured[0]!.value : 0;

  return [
    ...measured.map(({ entry, value }) => ({
      ...entry,
      barFraction: barFraction(value, best, metric.higherIsBetter),
    })),
    ...failed.map((entry) => ({ ...entry, barFraction: null })),
    ...never.map((entry) => ({ ...entry, barFraction: null })),
  ];
}

/** How many recorded runs each capability has, across the WHOLE history — not
 *  just the runs for one section, since the selector needs every capability's
 *  count at once to pick a default and to print a count beside every option.
 */
export function runCountsByCapability(runs: AiBenchmarkRun[]): Record<string, number> {
  return countBy(runs, (run) => run.capability);
}

/** How many recorded runs each MODEL has, within whatever `runs` the caller
 *  already scoped to one capability — the model picker's own version of
 *  `runCountsByCapability`, and the same reason a section's `defaultModel`
 *  needs this rather than a page-wide count: a model's run history only means
 *  anything within the capability it was measured under. */
export function runCountsByModel(runs: AiBenchmarkRun[]): Record<string, number> {
  return countBy(runs, (run) => run.model);
}

function countBy(runs: AiBenchmarkRun[], keyOf: (run: AiBenchmarkRun) => string): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const run of runs) counts[keyOf(run)] = (counts[keyOf(run)] ?? 0) + 1;
  return counts;
}

/** The item in `order` with the most recorded runs, ties broken by `order`'s
 *  OWN position — scanning left to right and only replacing the leader on a
 *  STRICTLY greater count, so the earlier item wins a tie rather than the
 *  later one that happened to match it. Falls through to `order[0]` when
 *  nothing has ever run (every count 0), which is the same left-to-right scan
 *  keeping its initial pick.
 *
 *  Shared by `defaultCapability` (order = registry order) and `defaultModel`
 *  (order = the leaderboard's own rank, so a tie breaks toward whichever
 *  model is already reading as the better one) — one algorithm, so a fix to
 *  the tie-break rule cannot land in one and not the other. */
function mostRuns(order: string[], counts: Record<string, number>): string | null {
  if (order.length === 0) return null;
  let best = order[0]!;
  let bestCount = counts[best] ?? 0;
  for (const item of order.slice(1)) {
    const count = counts[item] ?? 0;
    if (count > bestCount) {
      best = item;
      bestCount = count;
    }
  }
  return best;
}

/** `param` when it names something in `order`, `mostRuns`'s pick otherwise —
 *  shared by `resolveCapability` and `resolveModel`. A `param` naming nothing
 *  in `order` is treated exactly like an ABSENT one: a stale link, a value
 *  meant for a different reading of this page, or a value this frontend has
 *  never heard of all fall through to the same default rather than rendering
 *  an empty selector on a param that travelled here from somewhere else —
 *  the same forgiving posture `orderCapabilities` and `routes.ts`'s
 *  `tabFromPath` already take toward an unrecognised value. */
function resolveFromRuns(order: string[], param: string | null, counts: Record<string, number>): string | null {
  if (param && order.includes(param)) return param;
  return mostRuns(order, counts);
}

/** Which capability the selector opens on, absent an explicit `?cap=`: the one
 *  with the most recorded runs, ties broken by `capabilities`' OWN order —
 *  which is registry order, since callers pass it through `orderCapabilities`
 *  first.
 */
export function defaultCapability(
  capabilities: string[],
  counts: Record<string, number>,
): string | null {
  return mostRuns(capabilities, counts);
}

/** The selector's actual state: the URL's `?cap=` when it names a real
 *  capability, `defaultCapability`'s pick otherwise. See `resolveFromRuns`. */
export function resolveCapability(
  capabilities: string[],
  param: string | null,
  counts: Record<string, number>,
): string | null {
  return resolveFromRuns(capabilities, param, counts);
}

/** Which model the per-model trend chart opens on, absent an explicit
 *  `?benchModel=`: the one with the most recorded runs IN THIS CAPABILITY,
 *  ties broken by `models`' own order — pass the leaderboard's own rank
 *  order so a tie breaks toward the model already reading as the better one,
 *  rather than an arbitrary list order. */
export function defaultModel(models: string[], counts: Record<string, number>): string | null {
  return mostRuns(models, counts);
}

/** The sentinel `resolveModel` reads as "the reader explicitly closed the
 *  open row" (D481's toggle) — carrying the CAPABILITY it was closed
 *  under, rather than a bare `""`. `BenchmarkTab`'s `modelParam` is one
 *  piece of state shared across every capability (only the LEADERBOARD it
 *  is resolved against changes when `?benchCap=` does), so a bare `""`
 *  written while closing a row under "text-generation" would still read as
 *  "closed" the instant the reader switched to "embeddings" — collapsing a
 *  capability that was never actually closed, rather than opening its own
 *  default model. Prefixed with a token no real `?benchModel=` value can
 *  ever collide with (a capability id, never a model id), so a sentinel
 *  closed under one capability fails the equality check under any OTHER
 *  capability and falls through to `resolveFromRuns`'s ordinary default —
 *  self-correcting with no separate "clear this on capability switch"
 *  effect required anywhere. */
export function closedModelSentinel(capability: string): string {
  return `__closed__${capability}`;
}

/** The trend chart's actual model: the URL's `?benchModel=` when it names a
 *  model in THIS capability's leaderboard, `defaultModel`'s pick otherwise.
 *  See `resolveFromRuns` — a model belonging to a DIFFERENT capability (the
 *  reader just switched `?cap=`) falls through to the default exactly like a
 *  stale or foreign value would.
 *
 *  **`param === closedModelSentinel(capability)` is a THIRD state, distinct
 *  from absent (`null`) — the reader explicitly closed the open row for
 *  THIS capability, and that must NOT fall through to `defaultModel`'s pick
 *  the way an absent or unrecognised param does.** `null` means "no opinion
 *  yet, pick the usual default"; the sentinel means "there WAS a pick FOR
 *  THIS CAPABILITY, and it was closed" — a plain landing on this tab (or a
 *  shared link with no `?benchModel=` at all) still opens the best-ranked
 *  row, exactly as before, but a reader who closed it and then reloads
 *  gets the closed state back rather than the row silently re-opening
 *  under them. A sentinel closed under a DIFFERENT capability (stale
 *  `modelParam` state a capability switch does not itself clear) is
 *  neither this capability's own sentinel NOR a real model id, so it falls
 *  through to the ordinary default exactly like any other foreign value —
 *  the cross-capability leak this signature change exists to close. */
export function resolveModel(
  models: string[],
  param: string | null,
  counts: Record<string, number>,
  capability: string | null,
): string | null {
  if (capability !== null && param === closedModelSentinel(capability)) return null;
  return resolveFromRuns(models, param, counts);
}

/** Whether a model's history for one metric is a TREND — a shape with a
 *  direction, worth the full `ModelTrendChart` — or something smaller.
 *
 *  `"none"` — nothing measured yet: no chart, no chart-shaped empty box
 *  either (an axis under no data reads as "measured zero", the same rule
 *  `chartSeries` already follows).
 *
 *  `"single"` — exactly ONE measured point. This is deliberately its own
 *  state rather than falling into the same bucket as "none" or being drawn
 *  as a one-point chart: a lone dot in an otherwise empty ~400px frame was
 *  the actual bug report (`whisper-tiny.en-8bit`, one run, 95% dead space,
 *  the largest thing on the page) — a single measurement has no BEFORE to
 *  compare against, so there is no direction to plot, and the honest answer
 *  is the value itself stated plainly, at a fraction of the height, with an
 *  invitation to run it again.
 *
 *  `"trend"` from two points on — the smallest count two points *are* a
 *  direction, even a short one.
 */
export type TrendKind = "none" | "single" | "trend";

/** The point count boundary `trendKind` applies. Its own named constant
 *  rather than a bare `2` inside the function, so a reader (or a future
 *  change) sees the THRESHOLD as a decision, not an incidental comparison. */
export const MIN_TREND_POINTS = 2;

export function trendKind(pointCount: number): TrendKind {
  if (pointCount <= 0) return "none";
  if (pointCount < MIN_TREND_POINTS) return "single";
  return "trend";
}

/** The y-axis DOMAIN TOP for a chart whose highest measured value is `peak`
 *  — deliberately never `peak` itself. `yAxisTicks` divides whatever domain
 *  it is given into equal gridlines ending exactly at the top, so handing it
 *  the raw peak pins the best point to the very top gridline — which reads
 *  as the value being clipped against the frame's edge, not as the best
 *  result on the chart (859.7 sitting exactly on the top line was the bug).
 *
 *  20% headroom is a deliberate, fixed choice — not derived from `digits` or
 *  the metric's unit — chosen for the same reason `chartSeries`' zero
 *  baseline is unconditional: a rule that varies per metric is a rule nobody
 *  can hold in their head while comparing two charts.
 */
export function paddedAxisMax(peak: number): number {
  return peak > 0 ? peak * 1.2 : 0;
}

/** The "loose" round-number ladder a nice axis step is chosen from — the same
 *  set most charting libraries use (D3's `nice()` among them): steps of 1, 2
 *  or 5 read as round at a glance, and 2.5 is what keeps a domain like 1000
 *  from being forced into a step of either 200 (five ticks too many) or 500
 *  (only two ticks) — 250 is the one that actually lands on 0/250/500/750/1000.
 */
const NICE_STEP_MULTIPLES = [1, 2, 2.5, 5, 10];

/** The smallest value in `NICE_STEP_MULTIPLES`'s ladder, scaled to `rawStep`'s
 *  own magnitude, that is still `>= rawStep` — so `count` of them never
 *  undershoots the peak they are meant to cover. `0` over a non-positive
 *  input, which every caller already guards before multiplying by it. */
function niceStep(rawStep: number): number {
  if (rawStep <= 0) return 0;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const residual = rawStep / magnitude;
  const multiple = NICE_STEP_MULTIPLES.find((m) => residual <= m + 1e-9) ?? 10;
  return multiple * magnitude;
}

/** The byte ladder a byte-valued metric's NICE STEP is chosen WITHIN — the
 *  same KB/MB/GB/TB rungs `formatSize` (every row's own memory figure) would
 *  pick for a value near `peak`. Stepping directly in raw bytes produces a
 *  step that is round in bytes but ugly once formatted (a "nice" 250 million
 *  byte step reads as "238 MB", not "250 MB") — this is the one place that
 *  has to know the ladder exists, so every other metric (already stored in
 *  its own display unit) needs no equivalent. */
function byteStepDivisor(peak: number): number {
  const KB = 1024;
  const MB = KB * 1024;
  const GB = MB * 1024;
  const TB = GB * 1024;
  if (peak >= TB) return TB;
  if (peak >= GB) return GB;
  if (peak >= MB) return MB;
  if (peak >= KB) return KB;
  return 1;
}

/** The same idea as `byteStepDivisor`, for a seconds-valued metric: below one
 *  second, a nice STEP has to be a nice round number of MILLISECONDS
 *  (`formatDuration`'s own threshold) — stepping in raw seconds on a
 *  0.0349-second domain produces a step so small `niceStep`'s own magnitude
 *  math degenerates, and every resulting tick still formats as "0 ms". At or
 *  above one second the metric's stored unit already IS the display unit, so
 *  no scaling is needed. */
function secondsStepDivisor(peak: number): number {
  return peak < 1 ? 0.001 : 1;
}

/** The COMPARISON chart's own axis ticks: ROUND numbers derived from `peak`'s
 *  magnitude — 0/250/500/750/1000 style — never an even division of the raw
 *  peak (`yAxisTicks`'s job, for a chart that DOES need headroom past its
 *  peak — a line chart's, not a bar chart's; see `ComparisonChart.tsx`'s own
 *  comment on why a bar reaching the end of its axis is exactly how "this is
 *  the maximum" should read, not a defect to pad away).
 *
 *  The reported bug: 859.7 divided evenly into 4 lands on 214.9 / 429.9 /
 *  644.8 / 859.7 — a padded top on top of THAT (`paddedAxisMax`, the old
 *  code path) made it worse, landing on 343.9 / 687.7 / 1031.6. None of those
 *  is a number a reader would ever choose for a scale. `niceStep` is what
 *  fixes it: the smallest round step (from `NICE_STEP_MULTIPLES`'s ladder)
 *  that still covers the peak in `count` steps, so the axis top is always
 *  `>= peak` without ever being a padded fraction of it.
 *
 *  `metric` decides the unit the rounding happens WITHIN — a byte-valued
 *  metric rounds in KB/MB/GB/TB (`byteStepDivisor`) and a seconds-valued one
 *  rounds in ms below one second (`secondsStepDivisor`), each matching what
 *  its own formatter (`formatSize`, `formatDuration`) would print for a value
 *  near the peak, so "round" and "reads clean once formatted" are the same
 *  claim; every other metric rounds directly in its own stored unit, since
 *  that unit IS what a tick already reads in. Every label goes through
 *  `axisTickLabel`, the same formatter `yAxisTicks` uses — one function, so a
 *  metric with either kind of auto-scaling unit added later gets the fix for
 *  free rather than needing its own axis to remember it.
 */
export function niceAxisTicks(peak: number, metric: MetricSpec, count = 4): AxisTick[] {
  if (peak <= 0) return [];
  const divisor =
    metric.key === "peakResidentBytes"
      ? byteStepDivisor(peak)
      : metric.unit === "s"
        ? secondsStepDivisor(peak)
        : 1;
  const step = niceStep(peak / divisor / count) * divisor;
  if (step <= 0) return [];
  const ticks: AxisTick[] = [];
  for (let i = 0; i <= count; i++) {
    const value = step * i;
    ticks.push({ value, label: axisTickLabel(value, metric) });
  }
  return ticks;
}

/** The top of `niceAxisTicks`' own domain — always `>= peak` (never a padded
 *  FRACTION of it, unlike `paddedAxisMax`; a bar chart has no headroom
 *  problem to pad away). Just the last tick `niceAxisTicks` would draw,
 *  pulled out on its own because `ComparisonChart` needs the number to scale
 *  every bar's own `width: N%` against, separately from the tick labels
 *  themselves. */
export function niceAxisMax(peak: number, metric: MetricSpec, count = 4): number {
  const ticks = niceAxisTicks(peak, metric, count);
  return ticks.length > 0 ? ticks[ticks.length - 1]!.value : 0;
}

export interface ComparisonBar {
  model: string;
  /** The REAL measured value, not a normalised 0..1 fraction — this feeds a
   *  chart with an honest axis in the metric's own unit, where `leaderboard`'s
   *  `barFraction` (built for a compact inline bar with no axis to be honest
   *  about) would draw a "goodness" score that does not correspond to any
   *  gridline. */
  value: number;
}

/** Every model worth a bar in the COMPARISON chart, in the leaderboard's own
 *  best-first order — direction included, so a lower-is-better metric's
 *  winner (the smallest number) leads the list.
 *
 *  **Bar length is meant to be `value` itself, scaled linearly against a
 *  shared axis — never `barFraction`.** For a higher-is-better metric that
 *  already makes the winner's bar the longest, because winning means the
 *  biggest number. For a lower-is-better one it makes the winner's bar the
 *  SHORTEST, because winning means the smallest number — which is correct,
 *  not an inversion to fix: "shortest bar is the best model" is what a
 *  reader expects from a duration or a memory figure once the ranking itself
 *  (best-first, top to bottom) already says which end is which. Multiplying
 *  by an inverted `barFraction` here would draw a value that does not match
 *  any real gridline, which is dishonest in exactly the way a proper axis
 *  exists to prevent.
 *
 *  **Failed and never-benchmarked models are excluded — filtered by
 *  `barFraction === null`, `leaderboard`'s own signal for "nothing to bar"**,
 *  reused here rather than re-deriving "was this measured" a second way. They
 *  stay visible in the leaderboard ROWS (BenchmarkTab.tsx), which is where an
 *  action — Run, Details — belongs; a chart has no row for either and would
 *  either invent a value or draw a hole, and this function draws neither.
 */
export function comparisonBars(
  ranked: LeaderboardRow[],
  metric: MetricSpec | null,
): ComparisonBar[] {
  if (!metric) return [];
  const bars: ComparisonBar[] = [];
  for (const entry of ranked) {
    if (entry.barFraction === null || !entry.row) continue;
    const value = metricValueForSpec(entry.row.latest, metric);
    // Defensive rather than load-bearing: `barFraction !== null` already
    // means `leaderboard` found a measured value here, so this should never
    // actually be null — but a bar chart is exactly the place a fabricated
    // zero would be most visible, so the check stays rather than trusting a
    // sibling function's invariant silently.
    if (value === null) continue;
    bars.push({ model: entry.model, value });
  }
  return bars;
}


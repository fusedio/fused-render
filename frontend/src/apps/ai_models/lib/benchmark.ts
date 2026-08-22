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
import type { AiBenchmarkMetrics, AiBenchmarkRun } from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";
import { CAPABILITY_ORDER } from "@apps/ai_models/lib/aiModelGroups";

/** What an unmeasured number renders as. One constant, so a table cell, a
 *  summary line and a chart caption cannot each pick a different dash. */
export const DASH = "—";

export interface PrimaryMetric {
  /** The key in `run.metrics` this capability is compared on. */
  key: keyof AiBenchmarkMetrics;
  /** For a column heading or a chart's y axis. */
  label: string;
  unit: string;
  /** Whether a bigger number is a faster model. False for seconds per step. */
  higherIsBetter: boolean;
  /** Decimal places. Rates get one; a ratio people read as "40× faster than
   *  listening to it" gets one too, and nothing here gets more — a benchmark
   *  repeated twice does not agree to three. */
  digits: number;
}

/** One primary metric per capability — the number the section's chart plots and
 *  the row leads with.
 *
 *  Declared here rather than inferred from which keys a run happens to carry:
 *  every capability reports several metrics, so "the first non-null one" would
 *  silently change what a chart means the day a runner starts reporting one
 *  more. Keyed by the server's capability constants (ai/registry.py).
 */
const PRIMARY: Record<string, PrimaryMetric> = {
  "text-generation": {
    key: "tokensPerSecond",
    label: "Throughput",
    unit: "tok/s",
    higherIsBetter: true,
    digits: 1,
  },
  "text-to-image": {
    // NOT total seconds: the step count is per-model by design (a distilled
    // model runs at 4 where another needs 28), so the per-step figure is the
    // only comparable one. See ai/benchmark.py.
    key: "secondsPerStep",
    label: "Per step",
    unit: "s/step",
    higherIsBetter: false,
    digits: 2,
  },
  "automatic-speech-recognition": {
    key: "realtimeFactor",
    label: "Speed",
    unit: "× realtime",
    higherIsBetter: true,
    digits: 1,
  },
  "embeddings": {
    key: "textsPerSecond",
    label: "Throughput",
    unit: "texts/s",
    higherIsBetter: true,
    digits: 1,
  },
};

/** The primary metric for a capability, or null when this frontend does not
 *  know the capability.
 *
 *  Null rather than a fallback: a capability added server-side should render as
 *  a section with a run table and no chart, which is honest, rather than as a
 *  chart of whichever number happened to be first.
 */
export function primaryMetric(capability: string): PrimaryMetric | null {
  return PRIMARY[capability] ?? null;
}

/** A number, or null when it was not measured. `undefined` (the key absent for
 *  this capability) and `null` (the runner did not report it) collapse to the
 *  same answer, because both mean "no measurement" to a reader. */
function metricValue(run: AiBenchmarkRun, key: keyof AiBenchmarkMetrics): number | null {
  const value = run.metrics[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** The run's primary metric, or null. Null for a failed run (no metrics at
 *  all), for an unknown capability, and for a runner that did not report it. */
export function primaryValue(run: AiBenchmarkRun): number | null {
  const metric = primaryMetric(run.capability);
  return metric ? metricValue(run, metric.key) : null;
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
  return value === null ? DASH : `${formatNumber(value, digits)} ${unit}`;
}

/** The primary metric as the page shows it — "42.1 tok/s", or a dash. */
export function formatPrimary(run: AiBenchmarkRun): string {
  const metric = primaryMetric(run.capability);
  if (!metric) return DASH;
  return withUnit(metricValue(run, metric.key), metric.unit, metric.digits);
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
 */
export function rowHeadline(run: AiBenchmarkRun): string {
  if (!run.ok) return "Failed";
  const parts: string[] = [formatPrimary(run)];
  if (run.peakResidentBytes !== null) parts.push(formatMemory(run));
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
 */
export function rowDetail(run: AiBenchmarkRun): string | null {
  if (!run.ok) return null;
  const parts: string[] = [];
  const ttft = metricValue(run, "ttftMs");
  if (ttft !== null) parts.push(`TTFT ${formatNumber(ttft, 0)} ms`);
  const steps = metricValue(run, "steps");
  if (steps !== null) parts.push(`${formatNumber(steps, 0)} steps`);
  if (run.loadSeconds !== null) parts.push(`loaded in ${formatLoad(run)}`);
  if (run.device) parts.push(run.device);
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

/** Runs for one capability, oldest first. Order is the store's own append
 *  order re-established by `startedAt`, because a caller may have concatenated
 *  a fresh run onto a fetched list. */
export function runsFor(runs: AiBenchmarkRun[], capability: string): AiBenchmarkRun[] {
  return runs.filter((r) => r.capability === capability).sort((a, b) => a.startedAt - b.startedAt);
}

/** The newest run per model, with the delta against the last COMPARABLE one.
 *
 *  Comparable means: the same model, an earlier run, the same workload
 *  revision, and a primary metric that was actually measured. Each of those
 *  drops a specific wrong answer —
 *
 *  * a different revision measured different work, so the difference is not the
 *    model's (this is the seam `revision` exists for);
 *  * a failed or unmeasured run has no number, and treating its absence as a
 *    zero would report a 100% improvement out of nowhere.
 *
 *  Models come back in first-appearance order, which is the order the caller's
 *  list already has them in — the section's own model list decides layout, not
 *  this function.
 */
export function latestByModel(runs: AiBenchmarkRun[]): ModelLatest[] {
  const byModel = new Map<string, AiBenchmarkRun[]>();
  for (const run of [...runs].sort((a, b) => a.startedAt - b.startedAt)) {
    const list = byModel.get(run.model);
    if (list) list.push(run);
    else byModel.set(run.model, [run]);
  }
  const rows: ModelLatest[] = [];
  for (const [model, history] of byModel) {
    const latest = history[history.length - 1]!;
    const value = primaryValue(latest);
    let delta: BenchmarkDelta | null = null;
    if (value !== null) {
      for (let i = history.length - 2; i >= 0; i--) {
        const candidate = history[i]!;
        if (candidate.workload.revision !== latest.workload.revision) continue;
        const before = primaryValue(candidate);
        if (before === null || before === 0) continue;
        const percent = ((value - before) / before) * 100;
        const higherIsBetter = primaryMetric(latest.capability)?.higherIsBetter ?? true;
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

/** One polyline per model over one capability's runs, plus the y domain.
 *
 *  A run with no primary metric contributes no point: the alternative is
 *  plotting a zero, which on a throughput chart is a visible, believable claim
 *  that the model produced nothing.
 *
 *  The domain starts at **zero** rather than at the smallest value. A rate axis
 *  cropped to its own range turns a 3% difference between two models into a
 *  chart where one is twice the other — the classic misleading axis, and this
 *  chart's entire job is comparison.
 */
export function chartSeries(runs: AiBenchmarkRun[]): {
  series: Series[];
  yMin: number;
  yMax: number;
} {
  const ordered = [...runs].sort((a, b) => a.startedAt - b.startedAt);
  const series: Series[] = [];
  const index = new Map<string, Series>();
  let yMax = 0;
  ordered.forEach((run, x) => {
    const y = primaryValue(run);
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

/** `count + 1` evenly spaced gridlines from 0 to `yMax`, the chart's own
 *  domain — not a "nice round number" scale that would need to EXTEND the
 *  domain past the tallest point to land on one. Equal division always ends
 *  exactly on `yMax`, so the top gridline IS the peak, and the chart does not
 *  need a separate "peak N unit" caption to say so.
 *
 *  Empty over an empty domain: a scale with a top of zero is not a scale for
 *  anything, and the chart already draws nothing in that case (see
 *  `BenchmarkChart`).
 */
export function yAxisTicks(yMax: number, digits: number, count = 4): AxisTick[] {
  if (yMax <= 0) return [];
  const ticks: AxisTick[] = [];
  for (let i = 0; i <= count; i++) {
    const value = (yMax * i) / count;
    ticks.push({ value, label: formatNumber(value, digits) });
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

/** The page's reading order for a set of capabilities.
 *
 *  `CAPABILITY_ORDER` is imported from the Local tab's grouping rather than
 *  re-declared: the two tabs of one page must not disagree about where
 *  Embeddings goes. A capability neither list knows sorts after the known ones,
 *  in the order it arrived — the same rule `aiModelGroups.rank` follows, so a
 *  capability added server-side appears here instead of vanishing.
 */
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

/** What to tell the user when a run came back stopped rather than measured.
 *
 *  **The cause is elsewhere in the app, and the note does not guess which.** A
 *  benchmark owns no download-manager row and offers no ✕ of its own (see
 *  `ai/benchmark.py`), so a run that ends `cancelled` was stopped by something
 *  reaching the model it was using — which one resident model per capability,
 *  shared by the whole app, makes possible. `fused.ai.cancel()` from any open
 *  page does it; so does the ✕ on a download row the load opened, and so does
 *  the ✕ on a queued-transcription row a speech benchmark can inherit. The
 *  client cannot tell those apart, so the wording names the SHAPE of the cause
 *  and offers the commonest one as an example rather than asserting it — an
 *  earlier draft said "most likely fused.ai.cancel()" and was simply wrong for
 *  the two ✕ cases.
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
 *  Three groups, in this fixed order — measured, then failed, then never
 *  benchmarked — because they are different KINDS of "nothing to compare",
 *  and a plain sort by value would either crash on the ones with no number or
 *  silently treat "no run yet" as tied with "measured zero". Within the
 *  measured group, order is by the primary metric in whichever direction is
 *  actually better (see `barFraction`); the other two groups keep the input
 *  order, because there is no value to rank them by.
 *
 *  An unknown capability (`primaryMetric` returns null) draws no bars and
 *  ranks nothing — the same "no guessed number" posture `primaryMetric` and
 *  `chartSeries` already take.
 */
export function leaderboard(
  capability: string,
  entries: { model: string; row: ModelLatest | null }[],
): LeaderboardRow[] {
  const metric = primaryMetric(capability);
  if (!metric) return entries.map((e) => ({ ...e, barFraction: null }));

  const measured: { entry: (typeof entries)[number]; value: number }[] = [];
  const failed: (typeof entries)[number][] = [];
  const never: (typeof entries)[number][] = [];
  for (const entry of entries) {
    if (!entry.row) {
      never.push(entry);
      continue;
    }
    const value = primaryValue(entry.row.latest);
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

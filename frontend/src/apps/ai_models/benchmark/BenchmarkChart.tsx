// One capability's history as a line per model — inline SVG, no charting
// dependency (SPEC AI-14). This is the tab's HERO instrument: the page exists
// to answer "which model is fastest here, and is it trending up or down", and
// this is the only place that trend is drawn, so it gets real gridlines, real
// dates, and a value labelled on every line rather than a bare polyline.
//
// Self-contained sizing, like UsageTab's own chart: the SVG carries a
// `viewBox` and scales to whatever box CSS gives it, so nothing here measures
// the DOM, nothing re-renders on resize. Every label — y-axis ticks, x-axis
// dates, the per-series end labels — is an ordinary HTML text node positioned
// by PERCENTAGE of the plot's own box, never `<text>` inside the SVG: the
// viewBox stretches horizontally (`preserveAspectRatio="none"`) to fill
// whatever width CSS gives the chart, and a glyph drawn inside that stretch
// distorts with it, where a percentage of the surrounding box's real pixel
// width does not.
//
// **Every number it draws comes from `lib/benchmark.ts`.** This file decides
// nothing about which metric matters, which way is better, or what to leave
// out; it turns points into a path and ticks into positioned labels. That is
// what keeps the one place a benchmark can be misread — the metric and the
// domain — under test in `benchmark.test.ts` rather than inside a component.
//
// **The y axis starts at zero, always** (`yMin` from `chartSeries` is 0), and
// its top gridline IS the peak (`yAxisTicks` divides the domain equally, so
// the last tick lands exactly on `yMax`) — which is why there is no separate
// "peak N unit" caption any more: the axis already says it.
import { chartSeries, formatNumber, formatRunDate, primaryMetric, shortModelName, yAxisTicks } from "@apps/ai_models/lib/benchmark";
import type { AiBenchmarkRun } from "@platform/lib/api";

// The plot's own coordinate space. Taller than the old 160px box — this chart
// is now the section's centerpiece rather than a footnote under the model
// list, and a "generous fixed height" instrument reads as one at a glance.
const W = 600;
const H = 220;
const PAD = 6;

/** Up to this many distinct line colours before models start sharing one.
 *  Sharing is acceptable — every line carries its own end label, and a chart
 *  with fourteen unique hues is one nobody could tell apart by colour anyway. */
const COLOURS = 6;

export function BenchmarkChart({
  capability,
  runs,
}: {
  capability: string;
  runs: AiBenchmarkRun[];
}) {
  const metric = primaryMetric(capability);
  const { series, yMax } = chartSeries(runs);
  // Nothing plottable. Said in words rather than drawn as an empty box with
  // axes: an axis under no data reads as "zero throughput", which is a
  // measurement, and there has not been one.
  if (!metric || series.length === 0 || yMax <= 0) return null;

  // The x domain is the count of runs in the whole capability, since a point's
  // `x` is its position there — that is what puts two models' runs on one shared
  // timeline instead of each restarting at the left edge.
  const xMax = Math.max(1, runs.length - 1);
  const px = (x: number) => PAD + (x / xMax) * (W - 2 * PAD);
  // Inverted, because SVG y grows downward and the reader expects faster to be
  // higher — even on the one capability where "faster" is a smaller number, in
  // which case a line going UP is a slower model and the section's unit label
  // ("s/step") is what says so.
  const py = (y: number) => H - PAD - (y / yMax) * (H - 2 * PAD);
  // Percentage-of-box versions of the same two functions, for the HTML labels
  // laid over the plot — see the file header for why these can't be `<text>`.
  const pxPct = (x: number) => (px(x) / W) * 100;
  const pyPct = (y: number) => (py(y) / H) * 100;

  const ticks = yAxisTicks(yMax, metric.digits, 3);

  // Three date ticks at most — first run, the middle one, last run — never a
  // claim of even spacing the runs don't have (see the file header on why the
  // old axis said only "oldest"/"newest"). Two runs get just the ends; one run
  // gets its own single date rather than repeating it.
  const dateTicks: { x: number; label: string }[] =
    runs.length === 1
      ? [{ x: 0, label: formatRunDate(runs[0]!.startedAt) }]
      : [
          { x: 0, label: formatRunDate(runs[0]!.startedAt) },
          ...(runs.length > 2
            ? (() => {
                const mid = Math.floor((runs.length - 1) / 2);
                return [{ x: mid, label: formatRunDate(runs[mid]!.startedAt) }];
              })()
            : []),
          { x: runs.length - 1, label: formatRunDate(runs[runs.length - 1]!.startedAt) },
        ];

  return (
    <div className="am-bench-chart">
      <div className="am-bench-chartbody">
        {/* The y axis: a fixed-width column of tick labels, top to bottom,
            positioned by the SAME percentage math as the gridlines they sit
            beside — so a label and its line never drift apart. */}
        <div className="am-bench-yaxis" aria-hidden="true">
          {[...ticks].reverse().map((tick) => (
            <span key={tick.value} className="am-bench-ytick" style={{ top: `${pyPct(tick.value)}%` }}>
              {tick.label}
            </span>
          ))}
        </div>
        <div className="am-bench-plotwrap">
          <svg
            viewBox={`0 0 ${W} ${H}`}
            preserveAspectRatio="none"
            className="am-bench-plot"
            role="img"
            aria-label={`${metric.label} across ${runs.length} ${capability} benchmark runs, peak ${yMax.toFixed(metric.digits)} ${metric.unit}`}
          >
            {/* Gridlines for every tick, including the zero baseline — one
                rule rather than a special-cased "axis line" plus separate
                ticks, so the bottom of the domain is drawn the same way as
                every other step in it. */}
            {ticks.map((tick) => (
              <line
                key={tick.value}
                className={tick.value === 0 ? "am-bench-axis-line" : "am-bench-gridline"}
                x1={PAD}
                y1={py(tick.value)}
                x2={W - PAD}
                y2={py(tick.value)}
              />
            ))}
            {series.map((line, i) => (
              <g key={line.model} className={`am-bench-series c${i % COLOURS}`}>
                <polyline
                  className="am-bench-line"
                  points={line.points.map((p) => `${px(p.x)},${py(p.y)}`).join(" ")}
                />
                {/* A dot per run as well as the line: a model with exactly one
                    recorded run has no line to draw at all, and a series that
                    renders as nothing would read as a model that was never
                    benchmarked. Its end label (below) is what turns that lone
                    dot into a readable measurement rather than a floating
                    mark. */}
                {line.points.map((p) => (
                  <circle
                    key={p.run.id}
                    className="am-bench-dot"
                    cx={px(p.x)}
                    cy={py(p.y)}
                    r={3}
                  />
                ))}
              </g>
            ))}
          </svg>
          {/* One label per series, on its OWN latest point — not necessarily
              the newest run in the whole capability, since a model that
              hasn't been re-run in a while still ends its own line where it
              last measured. HTML, not SVG `<text>`, and positioned by the same
              percentage math as everything else here, for the reason in the
              file header. */}
          {series.map((line, i) => {
            const last = line.points[line.points.length - 1]!;
            return (
              <span
                key={line.model}
                className={`am-bench-endlabel c${i % COLOURS}`}
                style={{ left: `${pxPct(last.x)}%`, top: `${pyPct(last.y)}%` }}
                title={line.model}
              >
                {shortModelName(line.model)} {formatNumber(last.y, metric.digits)}
              </span>
            );
          })}
        </div>
      </div>
      <div className="am-bench-axis">
        {dateTicks.map((tick) => (
          <span key={tick.x}>{tick.label}</span>
        ))}
      </div>
    </div>
  );
}

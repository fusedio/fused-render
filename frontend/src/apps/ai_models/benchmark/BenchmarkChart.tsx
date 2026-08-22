// One capability's history as a line per model — inline SVG, no charting
// dependency (SPEC AI-14).
//
// Self-contained sizing, like UsageTab's own chart: the SVG carries a
// `viewBox` and scales to whatever box CSS gives it, so nothing here measures
// the DOM, nothing re-renders on resize, and the axis labels are ordinary text
// nodes in a row beneath the plot rather than `<text>` inside it — text laid out
// by the browser stays legible at any width, where `<text>` in a scaled viewBox
// stretches with the picture.
//
// **Every number it draws comes from `lib/benchmark.chartSeries`.** This file
// decides nothing about which metric matters, which way is better, or what to
// leave out; it turns points into a path. That is what keeps the one place a
// benchmark can be misread — the metric and the domain — under test in
// `benchmark.test.ts` rather than inside a component.
//
// **The y axis starts at zero, always** (`yMin` from `chartSeries` is 0). A rate
// axis cropped to its own range turns a 3% gap between two models into a chart
// where one line is twice the height of the other, and comparison is this
// chart's entire job.
import { chartSeries, primaryMetric } from "@apps/ai_models/lib/benchmark";
import type { AiBenchmarkRun } from "@platform/lib/api";

// The plot's own coordinate space. Wide and short because the x axis is "runs,
// in the order they were taken" — usually a handful — and the reader is
// comparing HEIGHTS between two lines, which a tall narrow box exaggerates.
const W = 600;
const H = 160;
const PAD = 6;

/** Up to this many distinct line colours before models start sharing one.
 *  Sharing is acceptable — the legend names every line, and a chart with
 *  fourteen unique hues is a chart nobody can match to a legend anyway. */
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

  return (
    <div className="am-bench-chart">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="am-bench-plot"
        role="img"
        aria-label={`${metric.label} across ${runs.length} ${capability} benchmark runs, peak ${yMax.toFixed(metric.digits)} ${metric.unit}`}
      >
        {/* The zero baseline, drawn so the domain's promise is visible: a line
            at the bottom of the box is what makes "this axis starts at zero"
            something the reader can see rather than something the code knows. */}
        <line className="am-bench-axis-line" x1={PAD} y1={py(0)} x2={W - PAD} y2={py(0)} />
        {series.map((line, i) => (
          <g key={line.model} className={`am-bench-series c${i % COLOURS}`}>
            <polyline
              className="am-bench-line"
              points={line.points.map((p) => `${px(p.x)},${py(p.y)}`).join(" ")}
            />
            {/* A dot per run as well as the line: a model with exactly one
                recorded run has no line to draw at all, and a series that
                renders as nothing would read as a model that was never
                benchmarked. */}
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
      <div className="am-bench-axis">
        {/* Oldest → newest, in words. No dates: the runs are not evenly spaced
            in TIME (two runs a minute apart and one a month later), so a time
            axis would be a lie about the spacing — the table below carries each
            run's date, which is where a reader who wants one should look. */}
        <span>oldest</span>
        <span className="am-bench-peak">
          peak {Number(yMax.toFixed(metric.digits))} {metric.unit}
        </span>
        <span>newest</span>
      </div>
      <div className="am-bench-legend">
        {series.map((line, i) => (
          <span key={line.model} className={`am-bench-key c${i % COLOURS}`}>
            <span className="am-bench-swatch" />
            <span className="cc-mono">{line.model}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

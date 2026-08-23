// The COMPARISON instrument: one horizontal bar per model that has actually
// been measured for the selected capability + metric, ranked best-first
// (SPEC AI-14). This is the tab's hero — it answers the question a reader
// arrives with ("which of these is fastest here"), and unlike the per-model
// trend chart (`ModelTrendChart.tsx`, which needs two runs of the SAME
// model) it renders whenever more than one model has been benchmarked at
// all, which is the normal case: real usage spreads a handful of runs
// ACROSS several different models far more often than it re-runs one model
// twice.
//
// **No SVG here, on purpose — plain CSS flex rows.** A vertical bar chart
// (`ModelTrendChart`) needs a stretched `viewBox` to fill an arbitrary width
// without measuring the DOM, which is exactly what forces every label in
// that file out of `<text>` and into percentage-positioned HTML. A
// HORIZONTAL bar's length is a plain CSS `width: N%` on a flex child, and the
// value label that follows it is simply the next flex sibling — no
// percentage-position math, no stretched-viewBox distortion to work around,
// because there is no SVG to distort. Reach for the SVG idiom only where the
// shape genuinely needs it (a line chart's diagonal, a dot at an arbitrary
// (x, y)); a bar chart does not.
//
// **Bar length is `value` itself, scaled linearly — never `leaderboard`'s
// `barFraction`.** `comparisonBars` (lib/benchmark.ts) explains why in full;
// in short, `barFraction` is a normalised 0..1 "goodness" score built for a
// compact inline bar with no axis to answer to, and plotting it against a
// REAL, gridlined axis here would draw a length that matches no gridline —
// dishonest in exactly the way an axis exists to prevent. Direct scaling
// already produces "shortest bar wins" for a lower-is-better metric (the
// winner's raw number is the smallest one), which is correct, not something
// to invert.
import { formatMetricSpecValue, middleEllipsis, niceAxisTicks, shortModelName, type ComparisonBar, type MetricSpec } from "@apps/ai_models/lib/benchmark";

export function ComparisonChart({
  bars,
  metric,
}: {
  /** Already filtered and ordered by `comparisonBars` — every entry here
   *  gets a row; this component draws no fallback text for the models it
   *  left out (failed, never benchmarked), because that is the LEADERBOARD
   *  rows' job (BenchmarkTab.tsx), not this chart's. */
  bars: ComparisonBar[];
  metric: MetricSpec;
}) {
  // Nothing plottable. Said in words by the CALLER (BenchmarkTab.tsx knows
  // whether that means "nothing benchmarked yet" or "only one model has"),
  // not here — the same "draws nothing rather than a lying empty axis" rule
  // `ModelTrendChart` follows.
  if (bars.length === 0) return null;

  let peak = 0;
  for (const bar of bars) if (bar.value > peak) peak = bar.value;
  // **No padding here, on purpose — this is a BAR chart, not a line chart.**
  // `ModelTrendChart` pads its domain because a line's topmost POINT must not
  // sit pinned to the frame's own edge, which reads as clipped. A bar has no
  // such problem: a bar reaching the end of the axis IS exactly how "this is
  // the maximum" should read. `niceAxisTicks` picks the axis's own top —
  // always `>= peak`, in ROUND numbers derived from its magnitude
  // (0/250/500/750/1000 style), never an even division of the raw peak
  // (859.7 divided into 3 used to land on 343.9/687.7/1031.6 — not a number
  // anyone would choose for a scale) — see its own comment in lib/benchmark.ts.
  const ticks = niceAxisTicks(peak, metric, 4);
  if (ticks.length === 0) return null;
  const axisMax = ticks[ticks.length - 1]!.value;
  const pct = (value: number) => (value / axisMax) * 100;

  return (
    <div
      className="am-bench-compare"
      role="img"
      aria-label={`${metric.label} across ${bars.length} benchmarked models, ranked best to worst, peak ${formatMetricSpecValue(peak, metric)}`}
    >
      <div className="am-bench-compare-body">
        {/* The name column and the empty spacer in the axis row below share
            ONE class (`am-bench-compare-namecol`) for their width, so the
            plot area and the tick row it explains can never drift out of
            alignment from editing one without the other. */}
        <div className="am-bench-compare-names am-bench-compare-namecol">
          {bars.map((bar) => (
            <div key={bar.model} className="am-bench-compare-name cc-mono" title={bar.model}>
              {middleEllipsis(shortModelName(bar.model), 26)}
            </div>
          ))}
        </div>
        <div className="am-bench-compare-plot">
          {/* Gridlines, including the zero baseline — the same one-rule
              treatment `ModelTrendChart` gives its own axis: the baseline
              solid, the rest dashed and lighter, so the domain's zero start
              is the one line that reads as load-bearing. */}
          {ticks.map((tick) => (
            <div
              key={tick.value}
              className={tick.value === 0 ? "am-bench-compare-axis-line" : "am-bench-compare-gridline"}
              style={{ left: `${pct(tick.value)}%` }}
            />
          ))}
          <div className="am-bench-compare-rows">
            {bars.map((bar) => (
              <div key={bar.model} className="am-bench-compare-row">
                <div className="am-bench-compare-bar" style={{ width: `${pct(bar.value)}%` }} />
                <span className="am-bench-compare-value">{formatMetricSpecValue(bar.value, metric)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="am-bench-compare-foot">
        <div className="am-bench-compare-namecol" aria-hidden="true" />
        <div className="am-bench-compare-axis">
          {ticks.map((tick) => (
            <span key={tick.value} style={{ left: `${pct(tick.value)}%` }}>
              {tick.label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

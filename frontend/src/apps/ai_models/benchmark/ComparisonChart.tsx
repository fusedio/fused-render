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
import { cn } from "@platform/lib/utils";
import { formatMetricSpecValue, middleEllipsis, niceAxisTicks, shortModelName, type ComparisonBar, type MetricSpec } from "@apps/ai_models/lib/benchmark";

/** The name column's width, shared by the name list AND the empty spacer in the
 *  axis row below it — ONE constant for both, so the plot area and the tick row
 *  explaining it can never drift out of alignment from editing one without the
 *  other. */
const NAMECOL = "shrink-0 basis-[26ch] min-w-0";

/** A row's height, shared by a name and the bar beside it so the two columns
 *  stay on the same baseline. */
const ROW_H = "h-[22px]";

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
      // Capped independently of the tab's own ceiling: this is the one
      // instrument here where more width is actively harmful — the name column
      // is fixed-width while the plot stretches, so extra width lengthens every
      // bar and drags the value label after it further from the name it belongs
      // to. On the WHOLE wrapper, not just the body: the foot's tick labels are
      // positioned by percentage of THEIR OWN element's width, so capping only
      // the body would misalign every gridline from the tick that names it.
      className="mb-4 max-w-[900px]"
      role="img"
      aria-label={`${metric.label} across ${bars.length} benchmarked models, ranked best to worst, peak ${formatMetricSpecValue(peak, metric)}`}
    >
      <div className="flex gap-2.5">
        {/* Width comes from NAMECOL, shared with the spacer in the axis row
            below — see that constant. */}
        <div className={cn("flex flex-col gap-1.5", NAMECOL)}>
          {bars.map((bar) => (
            <div
              key={bar.model}
              className={cn("flex items-center overflow-hidden whitespace-nowrap font-mono text-xs text-muted-foreground", ROW_H)}
              title={bar.model}
            >
              {middleEllipsis(shortModelName(bar.model), 26)}
            </div>
          ))}
        </div>
        <div className="relative flex-1 min-w-0">
          {/* Gridlines, including the zero baseline — the same one-rule
              treatment `ModelTrendChart` gives its own axis: the baseline
              solid, the rest dashed and lighter, so the domain's zero start
              is the one line that reads as load-bearing. */}
          {ticks.map((tick) => (
            <div
              key={tick.value}
              className={
                tick.value === 0
                  ? "absolute inset-y-0 w-px bg-border"
                  : "absolute inset-y-0 w-0 border-l border-dashed border-border opacity-60"
              }
              style={{ left: `${pct(tick.value)}%` }}
            />
          ))}
          <div className="flex flex-col gap-1.5">
            {bars.map((bar) => (
              <div key={bar.model} className={cn("flex items-center", ROW_H)}>
                {/* The bar reads the same chart token the trend chart's line does —
                    one instrument's worth of colour vocabulary across both, rather
                    than a second palette decision for the same numbers drawn a
                    different way. */}
                <div className="h-2.5 min-w-[2px] rounded-full bg-chart-1" style={{ width: `${pct(bar.value)}%` }} />
                <span className="ml-1.5 whitespace-nowrap text-xs tabular-nums text-muted-foreground">
                  {formatMetricSpecValue(bar.value, metric)}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="flex gap-2.5 mt-1">
        <div className={NAMECOL} aria-hidden="true" />
        <div className="relative flex-1 min-w-0 h-3.5 text-xs tabular-nums text-muted-foreground">
          {ticks.map((tick, i) => (
            <span
              key={tick.value}
              className="absolute whitespace-nowrap"
              // The first tick (0) and the last would hang off the plot's own
              // edges under a centring shift — anchor those two inward instead
              // of letting either spill past the chart's box.
              style={{
                left: `${pct(tick.value)}%`,
                transform: i === 0 ? undefined : i === ticks.length - 1 ? "translateX(-100%)" : "translateX(-50%)",
              }}
            >
              {tick.label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

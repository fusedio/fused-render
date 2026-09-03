// ONE MODEL's history for the SELECTED metric — inline SVG, no charting
// dependency (SPEC AI-14). This is the tab's second instrument: the
// leaderboard (BenchmarkTab.tsx) answers "which model is fastest here", and
// this answers "is THIS model trending up or down" — two different questions
// that an earlier design tried to answer with one chart (every model as its
// own series, sharing one timeline), which produced exactly the failure mode
// that prompted this split: with one or two runs per model that is a scatter
// of near-unlabelable dots, which is why it needed edge-avoiding end labels,
// and why its x axis — spanning whichever runs happened to be newest across
// EVERY model — kept landing inside one day and repeating "22 Aug" three
// times. Scoped to one model, both of those become what they always should
// have been: a handful of points with room to breathe, and a span that is
// actually about the model the reader picked.
//
// Self-contained sizing, like UsageTab's own chart: the SVG carries a
// `viewBox` and scales to whatever box CSS gives it, so nothing here measures
// the DOM, nothing re-renders on resize. Every label — y-axis ticks, x-axis
// dates, the latest-point value — is an ordinary HTML text node positioned by
// PERCENTAGE of the plot's own box, never `<text>` inside the SVG: the
// viewBox stretches horizontally (`preserveAspectRatio="none"`) to fill
// whatever width CSS gives the chart, and a glyph drawn inside that stretch
// distorts with it, where a percentage of the surrounding box's real pixel
// width does not.
//
// **Every number it draws comes from `lib/benchmark.ts`, including WHICH
// metric** — `metric` arrives as a prop, resolved by the caller from the
// reader's selection (`resolveMetric`), never re-derived here from a
// capability. That is what keeps the one place a benchmark can be misread —
// the metric and the domain — under test in `benchmark.test.ts` rather than
// inside a component.
//
// **The y axis starts at zero, always** (`yMin` from `chartSeries` is 0). The
// top does NOT sit exactly at the peak, on purpose: `paddedAxisMax` adds 20%
// headroom above the highest measured value before `yAxisTicks` divides the
// domain, so the best point sits visibly INSIDE the plot rather than pinned
// to the top gridline (859.7 exactly on the top line, reading as clipped
// against the frame, was the bug this fixes). There is still no separate
// "peak N unit" caption — the aria-label states the true peak in words, and
// the chart itself is for seeing the shape, not reading the exact top tick.
import { MIN_TREND_POINTS, chartAxisTicks, chartSeries, formatMetricSpecValue, paddedAxisMax, type MetricSpec, yAxisTicks } from "@apps/ai_models/lib/benchmark";
import type { AiBenchmarkRun } from "@platform/lib/api";

// The plot's own coordinate space. Taller than the old 160px box — this chart
// is a section's centerpiece, not a footnote, and a "generous fixed height"
// instrument reads as one at a glance.
const W = 600;
const H = 220;
const PAD = 6;

export function ModelTrendChart({
  runs,
  metric,
}: {
  /** ONE model's runs — the caller filters (`BenchmarkTab.tsx`); this
   *  component does not know or care whose they are, only that `chartSeries`
   *  groups by `run.model` and a single-model input therefore always produces
   *  exactly one line. */
  runs: AiBenchmarkRun[];
  metric: MetricSpec;
}) {
  const { series, yMax } = chartSeries(runs, metric);
  const line = series[0];
  // Nothing plottable, or not enough of it to be a TREND — `BenchmarkTab.tsx`
  // already decides this before choosing to render this component at all
  // (`trendKind`, the single-run compact state lives there), but the check is
  // repeated here too: this component's OWN contract is "a real chart or
  // nothing", so it must never draw a sparse one-point plot for a caller that
  // forgets the rule. Said in words rather than an empty box with axes: an
  // axis under no data reads as "zero throughput", which is a measurement,
  // and there has not been one.
  if (!line || line.points.length < MIN_TREND_POINTS || yMax <= 0) return null;

  // The DOMAIN top is padded past the true peak (`yMax`) — see the file
  // header — so `axisMax`, not `yMax`, is what the axis and every point's `y`
  // position are actually scaled against. `yMax` survives only for the
  // aria-label, which states the real peak in words.
  const axisMax = paddedAxisMax(yMax);

  // The x domain is the count of runs in this model's own history, since a
  // point's `x` is its position there.
  const xMax = Math.max(1, runs.length - 1);
  const px = (x: number) => PAD + (x / xMax) * (W - 2 * PAD);
  // Inverted, because SVG y grows downward and the reader expects faster to be
  // higher — even on the one capability where "faster" is a smaller number, in
  // which case a line going UP is a slower model and the section's unit label
  // ("s/step") is what says so.
  const py = (y: number) => H - PAD - (y / axisMax) * (H - 2 * PAD);
  // Percentage-of-box versions of the same two functions, for the HTML labels
  // laid over the plot — see the file header for why these can't be `<text>`.
  const pxPct = (x: number) => (px(x) / W) * 100;
  const pyPct = (y: number) => (py(y) / H) * 100;

  const ticks = yAxisTicks(axisMax, metric, 3);

  // Three x ticks at most — first run, the middle one, last run — never a
  // claim of even spacing the runs don't have (see the file header on why the
  // old axis said only "oldest"/"newest"). `chartAxisTicks` (lib/benchmark.ts)
  // decides dates vs. times: a same-day span draws times, with the shared
  // date stated once in `dateCaption` rather than on every tick — and NOW
  // that this chart is scoped to one model, that span is actually about the
  // model on screen rather than whichever runs across every model happened to
  // be newest.
  const { ticks: dateTicks, dateCaption } = chartAxisTicks(runs);

  // The newest point always sits at the right edge (x === xMax) here, since
  // there is only ever one series — so the edge-avoiding anchor logic below
  // is not a leftover from the multi-model design, it is MORE reliably
  // exercised now: this model's latest measurement is guaranteed to be the
  // rightmost point, and a point on the top gridline is exactly as likely as
  // it always was.
  const last = line.points[line.points.length - 1]!;
  const lastXPct = pxPct(last.x);
  const lastYPct = pyPct(last.y);
  const translateX = lastXPct > 80 ? "-100%" : lastXPct < 12 ? "0%" : "-50%";
  const translateY = lastYPct < 12 ? "40%" : "-135%";

  return (
    <div>
      {/* The y-axis label column plus the plot, side by side. Both children carry
          the SAME fixed plot height (h-[220px], matching H below) so a tick
          label's `top: N%` and the gridline it names land on the same pixel. */}
      <div className="flex items-stretch gap-[3px]">
        {/* The y axis: a fixed-width column of tick labels, top to bottom,
            positioned by the SAME percentage math as the gridlines they sit
            beside — so a label and its line never drift apart. */}
        <div className="relative shrink-0 w-10 h-[220px]" aria-hidden="true">
          {[...ticks].reverse().map((tick) => (
            <span
              key={tick.value}
              // Centred on its gridline rather than sitting above it, so the label
              // reads as "this line" and not "the line below me".
              className="absolute inset-x-0 -translate-y-1/2 text-right text-xs tabular-nums text-muted-foreground"
              style={{ top: `${pyPct(tick.value)}%` }}
            >
              {tick.label}
            </span>
          ))}
        </div>
        <div className="relative flex-1 min-w-0">
          <svg
            viewBox={`0 0 ${W} ${H}`}
            preserveAspectRatio="none"
            // Height is set here and the viewBox does not preserve its aspect
            // ratio, so the plot fills whatever width the column has without this
            // component measuring anything. No border or background of its own:
            // the gridlines and the baseline below already delineate the plot.
            className="block w-full h-[220px]"
            role="img"
            aria-label={`${metric.label} across ${runs.length} runs, peak ${formatMetricSpecValue(yMax, metric)}`}
          >
            {/* Gridlines for every tick, including the zero baseline — one
                rule rather than a special-cased "axis line" plus separate
                ticks, so the bottom of the domain is drawn the same way as
                every other step in it. */}
            {ticks.map((tick) => (
              <line
                key={tick.value}
                // The baseline carries the domain's promise (the axis starts at
                // zero); its equal-step siblings are lighter and dashed.
                // `vector-effect` because the viewBox is stretched horizontally,
                // which would otherwise scale the stroke with it.
                className={
                  tick.value === 0
                    ? "stroke-border [vector-effect:non-scaling-stroke]"
                    : "stroke-border opacity-60 [stroke-dasharray:2_3] [vector-effect:non-scaling-stroke]"
                }
                x1={PAD}
                y1={py(tick.value)}
                x2={W - PAD}
                y2={py(tick.value)}
              />
            ))}
            <polyline
              // The line, its dots and its value label all read ONE colour
              // (chart-1, the token palette charts share) — a label whose colour
              // was declared apart from its line could disagree with the chart.
              className="fill-none stroke-chart-1 stroke-2 [stroke-linejoin:round] [vector-effect:non-scaling-stroke]"
              points={line.points.map((p) => `${px(p.x)},${py(p.y)}`).join(" ")}
            />
            {/* A dot per run: a model with exactly one recorded run has no
                line to draw at all, and a series that rendered as nothing
                would read as a model that was never benchmarked. Its value
                label (below) is what turns that lone dot into a readable
                measurement rather than a floating mark. */}
            {line.points.map((p) => (
              <circle
                key={p.run.id}
                className="fill-chart-1 stroke-background [vector-effect:non-scaling-stroke]"
                strokeWidth={1}
                cx={px(p.x)}
                cy={py(p.y)}
                r={3}
              />
            ))}
          </svg>
          {/* The latest point's own value, labelled — not the model name
              (the section heading above already names it, and repeating it
              inside the plot is noise a single-series chart doesn't need).
              HTML, not SVG `<text>`, and positioned by the same percentage
              math as everything else here, for the reason in the file
              header.

              **The anchor flips near an edge, rather than always centring on
              the point.** The newest run is always at the right edge here
              (see above), which used to overflow the panel — a value
              clipping mid-number against the border because nothing reserved
              room for it — so past that threshold the label right-aligns and
              grows LEFTWARD into the plot instead. The same fix applies
              vertically: a point on the TOP gridline used to draw its label
              above the plot's own frame, reading as detached from the chart
              it names, so a point within ~12% of the top draws its label
              BELOW itself instead. */}
          <span
            className="pointer-events-none absolute whitespace-nowrap text-xs font-semibold tabular-nums text-chart-1"
            style={{
              left: `${lastXPct}%`,
              top: `${lastYPct}%`,
              transform: `translate(${translateX}, ${translateY})`,
            }}
          >
            {formatMetricSpecValue(last.y, metric)}
          </span>
        </div>
      </div>
      {/* The date ticks, in a row UNDER the plot rather than as <text> inside a
          stretched viewBox. Left-aligned first tick, right-aligned last, so each
          date sits under the edge of the plot it names; the padding clears the
          y-axis label column (40px + 3px gap). */}
      <div className="flex justify-between gap-3 mt-1 pl-[43px] text-xs tabular-nums text-muted-foreground">
        {dateTicks.map((tick) => (
          <span key={tick.x}>{tick.label}</span>
        ))}
      </div>
      {/* The date every time tick above shares, stated ONCE — only present
          when `chartAxisTicks` switched to times (a same-day span), since a
          row of dates needs no caption repeating one of them a third time. */}
      {dateCaption && <div className="mt-0.5 pl-[43px] text-xs text-muted-foreground">{dateCaption}</div>}
    </div>
  );
}

// The /monitor page's two chart shapes, plain SVG (no chart library):
//
// * `AreaChart` — one series as an area with a faint grid, a labelled y-axis
//   and the latest reading emphasised (the CPU and Memory meters).
// * `LinesChart` — up to five series as lines with a legend (the per-process
//   graph for the selected rows).
//
// The plot is an SVG stretched to its box (`preserveAspectRatio="none"`,
// strokes kept crisp with `vector-effect`), so anything that must NOT stretch
// — axis labels, the last-point dot — is HTML positioned in percent over it.
// Colours come from tokens only (`--accent` for a meter, `--series-N` per
// line), so both themes repaint them.
import type { CSSProperties } from "react";

export interface ChartPoint {
  t: number;
  v: number | null;
}

export interface Tick {
  v: number;
  label: string;
}

const W = 600;
const H = 100;

function geometry(windowS: number, end: number, max: number) {
  const t0 = end - windowS;
  return {
    t0,
    x: (t: number) => Math.min(W, Math.max(0, ((t - t0) / windowS) * W)),
    y: (v: number) => H - (Math.min(v, max) / max) * H,
  };
}

/** Readings further apart than this are a GAP (the sampler slept: nobody was
 *  reading), drawn as a break rather than a straight line across it. */
const GAP_S = 5;

function runsOf(points: { t: number; v: number }[]): { t: number; v: number }[][] {
  const runs: { t: number; v: number }[][] = [];
  for (const p of points) {
    const run = runs[runs.length - 1];
    if (run && p.t - run[run.length - 1].t <= GAP_S) run.push(p);
    else runs.push([p]);
  }
  return runs;
}

function pathOf(points: { t: number; v: number }[], x: (t: number) => number, y: (v: number) => number) {
  return runsOf(points)
    .map((run) => run.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(""))
    .join("");
}

function areaOf(points: { t: number; v: number }[], x: (t: number) => number, y: (v: number) => number) {
  return runsOf(points)
    .filter((run) => run.length > 1)
    .map(
      (run) =>
        `${pathOf(run, x, y)}L${x(run[run.length - 1].t).toFixed(1)},${H}L${x(run[0].t).toFixed(1)},${H}Z`,
    )
    .join("");
}

function known(points: ChartPoint[], t0: number): { t: number; v: number }[] {
  return points.filter((p): p is { t: number; v: number } => p.v !== null && p.t >= t0);
}

function Axis({ ticks, max }: { ticks: Tick[]; max: number }) {
  return (
    <div className="monitor-axis" aria-hidden="true">
      {ticks.map((t) => (
        <span key={t.v} style={{ top: `${(1 - t.v / max) * 100}%` }}>
          {t.label}
        </span>
      ))}
    </div>
  );
}

function Grid({ ticks, max }: { ticks: Tick[]; max: number }) {
  return (
    <>
      {ticks.map((t) => {
        const y = H - (t.v / max) * H;
        return <line key={t.v} className="monitor-grid" x1={0} x2={W} y1={y} y2={y} />;
      })}
    </>
  );
}

export function AreaChart({
  points,
  max,
  ticks,
  windowS,
  label,
}: {
  points: ChartPoint[];
  max: number;
  ticks: Tick[];
  windowS: number;
  label: string;
}) {
  const end = points.length ? points[points.length - 1].t : 0;
  const { t0, x, y } = geometry(windowS, end, max);
  const pts = known(points, t0);
  const last = pts[pts.length - 1];
  const line = pts.length > 1 ? pathOf(pts, x, y) : "";
  const area = line ? areaOf(pts, x, y) : "";
  return (
    <div className="monitor-chart" role="img" aria-label={label}>
      <Axis ticks={ticks} max={max} />
      <div className="monitor-plot">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
          <Grid ticks={ticks} max={max} />
          {area && <path className="monitor-area" d={area} />}
          {line && <path className="monitor-line" d={line} />}
        </svg>
        {last && (
          <span
            // Keyed on the reading, so each new sample remounts the dot and
            // its one-shot pulse plays once (styles/monitor.css).
            key={last.t}
            className="monitor-dot"
            style={{ left: `${(x(last.t) / W) * 100}%`, top: `${(y(last.v) / H) * 100}%` }}
          />
        )}
        {pts.length < 2 && <div className="monitor-chart-empty">Collecting…</div>}
      </div>
    </div>
  );
}

export interface LineSeries {
  key: string;
  label: string;
  points: ChartPoint[];
}

export function LinesChart({
  series,
  max,
  ticks,
  windowS,
  end,
  label,
}: {
  series: LineSeries[];
  max: number;
  ticks: Tick[];
  windowS: number;
  end: number;
  label: string;
}) {
  const { t0, x, y } = geometry(windowS, end, max);
  return (
    <div className="monitor-chart monitor-chart-lines" role="img" aria-label={label}>
      <Axis ticks={ticks} max={max} />
      <div className="monitor-plot">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
          <Grid ticks={ticks} max={max} />
          {series.map((s, i) => {
            const pts = known(s.points, t0);
            return pts.length > 1 ? (
              <path
                key={s.key}
                className="monitor-series"
                style={{ "--series": `var(--series-${(i % 6) + 1})` } as CSSProperties}
                d={pathOf(pts, x, y)}
              />
            ) : null;
          })}
        </svg>
        {series.map((s, i) => {
          const pts = known(s.points, t0);
          const last = pts[pts.length - 1];
          return last ? (
            <span
              key={`${s.key}:${last.t}`}
              className="monitor-dot monitor-dot-series"
              style={
                {
                  "--series": `var(--series-${(i % 6) + 1})`,
                  left: `${(x(last.t) / W) * 100}%`,
                  top: `${(y(last.v) / H) * 100}%`,
                } as CSSProperties
              }
            />
          ) : null;
        })}
      </div>
    </div>
  );
}

export function Legend({ series }: { series: { key: string; label: string }[] }) {
  return (
    <ul className="monitor-legend">
      {series.map((s, i) => (
        <li key={s.key} style={{ "--series": `var(--series-${(i % 6) + 1})` } as CSSProperties}>
          <span className="monitor-swatch" aria-hidden="true" />
          {s.label}
        </li>
      ))}
    </ul>
  );
}

// A small inline SVG line + fill over the last `windowS` seconds, shared by
// the System chip's popover (shell/SystemDock.tsx) and anything else that
// wants a figure's recent shape at a glance. The y-axis floor is `floor`
// (100 = one core for a CPU %), so a quiet series reads as a flat line rather
// than noise scaled up to full height.

export interface SparkPoint {
  t: number;
  v: number | null;
}

const SPARK_W = 296;
const SPARK_H = 34;

export function Sparkline({
  points,
  windowS = 60,
  floor = 100,
  label,
}: {
  points: SparkPoint[];
  windowS?: number;
  floor?: number;
  label: string;
}) {
  const known = points.filter((p): p is { t: number; v: number } => p.v !== null);
  const end = known.length ? known[known.length - 1].t : 0;
  const t0 = end - windowS;
  const pts = known.filter((p) => p.t >= t0);
  if (pts.length < 2) {
    return <div className="sys-spark sys-spark-empty">Collecting…</div>;
  }
  const max = Math.max(floor, ...pts.map((p) => p.v));
  const x = (t: number) => ((t - t0) / windowS) * SPARK_W;
  const y = (v: number) => SPARK_H - 1 - (v / max) * (SPARK_H - 2);
  const line = pts
    .map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`)
    .join("");
  const area = `${line}L${x(pts[pts.length - 1].t).toFixed(1)},${SPARK_H}L${x(pts[0].t).toFixed(1)},${SPARK_H}Z`;
  return (
    <svg
      className="sys-spark"
      viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
    >
      <path className="sys-spark-area" d={area} />
      <path className="sys-spark-line" d={line} />
    </svg>
  );
}

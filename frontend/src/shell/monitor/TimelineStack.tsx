// The stacked timeline: one Sparkline row per headline figure (requests/s, in
// flight, loop lag, CPU) on a shared time axis, with outage markers drawn
// across every row. Restarts are not drawn: the trail cannot tell them apart
// from other servers sharing it.
import { Sparkline } from "./Sparkline";
import type { TimelinePoint } from "./monitor-api";
import { timelineSeries } from "./monitor-net-lib";

const ROW_KEYS = new Set(["rps", "in_flight", "lag_ms", "cpu"]);

export default function TimelineStack({
  points,
  events,
  windowS = 600,
}: {
  points: TimelinePoint[];
  events: { t: number; kind: string }[];
  windowS?: number;
}) {
  const end = points.length ? points[points.length - 1].t : 0;
  const t0 = end - windowS;
  const rows = timelineSeries(points).filter((r) => ROW_KEYS.has(r.key));
  const marks = events.filter((e) => e.kind === "outage" && e.t >= t0 && e.t <= end);
  return (
    <div className="monitor-tl">
      {rows.map((r) => {
        const known = r.points.filter((p) => p.v !== null);
        const last = known.length ? known[known.length - 1].v : null;
        return (
          <div key={r.key} className="monitor-tl-row" data-series={r.key}>
            <div className="monitor-tl-label">
              <span>{r.label}</span>
              <span className="monitor-tl-now">{last === null ? "–" : Math.round(last * 10) / 10}</span>
            </div>
            <div className="monitor-tl-plot">
              <Sparkline points={r.points} windowS={windowS} floor={r.floor} label={r.label} />
              {marks.map((m, i) => (
                <span
                  key={i}
                  className="monitor-tl-mark is-outage"
                  style={{ left: `${(((m.t - t0) / windowS) * 100).toFixed(2)}%` }}
                  title="Server outage"
                />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

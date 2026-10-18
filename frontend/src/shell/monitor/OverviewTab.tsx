// /monitor, Overview tab — "why does fused-render feel slow?" at a glance.
//
// Top to bottom: the verdict banner (the likeliest cause first; each reason is
// a link that switches to the tab and filters that show it); any non-headline
// tile that is warn/bad; four chart cards (Requests, Connections, Server, CPU,
// the Processes tab's card form) over the since-restart window; and
// behind "Show all checks": the remaining tiles, the errors tail, stalled jobs,
// the slowest .py targets and the disk footer. Record 60s / Copy report sit in
// the banner.
//
// DATA: `GET /api/system/dashboard` every 2 s while mounted and the document
// visible. Every section is computed independently on the server, so one that
// failed is "unavailable" in its own tile and the rest still draw. Links are
// handed to `onNavigate`; the shell owns the URL (written on user action only).
import { useEffect, useRef, useState } from "react";
import { spaLinkProps } from "@platform/lib/router";
import { formatBytes } from "@platform/lib/sysmon";
import { getDashboard, sectionOk, type Dashboard } from "./monitor-api";
import { AreaChart, PCT_TICKS, type Tick } from "./Charts";
import {
  monitorUrl,
  niceCeil,
  overviewTiles,
  timelineSeries,
  timelineTitle,
  timelineWindowS,
  type Tab,
} from "./monitor-net-lib";
import { useRecord, type RecordApi } from "./useRecord";
import { usePolled } from "./usePolled";

const HEADLINE = ["requests", "queue", "server", "memory", "cpu"];

/** Identical consecutive labels collapse to one with a count: "Outage ×3". */
export function collapseRuns(labels: string[]): { label: string; n: number }[] {
  const out: { label: string; n: number }[] = [];
  for (const label of labels) {
    const last = out[out.length - 1];
    if (last && last.label === label) last.n += 1;
    else out.push({ label, n: 1 });
  }
  return out;
}

const ms = (n: number | null | undefined) => (n == null ? "–" : `${Math.round(n)} ms`);

export function plainClick(e: {
  button: number;
  metaKey?: boolean;
  ctrlKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
}): boolean {
  return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
}

export default function OverviewTab({
  load = getDashboard,
  pollMs = 2_000,
  recordApi,
  onNavigate,
  copy = (text: string) => navigator.clipboard.writeText(text),
}: {
  load?: () => Promise<Dashboard>;
  pollMs?: number;
  recordApi?: RecordApi;
  onNavigate: (link: string) => void;
  copy?: (text: string) => Promise<void> | void;
}) {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [copied, setCopied] = useState<"ok" | "failed" | null>(null);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (copiedTimer.current !== null) clearTimeout(copiedTimer.current);
    },
    [],
  );
  const record = useRecord(recordApi);

  usePolled(
    async () => {
      setData(await load());
      setError(null);
    },
    pollMs,
    null,
    (e) => setError(e.message),
  );

  const go = (link: string) => (e: Parameters<typeof plainClick>[0] & { preventDefault: () => void }) => {
    if (!plainClick(e)) return;
    e.preventDefault();
    onNavigate(link);
  };
  const doCopy = async () => {
    if (!data) return;
    // A rejected write, or no clipboard at all (navigator.clipboard is
    // undefined outside a secure context), reads "Copy failed" rather than
    // an unhandled rejection and a button that never answers.
    let state: "ok" | "failed" = "ok";
    try {
      await copy(JSON.stringify(data, null, 2));
    } catch {
      state = "failed";
    }
    setCopied(state);
    if (copiedTimer.current !== null) clearTimeout(copiedTimer.current);
    copiedTimer.current = setTimeout(() => {
      copiedTimer.current = null;
      setCopied(null);
    }, 2_000);
  };

  const v = data?.verdict;
  // A reason whose text repeats the headline is not a second line: the
  // headline itself becomes its link.
  const reasons = v ? v.reasons.filter((r) => r.text !== v.headline) : [];
  const headLink = v?.reasons.find((r) => r.text === v.headline && r.link)?.link;
  const allTiles = data ? overviewTiles(data) : [];
  const alerting = (l: string) => l === "warn" || l === "bad";
  const tiles = allTiles.filter((t) => !HEADLINE.includes(t.key) && alerting(t.level));
  const hiddenTiles = allTiles.filter((t) => !HEADLINE.includes(t.key) && !tiles.includes(t));
  const tile = (key: string) => allTiles.find((t) => t.key === key);
  const tl = data && sectionOk(data.timeline) ? data.timeline : null;
  const errors = data && sectionOk(data.errors) ? data.errors : null;
  const jobs = data && sectionOk(data.jobs) ? data.jobs : null;
  const calls = data && sectionOk(data.calls) ? data.calls : null;

  const points = tl?.points ?? [];
  const windowS = timelineWindowS(points);
  const title = timelineTitle(points);
  const series = (key: string) => timelineSeries(points).find((r) => r.key === key)?.points ?? [];
  const peak = (pts: { v: number | null }[]) => Math.max(0, ...pts.map((p) => p.v ?? 0));
  const halves = (max: number, unit: string): Tick[] => [
    { v: 0, label: "0" },
    { v: max / 2, label: `${max / 2}${unit}` },
    { v: max, label: `${max}${unit}` },
  ];
  const rps = series("rps");
  const inFlight = series("in_flight");
  const lag = series("lag_ms");
  const rpsMax = niceCeil(peak(rps), 1);
  const lagMax = niceCeil(peak(lag), 10);
  const connMax = Math.max(6, Math.ceil(peak(inFlight)));
  const memCaption = tile("memory")?.caption.split(" · ")[0];
  const cards = [
    {
      key: "requests",
      title: "Requests",
      tile: tile("requests"),
      figure: tile("requests")?.primary ?? "–",
      sub: tile("requests")?.caption ?? "",
      points: rps,
      max: rpsMax,
      ticks: halves(rpsMax, ""),
    },
    {
      key: "queue",
      title: "Connections",
      tile: tile("queue"),
      figure: (() => {
        const p = tile("queue")?.primary;
        return p && p !== "no data" && !p.startsWith("–") ? `${p} wait` : (p ?? "–");
      })(),
      sub: "browser wait p95 · limit 6 per host",
      points: inFlight,
      max: connMax,
      ticks: [
        { v: 0, label: "0" },
        { v: 3, label: "3" },
        { v: 6, label: "6 limit" },
      ] as Tick[],
    },
    {
      key: "server",
      title: "Server",
      tile: tile("server"),
      figure: tile("server")?.primary ?? "–",
      sub: tile("server")?.caption ?? "",
      points: lag,
      max: lagMax,
      ticks: halves(lagMax, " ms"),
    },
    {
      key: "cpu",
      title: "CPU",
      tile: tile("cpu"),
      figure: tile("cpu")?.primary ?? "–",
      sub: [tile("memory")?.primary, memCaption].filter(Boolean).join(" · "),
      points: series("cpu"),
      max: 100,
      ticks: PCT_TICKS,
    },
  ];

  const renderTile = (t: (typeof allTiles)[number]) => {
    const link = monitorUrl({ tab: t.tab as Tab });
    return (
      <a key={t.key} className={`monitor-tile is-${t.level}`} data-tile={t.key} href={link} onClick={go(link)}>
        <span className="monitor-tile-title">
          <span className="monitor-tile-dot" aria-hidden="true" />
          {t.title}
        </span>
        <span className="monitor-tile-value">{t.primary}</span>
        <span className="monitor-tile-base">{t.caption || " "}</span>
      </a>
    );
  };

  return (
    <div className="monitor-tab monitor-tab-scroll monitor-overview">
      {error && <p className="monitor-denied">Can't read the dashboard: {error}</p>}

      <section className={`monitor-verdict is-${v?.level ?? "ok"}`} aria-label="Verdict">
        <p className="monitor-verdict-head">
          {headLink ? (
            <a href={headLink} onClick={go(headLink)}>
              {v?.headline}
            </a>
          ) : v ? (
            v.headline
          ) : (
            "Reading…"
          )}
        </p>
        {reasons.length > 0 && (
          <ul className="monitor-verdict-reasons">
            {reasons.map((r) => (
              <li key={r.key} className={`is-${r.level}`}>
                <a href={r.link} onClick={go(r.link)}>
                  {r.text}
                </a>
              </li>
            ))}
          </ul>
        )}
        <div className="monitor-verdict-actions">
          <button
            type="button"
            className="btn btn-secondary monitor-btn"
            disabled={record.recording}
            onClick={() => void record.start()}
          >
            {record.recording ? `Recording… ${Math.ceil(record.status?.seconds_left ?? 0)}s` : "Record 60s"}
          </button>
          <button
            type="button"
            className="btn btn-secondary monitor-btn"
            disabled={!data}
            onClick={() => void doCopy()}
          >
            {copied === "ok" ? "Copied" : copied === "failed" ? "Copy failed" : "Copy report"}
          </button>
          {!record.recording && record.latest && record.dir && (
            <a className="monitor-recording-link" {...spaLinkProps(`${record.dir}/${record.latest.name}`)}>
              Saved: {record.latest.name}
            </a>
          )}
          {record.error && <span className="monitor-denied">{record.error}</span>}
        </div>
      </section>

      {tiles.length > 0 && <div className="monitor-tiles">{tiles.map(renderTile)}</div>}

      <h2 className="monitor-sec-title monitor-overview-window">{timelineTitle(tl?.points ?? [])}</h2>
      <div className="monitor-overview-cards">
        {cards.map((c) => {
          const t = c.tile;
          const link = t ? monitorUrl({ tab: t.tab as Tab }) : "";
          return (
            <a
              key={c.key}
              className={`monitor-card monitor-overview-card${t && alerting(t.level) ? ` is-${t.level}` : ""}`}
              data-card={c.key}
              aria-label={c.title}
              href={link}
              onClick={go(link)}
            >
              <header className="monitor-card-head">
                <h2>{c.title}</h2>
                <span className="monitor-card-figure">{c.figure}</span>
              </header>
              <div className="monitor-card-sub">
                <span>{c.sub}</span>
              </div>
              <AreaChart points={c.points} max={c.max} ticks={c.ticks} windowS={windowS} label={`${c.title}, ${title}`} />
            </a>
          );
        })}
      </div>

      <button type="button" className="monitor-more" aria-expanded={showAll} onClick={() => setShowAll((v) => !v)}>
        {showAll ? "Hide checks" : `Show all checks (${hiddenTiles.length})`}
      </button>

      {showAll && (
        <>
          {hiddenTiles.length > 0 && <div className="monitor-tiles">{hiddenTiles.map(renderTile)}</div>}
          <div className="monitor-lists">
            <section className="monitor-list-card">
              <h2 className="monitor-sec-title">Errors and outages</h2>
              {errors ? (
                <ul className="monitor-list">
                  {collapseRuns(
                    errors.outages
                      .slice(-3)
                      .map((o) => `Outage ${o.t_down ? new Date(o.t_down * 1000).toLocaleString() : ""}`.trim()),
                  ).map((o, i) => (
                    <li key={`o${i}`}>{o.n > 1 ? `${o.label} ×${o.n}` : o.label}</li>
                  ))}
                  {errors.crashes.slice(0, 3).map((c) => (
                    <li key={c.name}>Crash dump {c.name}</li>
                  ))}
                  {collapseRuns(errors.server_log_errors.slice(-5)).map((l, i) => (
                    <li key={`l${i}`} className="monitor-list-mono" title={l.label}>
                      {l.n > 1 ? `${l.label} ×${l.n}` : l.label}
                    </li>
                  ))}
                  {!errors.outages.length && !errors.crashes.length && !errors.server_log_errors.length && (
                    <li className="monitor-list-empty">Nothing recent.</li>
                  )}
                </ul>
              ) : (
                <p className="monitor-net-note">{data ? "unavailable" : "Reading…"}</p>
              )}
            </section>

            <section className="monitor-list-card">
              <h2 className="monitor-sec-title">Stalled jobs</h2>
              {jobs ? (
                <ul className="monitor-list">
                  {jobs.stalled_list.map((j, i) => (
                    <li key={j.id ?? i}>{j.title ?? j.id ?? "job"}</li>
                  ))}
                  {!jobs.stalled_list.length && <li className="monitor-list-empty">None.</li>}
                </ul>
              ) : (
                <p className="monitor-net-note">{data ? "unavailable" : "Reading…"}</p>
              )}
            </section>

            <section className="monitor-list-card">
              <h2 className="monitor-sec-title">Slowest .py targets (overhead)</h2>
              {calls ? (
                <ul className="monitor-list">
                  {calls.top_overhead.map((c) => (
                    <li key={c.entrypoint} title={c.entrypoint}>
                      <span className="monitor-list-mono">{c.entrypoint}</span>
                      <span className="monitor-list-num">
                        {ms(c.overhead_p95_ms)} over a {ms(c.run_p95_ms)} run
                      </span>
                    </li>
                  ))}
                  {!calls.top_overhead.length && <li className="monitor-list-empty">No calls in the last 10 min.</li>}
                </ul>
              ) : (
                <p className="monitor-net-note">{data ? "unavailable" : "Reading…"}</p>
              )}
            </section>
          </div>
          {data && sectionOk(data.disk) && data.disk.log_home != null && (
            <p className="monitor-net-note">
              Disk use: logs {formatBytes(data.disk.log_home, true)} · calls {formatBytes(data.disk.call_store, true)} ·
              index {formatBytes(data.disk.index_db, true)}
            </p>
          )}
        </>
      )}
    </div>
  );
}

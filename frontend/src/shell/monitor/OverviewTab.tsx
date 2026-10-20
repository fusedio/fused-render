// /monitor, Overview tab — "why does fused-render feel slow?" at a glance.
//
// Top to bottom: the verdict line, always present (when something is off its
// reasons as a plain list, each a link that switches to the tab and filters
// that show it; when all is well one "All good" row with the headline
// figures); any non-headline
// tile that is warn/bad; four chart cards (Requests, Connections, Server, CPU,
// the Processes tab's card form) over the since-restart window; and
// behind "Show all checks": the remaining tiles, the errors tail, stalled jobs,
// the slowest .py targets and the disk footer. Record 60s / Copy report sit
// right-aligned on the window heading's row. The errors tile toggles an inline
// panel of the recent errors instead of linking anywhere.
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
  basename,
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
  const [errorsOpen, setErrorsOpen] = useState(false);
  const [copied, setCopied] = useState<"ok" | "failed" | null>(null);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (copiedTimer.current !== null) clearTimeout(copiedTimer.current);
    },
    [],
  );
  const record = useRecord(recordApi);
  // The saved-recording link is for a recording finished during THIS page
  // session; one already on disk at mount is not advertised.
  const [recordedHere, setRecordedHere] = useState(false);
  useEffect(() => {
    if (record.recording) setRecordedHere(true);
  }, [record.recording]);

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
  const reasons = v && v.level !== "ok" ? v.reasons : [];
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
  const latest = (pts: { v: number | null }[]): number | null => {
    for (let i = pts.length - 1; i >= 0; i--) if (pts[i].v != null) return pts[i].v;
    return null;
  };
  const withFallback = (t: { primary: string; available: boolean } | undefined, v: number | null, fmt: (n: number) => string): string => {
    if (t?.available) return t.primary;
    return v == null ? "–" : fmt(v);
  };
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
      figure: withFallback(tile("requests"), latest(rps), (n) => `${Math.round(n * 10) / 10}/s`),
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
        const t = tile("queue");
        return t?.available ? `${t.primary} wait` : (t?.primary ?? "–");
      })(),
      sub: "browser wait p95 · limit 6 per host",
      points: inFlight,
      max: connMax,
      ticks: [
        { v: 0, label: "0" },
        { v: 3, label: "3" },
        { v: 6, label: "6 limit" },
      ] as Tick[],
      step: true,
    },
    {
      key: "server",
      title: "Server",
      tile: tile("server"),
      figure: withFallback(tile("server"), latest(lag), (n) => `${Math.round(n)} ms lag`),
      sub: tile("server")?.caption ?? "",
      points: lag,
      max: lagMax,
      ticks: halves(lagMax, " ms"),
    },
    {
      key: "cpu",
      title: "CPU",
      tile: tile("cpu"),
      figure: withFallback(tile("cpu"), latest(series("cpu")), (n) => `${Math.round(n)}%`),
      sub: [tile("memory")?.primary, memCaption].filter(Boolean).join(" · "),
      points: series("cpu"),
      max: 100,
      ticks: PCT_TICKS,
    },
  ];

  const okSummary = cards
    .map((c) => (c.key === "requests" ? `${c.figure} requests` : c.key === "server" ? c.figure : c.key === "cpu" ? `CPU ${c.figure}` : ""))
    .filter((t) => t && !t.startsWith("–"))
    .join(" · ");

  const errorsInTop = tiles.some((t) => t.key === "errors");
  const errorsPanel = errorsOpen && (
    <section className="monitor-list-card monitor-errors-panel" aria-label="Recent errors">
      <h2 className="monitor-sec-title">Errors and outages</h2>
      {errors ? <ErrorsList errors={errors} limit={10} /> : <p className="monitor-net-note">unavailable</p>}
    </section>
  );

  const renderTile = (t: (typeof allTiles)[number]) => {
    if (t.key === "errors") {
      return (
        <button
          key={t.key}
          type="button"
          className={`monitor-tile monitor-tile-button is-${t.level}`}
          data-tile={t.key}
          aria-expanded={errorsOpen}
          onClick={() => setErrorsOpen((o) => !o)}
        >
          <span className="monitor-tile-title">
            <span className="monitor-tile-dot" aria-hidden="true" />
            {t.title}
          </span>
          <span className="monitor-tile-value">{t.primary}</span>
          <span className="monitor-tile-base">{t.caption || " "}</span>
        </button>
      );
    }
    const link = monitorUrl({ tab: t.tab as Tab });
    return (
      <a
        key={t.key}
        className={`monitor-tile is-${t.level}`}
        data-tile={t.key}
        title={t.hint}
        href={link}
        onClick={go(link)}
      >
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

      {v && v.level === "ok" && (
        <ul className="monitor-reasons" aria-label="Verdict">
          <li className="is-ok">
            <span className="monitor-reason-dot" aria-hidden="true" />
            <span className="monitor-verdict-ok">All good</span>
            {okSummary && <span className="monitor-verdict-summary">· {okSummary}</span>}
          </li>
        </ul>
      )}
      {reasons.length > 0 && (
        <ul className="monitor-reasons" aria-label="What looks wrong">
          {reasons.map((r) => (
            <li key={r.key} className={`is-${r.level}`}>
              <span className="monitor-reason-dot" aria-hidden="true" />
              <a href={r.link} onClick={go(r.link)}>
                {r.text}
              </a>
            </li>
          ))}
        </ul>
      )}

      {tiles.length > 0 && <div className="monitor-tiles">{tiles.map(renderTile)}</div>}
      {errorsInTop && errorsPanel}
      <div className="monitor-overview-window">
        <h2 className="monitor-sec-title">{title}</h2>
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
          {recordedHere && !record.recording && record.latest && record.dir && (
            <a className="monitor-recording-link" {...spaLinkProps(`${record.dir}/${record.latest.name}`)}>
              Saved: {record.latest.name}
            </a>
          )}
          {record.error && <span className="monitor-denied">{record.error}</span>}
        </div>
      </div>
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
              <AreaChart
                points={c.points}
                max={c.max}
                ticks={c.ticks}
                windowS={windowS}
                label={`${c.title}, ${title}`}
                step={c.step}
              />
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
          {!errorsInTop && errorsPanel}
          <div className="monitor-lists">
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
                    <li key={c.entrypoint} className="monitor-list-target" title={c.entrypoint}>
                      <span className="monitor-list-mono monitor-list-text">{basename(c.entrypoint)}</span>
                      <span className="monitor-list-num">
                        {ms(c.overhead_p95_ms)} / {ms(c.run_p95_ms)} run
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

type ErrorsData = Extract<Dashboard["errors"], { outages: unknown }>;

function fmtDuration(sec: number): string {
  if (sec < 60) return `${Math.round(sec)} s`;
  if (sec < 3600) return `${Math.round(sec / 60)} min`;
  return `${(sec / 3600).toFixed(1)} h`;
}

function fmtAgo(t: number): string {
  const sec = Math.max(0, Date.now() / 1000 - t);
  return sec < 60 ? "just now" : `${fmtDuration(sec)} ago`;
}

function outageText(o: ErrorsData["outages"][number]): string {
  const when = new Date(o.t_down * 1000).toLocaleString();
  const d = fmtDuration(o.duration_s);
  return o.crashed ? `${when} · crashed, down ${d}` : `${when} · not responding for ${d} (server stayed up)`;
}

/** Unexpected outages, crash dumps (each a link to the dump) and the last log
 * error lines, repeats collapsed. Restarts are only counted. */
function ErrorsList({ errors, limit = 5 }: { errors: ErrorsData; limit?: number }) {
  const unexpected = errors.outages.filter((o) => !o.expected);
  const planned = errors.outages.length - unexpected.length;
  const empty = !unexpected.length && !errors.crashes.length && !errors.server_log_errors.length;
  return (
    <ul className="monitor-list">
      {unexpected.slice(0, 3).map((o) => (
        <li key={`o${o.t_down}`} title={outageText(o)}>
          <span className="monitor-list-text">{outageText(o)}</span>
        </li>
      ))}
      {errors.crashes.slice(0, 3).map((c) => (
        <li key={c.name} title={`${c.name} · ${formatBytes(c.bytes, true)} · ${fmtAgo(c.t)}`}>
          <a className="monitor-crash-link monitor-list-text" {...spaLinkProps(c.path)}>
            {c.name}
            <span className="monitor-crash-meta"> · {formatBytes(c.bytes, true)} · {fmtAgo(c.t)}</span>
          </a>
        </li>
      ))}
      {collapseRuns(errors.server_log_errors.slice(-limit)).map((l, i) => (
        <li key={`l${i}`} className="monitor-list-mono" title={l.label}>
          <span className="monitor-list-text">{l.n > 1 ? `${l.label} ×${l.n}` : l.label}</span>
        </li>
      ))}
      {planned > 0 && (
        <li className="monitor-list-empty">{planned} restarts hidden</li>
      )}
      {empty && !planned && <li className="monitor-list-empty">Nothing to report</li>}
    </ul>
  );
}

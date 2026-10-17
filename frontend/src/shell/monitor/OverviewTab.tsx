// /monitor, Overview tab — "why does fused-render feel slow?" at a glance.
//
// Top to bottom: the verdict banner (the likeliest cause first; each reason is
// a link that switches to the tab and filters that show it); a grid of twelve
// health tiles (green / amber / red, the key number, "usual ..." where a 24 h
// baseline exists, a link to the detail); the ten-minute stacked timeline with
// outage and restart markers; the errors tail, stalled jobs and the slowest
// .py targets; and Record 60s / Copy report.
//
// DATA: `GET /api/system/dashboard` every 2 s while mounted and the document
// visible. Every section is computed independently on the server, so one that
// failed is "unavailable" in its own tile and the rest still draw. Links are
// handed to `onNavigate`; the shell owns the URL (written on user action only).
import { useEffect, useRef, useState } from "react";
import { spaLinkProps } from "@platform/lib/router";
import { formatBytes } from "@platform/lib/sysmon";
import { getDashboard, sectionOk, type Dashboard } from "./monitor-api";
import { monitorUrl, overviewTiles, type Tab } from "./monitor-net-lib";
import TimelineStack from "./TimelineStack";
import { useRecord, type RecordApi } from "./useRecord";
import { usePolled } from "./usePolled";

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
  const tiles = data ? overviewTiles(data) : [];
  const tl = data && sectionOk(data.timeline) ? data.timeline : null;
  const errors = data && sectionOk(data.errors) ? data.errors : null;
  const jobs = data && sectionOk(data.jobs) ? data.jobs : null;
  const calls = data && sectionOk(data.calls) ? data.calls : null;

  return (
    <div className="monitor-tab monitor-tab-scroll monitor-overview">
      {error && <p className="monitor-denied">Can't read the dashboard: {error}</p>}

      <section className={`monitor-verdict is-${v?.level ?? "ok"}`} aria-label="Verdict">
        <p className="monitor-verdict-head">{v ? v.headline : "Reading…"}</p>
        {v && v.reasons.length > 0 && (
          <ul className="monitor-verdict-reasons">
            {v.reasons.map((r) => (
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
            {record.recording
              ? `Recording… ${Math.ceil(record.status?.seconds_left ?? 0)}s`
              : "Record 60s"}
          </button>
          <button type="button" className="btn btn-secondary monitor-btn" disabled={!data} onClick={() => void doCopy()}>
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

      <div className="monitor-tiles">
        {tiles.map((t) => {
          const link = monitorUrl({ tab: t.tab as Tab });
          return (
            <a
              key={t.key}
              className={`monitor-tile is-${t.level}`}
              data-tile={t.key}
              href={link}
              onClick={go(link)}
            >
              <span className="monitor-tile-title">
                <span className="monitor-tile-dot" aria-hidden="true" />
                {t.title}
              </span>
              <span className="monitor-tile-value">{t.value}</span>
              <span className="monitor-tile-base">{t.baseline ?? " "}</span>
            </a>
          );
        })}
      </div>

      <section className="monitor-net-sec">
        <h2 className="monitor-sec-title">Last 10 minutes</h2>
        {tl ? (
          <TimelineStack points={tl.points} events={tl.events} />
        ) : (
          <p className="monitor-net-note">{data ? "Timeline unavailable." : "Reading…"}</p>
        )}
      </section>

      <div className="monitor-lists">
        <section className="monitor-list-card">
          <h2 className="monitor-sec-title">Errors and outages</h2>
          {errors ? (
            <ul className="monitor-list">
              {errors.outages.slice(-3).map((o, i) => (
                <li key={`o${i}`}>
                  Outage {o.t_down ? new Date(o.t_down * 1000).toLocaleString() : ""}
                </li>
              ))}
              {errors.crashes.slice(0, 3).map((c) => (
                <li key={c.name}>Crash dump {c.name}</li>
              ))}
              {errors.server_log_errors.slice(-5).map((l, i) => (
                <li key={`l${i}`} className="monitor-list-mono" title={l}>
                  {l}
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
    </div>
  );
}

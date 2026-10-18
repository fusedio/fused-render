// /monitor, Network tab — "which page is hogging the browser's connections?"
//
// The browser allows six HTTP/1.1 connections per host, shared by every window,
// so one page that holds them queues everyone else in the BROWSER, where the
// server cannot see it. Top to bottom: a summary strip; a Connections chart
// (the busiest client's simultaneous requests over the last minute, computed
// server-side from request intervals, plus any request held > 2 s right now); the source x route table, busiest first (click a row to filter the
// list to it); the live request list, DevTools-style; and the open WebSockets.
//
// DATA: `/api/system/net/*` polled every 2 s while the tab is mounted and the
// document visible. The request list polls with `since=<cursor>` and appends
// (merge by id, capped at 2000 rows); open requests tick a live timer. Pause
// freezes the list, not the summary. The monitor's own polling requests are
// recorded by the server but hidden unless "show monitor's own requests" is on.
// `?source=&route=` seed the filters once, at mount; the URL is never written
// from here.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getNetGroups,
  getNetRequests,
  getNetSockets,
  getNetSummary,
  type NetFilters,
  type NetGroup,
  type NetRequests,
  type NetRow,
  type NetSocket,
  type NetSummary,
} from "./monitor-api";
import { AreaChart, type ChartPoint, type Tick } from "./Charts";
import { appendRows, ROW_LIMIT } from "./monitor-net-lib";
import { usePolled } from "./usePolled";

export interface NetworkApi {
  summary: () => Promise<NetSummary>;
  groups: () => Promise<{ groups: NetGroup[] }>;
  sockets: () => Promise<{ sockets: NetSocket[] }>;
  requests: (since: number, f: NetFilters) => Promise<NetRequests>;
}
const REAL_API: NetworkApi = {
  summary: getNetSummary,
  groups: () => getNetGroups(60),
  sockets: getNetSockets,
  requests: getNetRequests,
};

const OPEN_ONLY: NetFilters = {
  source: "",
  route: "",
  status: "",
  minMs: "",
  openOnly: true,
  own: false,
};
const SELF_PREFIX = "/api/system/";
const GROUP_TOP = 5;
const SLOTS = 6;

/** "never" only for exactly zero: a sub-100 ms stay at the limit still counts. */
function atLimitText(s: number | undefined): string {
  if (!s || s <= 0) return "never at the limit";
  return s < 0.1 ? "at the limit briefly" : `at the limit ${Math.round(s * 10) / 10} s`;
}
const LONG_HELD_MS = 2000;
const WINDOW_S = 60;
const CONN_TICKS: Tick[] = [
  { v: 0, label: "0" },
  { v: 3, label: "3" },
  { v: 6, label: "6 limit" },
];
const SLOTS_NOTE = "The browser opens at most 6 connections per host; extra requests wait in its queue.";
const DASH = "—";
const ms = (n: number | null | undefined) => (n == null ? DASH : `${Math.round(n)}`);
const STATUS_CLASSES = ["", "2xx", "3xx", "4xx", "5xx", "open"];
/** How long typing in a text filter settles before it reaches the poll. */
export const FILTER_DEBOUNCE_MS = 300;
type TextFilters = Pick<NetFilters, "source" | "route" | "minMs">;
/** Rows drawn at once (newest first); the full 2000 stay in memory. */
const DRAW_CAP = 300;

/** Age of a request in ms: finished rows carry it, open ones tick. */
function heldMs(r: NetRow, nowS: number): number | null {
  if (r.status == null && r.outcome == null) return Math.max(0, (nowS - r.t) * 1000);
  return r.held_ms;
}

function clock(t: number): string {
  const d = new Date(t * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const WAIT_WARN_MS = 100;

export default function NetworkTab({
  api = REAL_API,
  pollMs = 2_000,
  initialSource = "",
  initialRoute = "",
  now = () => Date.now() / 1000,
}: {
  api?: NetworkApi;
  pollMs?: number;
  initialSource?: string;
  initialRoute?: string;
  now?: () => number;
}) {
  const [summary, setSummary] = useState<NetSummary | null>(null);
  const [groups, setGroups] = useState<NetGroup[]>([]);
  const [sockets, setSockets] = useState<NetSocket[]>([]);
  const [rows, setRows] = useState<NetRow[]>([]);
  // Every open request, whatever the list's filters say: the held-requests
  // line is about the connection pool, not about what the list is narrowed to.
  const [openRows, setOpenRows] = useState<NetRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  // The request log is collapsed unless a deep link names a page or route.
  const [logOpen, setLogOpen] = useState(Boolean(initialSource || initialRoute));
  const [showAllGroups, setShowAllGroups] = useState(false);
  const [filters, setFilters] = useState<NetFilters>({
    source: initialSource,
    route: initialRoute,
    status: "",
    minMs: "",
    openOnly: false,
    own: false,
  });
  // The three text inputs type into a draft and reach `filters` (and so the
  // poll) after FILTER_DEBOUNCE_MS; toggles, the select and group-row clicks
  // commit at once and also overwrite the draft.
  const [draft, setDraft] = useState<TextFilters>({
    source: initialSource,
    route: initialRoute,
    minMs: "",
  });
  const draftRef = useRef(draft);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cursor = useRef(0);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  // A filter change throws the list away and starts again from cursor 0.
  const filterKey = JSON.stringify(filters);
  const logOpenRef = useRef(logOpen);
  logOpenRef.current = logOpen;
  useEffect(() => {
    cursor.current = 0;
    setRows([]);
  }, [filterKey, logOpen]);

  // The live timer: re-render a few times a second while anything is open.
  const [clockS, setClockS] = useState(now());
  const anyOpen = rows.some((r) => r.status == null && r.outcome == null) || openRows.length > 0;
  useEffect(() => {
    if (!anyOpen) return;
    const id = setInterval(() => setClockS(now()), 250);
    return () => clearInterval(id);
  }, [anyOpen, now]);

  usePolled(
    async () => {
      const f = filtersRef.current;
      const [s, g, k] = await Promise.all([api.summary(), api.groups(), api.sockets()]);
      setSummary(s);
      setGroups(g.groups);
      setSockets(k.sockets);
      setError(null);
      setOpenRows((await api.requests(0, OPEN_ONLY)).rows);
      if (logOpenRef.current && !pausedRef.current) {
        const since = cursor.current;
        const req = await api.requests(since, f);
        // A filter change while this was in flight: the answer is stale.
        if (filtersRef.current === f) {
          cursor.current = req.cursor;
          if (req.rows.length) setRows((cur) => appendRows(cur, req.rows, ROW_LIMIT));
          setClockS(now());
        }
      }
    },
    pollMs,
    `${filterKey}|${logOpen}`,
    (e) => setError(e.message),
  );

  const cancelDebounce = () => {
    if (debounce.current !== null) clearTimeout(debounce.current);
    debounce.current = null;
  };
  useEffect(() => cancelDebounce, []);

  const typeFilter = useCallback((patch: Partial<TextFilters>) => {
    draftRef.current = { ...draftRef.current, ...patch };
    setDraft(draftRef.current);
    cancelDebounce();
    debounce.current = setTimeout(() => {
      debounce.current = null;
      setFilters((cur) => ({ ...cur, ...draftRef.current }));
    }, FILTER_DEBOUNCE_MS);
  }, []);

  const setFilter = useCallback((patch: Partial<NetFilters>) => {
    if ("source" in patch || "route" in patch || "minMs" in patch) {
      cancelDebounce();
      draftRef.current = {
        source: patch.source ?? draftRef.current.source,
        route: patch.route ?? draftRef.current.route,
        minMs: patch.minMs ?? draftRef.current.minMs,
      };
      setDraft(draftRef.current);
      // The cancelled debounce may have held typed text for another field:
      // commit the whole draft so the list matches what the inputs show.
      setFilters((cur) => ({ ...cur, ...draftRef.current, ...patch }));
      return;
    }
    setFilters((cur) => ({ ...cur, ...patch }));
  }, []);

  const client = summary?.main_client ?? null;
  const conc = summary?.concurrency?.find((c) => c.client === client) ?? null;
  const heldNow = useMemo(
    () =>
      openRows
        .filter((r) => !r.self && r.client === client)
        .map((r) => ({ r, ms: heldMs(r, clockS) ?? 0 }))
        .filter((h) => h.ms > LONG_HELD_MS)
        .sort((a, b) => b.ms - a.ms),
    [openRows, client, clockS],
  );
  const connPoints: ChartPoint[] = useMemo(() => {
    const series = conc?.series ?? [];
    const end = now();
    return series.map((v, i) => ({ t: end - (series.length - 1 - i), v }));
  }, [conc, now]);
  // The monitor's own requests (the same /api/system/ rule the server marks
  // `self` with) stay out of the table unless the toggle is on.
  const shownGroups = useMemo(
    () => (filters.own ? groups : groups.filter((g) => !g.route.startsWith(SELF_PREFIX))),
    [groups, filters.own],
  );
  const visibleGroups = showAllGroups ? shownGroups : shownGroups.slice(0, GROUP_TOP);
  const drawn = useMemo(() => [...rows].reverse().slice(0, DRAW_CAP), [rows]);

  const p95 = summary?.browser_queue_p95_ms ?? null;

  return (
    <div className="monitor-tab monitor-tab-scroll monitor-net">
      {error && <p className="monitor-denied">Can't read request data: {error}</p>}

      <div className="monitor-net-strip" aria-label="Request summary">
        <Stat label="Requests/s" value={summary ? String(summary.rps_10s) : DASH} sub="last 10 s" />
        <Stat
          warn={p95 != null && p95 >= WAIT_WARN_MS}
          label="Browser wait p95"
          value={summary?.browser_queue_p95_ms == null ? DASH : `${ms(summary.browser_queue_p95_ms)} ms`}
          sub="time waiting for a connection"
        />
        {summary && summary.errors_60s > 0 && (
          <Stat label="Errors/min" value={String(summary.errors_60s)} sub="5xx and failures" />
        )}
      </div>

      <section className="monitor-net-sec">
        <div
          className={`monitor-card monitor-net-conn${conc && conc.at_limit_s > 0 ? " is-warn" : ""}`}
          data-card="connections"
        >
          <header className="monitor-card-head">
            <h2 title={SLOTS_NOTE}>Connections</h2>
            <span className="monitor-card-figure">{conc ? `peak ${conc.peak} of ${SLOTS}` : DASH}</span>
          </header>
          <div className="monitor-card-sub">
            <span>
              {`last 60 s · ${
                atLimitText(conc?.at_limit_s)
              }`}
            </span>
          </div>
          <AreaChart
            points={connPoints}
            max={Math.max(SLOTS, conc?.peak ?? 0)}
            ticks={CONN_TICKS}
            windowS={WINDOW_S}
            label="Connections, last 60 s"
          />
        </div>
        {heldNow.length > 0 && (
          <ul className="monitor-net-held">
            {heldNow.map(({ r, ms: held }) => (
              <li key={r.id}>{`${r.method} ${r.route} · from ${r.source} · ${Math.round(held / 1000)} s`}</li>
            ))}
          </ul>
        )}
      </section>

      <section className="monitor-net-sec">
        <h2 className="monitor-sec-title">By page and route · last minute</h2>
        <div className="monitor-table-wrap monitor-net-table-wrap">
          <table className="monitor-table monitor-net-table">
            <thead>
              <tr>
                <th>Page</th>
                <th>Route</th>
                <th className="monitor-c-num">Calls/min</th>
                <th className="monitor-c-num">Server p95</th>
                <th className="monitor-c-num">Browser wait</th>
              </tr>
            </thead>
            <tbody>
              {visibleGroups.map((g) => {
                const on = filters.source === g.source && filters.route === g.route;
                return (
                  <tr
                    key={`${g.source}\n${g.route}`}
                    className={`monitor-row monitor-group-row${on ? " is-selected" : ""}`}
                    data-group={`${g.source} ${g.route}`}
                    onClick={() => {
                      setFilter(on ? { source: "", route: "" } : { source: g.source, route: g.route });
                      if (!on) setLogOpen(true);
                    }}
                  >
                    <td>{g.source}</td>
                    <td className="monitor-net-route">
                      {g.route}
                      {g.errors > 0 && <span className="monitor-net-err">{g.errors} err</span>}
                    </td>
                    <td className="monitor-c-num">{g.per_min}</td>
                    <td className="monitor-c-num">{ms(g.p95_ms)}</td>
                    <td className="monitor-c-num">{ms(g.queue_p95_ms)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!shownGroups.length && <div className="monitor-empty">No requests in the last minute.</div>}
        </div>
        {shownGroups.length > GROUP_TOP && (
          <button
            type="button"
            className="monitor-more"
            aria-expanded={showAllGroups}
            onClick={() => setShowAllGroups((v) => !v)}
          >
            {showAllGroups ? "Show fewer" : `Show all (${shownGroups.length})`}
          </button>
        )}
      </section>

      <section className="monitor-net-sec">
        <h2 className="monitor-sec-title">Request log</h2>
        <button type="button" className="monitor-more" aria-expanded={logOpen} onClick={() => setLogOpen((v) => !v)}>
          {logOpen ? "Hide request log" : "Show request log"}
        </button>
        {logOpen && (
          <>
            <div className="monitor-toolbar monitor-net-filters">
              <input
                className="field-control monitor-net-input"
                placeholder="Page"
                aria-label="Filter by page"
                value={draft.source}
                onChange={(e) => typeFilter({ source: e.target.value })}
              />
              <input
                className="field-control monitor-net-input"
                placeholder="Route"
                aria-label="Filter by route"
                value={draft.route}
                onChange={(e) => typeFilter({ route: e.target.value })}
              />
              <select
                className="field-control monitor-net-select"
                aria-label="Filter by status"
                value={filters.status}
                onChange={(e) => setFilter({ status: e.target.value })}
              >
                {STATUS_CLASSES.map((s) => (
                  <option key={s} value={s}>
                    {s || "any status"}
                  </option>
                ))}
              </select>
              <input
                className="field-control monitor-net-input monitor-net-min"
                placeholder="min ms"
                aria-label="Minimum server ms"
                inputMode="numeric"
                value={draft.minMs}
                onChange={(e) => typeFilter({ minMs: e.target.value })}
              />
              <button
                type="button"
                className={`monitor-toggle${filters.openOnly ? " is-on" : ""}`}
                aria-pressed={filters.openOnly}
                onClick={() => setFilter({ openOnly: !filters.openOnly })}
              >
                open only
              </button>
              <button
                type="button"
                className={`monitor-toggle${filters.own ? " is-on" : ""}`}
                aria-pressed={filters.own}
                onClick={() => setFilter({ own: !filters.own })}
              >
                show monitor's own requests
              </button>
              <span className="monitor-spacer" />
              <span className="monitor-count">
                {rows.length} requests
                {rows.length >= ROW_LIMIT ? ` (newest ${ROW_LIMIT})` : ""}
              </span>
              <button
                type="button"
                className={`monitor-toggle${paused ? " is-on" : ""}`}
                aria-pressed={paused}
                onClick={() => setPaused(!paused)}
              >
                {paused ? "Resume" : "Pause"}
              </button>
            </div>
            <div className="monitor-table-wrap monitor-net-table-wrap monitor-net-list">
              <table className="monitor-table monitor-net-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Route</th>
                    <th>Page</th>
                    <th className="monitor-c-num">Status</th>
                    <th className="monitor-c-num">ms</th>
                  </tr>
                </thead>
                <tbody>
                  {drawn.map((r) => {
                    const open = r.status == null && r.outcome == null;
                    const held = heldMs(r, clockS);
                    const bad = (r.status ?? 0) >= 500 || r.outcome === "error";
                    return (
                      <tr
                        key={r.id}
                        className={`monitor-row monitor-req-row${open ? " is-open" : ""}${bad ? " is-bad" : ""}`}
                        data-req={r.id}
                      >
                        <td>{clock(r.t)}</td>
                        <td className="monitor-net-route" title={r.path}>
                          {r.method === "GET" ? r.route : `${r.method} ${r.route}`}
                        </td>
                        <td>{r.source}</td>
                        <td className="monitor-c-num">{open ? "open" : (r.status ?? r.outcome ?? DASH)}</td>
                        <td className="monitor-c-num">
                          {open ? `${((held ?? 0) / 1000).toFixed(1)} s` : ms(r.server_ms ?? held)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {!drawn.length && <div className="monitor-empty">No matching requests yet.</div>}
            </div>
          </>
        )}
      </section>

      <section className="monitor-net-sec">
        <h2 className="monitor-sec-title">WebSockets</h2>
        <div className="monitor-table-wrap monitor-net-table-wrap">
          <table className="monitor-table monitor-net-table">
            <thead>
              <tr>
                <th>Kind</th>
                <th>Page</th>
                <th className="monitor-c-num">Open</th>
                <th className="monitor-c-num">Msgs/s</th>
              </tr>
            </thead>
            <tbody>
              {sockets.map((s) => (
                <tr key={`${s.kind}\n${s.source}`} className="monitor-row">
                  <td>{s.kind}</td>
                  <td>{s.source}</td>
                  <td className="monitor-c-num">{s.open}</td>
                  <td className="monitor-c-num">{s.msgs_per_s}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!sockets.length && <div className="monitor-empty">No open WebSockets.</div>}
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value, sub, warn }: { label: string; value: string; sub: string; warn?: boolean }) {
  return (
    <div className={`monitor-net-stat${warn ? " is-warn" : ""}`}>
      <div className="monitor-net-stat-label">{label}</div>
      <div className="monitor-net-stat-value">{value}</div>
      <div className="monitor-net-stat-sub">{sub}</div>
    </div>
  );
}

// /monitor — a process monitor in the shell's own theme (Akshil: "a shell
// interface with pytop data, but our theme UI ... memory and cpu for now, and
// give me actions like kill process"). Entered from the System chip's "Open
// Monitor"; offered only with the `monitor_enabled` pref on (App.tsx).
//
// OPENS ON FUSED-RENDER'S OWN PROCESSES — the app and everything it runs —
// with "fused-render only" pressed (Akshil 2026-10-04: "show only fused render
// apps and processes by default"); the toggle widens to the whole machine. The
// data is the whole machine either way (`?scope=all`), so widening is instant.
//
// Top to bottom: CPU and Memory meters (real charts, last two minutes) and,
// with "fused-render only" on, what fused-render itself costs; a toolbar
// (search, fused-render only, count, Pause, last update — and, with rows
// selected, the ACTIONS: "N selected · Kill · Force kill · Clear"); the
// process table; and — once rows are selected — a dock with a per-process
// CPU/Memory graph for up to five of them.
//
// ACTIONS LIVE IN THE TOOLBAR, NOT IN ROWS (Akshil: "have kill button on top
// rather than in that row because rows jump which makes it harder to click").
// Selecting is the only path to an action: click a row, Cmd-click to add,
// Shift-click a range, the header box for everything shown.
//
// THE ORDER HOLDS while the pointer is over the table or anything is selected:
// figures keep updating in place but no row moves (new processes append at
// the bottom, exited ones drop out). It re-sorts when the pointer leaves with
// nothing selected, on a column-header click, or when Pause is lifted; the
// toolbar says "order held" meanwhile.
//
// DATA: one subscription to `system.activity` with `{ scope: "all" }` on the
// document's events-bus socket (platform/lib/events) while the page is
// mounted — the server pushes `GET /api/system/activity?scope=all`'s body on
// subscribe and on every sampler tick after, and only samples the whole
// machine while someone is subscribed. The client drops the subscription
// while the document is hidden and resubscribes on return (the snapshot that
// answers is the catch-up). Nothing here fetches on a timer (D3): the 2 s
// poll, its visibility restart and the per-process history's own 2 s poll
// are gone. The per-process history is a query with arguments
// (`/history?pids=`), so it stays a GET — re-asked each time a new activity
// snapshot lands, which is exactly when its answer can have moved (D11).
// Pause freezes what is DRAWN, never the subscription, so un-pausing is
// instant and the 3 s "still running?" check after a kill keeps seeing live
// data.
//
// ENDING A PROCESS: fused-render's own rows with an owner (Claude, engines,
// models, terminals) go through the owner-aware `POST /stop` and read "Stop";
// every other row is signalled through `POST /kill` (SIGTERM; "Force kill" is
// SIGKILL). The app's own row cannot be selected. The toolbar confirms inline
// ("Kill 2 processes? [Kill] [Cancel]") and auto-cancels after 4 s.
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { isMac } from "@platform/lib/platform";
import { notify } from "@platform/lib/notifications";
import { subscribeTopic } from "@platform/lib/events";
import {
  formatBytes,
  formatCpu,
  getProcHistory,
  type MachineSnapshot,
  type MachineProc,
  type ProcHistoryPoint,
} from "@platform/lib/sysmon";
import { useTween } from "./useTween";
import { AreaChart, Legend, LinesChart, type LineSeries, type Tick } from "./Charts";
import {
  CONFIRM_MS,
  DEFAULT_SORT,
  EMPTY_SELECTION,
  GRAPH_CAP,
  KIND_CHIP,
  ROW_CAP,
  STILL_ALIVE_MS,
  bulkVerb,
  clickSelect,
  confirmQuestion,
  cpuTone,
  displayName,
  endProcess,
  endRoute,
  filterProcs,
  fusedTotals,
  holdOrder,
  isThisApp,
  memOfTotal,
  nextSort,
  niceCeil,
  sortProcs,
  summarizeResults,
  type EndResult,
  type Selection,
  type SortKey,
  type SortState,
} from "./monitor-lib";

const GIB = 1024 ** 3;
const MIB = 1024 ** 2;
const METER_WINDOW_S = 120;
const PROC_WINDOW_S = 60;
const DASH = "—";
const NO_PIDS: Set<number> = new Set();

// A row's state after an action from the toolbar, drawn as a small tag beside
// its name. `token` ties the denial's clear-timer to the one denial it was
// armed for, the same way the toolbar's confirm timer is tied.
type RowState =
  | { phase: "busy" }
  | { phase: "denied"; token: number }
  | { phase: "still" }
  | { phase: "error"; message: string };

function clock(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ---- meters ------------------------------------------------------------------------

const PCT_TICKS: Tick[] = [
  { v: 0, label: "0%" },
  { v: 50, label: "50%" },
  { v: 100, label: "100%" },
];

function CpuMeter({ data }: { data: MachineSnapshot | null }) {
  const host = data?.host ?? null;
  const points = (data?.history ?? []).map((h) => ({ t: h.t, v: h.cpuPct }));
  return (
    <section className="monitor-card" aria-label="CPU">
      <header className="monitor-card-head">
        <h2>CPU</h2>
        <span className="monitor-card-figure">{formatCpu(useTween(host?.cpuPct ?? null))}</span>
      </header>
      <div className="monitor-card-sub">
        <span>user {formatCpu(host?.cpuUser ?? null)}</span>
        <span>system {formatCpu(host?.cpuSystem ?? null)}</span>
        {host?.load && <span>load {host.load.map((l) => l.toFixed(2)).join(" ")}</span>}
        {host && <span>{host.ncpu} cores</span>}
      </div>
      <AreaChart
        points={points}
        max={100}
        ticks={PCT_TICKS}
        windowS={METER_WINDOW_S}
        label="Whole-machine CPU, last two minutes"
      />
    </section>
  );
}

function MemoryMeter({ data }: { data: MachineSnapshot | null }) {
  const host = data?.host ?? null;
  const total = host?.memTotal ?? 0;
  const points = (data?.history ?? []).map((h) => ({ t: h.t, v: h.memUsed }));
  const gb = total ? Math.round(total / GIB) : 0;
  const ticks: Tick[] = total
    ? [
        { v: 0, label: "0" },
        { v: total / 2, label: `${gb / 2} GB` },
        { v: total, label: `${gb} GB` },
      ]
    : [];
  const parts = [
    { key: "app", label: "App", bytes: host?.memApp ?? null },
    { key: "wired", label: "Wired", bytes: host?.memWired ?? null },
    { key: "compressed", label: "Compressed", bytes: host?.memCompressed ?? null },
  ].filter((p) => p.bytes !== null);
  return (
    <section className="monitor-card" aria-label="Memory">
      <header className="monitor-card-head">
        <h2>Memory</h2>
        <span className="monitor-card-figure">{memOfTotal(useTween(host?.memUsed ?? null), host?.memTotal ?? null)}</span>
      </header>
      {total > 0 && parts.length > 0 && (
        <div className="monitor-membar-row">
          <div className="monitor-membar" role="img" aria-label="Memory used, by kind">
            {parts.map((p) => (
              <span
                key={p.key}
                className={`monitor-membar-seg monitor-mem-${p.key}`}
                style={{ width: `${((p.bytes as number) / total) * 100}%` }}
              />
            ))}
          </div>
          <ul className="monitor-memlegend">
            {parts.map((p) => (
              <li key={p.key} className={`monitor-mem-${p.key}`}>
                <span className="monitor-swatch" aria-hidden="true" />
                {p.label} {formatBytes(p.bytes, true)}
              </li>
            ))}
          </ul>
        </div>
      )}
      <AreaChart
        points={points}
        max={total || 1}
        ticks={ticks}
        windowS={METER_WINDOW_S}
        label="Whole-machine memory used, last two minutes"
      />
    </section>
  );
}

function FusedStat({ procs }: { procs: MachineProc[] }) {
  const t = fusedTotals(procs);
  return (
    <section className="monitor-card monitor-card-stat" aria-label="fused-render's own cost">
      <header className="monitor-card-head">
        <h2>fused-render</h2>
      </header>
      <div className="monitor-stat">
        <div>
          <span className="monitor-stat-k">CPU</span>
          <span className="monitor-stat-v">{formatCpu(useTween(t.cpuPct))}</span>
        </div>
        <div>
          <span className="monitor-stat-k">Memory</span>
          <span className="monitor-stat-v">{formatBytes(useTween(t.memBytes))}</span>
        </div>
        <div>
          <span className="monitor-stat-k">Processes</span>
          <span className="monitor-stat-v">{procs.filter((p) => p.fused).length}</span>
        </div>
      </div>
    </section>
  );
}

// ---- table ---------------------------------------------------------------------------

const COLUMNS: { key: SortKey; label: string; numeric?: boolean }[] = [
  { key: "name", label: "Process" },
  { key: "pid", label: "PID", numeric: true },
  { key: "user", label: "User" },
  { key: "cpu", label: "% CPU", numeric: true },
  { key: "mem", label: "Memory", numeric: true },
];

const ProcRow = memo(function ProcRow({
  p,
  selected,
  fresh,
  state,
  onSelect,
}: {
  p: MachineProc;
  selected: boolean;
  /** First listed in the latest snapshot: fades in once. */
  fresh: boolean;
  state: RowState | undefined;
  onSelect: (pid: number, mods: { toggle: boolean; range: boolean }) => void;
}) {
  const name = displayName(p);
  const app = isThisApp(p);
  const onRow = (e: ReactMouseEvent) => {
    if (app) return;
    onSelect(p.pid, { toggle: e.metaKey || e.ctrlKey, range: e.shiftKey });
  };
  const tone = cpuTone(p.cpuPct);
  const chip = p.fused ? KIND_CHIP[p.kind] : undefined;
  const tag =
    state?.phase === "busy"
      ? { cls: "is-muted", text: "Ending…" }
      : state?.phase === "denied"
        ? { cls: "is-error", text: "Permission denied" }
        : state?.phase === "error"
          ? { cls: "is-error", text: state.message }
          : state?.phase === "still"
            ? { cls: "is-warn", text: "Still running" }
            : null;
  return (
    <tr
      className={`monitor-row${selected ? " is-selected" : ""}${app ? " is-app" : ""}${fresh ? " is-new" : ""}${state?.phase === "busy" ? " is-ending" : ""}`}
      data-pid={p.pid}
      onClick={onRow}
    >
      <td className="monitor-c-check">
        <input
          type="checkbox"
          checked={selected}
          disabled={app}
          title={app ? "This app" : undefined}
          aria-label={app ? "This app" : `Select ${name}`}
          onChange={() => {}}
          onClick={(e) => {
            e.stopPropagation();
            onSelect(p.pid, { toggle: true, range: e.shiftKey });
          }}
        />
      </td>
      <td className="monitor-c-name" title={p.args}>
        <span className="monitor-name">{name}</span>
        {chip && <span className={`monitor-kind monitor-kind-${p.kind}`}>{chip}</span>}
        {tag && <span className={`monitor-rowtag ${tag.cls}`}>{tag.text}</span>}
      </td>
      <td className="monitor-c-num">{p.pid}</td>
      <td className="monitor-c-user">{p.user ?? DASH}</td>
      <td className={`monitor-c-num${tone ? ` monitor-cpu-${tone}` : ""}`}>
        {p.cpuPct === null ? DASH : p.cpuPct.toFixed(1)}
      </td>
      <td className="monitor-c-num">{p.memBytes === null ? DASH : formatBytes(p.memBytes)}</td>
    </tr>
  );
});

// ---- per-process graph ------------------------------------------------------------------

function memTicks(maxBytes: number): { max: number; ticks: Tick[] } {
  // GB once the line is near 1 GB, so a ~950 MB process reads
  // "0 · 0.5 GB · 1 GB", never "1000 MB · 2000 MB".
  const raw = maxBytes * 1.1;
  const unit = raw >= 0.75 * GIB ? GIB : MIB;
  const suffix = unit === GIB ? "GB" : "MB";
  const max = niceCeil(Math.max(raw, unit) / unit) * unit;
  const fmt = (v: number) => `${+(v / unit).toFixed(1)} ${suffix}`;
  return { max, ticks: [0, max / 2, max].map((v) => ({ v, label: v ? fmt(v) : "0" })) };
}

function ProcGraphs({
  procs,
  history,
}: {
  procs: MachineProc[];
  history: Record<string, ProcHistoryPoint[]>;
}) {
  const shown = procs.slice(0, GRAPH_CAP);
  const cpu: LineSeries[] = [];
  const mem: LineSeries[] = [];
  let end = 0;
  let maxCpu = 0;
  let maxMem = 0;
  for (const p of shown) {
    const h = history[String(p.pid)] ?? [];
    const label = `${displayName(p)} (${p.pid})`;
    cpu.push({ key: String(p.pid), label, points: h.map((x) => ({ t: x.t, v: x.cpuPct })) });
    mem.push({ key: String(p.pid), label, points: h.map((x) => ({ t: x.t, v: x.memBytes })) });
    for (const x of h) {
      end = Math.max(end, x.t);
      maxCpu = Math.max(maxCpu, x.cpuPct ?? 0);
      maxMem = Math.max(maxMem, x.memBytes ?? 0);
    }
  }
  const cpuMax = niceCeil(Math.max(10, maxCpu * 1.1));
  const m = memTicks(maxMem);
  const unmeasured = shown.filter((p) => p.memBytes === null).length;
  return (
    <div className="monitor-graphs">
      <div className="monitor-graph">
        <div className="monitor-graph-title">% CPU · last minute</div>
        <LinesChart
          series={cpu}
          max={cpuMax}
          ticks={[0, cpuMax / 2, cpuMax].map((v) => ({ v, label: `${+v.toFixed(1)}%` }))}
          windowS={PROC_WINDOW_S}
          end={end}
          label="CPU of the selected processes, last minute"
        />
      </div>
      <div className="monitor-graph">
        <div className="monitor-graph-title">Memory · last minute</div>
        <LinesChart
          series={mem}
          max={m.max}
          ticks={m.ticks}
          windowS={PROC_WINDOW_S}
          end={end}
          label="Memory of the selected processes, last minute"
        />
      </div>
      <div className="monitor-graph-legend">
        <Legend series={cpu} />
        {procs.length > GRAPH_CAP && (
          <div className="monitor-muted">Graphing the first {GRAPH_CAP} of {procs.length} selected.</div>
        )}
        {unmeasured > 0 && (
          <div className="monitor-muted">
            {unmeasured === 1 ? "One is" : `${unmeasured} are`} another user's — no figures.
          </div>
        )}
      </div>
    </div>
  );
}

// ---- the page --------------------------------------------------------------------------

export default function MonitorPage({
  loadHistory = getProcHistory,
  confirmMs = CONFIRM_MS,
}: {
  loadHistory?: (pids: number[]) => Promise<Record<string, ProcHistoryPoint[]>>;
  confirmMs?: number;
} = {}) {
  const [data, setData] = useState<MachineSnapshot | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [query, setQuery] = useState("");
  // fused-render's own processes by default (Akshil 2026-10-04: "show only
  // fused render apps and processes by default"); the toolbar toggle widens
  // to the whole machine.
  const [fusedOnly, setFusedOnly] = useState(true);
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT);
  const [sel, setSel] = useState<Selection>(EMPTY_SELECTION);
  const [rows, setRows] = useState<Record<number, RowState>>({});
  // An ARMED confirm: the exact processes it will end, snapshotted (with their
  // start stamps) the moment it was armed. Any selection change disarms it.
  const [bulk, setBulk] = useState<{
    force: boolean;
    token: number;
    verb: "Kill" | "Stop";
    targets: MachineProc[];
  } | null>(null);
  const tokens = useRef(0);
  const confirmMsRef = useRef(confirmMs);
  confirmMsRef.current = confirmMs;
  const [history, setHistory] = useState<Record<string, ProcHistoryPoint[]>>({});

  const latest = useRef<MachineSnapshot | null>(null);
  // Which pids the DRAWN snapshot added, for the row enter fade. Only the
  // snapshot a pid first appears in carries the class, so a row React merely
  // moves on a re-sort never replays it; the first snapshot marks nothing.
  const [fresh, setFresh] = useState<Set<number>>(NO_PIDS);
  const drawnPids = useRef<Set<number> | null>(null);
  const draw = useCallback((d: MachineSnapshot) => {
    const cur = new Set(d.procs.map((p) => p.pid));
    const prev = drawnPids.current;
    drawnPids.current = cur;
    setFresh(prev ? new Set([...cur].filter((pid) => !prev.has(pid))) : NO_PIDS);
    setData(d);
    setUpdatedAt(Date.now());
  }, []);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const timers = useRef(new Set<ReturnType<typeof setTimeout>>());
  useEffect(() => {
    const set = timers.current;
    return () => set.forEach(clearTimeout);
  }, []);
  const later = useCallback((ms: number, fn: () => void) => {
    const id = setTimeout(() => {
      timers.current.delete(id);
      fn();
    }, ms);
    timers.current.add(id);
  }, []);

  // Which activity snapshot is the latest, for the history re-ask below: a
  // counter rather than the snapshot itself, so a paused page (which does not
  // draw) still re-asks on the server's tick.
  const [activityTick, setActivityTick] = useState(0);
  useEffect(() => {
    return subscribeTopic<MachineSnapshot>("system.activity", { scope: "all" }, (snap, _delta, meta) => {
      if (meta.error) {
        setError(meta.error);
        return;
      }
      if (snap === null) return;
      latest.current = snap;
      setError(null);
      if (!pausedRef.current) draw(snap);
      setActivityTick((t) => t + 1);
    });
  }, [draw]);

  const togglePause = () => {
    if (paused) {
      if (latest.current) draw(latest.current);
      resort();
    }
    setPaused(!paused);
  };

  const procs = useMemo(() => data?.procs ?? [], [data]);
  const byPid = useMemo(() => new Map(procs.map((p) => [p.pid, p])), [procs]);
  const sortedAll = useMemo(
    () => sortProcs(filterProcs(procs, query, fusedOnly), sort),
    [procs, query, fusedOnly, sort],
  );
  // THE HELD ORDER (see the header): `held` is the last order drawn while
  // holding; `holdEpoch` bumps to drop it (header click, Pause lifted).
  const [hovering, setHovering] = useState(false);
  const [held, setHeld] = useState<number[] | null>(null);
  // Only a selection that is still on screen holds the order: once the
  // selected processes have all been killed, it lets go.
  const holdingSel = [...sel.pids].some((pid) => {
    const p = byPid.get(pid);
    return !!p && (!fusedOnly || p.fused);
  });
  const frozen = hovering || holdingSel;
  // Derived in render, remembered in an effect: while frozen, the order drawn
  // becomes the next render's `held`; unfrozen (or after resort()), null.
  const shownAll = useMemo(() => {
    if (!frozen || held === null) return sortedAll;
    const byPidHere = new Map(sortedAll.map((p) => [p.pid, p]));
    return holdOrder(held, sortedAll.map((p) => p.pid)).map((pid) => byPidHere.get(pid) as MachineProc);
  }, [sortedAll, frozen, held]);
  useEffect(() => {
    const next = frozen ? shownAll.map((p) => p.pid) : null;
    setHeld((cur) => (sameOrder(cur, next) ? cur : next));
  }, [frozen, shownAll]);
  const resort = useCallback(() => setHeld(null), []);
  const shown = shownAll.length > ROW_CAP ? shownAll.slice(0, ROW_CAP) : shownAll;
  // Shift-click ranges run over the rows as drawn, minus the app's own row,
  // which is never selectable.
  const order = useMemo(() => shown.filter((p) => !isThisApp(p)).map((p) => p.pid), [shown]);
  const scopeCount = fusedOnly ? procs.filter((p) => p.fused).length : procs.length;

  const orderRef = useRef(order);
  orderRef.current = order;

  // The selection only ever counts rows that are still listed and in scope
  // (fused-render only narrows it; a process that exits drops out), in the
  // order the table draws them. A search does NOT drop a selected row.
  const selected = useMemo(() => {
    const rank = new Map(shownAll.map((p, i) => [p.pid, i]));
    return [...sel.pids]
      .map((pid) => byPid.get(pid))
      .filter((p): p is MachineProc => !!p && (!fusedOnly || p.fused))
      .sort((a, b) => (rank.get(a.pid) ?? Infinity) - (rank.get(b.pid) ?? Infinity));
  }, [sel, shownAll, byPid, fusedOnly]);
  const selectedKey = selected
    .slice(0, GRAPH_CAP)
    .map((p) => p.pid)
    .join(",");

  // Per-process history, only while something is selected: asked when the
  // selection changes and again on every activity snapshot (D11). An answer
  // that lands after the next ask began is dropped — it is already stale.
  const loadSelected = useMemo(
    () =>
      selectedKey
        ? () => loadHistory(selectedKey.split(",").map(Number))
        : null,
    [selectedKey, loadHistory],
  );
  useEffect(() => {
    if (!loadSelected) return;
    let live = true;
    loadSelected()
      .then((h) => {
        if (live && !pausedRef.current) setHistory(h);
      })
      .catch(() => {
        // Best effort: the graph keeps its last minute.
      });
    return () => {
      live = false;
    };
  }, [loadSelected, activityTick]);
  useEffect(() => {
    if (!selectedKey) setHistory({});
  }, [selectedKey]);

  // ---- row actions ----------------------------------------------------------

  const setRow = useCallback((pid: number, state: RowState | null) => {
    setRows((cur) => {
      const next = { ...cur };
      if (state) next[pid] = state;
      else delete next[pid];
      return next;
    });
  }, []);

  const watchStill = useCallback(
    (p: MachineProc) => {
      later(STILL_ALIVE_MS, () => {
        const now = latest.current?.procs.find((x) => x.pid === p.pid);
        setRow(p.pid, now && now.startedAt === p.startedAt ? { phase: "still" } : null);
      });
    },
    [later, setRow],
  );

  const settle = useCallback(
    (p: MachineProc, r: EndResult, force: boolean) => {
      if (r.outcome === "ended") {
        if (force) setRow(p.pid, null);
        else {
          setRow(p.pid, { phase: "busy" });
          watchStill(p);
        }
      } else if (r.outcome === "denied") {
        const token = ++tokens.current;
        setRow(p.pid, { phase: "denied", token });
        later(6_000, () => setRows((cur) => dropIfToken(cur, p.pid, token)));
      } else if (r.outcome === "gone") {
        setRow(p.pid, null);
      } else {
        setRow(p.pid, { phase: "error", message: r.message ?? "Refused" });
      }
    },
    [later, setRow, watchStill],
  );

  const onSelect = useCallback(
    (pid: number, mods: { toggle: boolean; range: boolean }) =>
      setSel((cur) => clickSelect(cur, orderRef.current, pid, mods)),
    [],
  );

  // ---- selection ----------------------------------------------------------------

  const selectable = shown.filter((p) => !isThisApp(p));
  const allSelected = selectable.length > 0 && selectable.every((p) => sel.pids.has(p.pid));
  const someSelected = selected.length > 0;
  const headCheck = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (headCheck.current) headCheck.current.indeterminate = someSelected && !allSelected;
  }, [someSelected, allSelected]);
  // A selection names PROCESSES, not pids: each selected pid's start stamp is
  // remembered, and a pid that comes back with a different one (recycled — a
  // new process) drops out of the selection.
  const selStamps = useRef(new Map<number, number | null>());
  useEffect(() => {
    const stamps = selStamps.current;
    const changed: number[] = [];
    for (const pid of sel.pids) {
      const p = byPid.get(pid);
      if (!stamps.has(pid)) {
        if (p) stamps.set(pid, p.startedAt);
      } else if (p && p.startedAt !== stamps.get(pid)) {
        changed.push(pid);
      }
    }
    for (const pid of [...stamps.keys()]) if (!sel.pids.has(pid)) stamps.delete(pid);
    if (changed.length) {
      for (const pid of changed) stamps.delete(pid);
      setSel((cur) => ({
        pids: new Set([...cur.pids].filter((pid) => !changed.includes(pid))),
        anchor: cur.anchor !== null && changed.includes(cur.anchor) ? null : cur.anchor,
      }));
    }
  }, [sel, byPid]);
  // Any change to what is selected — or to what the selection is filtered
  // by — disarms a pending confirm: it must end exactly what it asked about.
  useEffect(() => setBulk(null), [sel, fusedOnly, query]);

  const toggleAll = () =>
    setSel(allSelected ? EMPTY_SELECTION : { pids: new Set(selectable.map((p) => p.pid)), anchor: null });
  const clearSel = useCallback(() => {
    setSel(EMPTY_SELECTION);
    setBulk(null);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setBulk(null);
      setSel(EMPTY_SELECTION);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const toggleFusedOnly = () => {
    const next = !fusedOnly;
    setFusedOnly(next);
    if (next) {
      setSel((cur) => ({
        pids: new Set([...cur.pids].filter((pid) => byPid.get(pid)?.fused)),
        anchor: cur.anchor,
      }));
    }
  };

  const killable = selected.filter((p) => endRoute(p) !== null);
  const verb = bulkVerb(killable);
  const anyStill = killable.some((p) => rows[p.pid]?.phase === "still");
  const askBulk = (force: boolean) => {
    const token = ++tokens.current;
    setBulk({ force, token, verb, targets: killable.map((p) => ({ ...p })) });
    later(confirmMs, () => setBulk((cur) => (cur?.token === token ? null : cur)));
  };
  const runBulk = async () => {
    if (!bulk) return;
    const { force, targets, verb: v } = bulk;
    setBulk(null);
    for (const p of targets) setRow(p.pid, { phase: "busy" });
    const results = await Promise.all(targets.map((p) => endProcess(p, force)));
    notify({
      title: summarizeResults(results, force, v),
      detail: results.length === 1 ? `pid ${results[0].pid}` : undefined,
      tone: results.some((r) => r.outcome === "refused") ? "error" : "info",
    });
    results.forEach((r, i) => settle(targets[i], r, force));
  };

  const machine = isMac ? "this Mac" : "this computer";

  return (
    <div className="monitor-page">
      <header className="monitor-head">
        <div>
          <h1 className="monitor-title">Monitor</h1>
          <p className="monitor-subtitle">
            {fusedOnly
              ? "fused-render and the processes it runs: live CPU and memory."
              : `Every process on ${machine}: live CPU and memory.`}
          </p>
        </div>
      </header>

      <div className={`monitor-meters${fusedOnly ? " has-stat" : ""}`}>
        <CpuMeter data={data} />
        <MemoryMeter data={data} />
        {fusedOnly && <FusedStat procs={procs} />}
      </div>

      <div className="monitor-toolbar">
        <input
          type="search"
          className="field-control monitor-search"
          placeholder="Search name, command, user or PID"
          aria-label="Search processes"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button
          type="button"
          className={`monitor-toggle${fusedOnly ? " is-on" : ""}`}
          aria-pressed={fusedOnly}
          onClick={toggleFusedOnly}
        >
          fused-render only
        </button>
        <span className="monitor-count">
          {shownAll.length === scopeCount
            ? `${scopeCount} processes`
            : `${shownAll.length} of ${scopeCount} processes`}
          {shownAll.length > ROW_CAP && ` · showing the first ${ROW_CAP}`}
        </span>
        {frozen && (
          <span className="monitor-held" title="Rows keep their place while you point at or select them">
            order held
          </span>
        )}
        <span className="monitor-spacer" />
        <span className="monitor-updated">
          {error ? (
            <span className="monitor-denied">Can't read processes: {error}</span>
          ) : updatedAt ? (
            `${paused ? "Paused · " : ""}Updated ${clock(updatedAt)}`
          ) : (
            "Reading…"
          )}
        </span>
        <button
          type="button"
          className={`monitor-toggle${paused ? " is-on" : ""}`}
          aria-pressed={paused}
          onClick={togglePause}
        >
          {paused ? "Resume" : "Pause"}
        </button>
        {someSelected && (
          <div className="monitor-selbar" role="toolbar" aria-label="Selected processes">
            {bulk ? (
              <>
                <span
                  className="monitor-selq"
                  title={confirmQuestion(bulk.targets, bulk.force ? "Force kill" : bulk.verb)}
                >
                  {confirmQuestion(bulk.targets, bulk.force ? "Force kill" : bulk.verb)}
                </span>
                <button
                  type="button"
                  className="btn btn-danger monitor-btn"
                  disabled={!bulk.targets.length}
                  onClick={() => void runBulk()}
                >
                  {bulk.force ? "Force kill" : bulk.verb}
                </button>
                <button type="button" className="btn btn-secondary monitor-btn" onClick={() => setBulk(null)}>
                  Cancel
                </button>
              </>
            ) : (
              <>
                <span className="monitor-selcount">{selected.length} selected</span>
                <button
                  type="button"
                  className="btn btn-secondary monitor-btn"
                  disabled={!killable.length}
                  onClick={() => askBulk(false)}
                >
                  {verb}
                </button>
                <button
                  type="button"
                  className={`btn btn-secondary monitor-btn monitor-btn-danger${anyStill ? " is-suggested" : ""}`}
                  disabled={!killable.length}
                  title={anyStill ? "Still running after a kill — force it" : undefined}
                  onClick={() => askBulk(true)}
                >
                  Force kill
                </button>
                <button type="button" className="monitor-link-btn" onClick={clearSel}>
                  Clear
                </button>
              </>
            )}
          </div>
        )}
      </div>

      <div className="monitor-table-wrap">
        <table className="monitor-table">
          <colgroup>
            <col className="monitor-w-check" />
            <col />
            <col className="monitor-w-pid" />
            <col className="monitor-w-user" />
            <col className="monitor-w-cpu" />
            <col className="monitor-w-mem" />
          </colgroup>
          <thead>
            <tr>
              <th className="monitor-c-check">
                <input
                  ref={headCheck}
                  type="checkbox"
                  checked={allSelected}
                  disabled={!selectable.length}
                  aria-label="Select all shown processes"
                  onChange={toggleAll}
                />
              </th>
              {COLUMNS.map((c) => (
                <th
                  key={c.key}
                  className={c.numeric ? "monitor-c-num" : undefined}
                  aria-sort={
                    sort.key === c.key ? (sort.dir === "asc" ? "ascending" : "descending") : "none"
                  }
                >
                  <button
                    type="button"
                    className="monitor-sort"
                    onClick={() => {
                      setSort(nextSort(sort, c.key));
                      resort();
                    }}
                  >
                    {c.label}
                    <span className="monitor-sort-mark" aria-hidden="true">
                      {sort.key === c.key ? (sort.dir === "asc" ? "▲" : "▼") : ""}
                    </span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody onMouseEnter={() => setHovering(true)} onMouseLeave={() => setHovering(false)}>
            {shown.map((p) => (
              <ProcRow
                key={p.pid}
                p={p}
                selected={sel.pids.has(p.pid)}
                fresh={fresh.has(p.pid)}
                state={rows[p.pid]}
                onSelect={onSelect}
              />
            ))}
          </tbody>
        </table>
        {data && !shown.length && (
          <div className="monitor-empty">{query ? `Nothing matches “${query}”.` : "No processes."}</div>
        )}
        {!data && !error && <div className="monitor-empty">Reading processes…</div>}
      </div>

      {someSelected && (
        <div className="monitor-dock">
          <ProcGraphs procs={selected} history={history} />
        </div>
      )}
    </div>
  );
}

function sameOrder(a: number[] | null, b: number[] | null): boolean {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  return a.every((pid, i) => pid === b[i]);
}

/** Drop `pid`'s row state only if it is still the confirm/denial `token` armed. */
function dropIfToken(cur: Record<number, RowState>, pid: number, token: number): Record<number, RowState> {
  const st = cur[pid];
  return st && "token" in st && st.token === token ? omit(cur, pid) : cur;
}

function omit<T>(obj: Record<number, T>, key: number): Record<number, T> {
  const next = { ...obj };
  delete next[key];
  return next;
}

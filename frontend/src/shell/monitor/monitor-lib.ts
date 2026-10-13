// The /monitor page's pure rules: which rows show, in what order, which are
// selected, how one is ended and how a batch of results reads. No React, no
// network beyond `endProcess` (which only picks the endpoint), so the suite
// can pin each rule directly.
import {
  killSystemProcess,
  stopSystemProcess,
  type MachineProc,
} from "@platform/lib/sysmon";

/** Rows drawn at once; the rest are counted ("showing 400 of N"). */
export const ROW_CAP = 400;
/** Lines on the per-process graph. */
export const GRAPH_CAP = 5;
export const CONFIRM_MS = 4_000;
/** How long after a SIGTERM a still-listed process earns "Force kill". */
export const STILL_ALIVE_MS = 3_000;

/** fused-render's own kinds with an owner that can end them properly
 *  (`POST /stop`): an engine through engine_host, a model through the
 *  supervisor, a Claude run through its own cancel, a terminal through its
 *  session. Every other row is signalled (`POST /kill`). */
export const STOP_KINDS = new Set(["claude", "engine", "model", "terminal"]);

export type SortKey = "name" | "pid" | "user" | "cpu" | "mem";
export interface SortState {
  key: SortKey;
  dir: "asc" | "desc";
}
export const DEFAULT_SORT: SortState = { key: "cpu", dir: "desc" };
/** A header's first click: numbers start high, text starts at A. */
export const FIRST_DIR: Record<SortKey, SortState["dir"]> = {
  name: "asc",
  pid: "asc",
  user: "asc",
  cpu: "desc",
  mem: "desc",
};

export function nextSort(cur: SortState, key: SortKey): SortState {
  if (cur.key !== key) return { key, dir: FIRST_DIR[key] };
  return { key, dir: cur.dir === "asc" ? "desc" : "asc" };
}

/** The name a row shows: our own processes by their friendly label, the rest
 *  by command. */
export function displayName(p: MachineProc): string {
  return p.fused ? p.label : p.command;
}

export const KIND_CHIP: Partial<Record<MachineProc["kind"], string>> = {
  app: "App",
  claude: "Claude",
  engine: "Engine",
  model: "Model",
  terminal: "Terminal",
  install: "Install",
  run: "Run",
  other: "fused-render",
};

export function isThisApp(p: MachineProc): boolean {
  return p.fused && p.kind === "app";
}

/** Which endpoint ends `p`, or null for the app itself (never offered). */
export function endRoute(p: MachineProc): "stop" | "kill" | null {
  if (isThisApp(p)) return null;
  return p.fused && STOP_KINDS.has(p.kind) ? "stop" : "kill";
}

/** Case-insensitive substring over command, args, label, user and pid. */
export function matchesQuery(p: MachineProc, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return (
    p.command.toLowerCase().includes(q) ||
    p.args.toLowerCase().includes(q) ||
    p.label.toLowerCase().includes(q) ||
    (p.user ?? "").toLowerCase().includes(q) ||
    String(p.pid).includes(q)
  );
}

export function filterProcs(procs: MachineProc[], query: string, fusedOnly: boolean): MachineProc[] {
  return procs.filter((p) => (!fusedOnly || p.fused) && matchesQuery(p, query));
}

function sortValue(p: MachineProc, key: SortKey): number | string | null {
  switch (key) {
    case "name":
      return displayName(p).toLowerCase();
    case "pid":
      return p.pid;
    case "user":
      return p.user?.toLowerCase() ?? null;
    case "cpu":
      return p.cpuPct;
    case "mem":
      return p.memBytes;
  }
}

/** Sorted copy. A null figure (another user's process the kernel will not
 *  measure, or a first reading) sorts LAST in either direction; pid breaks
 *  ties so rows do not shuffle between polls. */
export function sortProcs(procs: MachineProc[], sort: SortState): MachineProc[] {
  const sign = sort.dir === "asc" ? 1 : -1;
  return [...procs].sort((a, b) => {
    const va = sortValue(a, sort.key);
    const vb = sortValue(b, sort.key);
    if (va === null || vb === null) {
      if (va !== vb) return va === null ? 1 : -1;
    } else if (va !== vb) {
      return (va < vb ? -1 : 1) * sign;
    }
    return a.pid - b.pid;
  });
}

/** CPU text colour: amber past half a core, red past a whole one. */
export function cpuTone(pct: number | null): "warm" | "hot" | undefined {
  if (pct === null) return undefined;
  if (pct > 100) return "hot";
  if (pct > 50) return "warm";
  return undefined;
}

// ---- selection ---------------------------------------------------------------

export interface Selection {
  pids: Set<number>;
  anchor: number | null;
}

export const EMPTY_SELECTION: Selection = { pids: new Set(), anchor: null };

/** Finder/Activity Monitor rules: a click selects that row alone,
 *  Cmd/Ctrl-click toggles it, Shift-click selects the run from the anchor
 *  through it (in the order the rows are drawn). */
export function clickSelect(
  sel: Selection,
  order: number[],
  pid: number,
  mods: { toggle: boolean; range: boolean },
): Selection {
  if (mods.range && sel.anchor !== null) {
    const a = order.indexOf(sel.anchor);
    const b = order.indexOf(pid);
    if (a !== -1 && b !== -1) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      const pids = mods.toggle ? new Set(sel.pids) : new Set<number>();
      for (const p of order.slice(lo, hi + 1)) pids.add(p);
      return { pids, anchor: sel.anchor };
    }
  }
  if (mods.toggle) {
    const pids = new Set(sel.pids);
    if (pids.has(pid)) pids.delete(pid);
    else pids.add(pid);
    return { pids, anchor: pid };
  }
  return { pids: new Set([pid]), anchor: pid };
}

// ---- ending processes -----------------------------------------------------------

export type EndOutcome = "ended" | "denied" | "gone" | "refused";
export interface EndResult {
  pid: number;
  name: string;
  outcome: EndOutcome;
  message?: string;
}

/** End one process through the endpoint `endRoute` picks. `force` always
 *  signals (a SIGKILL has no owner-aware equivalent). Never throws. */
export async function endProcess(p: MachineProc, force = false): Promise<EndResult> {
  const name = displayName(p);
  const route = endRoute(p);
  if (route === null) return { pid: p.pid, name, outcome: "refused", message: "This app" };
  try {
    if (route === "stop" && !force) {
      await stopSystemProcess(p.pid, p.startedAt);
      return { pid: p.pid, name, outcome: "ended" };
    }
    const r = await killSystemProcess(p.pid, p.startedAt, force);
    if (r.ok) return { pid: p.pid, name, outcome: "ended" };
    return {
      pid: p.pid,
      name,
      outcome: r.error === "permission denied" ? "denied" : "refused",
      message: r.error,
    };
  } catch (e) {
    const status = (e as { status?: number }).status;
    // 404: exited, or the pid now names a different process ("process
    // changed") — either way the process the user picked is gone.
    if (status === 404) return { pid: p.pid, name, outcome: "gone" };
    return { pid: p.pid, name, outcome: "refused", message: (e as Error).message };
  }
}

/** One line for a batch: "Killed 2 · 1 permission denied · 1 already gone".
 *  A single ended process is named instead: "Killed sleep". */
export function summarizeResults(results: EndResult[], force = false, verb: "Kill" | "Stop" = "Kill"): string {
  const count = (o: EndOutcome) => results.filter((r) => r.outcome === o).length;
  const parts: string[] = [];
  const ended = count("ended");
  const past = force ? "Force killed" : verb === "Stop" ? "Stopped" : "Killed";
  if (ended === 1 && results.length === 1) return `${past} ${results[0].name}`;
  if (ended) parts.push(`${past} ${ended}`);
  const denied = count("denied");
  if (denied) parts.push(`${denied} permission denied`);
  const gone = count("gone");
  if (gone) parts.push(`${gone} already gone`);
  const refused = count("refused");
  if (refused) parts.push(`${refused} refused`);
  return parts.join(" · ") || "Nothing to kill";
}

/** fused-render's own cost: CPU and memory summed over its rows. */
export function fusedTotals(procs: MachineProc[]): { cpuPct: number | null; memBytes: number } {
  const ours = procs.filter((p) => p.fused);
  const cpus = ours.map((p) => p.cpuPct).filter((c): c is number => c !== null);
  return {
    cpuPct: cpus.length ? cpus.reduce((a, b) => a + b, 0) : null,
    memBytes: ours.reduce((a, p) => a + (p.memBytes ?? 0), 0),
  };
}

const GIB = 1024 ** 3;

/** "12.6 of 16 GB". */
export function memOfTotal(used: number | null, total: number | null): string {
  if (used === null || !total) return "—";
  return `${(used / GIB).toFixed(1)} of ${Math.round(total / GIB)} GB`;
}

/** The smallest 1/2/2.5/5 × 10^k at or above `x` — a chart's y ceiling. */
export function niceCeil(x: number): number {
  if (!(x > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(x));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= x - 1e-9) return m * p;
  return 10 * p;
}

/** The verb the action bar offers for a selection: "Stop" only when every
 *  row in it is one of fused-render's own, owner-stoppable processes. */
export function bulkVerb(procs: MachineProc[]): "Kill" | "Stop" {
  return procs.length > 0 && procs.every((p) => endRoute(p) === "stop") ? "Stop" : "Kill";
}

/** The row order to draw while the order is HELD (pointer over the table, or
 *  a selection): the previous order, minus pids no longer listed, with new
 *  pids appended at the bottom in their sorted order. Values still update in
 *  place; only the ORDER stands still, so a row never moves under the
 *  pointer. Idempotent: holding an already-held order returns it unchanged. */
export function holdOrder(held: number[], sorted: number[]): number[] {
  const live = new Set(sorted);
  const kept = held.filter((pid) => live.has(pid));
  const seen = new Set(kept);
  return kept.concat(sorted.filter((pid) => !seen.has(pid)));
}

/** The toolbar's confirm question. Names up to three processes outright; past
 *  that, counts them and names the first two; past ten, warns that apps may
 *  close. `verb` is the bar's verb ("Kill"/"Stop"), or "Force kill". */
export function confirmQuestion(targets: MachineProc[], verb: string): string {
  const named = (p: MachineProc) => `${displayName(p)} (${p.pid})`;
  const n = targets.length;
  if (n === 0) return `${verb} nothing?`;
  if (n <= 3) {
    const names = targets.map(named);
    const list = n === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names[n - 1]}`;
    return `${verb} ${list}?`;
  }
  const lead = `${verb} ${n} processes? This includes ${targets.slice(0, 2).map(named).join(", ")}…`;
  return n > 10 ? `${lead} This may close apps you are using.` : lead;
}

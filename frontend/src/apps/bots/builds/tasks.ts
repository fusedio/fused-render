// fused.tasks for the bots page (runtime.js's `fused.tasks` block, D890), as plain fetches. OpenBot ran inside
// /render and called fused.tasks.*; this page has no runtime, so the pieces Builds needs live here:
//   list / watch   GET /api/tasks and the GET /api/tasks/changes long poll (generation cursor), ONE shared loop per
//                  scope key, refcounted, with runtime.js's rules: a full listing first, deltas folded in, `full`
//                  answered by a re-read, a 20 s floor re-read, hidden tabs sitting the poll out.
//   create         POST /api/tasks/create → a handle {entryId, key (getter: pending:<entry> → session id), done}
//   markRead       POST /api/tasks/read {key, all: true}
//   ui             GET /api/tasks/ui?view=&task=&scope= → the /tasks?embed=1… iframe src
// camelCase in, snake_case on the wire. Every POST carries X-Fused: 1. Not routed through lib/api request(): the
// long poll would trip its "Still waiting on the worker" stall banner every 8 s.
//
// SCOPE: "all" is NOT a `scope=all` query on /api/tasks or /api/tasks/changes (the server 400s anything but "app");
// as in runtime.js it is the bare, unfiltered listing. Only /api/tasks/ui takes scope=all. The page has no
// X-Fused-Page, so "app" scope is not available here. A handle rides the same unscoped feed (runtime.js used an
// `under=<dir>` feed per handle only because it had no shared unscoped one).

/** A row of GET /api/tasks (routes/tasks.py `_row`): the fields Builds reads. */
export interface TaskRow {
  key: string;
  entry_id?: string;
  status: string;
  title?: string;
  messages?: { body?: string; [k: string]: unknown }[];
  project?: string;
  target?: string;
  folder?: string;
  cwd?: string;
  started?: number;
  last_reply?: string;
  session_id?: string;
  [k: string]: unknown;
}

export interface TaskChange { full: boolean; rows: TaskRow[]; gone: string[] }
export interface ScopeOpts { scope?: "all" | "app" }
export interface TaskSpec { prompt: string; target?: string; title?: string; model?: string; effort?: string; permissionMode?: string }
export interface TaskHandle {
  readonly key: string;
  entryId: string;
  /** Resolves with the task's last row once it settles (done/archived twice in a row after it ran). Never rejects. */
  done: Promise<TaskRow>;
}

const CHANGES_WAIT_S = 25, BACKOFF_MS = 3000, FLOOR_MS = 20000, CATCH_UP_MS = 1000;
/** A status that proves the task ran (OpenBot agents.py _watch_build `seen_running`). */
const SEEN_RUNNING = new Set(["in_progress", "queued", "needs_attention", "blocked"]);
const SETTLED = new Set(["done", "archived"]);
/** A first quiet row is re-read after this long (runtime.js TASKS_CONFIRM_MS, the server's running-mark TTL). */
const CONFIRM_MS = 15000;

// ------------------------------------------------------------------ fetch ----
export class TaskError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

async function taskFetch<T = Record<string, unknown>>(method: "GET" | "POST", url: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const init: RequestInit = { method, signal, cache: "no-store" };
  if (method === "POST") {
    init.headers = { "Content-Type": "application/json", "X-Fused": "1" };
    init.body = JSON.stringify(body || {});
  }
  const res = await fetch(url, init);
  // A non-JSON body (proxy error, HTML page) reads as {} so the status still makes an Error.
  const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  if (!res.ok) {
    const said = (typeof data.error === "string" && data.error) || (typeof data.detail === "string" && data.detail) || "HTTP " + res.status;
    throw new TaskError(said, res.status);
  }
  return data as T;
}

/** The listing's query for a scope key ("" = every task, "under=<dir>" = one folder) plus extras. */
function query(scopeKey: string, extra?: Record<string, string>): string {
  const q = new URLSearchParams(scopeKey);
  for (const [k, v] of Object.entries(extra || {})) q.set(k, v);
  const s = q.toString();
  return s ? "?" + s : "";
}

/** runtime.js taskScope, minus "app" (no X-Fused-Page here): every caller of this page asks for "all". */
const scopeKeyOf = (_opts?: ScopeOpts): string => "";
const pendingEntry = (key: unknown): string => (typeof key === "string" && key.startsWith("pending:") ? key.slice(8) : "");
/** archived rows drop out unless asked for (runtime.js taskFilter's default). */
const keepRow = (r: TaskRow | null | undefined): r is TaskRow => !!r && !!r.key && r.status !== "archived";

async function listing(scopeKey: string): Promise<{ rows: TaskRow[]; generation?: number }> {
  const d = await taskFetch<{ tasks?: TaskRow[]; generation?: number }>("GET", "/api/tasks" + query(scopeKey));
  return { rows: (Array.isArray(d.tasks) ? d.tasks : []).filter((r) => r && r.key), generation: d.generation };
}

/** fused.tasks.list({scope}): every task (archived left out), newest first. */
export async function tasksList(opts: ScopeOpts = { scope: "all" }): Promise<TaskRow[]> {
  return (await listing(scopeKeyOf(opts))).rows.filter(keepRow);
}

// ------------------------------------------------------------------ the shared feed ----
type FeedChange = TaskChange & { before?: Set<string>; replay?: boolean };
interface Feed {
  scope: string; subs: Set<(c: FeedChange) => void>; rows: Map<string, TaskRow> | null; gen: number;
  stopped: boolean; run: number; abort: AbortController | null; floor: ReturnType<typeof setInterval> | null;
  wake: (() => void) | null; seat: number; catchingUp: boolean;
}
const feeds: Record<string, Feed> = {};
const hidden = (): boolean => typeof document !== "undefined" && document.hidden;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function feedOf(scope: string): Feed {
  return feeds[scope] || (feeds[scope] = { scope, subs: new Set(), rows: null, gen: -1, stopped: true, run: 0, abort: null, floor: null, wake: null, seat: 0, catchingUp: false });
}
const feedRows = (f: Feed): TaskRow[] => (f.rows ? [...f.rows.values()] : []);
function emit(f: Feed, change: FeedChange) {
  for (const sub of [...f.subs]) { try { sub(change); } catch (e) { console.error("[bots tasks] a watch callback threw:", e); } }
}

async function feedLoad(f: Feed) {
  const mine = ++f.seat;
  try {
    const l = await listing(f.scope);
    if (f.stopped || f.seat !== mine) return;
    f.catchingUp = false;
    // A listing that left before a delta landed is OLDER than what is held.
    if (typeof l.generation === "number") {
      if (f.rows && l.generation < f.gen) return;
      if (f.gen < 0 || l.generation > f.gen) f.gen = l.generation;
    }
    const prev = f.rows;
    f.rows = new Map(l.rows.map((r) => [r.key, r]));
    const gone = prev ? [...prev.keys()].filter((k) => !f.rows!.has(k)) : [];
    emit(f, { full: true, rows: l.rows, gone });
  } catch {
    // A failed read keeps what is held: the next floor read or delta catches up.
    if (!f.stopped && f.seat === mine) f.catchingUp = false;
  }
}

async function feedWatch(f: Feed, run: number) {
  const live = () => !f.stopped && f.run === run;
  while (live()) {
    if (hidden()) {
      await new Promise<void>((resolve) => {
        const fire = () => { document.removeEventListener("visibilitychange", fire); if (f.wake === fire) f.wake = null; resolve(); };
        f.wake = fire;
        document.addEventListener("visibilitychange", fire);
      });
      continue;
    }
    const ctl = new AbortController();
    f.abort = ctl;
    let r: { generation?: number; full?: boolean; rows?: TaskRow[]; gone?: string[] };
    try {
      // `since` is the listing's own generation, so nothing slips between "listed at N" and "changes since N".
      r = await taskFetch("GET", "/api/tasks/changes" + query(f.scope, { since: String(f.gen), wait: String(CHANGES_WAIT_S) }), undefined, ctl.signal);
    } catch {
      if (ctl.signal.aborted || !live()) return;
      await sleep(BACKOFF_MS);
      continue;
    } finally {
      if (f.abort === ctl) f.abort = null;
    }
    if (!live()) return;
    const handshake = f.gen < 0;
    if (typeof r.generation === "number") f.gen = r.generation;
    if (handshake) continue;
    if (r.full) {
      // A restarted server counts from zero again: forget the generation first, fold nothing until the re-read lands.
      f.gen = -1; f.catchingUp = true;
      void feedLoad(f);
      await sleep(CATCH_UP_MS);
      continue;
    }
    if (f.catchingUp) continue;
    const rows = (Array.isArray(r.rows) ? r.rows : []).filter((x) => x && x.key);
    const gone = Array.isArray(r.gone) ? r.gone : [];
    if (!rows.length && !gone.length) continue;
    const before = new Set(f.rows ? f.rows.keys() : []);
    // `gone` is not scope-filtered server-side: a key this feed never held is somebody else's news.
    const held = gone.filter((k) => before.has(k));
    if (!rows.length && !held.length) continue;
    // Rows that moved go to the front, newest first like the listing.
    const next = new Map<string, TaskRow>(rows.map((x) => [x.key, x]));
    for (const [k, x] of f.rows || new Map<string, TaskRow>()) if (!next.has(k) && !held.includes(k)) next.set(k, x);
    f.rows = next;
    emit(f, { full: false, rows, gone: held, before });
  }
}

function feedStart(f: Feed) {
  f.stopped = false;
  const run = ++f.run;
  f.gen = -1; f.rows = null; f.catchingUp = false;
  void feedLoad(f).then(() => { if (!f.stopped && f.run === run) void feedWatch(f, run); });
  // The floor: the answer to a watcher that missed something.
  f.floor = setInterval(() => { if (!hidden()) void feedLoad(f); }, FLOOR_MS);
}

function feedStop(f: Feed) {
  f.stopped = true; f.run++; f.seat++;
  f.abort?.abort(); f.abort = null;
  if (f.floor) clearInterval(f.floor);
  f.floor = null;
  f.wake?.();
  f.rows = null;
}

/** Subscribe to a scope's feed; a late subscriber is replayed what is held, on a microtask. Returns the unsubscribe. */
function subscribe(scope: string, sub: (c: FeedChange) => void): () => void {
  const f = feedOf(scope);
  f.subs.add(sub);
  if (f.stopped) feedStart(f);
  else if (f.rows) {
    void Promise.resolve().then(() => {
      if (!f.subs.has(sub) || !f.rows) return;
      try { sub({ full: true, rows: feedRows(f), gone: [], replay: true }); } catch (e) { console.error("[bots tasks] a watch callback threw:", e); }
    });
  }
  let on = true;
  return () => {
    if (!on) return;
    on = false;
    f.subs.delete(sub);
    if (!f.subs.size) feedStop(f);
  };
}

/** fused.tasks.watch(fn, {scope}): fn(rows, change) with the current listing (archived left out). Returns the unsubscribe. */
export function tasksWatch(fn: (rows: TaskRow[], change: TaskChange) => void, opts: ScopeOpts = { scope: "all" }): () => void {
  const scope = scopeKeyOf(opts), f = feedOf(scope);
  return subscribe(scope, (c) => fn(feedRows(f).filter(keepRow), { full: !!c.full, rows: c.rows, gone: c.gone }));
}

// ------------------------------------------------------------------ verbs ----
/** fused.tasks.markRead(key): the whole task. */
export async function tasksMarkRead(key: string): Promise<void> {
  await taskFetch("POST", "/api/tasks/read", { key, all: true });
}

/** fused.tasks.ui({view, task, scope}) → the Tasks UI iframe src (relative). */
export async function tasksUi(args: { view?: string; task?: string; scope?: "all" | "app" } = {}): Promise<string> {
  const q: string[] = [];
  if (args.view) q.push("view=" + encodeURIComponent(args.view));
  if (args.task) q.push("task=" + encodeURIComponent(args.task));
  q.push("scope=" + (args.scope === "all" ? "all" : "app"));
  const d = await taskFetch<{ url?: string }>("GET", "/api/tasks/ui?" + q.join("&"));
  return d.url || "";
}

/** fused.tasks.create(spec) → a handle. Does not wait for the spawn: the handle follows the row. */
export async function tasksCreate(spec: TaskSpec): Promise<TaskHandle> {
  const body: Record<string, string> = { prompt: spec.prompt };
  if (spec.target) body.target = spec.target;
  if (spec.title) body.title = spec.title;
  if (spec.model) body.model = spec.model;
  if (spec.effort) body.effort = spec.effort;
  if (spec.permissionMode) body.permission_mode = spec.permissionMode;
  const d = await taskFetch<{ entry_id?: string; key?: string; under?: string }>("POST", "/api/tasks/create", body);
  const entryId = d.entry_id || pendingEntry(d.key);
  const under = d.under ? String(d.under) : spec.target || "";
  return taskHandle(d.key || "pending:" + entryId, entryId, under);
}

/** Pure: one observation of the task's status under OpenBot's _watch_build rule. */
export interface DoneState { seenRunning: boolean; stable: string }
export function observeStatus(s: DoneState, status: string): { state: DoneState; settled: boolean } {
  const seenRunning = s.seenRunning || SEEN_RUNNING.has(status);
  if (SETTLED.has(status) && seenRunning) {
    // Status can flicker for ~15 s after a turn: want it twice in a row.
    if (s.stable !== status) return { state: { seenRunning, stable: status }, settled: false };
    return { state: { seenRunning, stable: status }, settled: true };
  }
  return { state: { seenRunning, stable: "" }, settled: false };
}

// The key starts as `pending:<entry>` and flips to the session id once a row carries the same entry_id (or, for a
// session row that does not, the one new row arriving in the same answer that retires the pending key). `done`
// settles the _watch_build way: once the task was seen running, a done/archived status twice in a row; the second
// look is the next feed event for the row or a re-read CONFIRM_MS later, whichever comes first. A row that leaves
// the listing after it was seen settles with its last row (runtime.js: `done` never rejects).
function taskHandle(firstKey: string, entryId: string, under: string): TaskHandle {
  let key = firstKey, last: TaskRow | null = null, finished = false, st: DoneState = { seenRunning: false, stable: "" };
  let confirm: ReturnType<typeof setTimeout> | null = null;
  let settle!: (r: TaskRow) => void;
  const done = new Promise<TaskRow>((r) => { settle = r; });
  // Rides the shared unscoped feed the Builds chip already keeps alive (one socket for every build in flight, not
  // one `under=` long poll each: the page also holds the status poll and the framed Tasks page's own pulse).
  void under;
  const scope = "";
  const mine = (r: TaskRow) => r.key === key || (!!entryId && r.entry_id === entryId);
  let stop = () => {};

  const finish = (row: TaskRow) => {
    if (finished) return;
    finished = true;
    if (confirm) clearTimeout(confirm);
    confirm = null;
    settle(row);
    stop();
  };
  const consider = (row: TaskRow) => {
    key = row.key; last = row;
    const o = observeStatus(st, row.status);
    st = o.state;
    if (o.settled) return finish(row);
    if (confirm) { clearTimeout(confirm); confirm = null; }
    if (st.stable) arm();
  };
  function arm() {
    confirm = setTimeout(() => {
      confirm = null;
      if (finished) return;
      listing(scope).then((l) => {
        if (finished) return;
        const row = l.rows.find(mine);
        if (row) consider(row); else if (last) finish(last);
      }, () => { if (!finished) arm(); });  // an unreadable listing proves nothing; look again
    }, CONFIRM_MS);
  }

  stop = subscribe(scope, (c) => {
    if (finished) return;
    const rows = c.rows || [];
    let row = rows.find(mine);
    if (!row && !c.full && pendingEntry(key) && c.gone.includes(key)) {
      const fresh = rows.filter((r) => !c.before || !c.before.has(r.key));
      if (fresh.length === 1) row = fresh[0];
    }
    if (row) return consider(row);
    if (last && (c.full || c.gone.includes(key))) finish(last);
  });

  return { get key() { return key; }, entryId, done };
}

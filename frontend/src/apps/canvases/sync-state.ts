// The canvas sync "two loops" as the host page sees them (pure: no React, no
// fetch — everything here can be unit-tested on plain objects).
//
// Four legs, each with a state and a reason:
//   embed_to_disk   the workbench frame's snapshots → the clone (local loop).
//                   The HOST computes this one from its own relay queue: the
//                   oldest snapshot it received from the frame and has not yet
//                   seen acked or rejected. The server cannot know it.
//   disk_to_embed   the clone's moves → the frame as deltas (local loop).
//   disk_to_server  the watcher's push of the clone (cloud loop).
//   server_to_disk  the server moved since the last sync point (cloud loop).
// The server fills the last three (GET /api/canvases/sync/state, and the
// `state` message on WS /api/canvases/sync/events); `mergeEmbedLeg` folds the
// host's leg in before anything renders it or posts it to the frame.

export type LegName = "embed_to_disk" | "disk_to_embed" | "disk_to_server" | "server_to_disk";

/** Local-first: the order legs are reported in and tie-broken by. */
export const LEG_ORDER: LegName[] = ["embed_to_disk", "disk_to_embed", "disk_to_server", "server_to_disk"];

export const LEG_LABEL: Record<LegName | "watcher", string> = {
  embed_to_disk: "Canvas → disk",
  disk_to_embed: "Disk → canvas",
  disk_to_server: "Disk → server",
  server_to_disk: "Server → disk",
  watcher: "Watcher",
};

/** Short names for the strip's four dots, in the drawer's legend line. */
export const LEG_SHORT: Record<LegName, string> = {
  embed_to_disk: "canvas→disk",
  disk_to_embed: "disk→canvas",
  disk_to_server: "publish",
  server_to_disk: "server",
};

/** Local-loop budgets (ms): past these the leg is amber. */
export const EMBED_TO_DISK_BUDGET_MS = 100;
export const DISK_TO_EMBED_BUDGET_MS = 300;

export interface SyncLeg {
  state: string;
  oldest_age_ms?: number | null;
  reason?: string | null;
}

export interface EmbedToDiskLeg extends SyncLeg {
  state: "idle" | "pending" | "blocked" | "error";
}

export interface DiskToEmbedLeg extends SyncLeg {
  state: "idle" | "pending" | "held" | "error";
}

export interface DiskToServerLeg extends SyncLeg {
  state: "idle" | "pending" | "pushing" | "held" | "error";
  /** Epoch seconds of the last successful push, or null. */
  last_push_at: number | null;
  push_seq: number;
}

export interface ServerToDiskLeg {
  state: "idle" | "stale" | "pulling" | "error";
  since_ms: number | null;
  changed: { slug: string; kind: string }[];
  reason: string | null;
}

export interface SyncOwner {
  pid: number;
  port: number;
  since?: number;
}

/** GET /api/canvases/sync/state (contract shape). */
export interface SyncState {
  name: string;
  legs: {
    embed_to_disk: EmbedToDiskLeg;
    disk_to_embed: DiskToEmbedLeg;
    disk_to_server: DiskToServerLeg;
    server_to_disk: ServerToDiskLeg;
  };
  /** The clone's disk revision; the page asks for a delta when it advances. */
  disk_rev: number;
  /** Identifies the watcher behind disk_rev; a new one counts from 0 again.
   *  null = no watcher runs in this process (the page re-arms it). */
  watcher_epoch: string | null;
  owner: SyncOwner | null;
  other_watchers: SyncOwner[];
  /** Full per-line CLI output of the failing push; empty when it succeeded. */
  error_detail: string[];
  /** Optional extras (not in the base contract): read when present. */
  watching?: boolean;
  fix_active?: boolean;
}

/** One sync/log ring entry. */
export interface SyncLogEvent {
  seq: number;
  /** Epoch seconds. */
  t: number;
  leg: LegName | "watcher";
  action: string;
  ms: number | null;
  ok: boolean;
  detail: string;
}

export type SyncLevel = "ok" | "warn" | "error";

export interface SyncWorst {
  leg: LegName | null;
  level: SyncLevel;
  /** The worst leg's reason, or a calm summary ("In sync", "Published",
   *  "Publishing…"). Carries NO relative age, so it does not change by
   *  itself as time passes; the strip adds the age (`stripText`). */
  text: string;
}

/** What the page posts to the frame and renders: the server's state with
 *  the host's embed_to_disk leg merged in, plus the precomputed worst leg. */
export interface MergedSyncState extends SyncState {
  worst: SyncWorst;
}

/** One snapshot (or rename hint) the host received from the frame and has
 *  not yet seen acked, rejected or abandoned. */
export interface RelayEntry {
  id: number;
  gen: number;
  seq: number;
  /** Date.now() when the frame's message arrived. */
  at: number;
  /** The clone answered 409 (a force pull or a pause holds it, or the
   *  watcher is being re-armed); the relay is retrying. */
  busy: boolean;
}

/** A state with every leg idle — before the server's first answer. */
export function emptySyncState(name: string): SyncState {
  return {
    name,
    legs: {
      embed_to_disk: { state: "idle", oldest_age_ms: null, reason: null },
      disk_to_embed: { state: "idle", oldest_age_ms: null, reason: null },
      disk_to_server: { state: "idle", oldest_age_ms: null, reason: null, last_push_at: null, push_seq: 0 },
      server_to_disk: { state: "idle", since_ms: null, changed: [], reason: null },
    },
    disk_rev: 0,
    watcher_epoch: null,
    owner: null,
    other_watchers: [],
    error_detail: [],
  };
}

/** Human age: "40 ms", "12 s", "3 min", "2 h". */
export function formatAge(ms: number): string {
  const v = Math.max(0, ms);
  if (v < 1000) return `${Math.round(v)} ms`;
  if (v < 60_000) return `${Math.floor(v / 1000)} s`;
  if (v < 3_600_000) return `${Math.floor(v / 60_000)} min`;
  return `${Math.floor(v / 3_600_000)} h`;
}

/** Why the last relay did NOT land (cleared by the next one applied):
 *  - "rejected": the clone refused it (400 etc.); `detail` is the reason.
 *  - "expired": 120 s of 409 busy with nothing newer queued behind it, so no
 *    later snapshot carries its state: the change is not on disk.
 *  A relay abandoned because a NEWER one superseded it is never a failure:
 *  the newer snapshot carries the full state, nothing is lost. */
export interface EmbedFailure {
  kind: "rejected" | "expired";
  detail: string;
}

export function embedFailureText(f: EmbedFailure): string {
  return f.kind === "expired"
    ? "Change not saved — the clone stayed busy for 2 min; edit again to retry"
    : `Snapshot rejected: ${f.detail}`;
}

/** The host's embed_to_disk leg from its relay queue and its last failure. */
export function embedLegFromQueue(
  queue: readonly RelayEntry[],
  failure: EmbedFailure | null,
  now: number,
): EmbedToDiskLeg {
  if (queue.length === 0) {
    if (failure) return { state: "error", oldest_age_ms: null, reason: embedFailureText(failure) };
    return { state: "idle", oldest_age_ms: null, reason: null };
  }
  let oldest = queue[0];
  for (const e of queue) if (e.at < oldest.at) oldest = e;
  const age = Math.max(0, now - oldest.at);
  const n = queue.length;
  const what = n > 1 ? `${n} canvas changes` : "Canvas change";
  if (queue.some((e) => e.busy)) {
    return {
      state: "blocked",
      oldest_age_ms: age,
      reason: `${what} waiting ${formatAge(age)} — the clone is busy (force pull or pause)`,
    };
  }
  return {
    state: "pending",
    oldest_age_ms: age,
    reason: age > EMBED_TO_DISK_BUDGET_MS ? `${what} saving to disk for ${formatAge(age)}` : null,
  };
}

/** The server's state with the host's embed_to_disk leg folded in. */
export function mergeEmbedLeg(
  server: SyncState | null,
  name: string,
  queue: readonly RelayEntry[],
  failure: EmbedFailure | null,
  now: number,
): MergedSyncState {
  const base = server ?? emptySyncState(name);
  const legs = { ...base.legs, embed_to_disk: embedLegFromQueue(queue, failure, now) };
  const merged: SyncState = { ...base, legs };
  return { ...merged, worst: worstLeg(merged) };
}

interface LegVerdict {
  level: SyncLevel;
  reason: string | null;
}

function legVerdict(name: LegName, state: SyncState): LegVerdict {
  const leg = state.legs[name];
  const reason = leg.reason ?? null;
  if (leg.state === "error") {
    const fallback: Record<LegName, string> = {
      embed_to_disk: "Saving the canvas to disk failed",
      disk_to_embed: "Updating the canvas from disk failed",
      disk_to_server: "Publishing failed",
      server_to_disk: "Pulling server changes failed",
    };
    return { level: "error", reason: reason ?? fallback[name] };
  }
  switch (name) {
    case "embed_to_disk": {
      const l = state.legs.embed_to_disk;
      if (l.state === "blocked") return { level: "warn", reason: reason ?? "Saving to disk is blocked" };
      if (l.state === "pending" && (l.oldest_age_ms ?? 0) > EMBED_TO_DISK_BUDGET_MS) {
        return { level: "warn", reason: reason ?? `Saving to disk for ${formatAge(l.oldest_age_ms ?? 0)}` };
      }
      return { level: "ok", reason: null };
    }
    case "disk_to_embed": {
      const l = state.legs.disk_to_embed;
      if (l.state === "held") return { level: "warn", reason: reason ?? "A disk change is held" };
      if (l.state === "pending" && (l.oldest_age_ms ?? 0) > DISK_TO_EMBED_BUDGET_MS) {
        return { level: "warn", reason: reason ?? `Updating the canvas for ${formatAge(l.oldest_age_ms ?? 0)}` };
      }
      return { level: "ok", reason: null };
    }
    case "disk_to_server": {
      const l = state.legs.disk_to_server;
      if (l.state === "held") return { level: "warn", reason: reason ?? "Publishing is held" };
      // Pending / pushing is the cloud loop doing its job: amber only when
      // the server says why it is slow.
      if ((l.state === "pending" || l.state === "pushing") && reason) return { level: "warn", reason };
      return { level: "ok", reason: null };
    }
    case "server_to_disk": {
      const l = state.legs.server_to_disk;
      if (l.state === "stale") return { level: "warn", reason: reason ?? serverChangesText(l) };
      return { level: "ok", reason: null };
    }
  }
}

/** "Server changes: a (changed), b (added)" — or the layout when no UDF. */
export function serverChangesText(leg: ServerToDiskLeg): string {
  if (leg.changed.length === 0) return "Server changes: the canvas layout";
  return "Server changes: " + leg.changed.map((u) => `${u.slug} (${u.kind})`).join(", ");
}

const LEVEL_RANK: Record<SyncLevel, number> = { ok: 0, warn: 1, error: 2 };

/** Per-leg verdicts, local legs first. */
export function legVerdicts(state: SyncState): { leg: LegName; level: SyncLevel; reason: string | null }[] {
  return LEG_ORDER.map((leg) => ({ leg, ...legVerdict(leg, state) }));
}

/** The worst leg (error > warn > ok; ties go to the local legs) and the
 *  strip's text. Nothing wrong: "Publishing…" while a push is in flight,
 *  "Published" once one has landed, else "In sync". */
export function worstLeg(state: SyncState): SyncWorst {
  let worst: { leg: LegName; level: SyncLevel; reason: string | null } | null = null;
  for (const v of legVerdicts(state)) {
    if (v.level === "ok") continue;
    if (!worst || LEVEL_RANK[v.level] > LEVEL_RANK[worst.level]) worst = v;
  }
  if (worst) return { leg: worst.leg, level: worst.level, text: worst.reason ?? "" };
  const push = state.legs.disk_to_server;
  if (push.state === "pending" || push.state === "pushing") return { leg: null, level: "ok", text: "Publishing…" };
  if (state.legs.server_to_disk.state === "pulling") return { leg: null, level: "ok", text: "Pulling server changes…" };
  if (push.last_push_at != null) return { leg: null, level: "ok", text: "Published" };
  return { leg: null, level: "ok", text: "In sync" };
}

/** The strip's line: the worst text, with "Published" given its age. */
export function stripText(state: MergedSyncState, now: number): string {
  const w = state.worst;
  const at = state.legs.disk_to_server.last_push_at;
  if (w.leg === null && w.text === "Published" && at != null) {
    return `Published ${formatAge(now - at * 1000)} ago`;
  }
  return w.text;
}

/** The body of a sync/start 409 that means "another process owns this
 *  clone", or null for any other failure (a 409 "not signed in" carries no
 *  code). Read flat (`{error, code, owner}`, the server's `_error` shape) and
 *  tolerated under FastAPI's `detail`. */
export function ownerFromError(e: unknown): SyncOwner | null {
  if (!e || typeof e !== "object") return null;
  const { status, body } = e as { status?: unknown; body?: unknown };
  if (status !== 409 || !body || typeof body !== "object") return null;
  const flat = body as { code?: unknown; owner?: unknown; detail?: unknown };
  const src =
    flat.code === "owned"
      ? flat
      : flat.detail && typeof flat.detail === "object" && (flat.detail as { code?: unknown }).code === "owned"
        ? (flat.detail as { owner?: unknown })
        : null;
  if (!src) return null;
  const o = (src.owner ?? {}) as { pid?: unknown; port?: unknown; since?: unknown };
  return {
    pid: typeof o.pid === "number" ? o.pid : 0,
    port: typeof o.port === "number" ? o.port : 0,
    ...(typeof o.since === "number" ? { since: o.since } : {}),
  };
}

/** The strip's line while another process owns the clone. */
export function ownedText(o: SyncOwner): string {
  return `pid ${o.pid} (:${o.port}) owns this canvas — close it there to edit here`;
}

/** Fold new log events into the buffer: dedupe by seq, newest first,
 *  capped. (The ring's seq restarts with a new watcher; the page clears the
 *  buffer when `watcher_epoch` changes.) */
export function mergeLogEvents(
  buffer: readonly SyncLogEvent[],
  incoming: readonly SyncLogEvent[],
  cap = 200,
): SyncLogEvent[] {
  if (incoming.length === 0) return buffer as SyncLogEvent[];
  const bySeq = new Map<number, SyncLogEvent>();
  for (const e of buffer) bySeq.set(e.seq, e);
  for (const e of incoming) bySeq.set(e.seq, e);
  return Array.from(bySeq.values())
    .sort((a, b) => b.seq - a.seq)
    .slice(0, cap);
}

/** "13:04:22.481" for a log event's epoch-seconds stamp. */
export function formatClock(t: number): string {
  const d = new Date(t * 1000);
  const p = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

/** Whether a server state means no watcher runs here (re-arm it). */
export function noWatcher(s: SyncState): boolean {
  return s.watching === false || s.watcher_epoch == null;
}

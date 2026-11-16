// THE READ LEG OF THE EMBED RELAY (SPEC §53.3 NV-7, §53.4), on its own: what
// a page that frames the hosted workbench does to keep the frame's picture of
// a canvas equal to disk, and nothing that writes. The canvas node view
// (NodeView.tsx) is its first user. CanvasWorkspace.tsx keeps its own copy of
// the same protocol for now (its header comment is the protocol's reference);
// this module is shaped so that it could move onto it — the transport is
// injected and the rules are plain functions — but nothing here is wired into
// the workspace yet.
//
// The leg, as the workspace runs it:
//   iframe → { type: "fused-embed-auth-ready" } | "-auth-refresh"
//   parent → { type: "fused-embed-auth-token", accessToken, caps, publicOrigin? }
//            posted at the frame's EXACT origin (the transport's job); `caps`
//            is EMPTY here: a reading frame speaks none of the write channels
//            (no body, no AI gateway, no open, no rename).
//   iframe → { type: "fused-embed-hello" }   → a FULL cut
//   disk_rev / watcher_epoch moved (events socket, else the 2 s poll) → a cut
//   parent → { type: "fused-embed-delta", rev, base_rev, full, ops, gen, seq }
//   iframe → { type: "fused-embed-delta-applied", rev, skipped? } | "-rejected"
//            → POST delta/ack; after a REAL refusal (not "not hydrated yet")
//            one healing full cut, and no second until a delta is applied
//   parent → { type: "fused-embed-sync-state", state }  (SyncStrip's
//            SyncFramePoster throttles and heartbeats it; `postSyncState`
//            is the post itself)
// plus, from a frame in node mode (§53.2 NM-6, NM-7):
//   iframe → { type: "fused-embed-node-ready", udfName }  with auth-ready,
//            before anything loads: this workbench knows node mode. Without
//            it within NODE_READY_WAIT_MS of auth-ready the page is told
//            (`onNoNodeMode`); once it came, the frame stays whatever follows.
//   iframe → { type: "fused-embed-node-state", udfName, present, kind, status, error? }
//            drives the header strip, and NV-9 through `nodePresence`: a
//            `present: false` counts only once it still holds
//            NODE_ABSENT_GRACE_MS after the frame's FIRST delta ack (a node
//            not pushed yet is missing from the server's copy until the first
//            full delta brings it); before that ack it is "pending".
//
// ORDER. Cuts go out on ONE chain, so deltas reach the frame in the order they
// were cut; acks go out on another chain, so they reach the server in the
// order the frame sent them, and the healing cut is asked for only after the
// refusal it heals has been acked — the workspace's two chains, minus the
// snapshot traffic its ack chain also carries (a reading frame posts none).
//
// TRUST. Every message is taken only from the frame's own window on the
// workbench's origin (`acceptFrameMessage`); the caller checks that before
// handing `data` to `handleMessage`.
import type { CanvasDelta, NodeKind } from "./api";
import type { SyncState } from "./sync-state";

/** How long after auth-ready the page waits for `fused-embed-node-ready`
 *  before deciding the workbench has no node mode (§53.2 NM-7,
 *  `NODE_READY_WAIT_S`). */
export const NODE_READY_WAIT_MS = 10_000;
/** A `present: false` is believed only once it still holds this long after
 *  the frame's first delta ack (NV-9). */
export const NODE_ABSENT_GRACE_MS = 2_000;
/** A held cut (the clone mid-write) or a 409 is asked again this far apart,
 *  for up to this long (CanvasWorkspace's DELTA_RETRY_*). */
export const DELTA_RETRY_MS = 1_000;
export const DELTA_RETRY_WINDOW_MS = 30_000;
/** The events socket's reconnect backoff, and the poll while it is down. */
export const SYNC_POLL_MS = 2_000;
export const WS_RETRY_MIN_MS = 1_000;
export const WS_RETRY_MAX_MS = 15_000;
/** Consecutive failed polls read as a dropped watcher. */
export const FAILED_POLLS_BEFORE_HEAL = 3;

/** A frame's refusal that is no error: it has not hydrated yet, and its
 *  hello will ask for the full state. */
export const BOOTING_REFUSAL = "not hydrated yet";

// ---------------------------------------------------------------- trust ----

/** Whether a `message` event came from THE frame: its window, on the
 *  workbench's origin. A same-origin window that guessed the protocol, or a
 *  frame that navigated elsewhere, is ignored. */
export function acceptFrameMessage(
  event: { origin: string; source: unknown },
  frame: { origin: string | null; window: unknown },
): boolean {
  return (
    !!frame.origin && event.origin === frame.origin && frame.window != null && event.source === frame.window
  );
}

// ------------------------------------------------------- the cut trigger ----

/** What the page remembers to decide whether a new sync state owes the frame
 *  a cut. `seenRev`/`seenEpoch`: the last values looked at (a state that
 *  repeats them is not a move — the workspace's effect deps). `lastRev`: the
 *  rev of the last cut (set by the cut itself). `epoch`: the watcher the
 *  page last counted with. */
export interface DeltaCursor {
  seenRev: number | null;
  seenEpoch: string | null | undefined;
  lastRev: number | null;
  epoch: string | null | undefined;
}

export const freshCursor = (): DeltaCursor => ({
  seenRev: null,
  seenEpoch: undefined,
  lastRev: null,
  epoch: undefined,
});

/** One sync state through the cursor (mutated): true when a cut is owed.
 *  First sight records the counter and cuts nothing — the frame's hello asks
 *  for the full state. A DIFFERENT watcher (a stop/start, a server restart)
 *  counts from 0 again and knows nothing of the frame: ask at once, unless
 *  its counter is 0 (nothing to ask yet). Otherwise a move past the last cut
 *  is a cut. */
export function deltaOwed(cursor: DeltaCursor, rev: unknown, epochRaw: unknown): boolean {
  if (typeof rev !== "number") return false;
  const epoch = typeof epochRaw === "string" ? epochRaw : null;
  if (cursor.seenRev === rev && cursor.seenEpoch === epoch) return false;
  cursor.seenRev = rev;
  cursor.seenEpoch = epoch;
  if (cursor.lastRev === null) {
    cursor.lastRev = rev;
    cursor.epoch = epoch;
    return false;
  }
  if (epoch !== cursor.epoch || rev < cursor.lastRev) {
    cursor.epoch = epoch;
    cursor.lastRev = rev;
    return rev !== 0;
  }
  return rev !== cursor.lastRev;
}

/** A state older than the one held (same watcher, lower disk_rev — a slow
 *  poll answer overtaken by the socket) is dropped. */
export function keepNewer<S extends Pick<SyncState, "disk_rev" | "watcher_epoch">>(prev: S | null, next: S): S {
  return prev && prev.watcher_epoch === next.watcher_epoch && next.disk_rev < prev.disk_rev ? prev : next;
}

// ------------------------------------------------------------ node state ----

/** §53.2 NM-6, as the host reads it. */
export interface NodeState {
  udfName: string;
  present: boolean;
  /** Null when `present` is false (and for a kind this page does not know). */
  kind: NodeKind | null;
  status: string | null;
  error: { line: number | null; text: string } | null;
}

const NODE_KINDS: readonly NodeKind[] = ["udf", "widget", "html", "note"];
const ERROR_TEXT_MAX = 8 * 1024;

/** A `fused-embed-node-state` body, checked field by field; null when it is
 *  not one. The error text is capped as the frame caps it (8 KB). */
export function parseNodeState(data: unknown): NodeState | null {
  if (!data || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  if (typeof d.udfName !== "string" || !d.udfName || typeof d.present !== "boolean") return null;
  if (!d.present) return { udfName: d.udfName, present: false, kind: null, status: null, error: null };
  const kind = NODE_KINDS.includes(d.kind as NodeKind) ? (d.kind as NodeKind) : null;
  const status = typeof d.status === "string" && d.status ? d.status : null;
  let error: NodeState["error"] = null;
  const e = d.error as Record<string, unknown> | null | undefined;
  if (e && typeof e === "object" && typeof e.text === "string") {
    const line = typeof e.line === "number" && Number.isInteger(e.line) && e.line > 0 ? e.line : null;
    error = { line, text: e.text.length > ERROR_TEXT_MAX ? e.text.slice(-ERROR_TEXT_MAX) : e.text };
  }
  return { udfName: d.udfName, present: d.present, kind, status, error };
}

/** "pending": no state yet, or `present: false` before it may be believed;
 *  "present"; "absent": NV-9 — the pane says the node is not there. */
export type NodePresence = "pending" | "present" | "absent";

/** NV-9: `present: false` is believed only once it still holds
 *  NODE_ABSENT_GRACE_MS after the frame's first delta ack (`firstAckAt`, null
 *  until then). Returns the reading and, for "pending" with an ack in, the
 *  time at which to ask again. */
export function nodePresence(
  state: NodeState | null,
  firstAckAt: number | null,
  now: number,
): { presence: NodePresence; recheckAt: number | null } {
  if (!state) return { presence: "pending", recheckAt: null };
  if (state.present) return { presence: "present", recheckAt: null };
  if (firstAckAt === null) return { presence: "pending", recheckAt: null };
  const due = firstAckAt + NODE_ABSENT_GRACE_MS;
  return now >= due ? { presence: "absent", recheckAt: null } : { presence: "pending", recheckAt: due };
}

// ------------------------------------------------------------- the relay ----

export interface RelayTransport {
  /** GET /api/canvases/delta for this page's frame (and scope). Throws with
   *  `.status` on an HTTP error. */
  getDelta(full: boolean): Promise<CanvasDelta>;
  /** POST /api/canvases/delta/ack for this page's frame (and scope). */
  ackDelta(rev: number, ok: boolean, reason?: string, skipped?: string[]): Promise<unknown>;
  /** The bearer for the frame (GET /api/canvases/token). */
  getToken(): Promise<string>;
  /** Post to the frame, at its exact origin. False when there is no frame
   *  window (yet, or any more): the message is dropped. */
  post(message: Record<string, unknown>): boolean;
  sleep?(ms: number): Promise<void>;
  now?(): number;
  setTimeout?(fn: () => void, ms: number): unknown;
  clearTimeout?(id: unknown): void;
}

export interface RelayHooks {
  /** The node this frame shows; node-state for any other name is ignored. */
  node?: string;
  /** The workbench's real origin, for the links it builds (custom env behind
   *  the loopback proxy). */
  publicOrigin?(): string | undefined;
  onAuthReady?(): void;
  onHello?(): void;
  onNodeState?(state: NodeState): void;
  /** NV-9's reading of the node states so far (`nodePresence`). */
  onPresence?(presence: NodePresence): void;
  /** No node-ready within NODE_READY_WAIT_MS of auth-ready (NM-7). */
  onNoNodeMode?(): void;
  onError?(message: string): void;
}

export interface EmbedDeltaRelay {
  /** A message already accepted by `acceptFrameMessage`. */
  handleMessage(data: unknown): void;
  /** Every sync state the page gets (socket or poll), in arrival order. */
  onSyncState(state: Pick<SyncState, "disk_rev" | "watcher_epoch">): void;
  /** Queue a cut. Resolves when it (and every cut before it) is done. */
  relayDelta(full: boolean): Promise<void>;
  postSyncState(state: unknown): void;
  /** Both chains settled — for tests and teardown. */
  idle(): Promise<void>;
  dispose(): void;
}

export function createEmbedDeltaRelay(t: RelayTransport, hooks: RelayHooks = {}): EmbedDeltaRelay {
  const now = t.now ?? (() => Date.now());
  const sleep = t.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const setT = t.setTimeout ?? ((fn: () => void, ms: number) => setTimeout(fn, ms));
  const clearT = t.clearTimeout ?? ((id: unknown) => clearTimeout(id as ReturnType<typeof setTimeout>));
  const cursor = freshCursor();
  let deltaChain: Promise<void> = Promise.resolve();
  let ackChain: Promise<void> = Promise.resolve();
  // A full state was sent to heal a refusal and no delta has been applied
  // since: a second refusal does not heal again (no loop).
  let healed = false;
  let nodeTimer: unknown = null;
  // NM-7: once this workbench said it knows node mode, it is never doubted
  // again (a reload of the same frame is the same workbench).
  let nodeReady = false;
  // NV-9's inputs: the last node state, and when this document first acked.
  let lastNodeState: NodeState | null = null;
  let firstAckAt: number | null = null;
  let presenceTimer: unknown = null;
  let lastPresence: NodePresence | null = null;
  let disposed = false;

  const stopNodeTimer = () => {
    if (nodeTimer !== null) clearT(nodeTimer);
    nodeTimer = null;
  };
  const stopPresenceTimer = () => {
    if (presenceTimer !== null) clearT(presenceTimer);
    presenceTimer = null;
  };
  const reportPresence = () => {
    stopPresenceTimer();
    if (disposed) return;
    const { presence, recheckAt } = nodePresence(lastNodeState, firstAckAt, now());
    if (recheckAt !== null) presenceTimer = setT(reportPresence, Math.max(0, recheckAt - now()));
    if (presence !== lastPresence) {
      lastPresence = presence;
      hooks.onPresence?.(presence);
    }
  };

  const relayDelta = (full: boolean): Promise<void> => {
    deltaChain = deltaChain.then(async () => {
      const started = now();
      for (;;) {
        if (disposed) return;
        try {
          const delta = await t.getDelta(full);
          if (delta.held !== undefined) {
            if (now() - started > DELTA_RETRY_WINDOW_MS) return;
            await sleep(DELTA_RETRY_MS);
            continue;
          }
          // Assigned, not max'd: the counter is per watcher and starts over
          // with a new one (`deltaOwed`).
          cursor.lastRev = delta.rev;
          const ops = delta.ops ?? [];
          // Nothing to apply against a known base: nothing to post.
          if (!delta.full && ops.length === 0) return;
          // No frame to tell: the cut is dropped, unacked. Its hello asks for
          // a full state whatever the server thinks it holds.
          t.post({
            type: "fused-embed-delta",
            rev: delta.rev,
            base_rev: delta.base_rev ?? null,
            full: Boolean(delta.full),
            ops,
            gen: delta.gen ?? null,
            seq: delta.seq ?? null,
          });
          return;
        } catch (e) {
          const status = (e as { status?: number }).status;
          if (status === 409 && now() - started <= DELTA_RETRY_WINDOW_MS) {
            await sleep(DELTA_RETRY_MS);
            continue;
          }
          return;
        }
      }
    });
    return deltaChain;
  };

  const seedToken = async () => {
    try {
      const accessToken = await t.getToken();
      if (disposed) return;
      let publicOrigin: string | undefined;
      try {
        publicOrigin = hooks.publicOrigin?.();
      } catch {
        publicOrigin = undefined;
      }
      t.post({
        type: "fused-embed-auth-token",
        accessToken,
        // A reading frame: none of the write channels (body, ai, open, gone,
        // rename) — the frame keeps its editors hidden and posts no writes.
        caps: [],
        ...(publicOrigin ? { publicOrigin } : {}),
      });
    } catch (e) {
      hooks.onError?.((e as Error).message);
    }
  };

  const handleMessage = (data: unknown) => {
    if (disposed || !data || typeof data !== "object") return;
    const d = data as Record<string, unknown>;
    const type = d.type;
    if (type === "fused-embed-auth-ready" || type === "fused-embed-auth-refresh") {
      void seedToken();
      if (type === "fused-embed-auth-ready") {
        // A fresh document: it owes its node state, and its first ack, again.
        lastNodeState = null;
        firstAckAt = null;
        reportPresence();
        stopNodeTimer();
        if (!nodeReady) {
          nodeTimer = setT(() => {
            nodeTimer = null;
            if (!disposed && !nodeReady) hooks.onNoNodeMode?.();
          }, NODE_READY_WAIT_MS);
        }
        hooks.onAuthReady?.();
      }
      return;
    }
    if (type === "fused-embed-node-ready") {
      // Only for the node this page framed (a frame told another name is
      // not this view's).
      if (typeof d.udfName !== "string" || (hooks.node && d.udfName !== hooks.node)) return;
      nodeReady = true;
      stopNodeTimer();
      return;
    }
    if (type === "fused-embed-hello") {
      // The frame (re)hydrated from the server: bring it to disk with a full
      // state, whatever the server thought it held before.
      void relayDelta(true);
      hooks.onHello?.();
      return;
    }
    if (type === "fused-embed-delta-applied" || type === "fused-embed-delta-rejected") {
      const rev = d.rev;
      if (typeof rev !== "number") return;
      const ok = type === "fused-embed-delta-applied";
      if (firstAckAt === null) {
        firstAckAt = now();
        reportPresence();
      }
      const reason = ok ? undefined : typeof d.reason === "string" ? d.reason : "refused";
      const booting = reason === BOOTING_REFUSAL;
      if (ok) healed = false;
      const skipped = Array.isArray(d.skipped)
        ? d.skipped.filter((n: unknown): n is string => typeof n === "string")
        : undefined;
      ackChain = ackChain.then(() =>
        t.ackDelta(rev, ok, reason, skipped).then(
          () => undefined,
          () => undefined,
        ),
      );
      // A REAL refusal: the server dropped its picture of the frame. One
      // full state, once, after the ack — a full state refused again stays
      // refused (until the next applied delta) rather than looping.
      if (!ok && !booting && !healed) {
        healed = true;
        ackChain = ackChain.then(() => {
          void relayDelta(true);
        });
      }
      return;
    }
    if (type === "fused-embed-node-state") {
      const state = parseNodeState(d);
      if (!state || (hooks.node && state.udfName !== hooks.node)) return;
      lastNodeState = state;
      hooks.onNodeState?.(state);
      reportPresence();
    }
  };

  return {
    handleMessage,
    onSyncState(state) {
      if (disposed) return;
      if (deltaOwed(cursor, state.disk_rev, state.watcher_epoch)) void relayDelta(false);
    },
    relayDelta,
    postSyncState(state) {
      if (!disposed) t.post({ type: "fused-embed-sync-state", state });
    },
    async idle() {
      // A chain can grow while it is awaited (a heal queues a cut): settle
      // until neither moves.
      for (;;) {
        const d = deltaChain;
        const a = ackChain;
        await Promise.all([d, a]);
        if (d === deltaChain && a === ackChain) return;
      }
    },
    dispose() {
      disposed = true;
      stopNodeTimer();
      stopPresenceTimer();
    },
  };
}

// ------------------------------------------------------ the events feed ----

/** The part of a WebSocket the feed uses (injected in tests). */
export interface SocketLike {
  onopen: ((ev?: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onerror: ((ev?: unknown) => void) | null;
  onclose: ((ev?: unknown) => void) | null;
  close(): void;
}

export interface SyncFeedOptions {
  url: string;
  /** GET sync/state for this page's frame. */
  poll(): Promise<SyncState>;
  onState(state: SyncState): void;
  onLive?(live: boolean): void;
  /** FAILED_POLLS_BEFORE_HEAL polls in a row failed: re-arm the watcher. */
  onHeal?(): void;
  openSocket?(url: string): SocketLike;
  setTimeout?(fn: () => void, ms: number): unknown;
  clearTimeout?(id: unknown): void;
  setInterval?(fn: () => void, ms: number): unknown;
  clearInterval?(id: unknown): void;
}

/** WS /api/canvases/sync/events, with the sync/state poll while it is down
 *  and a doubling reconnect — CanvasWorkspace's feed. The first state does
 *  not wait for the socket. Returns the teardown. */
export function connectSyncFeed(o: SyncFeedOptions): () => void {
  const setT = o.setTimeout ?? ((fn: () => void, ms: number) => setTimeout(fn, ms));
  const clearT = o.clearTimeout ?? ((id: unknown) => clearTimeout(id as ReturnType<typeof setTimeout>));
  const setI = o.setInterval ?? ((fn: () => void, ms: number) => setInterval(fn, ms));
  const clearI = o.clearInterval ?? ((id: unknown) => clearInterval(id as ReturnType<typeof setInterval>));
  const open = o.openSocket ?? ((url: string) => new WebSocket(url) as unknown as SocketLike);
  let closed = false;
  let sock: SocketLike | null = null;
  let retryTimer: unknown = null;
  let pollTimer: unknown = null;
  let retryMs = 0;
  let failures = 0;

  const pollOnce = () => {
    void o.poll().then(
      (s) => {
        failures = 0;
        if (!closed) o.onState(s);
      },
      () => {
        failures += 1;
        if (!closed && failures >= FAILED_POLLS_BEFORE_HEAL) {
          failures = 0;
          o.onHeal?.();
        }
      },
    );
  };
  const startPolling = () => {
    if (pollTimer === null) pollTimer = setI(pollOnce, SYNC_POLL_MS);
  };
  const stopPolling = () => {
    if (pollTimer !== null) clearI(pollTimer);
    pollTimer = null;
  };
  const scheduleReconnect = () => {
    if (closed || retryTimer !== null) return;
    retryMs = Math.min(WS_RETRY_MAX_MS, retryMs ? retryMs * 2 : WS_RETRY_MIN_MS);
    retryTimer = setT(connect, retryMs);
  };
  function connect() {
    retryTimer = null;
    if (closed) return;
    let s: SocketLike;
    try {
      s = open(o.url);
    } catch {
      startPolling();
      scheduleReconnect();
      return;
    }
    sock = s;
    s.onopen = () => {
      retryMs = 0;
      stopPolling();
      o.onLive?.(true);
      pollOnce(); // whatever moved while the socket was down
    };
    s.onmessage = (ev) => {
      let msg: unknown;
      try {
        msg = JSON.parse(String(ev.data));
      } catch {
        return;
      }
      const m = msg as { type?: unknown; state?: unknown } | null;
      if (m && m.type === "state" && m.state && typeof m.state === "object") o.onState(m.state as SyncState);
      // `log` and `ping`: nothing for a reading frame to do.
    };
    // `error` is always followed by `close`, which does the bookkeeping.
    s.onerror = () => {};
    s.onclose = () => {
      if (sock === s) sock = null;
      if (closed) return;
      o.onLive?.(false);
      startPolling();
      scheduleReconnect();
    };
  }
  pollOnce();
  connect();
  return () => {
    closed = true;
    stopPolling();
    if (retryTimer !== null) clearT(retryTimer);
    sock?.close();
    o.onLive?.(false);
  };
}

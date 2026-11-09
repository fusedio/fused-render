// ONE CHAT CONTROLLER PER SESSION, shared by every mount that shows it
// (design.md "Model"). Pure TS, no React.
//
// A registry keyed by (file, session id) hands out a refcounted `SharedChat`. The
// controller inside is built ONCE, on a deps object whose mount-specific half
// is a ROUTER over the mounts currently attached ("host seats"):
//   * reads (model, effort, backend, hasPane, appStateBlock, terminalHint) → the
//     seat whose bound send is in flight, else the FRONT seat (newest attached,
//     or the last `setFront`); focusComposer → the front seat;
//   * a send's own callbacks → the seat that made that send (recorded by
//     `bindSender` under its sendId / clientId);
//   * run-wide events → every seat.
// Seats are read at CALL time: a mount may hand in one mutable object and
// update its methods every render.
//
// Refcount 0 does not dispose at once: a 30 s grace keeps the run loop alive so
// a popover closed and reopened lands on the same state.
import {
  createMemoryParamsStore,
  type ParamsSnapshot,
  type ParamsStore,
} from "../params/store";
import type {
  ChatController,
  ControllerDeps,
  SendOptions,
  StrandedLine,
} from "./controller-api";

export const CHAT_SHARED_KEYS = [
  "session_id",
  "run",
  "permission",
  "backend",
] as const;

/** Exactly `ControllerDeps.onSendReturned`'s argument. */
export type SendReturnedInfo = Parameters<
  NonNullable<ControllerDeps["onSendReturned"]>
>[0];

export interface HostSeat {
  id: string;
  model(): string;
  effort(): string;
  backend(): string;
  hasPane(): boolean | null;
  appStateBlock(): Promise<string>;
  terminalHint: ControllerDeps["terminalHint"];
  focusComposer(): void;
  onActivity?(): void;
  /** Optional: this mount shows a canvas (a mount asks "does any seat have one" via `seats()`). */
  hasCanvas?(): boolean;
  onStranded(lines: StrandedLine[]): void;
  onSendDelivered(clientId: string): void;
  onSendReturned(info: SendReturnedInfo): void;
  onArtifactsTick(): void;
  onRunEnded(): void;
  onRunAbandoned(): void;
  onCommandsChanged(): void;
}

/** The deps the router owns; the rest comes from `AcquireOpts.config`. */
export type HostDepKeys =
  | "model"
  | "effort"
  | "backend"
  | "hasPane"
  | "appStateBlock"
  | "terminalHint"
  | "focusComposer"
  | "onActivity"
  | "onStranded"
  | "onSendDelivered"
  | "onSendReturned"
  | "onArtifactsTick"
  | "onRunEnded"
  | "onRunAbandoned"
  | "onCommandsChanged";

export interface SharedChat {
  /** The session id this chat is registered under (null = private). The registry key itself is
   *  `sharedChatKey(file, key)`. */
  readonly key: string | null;
  controller: ChatController;
  /** The controller-owned store the controller was built on (CHAT_SHARED_KEYS live here). */
  params: ParamsStore;
  /** Register a mount. The newest attached seat becomes front. Returns detach. */
  attachHost(seat: HostSeat): () => void;
  setFront(id: string): void;
  frontId(): string | null;
  /** Wrap the two send roads so this host receives the returns for ITS sends. Records
   *  opts.sendId and opts.clientId → host id before delegating; also remembers "last sender". */
  bindSender(id: string): Pick<ChatController, "sendMessage" | "sendFollowUp">;
  /** refcount--; at 0 the controller is disposed after GRACE_MS (30_000) unless re-acquired. */
  release(): void;
  /** Read-only snapshot of the attached seats, in attach order (newest last). */
  seats(): readonly HostSeat[];
  /** Any seat attached? A host holds auto-sends while this is false (reads answer "" then). */
  hasSeats(): boolean;
  /** The seat that answers canvas shots: the first attached seat whose `hasCanvas()` is true,
   *  else the front seat. */
  canvasResponderId(): string | null;
  /** Fires on attach / detach / setFront (and dispose). `seats()` keeps its identity between
   *  changes, so it is a valid `useSyncExternalStore` snapshot. */
  onSeatsChange(cb: () => void): () => void;
}

/** Registry key: one chat per (file, session). Different files on one session are different chats. */
export function sharedChatKey(file: string | null, sessionId: string): string {
  return (file ?? "") + "\0" + sessionId;
}

/** Most send records kept for return routing; the oldest is dropped beyond this. */
export const SEND_RECORD_CAP = 512;

export const GRACE_MS = 30_000;

export interface Timers {
  set(fn: () => void, ms: number): unknown;
  clear(h: unknown): void;
}

export interface AcquireOpts {
  /** Session id; null = private (never registered). Registered under
   *  `sharedChatKey(config.file, key)`. */
  key: string | null;
  /** `createChatController` (injected for tests). */
  make: (deps: ControllerDeps) => ChatController;
  /** file, breakout, history, historyCache, schedule*, now, run, sleep… */
  config: Omit<ControllerDeps, "params" | HostDepKeys>;
  /** Initial values for the shared store (session_id, run, backend…). */
  seed?: ParamsSnapshot;
  /** For tests. */
  timers?: Timers;
}

interface Entry {
  key: string | null;
  file: string | null;
  regKey: string | null;
  shared: SharedChat;
  refcount: number;
  grace: unknown;
  graceArmed: boolean;
  disposed: boolean;
  timers: Timers;
  dispose(): void;
}

const registry = new Map<string, Entry>();
const entries = new WeakMap<SharedChat, Entry>();
/** Every live chat, registered or private — what `_registry().disposeAll()` reaches. */
const live = new Set<Entry>();

const defaultTimers: Timers = {
  set: (fn, ms) => setTimeout(fn, ms),
  clear: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

function cancelGrace(entry: Entry): void {
  if (!entry.graceArmed) return;
  entry.timers.clear(entry.grace);
  entry.grace = null;
  entry.graceArmed = false;
}

export function acquireSharedChat(opts: AcquireOpts): SharedChat {
  const file = opts.config.file ?? null;
  const regKey = opts.key === null ? null : sharedChatKey(file, opts.key);
  if (regKey !== null) {
    const existing = registry.get(regKey);
    if (existing && !existing.disposed) {
      existing.refcount++;
      cancelGrace(existing);
      return existing.shared;
    }
  }

  const timers = opts.timers ?? defaultTimers;
  const params = createMemoryParamsStore(opts.seed ?? {});
  /** Insertion order = attach order; the last entry is the newest. */
  const seats = new Map<string, HostSeat>();
  let front: string | null = null;
  let lastSender: string | null = null;
  /** One record per bound send, indexed by its sendId and/or clientId. Dropped once returned
   *  and (delivered, or it has no clientId) — or when `SEND_RECORD_CAP` is passed. */
  interface SendRecord {
    seat: string;
    sendId?: string;
    clientId?: string;
    delivered: boolean;
    returned: boolean;
  }
  const records = new Set<SendRecord>();
  const bySendId = new Map<string, SendRecord>();
  const byClientId = new Map<string, SendRecord>();
  const dropRecord = (rec: SendRecord) => {
    records.delete(rec);
    if (rec.sendId !== undefined && bySendId.get(rec.sendId) === rec)
      bySendId.delete(rec.sendId);
    if (rec.clientId !== undefined && byClientId.get(rec.clientId) === rec)
      byClientId.delete(rec.clientId);
  };
  const addRecord = (rec: SendRecord) => {
    records.add(rec);
    if (rec.sendId !== undefined) bySendId.set(rec.sendId, rec);
    if (rec.clientId !== undefined) byClientId.set(rec.clientId, rec);
    while (records.size > SEND_RECORD_CAP) {
      const oldest = records.values().next().value;
      if (!oldest) break;
      dropRecord(oldest);
    }
  };
  /** Bound sends in flight, newest last; the newest is the "current sender" reads go to. */
  const inFlight: Array<{ id: string }> = [];
  let disposed = false;
  const seatListeners = new Set<() => void>();
  let seatsSnapshot: readonly HostSeat[] = [];
  const seatsChanged = () => {
    seatsSnapshot = disposed ? [] : [...seats.values()];
    for (const cb of [...seatListeners]) cb();
  };

  const seat = (id: string | null | undefined): HostSeat | null =>
    id == null ? null : (seats.get(id) ?? null);
  const frontSeat = () => seat(front);
  /** Reads: the seat whose bound send is in flight, else front. */
  const readSeat = (): HostSeat | null => {
    if (disposed) return null;
    for (let i = inFlight.length - 1; i >= 0; i--) {
      const s = seat(inFlight[i].id);
      if (s) return s;
    }
    return frontSeat();
  };
  const newestId = (): string | null => {
    let last: string | null = null;
    for (const id of seats.keys()) last = id;
    return last;
  };
  const broadcast = (call: (s: HostSeat) => void) => {
    if (disposed) return;
    let first: unknown = null;
    let threw = false;
    for (const s of [...seats.values()]) {
      try {
        call(s);
      } catch (e) {
        if (!threw) {
          threw = true;
          first = e;
        }
      }
    }
    if (threw) throw first;
  };

  const router: Pick<ControllerDeps, HostDepKeys> = {
    model: () => readSeat()?.model() ?? "",
    effort: () => readSeat()?.effort() ?? "",
    backend: () => readSeat()?.backend() ?? "",
    hasPane: () => readSeat()?.hasPane() ?? null,
    appStateBlock: () => {
      const s = readSeat();
      return s ? s.appStateBlock() : Promise.resolve("");
    },
    // Absent dep = no `terminal_hint` param; an empty answer means the same.
    terminalHint: () => {
      const hint = readSeat()?.terminalHint;
      return hint ? hint() : Promise.resolve("");
    },
    focusComposer: () => {
      if (!disposed) frontSeat()?.focusComposer();
    },
    onActivity: () => broadcast((s) => s.onActivity?.()),
    onStranded: (lines) => {
      if (disposed) return;
      (seat(lastSender) ?? frontSeat())?.onStranded(lines);
    },
    onSendDelivered: (clientId) => {
      if (disposed) return;
      const rec = byClientId.get(clientId);
      const target = seat(rec?.seat) ?? seat(lastSender) ?? frontSeat();
      if (rec) {
        rec.delivered = true;
        if (rec.returned) dropRecord(rec);
      }
      target?.onSendDelivered(clientId);
    },
    onSendReturned: (info) => {
      if (disposed) return;
      const bySend =
        info.sendId !== undefined ? bySendId.get(info.sendId) : undefined;
      const byClient =
        info.clientId !== undefined ? byClientId.get(info.clientId) : undefined;
      const target =
        seat(bySend?.seat) ??
        seat(byClient?.seat) ??
        seat(lastSender) ??
        frontSeat();
      for (const rec of new Set([bySend, byClient])) {
        if (!rec) continue;
        rec.returned = true;
        if (rec.clientId === undefined || rec.delivered) dropRecord(rec);
      }
      target?.onSendReturned(info);
    },
    onArtifactsTick: () => broadcast((s) => s.onArtifactsTick()),
    onRunEnded: () => broadcast((s) => s.onRunEnded()),
    onRunAbandoned: () => broadcast((s) => s.onRunAbandoned()),
    onCommandsChanged: () => broadcast((s) => s.onCommandsChanged()),
  };

  const controller = opts.make({ ...opts.config, params, ...router });

  const bound =
    (id: string, road: "sendMessage" | "sendFollowUp") =>
    (text: string, sendOpts?: SendOptions) => {
      if (disposed) {
        // The controller is gone: hand the words back to the box that typed them.
        seat(id)?.onSendReturned({
          text,
          attachments: sendOpts?.attachments,
          sendId: sendOpts?.sendId,
          clientId: sendOpts?.clientId,
          refused: true,
        });
        return Promise.resolve();
      }
      if (sendOpts?.sendId !== undefined || sendOpts?.clientId !== undefined) {
        addRecord({
          seat: id,
          sendId: sendOpts.sendId,
          clientId: sendOpts.clientId,
          delivered: false,
          returned: false,
        });
      }
      lastSender = id;
      // Reads made while this send is in flight answer from THIS seat, not the front one.
      const token = { id };
      inFlight.push(token);
      const done = () => {
        const i = inFlight.indexOf(token);
        if (i >= 0) inFlight.splice(i, 1);
      };
      let p: Promise<void>;
      try {
        p = controller[road](text, sendOpts);
      } catch (e) {
        done();
        throw e;
      }
      return p.finally(done);
    };

  const entry: Entry = {
    key: opts.key,
    file,
    regKey,
    refcount: 1,
    grace: null,
    graceArmed: false,
    disposed: false,
    timers,
    shared: null as unknown as SharedChat,
    dispose() {
      if (entry.disposed) return;
      cancelGrace(entry);
      entry.disposed = true;
      live.delete(entry);
      disposed = true;
      if (entry.regKey !== null && registry.get(entry.regKey) === entry)
        registry.delete(entry.regKey);
      // Seats are kept: a bound send after dispose hands its words back to them.
      records.clear();
      bySendId.clear();
      byClientId.clear();
      inFlight.length = 0;
      front = null;
      lastSender = null;
      controller.dispose();
      seatsChanged();
      seatListeners.clear();
    },
  };

  const shared: SharedChat = {
    get key() {
      return entry.key;
    },
    controller,
    params,
    attachHost(s) {
      if (disposed) return () => {};
      const id = s.id;
      seats.delete(id); // re-attach moves it to newest
      seats.set(id, s);
      front = id;
      seatsChanged();
      let attached = true;
      return () => {
        if (!attached) return;
        attached = false;
        if (seats.get(id) !== s) return;
        seats.delete(id);
        if (front === id) front = newestId();
        if (lastSender === id) lastSender = null;
        seatsChanged();
      };
    },
    setFront(id) {
      if (disposed) return;
      if (!seats.has(id) || front === id) return;
      front = id;
      seatsChanged();
    },
    frontId: () => (disposed ? null : front),
    seats: () => seatsSnapshot,
    hasSeats: () => !disposed && seats.size > 0,
    canvasResponderId() {
      if (disposed) return null;
      for (const s of seats.values()) if (s.hasCanvas?.()) return s.id;
      return front;
    },
    onSeatsChange(cb) {
      if (disposed) return () => {};
      seatListeners.add(cb);
      return () => {
        seatListeners.delete(cb);
      };
    },
    bindSender(id) {
      return {
        sendMessage: bound(id, "sendMessage"),
        sendFollowUp: bound(id, "sendFollowUp"),
      };
    },
    release() {
      if (entry.disposed || entry.refcount === 0) return;
      entry.refcount--;
      if (entry.refcount > 0) return;
      cancelGrace(entry);
      entry.graceArmed = true;
      entry.grace = timers.set(() => {
        entry.graceArmed = false;
        entry.grace = null;
        if (entry.refcount === 0) entry.dispose();
      }, GRACE_MS);
    },
  };
  entry.shared = shared;
  entries.set(shared, entry);
  live.add(entry);
  if (regKey !== null) registry.set(regKey, entry);
  return shared;
}

/** A private (key null) chat learned its session id: register it so later mounts share it.
 *  `key` is the session id; it is registered under `sharedChatKey(<this chat's file>, key)`.
 *  If that is already registered to ANOTHER controller, leave the registry alone and return false. */
export function adoptKey(shared: SharedChat, key: string): boolean {
  const entry = entries.get(shared);
  if (!entry || entry.disposed) return false;
  const regKey = sharedChatKey(entry.file, key);
  const existing = registry.get(regKey);
  if (existing && existing !== entry && !existing.disposed) return false;
  if (
    entry.regKey !== null &&
    entry.regKey !== regKey &&
    registry.get(entry.regKey) === entry
  )
    registry.delete(entry.regKey);
  entry.key = key;
  entry.regKey = regKey;
  registry.set(regKey, entry);
  return true;
}

/** test/debug: registry size; `clear()` forgets the registered keys WITHOUT disposing anything
 *  (controllers and grace timers live on); `disposeAll()` disposes every live chat, registered
 *  or private, at once — grace timers cancelled, `controller.dispose()` called. */
export function _registry(): {
  size(): number;
  clear(): void;
  disposeAll(): void;
} {
  return {
    size: () => registry.size,
    clear: () => registry.clear(),
    disposeAll: () => {
      for (const entry of [...live]) entry.dispose();
      registry.clear();
    },
  };
}

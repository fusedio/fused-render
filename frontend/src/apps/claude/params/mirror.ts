// Two-way mirror of a few chat params between two ParamsStores — the mount's
// own store (the shell URL for a pane, memory for a popover) and the shared
// chat's controller-owned memory store (design.md "Params"). Pure TS.
//
// LOOP-SAFE: a write is skipped when the target already holds the value, so a
// mirrored write that echoes back through the other side's `onChange` stops
// there. Only keys whose SOURCE value moved since the last change are copied,
// so an unrelated write on one side (a `model` pick on the mount) never pushes
// stale mirrored values over fresh ones.
//
// HISTORY: `onChange` hands over the snapshot only, not the `SetOpts` of the
// write that caused it. To pass the controller's `{history}` through to the URL
// (its `run` writes are "replace", its `session_id` writes a gesture-gated
// push), each mirrored store's `set` is tapped for the duration of the mirror:
// one wrapper per store, refcounted across mirrors, that records the opts of
// the `set` in flight. A change that arrives with no `set` in flight (a
// popstate, a `fused:urlchange`) writes with "replace".
import type {
  ParamsPatch,
  ParamsSnapshot,
  ParamsStore,
  SetOpts,
} from "./store";

interface Tap {
  orig: ParamsStore["set"];
  wrapper: ParamsStore["set"];
  users: number;
  /** The opts of the `set` currently executing on this store, if any. */
  current: SetOpts | undefined;
  inSet: boolean;
}

const taps = new WeakMap<ParamsStore, Tap>();

function tap(store: ParamsStore): Tap {
  let t = taps.get(store);
  if (t) {
    t.users++;
    return t;
  }
  const orig = store.set;
  const created: Tap = {
    orig,
    users: 1,
    current: undefined,
    inSet: false,
    wrapper: (patch, opts) => {
      const prevOpts = created.current;
      const prevIn = created.inSet;
      created.current = opts;
      created.inSet = true;
      try {
        orig.call(store, patch, opts);
      } finally {
        created.current = prevOpts;
        created.inSet = prevIn;
      }
    },
  };
  store.set = created.wrapper;
  taps.set(store, created);
  return created;
}

function untap(store: ParamsStore): void {
  const t = taps.get(store);
  if (!t) return;
  t.users--;
  if (t.users > 0) return;
  taps.delete(store);
  // Restore only if nobody replaced our wrapper in the meantime.
  if (store.set === t.wrapper) store.set = t.orig;
}

function pick(
  all: ParamsSnapshot,
  keys: readonly string[],
): Record<string, string | undefined> {
  const out: Record<string, string | undefined> = {};
  for (const k of keys) out[k] = all[k];
  return out;
}

export interface MirrorOpts {
  /** Start: copy the mount's `run` up into a shared store that has none. Default false — a
   *  `run` only ever flows shared→mount at start (a stale deep-link `run` must not be planted
   *  in a chat that already knows its own run state). */
  seedRun?: boolean;
}

const SESSION = "session_id";
const RUN = "run";

/** Two-way mirror of `keys` between two ParamsStores, loop-safe (a write is skipped when the
 *  target already holds that value). Start: for each key, if `shared` has a value copy shared→mount,
 *  else if `mount` has one copy mount→shared (`run` only with `opts.seedRun`). Then subscribe both
 *  ways via `onChange`.
 *
 *  `session_id` flows shared→mount ONLY once live: a mount-side change of it (popstate, a door
 *  opening another chat, a host-id push) is a session SWAP the mount handles by re-acquiring, not
 *  a fact about this chat. The other keys flow mount→shared only while both stores name the same
 *  session (or the shared one names none yet).
 *
 *  A mirrored write passes the originating `set`'s opts through as-is (a bare `set` stays bare —
 *  the URL store's bare = gesture-gated push), and "replace" when no `set` is in flight (a
 *  popstate, a `fused:urlchange`). Returns the unsubscribe. */
export function mirrorParams(
  mount: ParamsStore,
  shared: ParamsStore,
  keys: readonly string[],
  opts: MirrorOpts = {},
): () => void {
  if (mount === shared || keys.length === 0) return () => {};

  // ---- start: shared wins, mount fills the gaps -----------------------------
  const toMount: ParamsPatch = {};
  const toShared: ParamsPatch = {};
  for (const k of keys) {
    const s = shared.get(k);
    const m = mount.get(k);
    if (s !== undefined) {
      if (m !== s) toMount[k] = s;
    } else if (m !== undefined && (k !== RUN || opts.seedRun)) {
      toShared[k] = m;
    }
  }
  if (Object.keys(toMount).length) mount.set(toMount, { history: "replace" });
  if (Object.keys(toShared).length)
    shared.set(toShared, { history: "replace" });

  const mountTap = tap(mount);
  const sharedTap = tap(shared);
  let lastMount = pick(mount.getAll(), keys);
  let lastShared = pick(shared.getAll(), keys);
  let live = true;

  const relay = (
    all: ParamsSnapshot,
    last: Record<string, string | undefined>,
    target: ParamsStore,
    sourceTap: Tap,
    upward: boolean,
  ): Record<string, string | undefined> => {
    const next = pick(all, keys);
    const patch: ParamsPatch = {};
    let any = false;
    // Mount→shared only while both name the same chat (or the shared one names none yet).
    const sharedSession = shared.get(SESSION);
    const sameChat =
      sharedSession === undefined || all[SESSION] === sharedSession;
    for (const k of keys) {
      if (upward && (k === SESSION || !sameChat)) continue;
      const v = next[k];
      if (v === last[k]) continue;
      if (target.get(k) === v) continue;
      patch[k] = v === undefined ? null : v;
      any = true;
    }
    if (any) {
      if (sourceTap.inSet) target.set(patch, sourceTap.current);
      else target.set(patch, { history: "replace" });
    }
    return next;
  };

  const offMount = mount.onChange((all) => {
    if (!live) return;
    lastMount = relay(all, lastMount, shared, mountTap, true);
  });
  const offShared = shared.onChange((all) => {
    if (!live) return;
    lastShared = relay(all, lastShared, mount, sharedTap, false);
  });

  return () => {
    if (!live) return;
    live = false;
    offMount();
    offShared();
    untap(mount);
    untap(shared);
  };
}

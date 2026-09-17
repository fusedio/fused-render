// The chat's view-state params (`session_id`, `run`, `split`, …), behind one
// interface with two backings: the SHELL URL (sidebar / content / canvas —
// bookmarkable, what the template wrote through `fused.params`) and MEMORY
// (cards / peek / panel / tab — what the iframe's `_fusedParamBoundary` used to
// keep off the shell URL). Pure TS, no React; `useChatParams.ts` subscribes.
import { splitShellSearch } from "@platform/lib/layout-codec";
import { replaceSearch } from "@platform/lib/router";

/** The 11 keys the template reads/writes (T params.set list; design.md §2),
 *  plus `queued` — the project queue's own (2026-09-12, see below).
 *
 *  `_file` is deliberately NOT here. runtime.js THROWS for any key starting
 *  with `_` (R:885-889) — the underscore namespace is the runtime's own — so
 *  the target is something the chat is TOLD, not something it can set, and T
 *  never sets it either. */
export const CHAT_PARAM_KEYS = [
  "session_id",
  /**
   * A CHAT THAT HAS NEVER RUN, NAMED BY THE ENTRY IT IS WAITING AS —
   * `queued=<entry id>` (`platform/lib/queue.QUEUED_PARAM`).
   *
   * `session_id` cannot open one: there is no session, because nothing has run.
   * The conversation exists all the same — it is a leader entry plus every
   * message typed behind it, grouped server-side under `pending:<leader id>` —
   * and the leader's id is the only name it has until the scheduler gives it a
   * real one. A pane mounting with this remembers it as its queue leader, which
   * is what draws the waiting rows, reads the right `/api/tasks` row for the
   * header, and adopts the session the moment the leader runs.
   *
   * Never written beside a `session_id`: the moment there is a session, that is
   * the name, and this one is cleared.
   */
  "queued",
  "run",
  "permission",
  "split",
  "paneview",
  "annmode",
  "annotations",
  "model",
  "effort",
  "msg",
  "leftmode",
] as const;
export type ChatParamKey = (typeof CHAT_PARAM_KEYS)[number];

/** R:885-889 — the runtime reserves the `_` namespace for itself (`_file`,
 *  `_layout`, `_remote`, `_preview`, …) and throws on a write to it. Here a
 *  reserved write is dropped with a dev warning rather than thrown: the chat is
 *  one pane of a page, and a bad param write is not worth taking the shell down
 *  with it. */
export function isReservedParam(key: string): boolean {
  return key.startsWith("_");
}

function withoutReserved(patch: ParamsPatch): ParamsPatch {
  let clean: ParamsPatch | null = null;
  for (const k of Object.keys(patch)) {
    if (!isReservedParam(k)) continue;
    if (!clean) clean = { ...patch };
    delete clean[k];
    if (typeof console !== "undefined") {
      console.warn("[chat] refusing to write the reserved param " + JSON.stringify(k));
    }
  }
  return clean ?? patch;
}

/** `null` removes the key (runtime.js set(): `k=` is not "none" for ids). */
export type ParamsPatch = Record<string, string | null>;
export type ParamsSnapshot = Record<string, string>;

export interface SetOpts {
  /** The DEFAULT is push — once per visit, gesture-gated (R:1254, D268): a bare
   *  `set` takes the visit's one history entry if, and only if, the user has
   *  already interacted with this document and this entry has not been pushed
   *  for. Everything else — an explicit `"replace"`, a write before any gesture
   *  (a param the page computed for itself at boot), a second write on an entry
   *  already pushed for — takes the coalesced replace path instead.
   *  `"replace"` says "never spend the push, whatever else is true". */
  history?: "replace" | "push";
}

export interface ParamsStore {
  get(key: string): string | undefined;
  getAll(): ParamsSnapshot;
  set(patch: ParamsPatch, opts?: SetOpts): void;
  /** Fires only when the visible snapshot actually changed (D46). */
  onChange(cb: (all: ParamsSnapshot) => void): () => void;
}

/** runtime.js HISTORY_MIN_INTERVAL_MS (R:930): WebKit throttles history writes. */
export const HISTORY_MIN_INTERVAL_MS = 400;
/** history.state flag marking the entry a param write already pushed (R:1256). */
export const PARAM_ENTRY_FLAG = "fusedParamEntry";

function applyDelta(search: string, delta: Map<string, string | null>): string {
  const { layout, params } = splitShellSearch(search);
  for (const [k, v] of delta) {
    if (v === null) params.delete(k);
    else params.set(k, v);
  }
  let out = params.toString();
  // `_layout=(...)` stays raw and LAST (D51), untouched by URLSearchParams —
  // and RAW means raw: R:989-990 reinserts the span byte-for-byte, and
  // `urlSafeLayout` (layout-codec) has already escaped the only three
  // characters that need it. Re-encoding here turned every `,` `/` `=` in a
  // layout into percent-soup on the first chat param write, and the result
  // stopped matching what the layout writer produces.
  if (layout !== null) out += (out ? "&" : "") + "_layout=(" + layout + ")";
  return out ? "?" + out : "";
}

function snapshotOf(search: string): ParamsSnapshot {
  const out: ParamsSnapshot = {};
  for (const [k, v] of splitShellSearch(search).params) out[k] = v;
  return out;
}

/** Key-ORDER-insensitive: a URL rewrite that reorders keys without changing any
 *  value is not a change, and `JSON.stringify` of a plain object says it is. */
function fingerprint(snapshot: ParamsSnapshot): string {
  return JSON.stringify(
    Object.keys(snapshot)
      .sort()
      .map((k) => [k, snapshot[k]]),
  );
}

function makeNotifier() {
  const listeners = new Set<(all: ParamsSnapshot) => void>();
  let last: string | null = null;
  return {
    listeners,
    notifyIfChanged(snapshot: ParamsSnapshot) {
      const s = fingerprint(snapshot);
      if (s === last) return;
      last = s;
      for (const cb of listeners) cb(snapshot);
    },
  };
}

/** The browser pieces the URL store touches — injectable for bun tests. */
export interface UrlEnv {
  location: { pathname: string; search: string };
  history: { state: unknown; pushState(state: unknown, unused: string, url: string): void };
  /** `history.replaceState` through the shell's wrapper (fires fused:urlchange). */
  replace(url: string): void;
  /** Where `fused:urlchange` / `popstate` / `pagehide` arrive, and where the
   *  store DISPATCHES `fused:urlchange` on every `set` (R:1294). */
  events: EventTarget;
  /** R:1294 — `set()` announces on the event path so the shell's own consumers
   *  (`useUrlVersion`, the layout-codec hooks) see a chat param the moment it is
   *  written rather than up to one coalescing window later. */
  dispatchUrlChange(): void;
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(id: unknown): void;
}

function browserEnv(): UrlEnv {
  return {
    location,
    history,
    replace: replaceSearch,
    events: window,
    dispatchUrlChange: () => window.dispatchEvent(new Event("fused:urlchange")),
    now: Date.now,
    setTimeout: (fn, ms) => window.setTimeout(fn, ms),
    clearTimeout: (id) => window.clearTimeout(id as number),
  };
}

/**
 * Shell-URL backed store with runtime.js's coalescing (R:920-1370, D99): a
 * pending KEY→VALUE overlay serves readers at once, history sees ≤1 write per
 * 400 ms with a trailing flush, `pagehide` flushes, a traversal (`popstate`)
 * drops the pending write, and a write aimed at a pathname we have since left
 * is dropped rather than invented on the new page.
 */
export function createUrlParamsStore(
  env: UrlEnv = browserEnv(),
): ParamsStore & { attach(): void; dispose(): void } {
  const { listeners, notifyIfChanged } = makeNotifier();
  let pending: Map<string, string | null> | null = null;
  let pendingPath: string | null = null;
  let timer: unknown = null;
  let lastWrite = 0;
  // R:1056-1075 `sawGesture`. The once-per-visit push may only be spent on a
  // write the USER caused: a param this page computes for itself at boot (the
  // session id a poll reported, a mode it stamped) describes the state it
  // ALREADY loaded in, and pushing for it is a Back trap — Back lands on the
  // pristine entry, the view re-seeds, and pushes again. Sticky and
  // capture-phase for the reason R gives: the question is "has this document
  // been interacted with at all", and a control that stops propagation on its
  // own events still counts.
  let sawGesture = false;
  const markGesture = () => {
    sawGesture = true;
  };

  const pendingIsStale = () => pendingPath !== null && pendingPath !== env.location.pathname;
  const targetSearch = () =>
    pendingIsStale() || !pending ? env.location.search : applyDelta(env.location.search, pending);

  const cancelPending = () => {
    pending = null;
    pendingPath = null;
    if (timer !== null) {
      env.clearTimeout(timer);
      timer = null;
    }
  };

  const flush = () => {
    timer = null;
    if (!pending) return;
    const delta = pending;
    const stale = pendingIsStale();
    pending = null;
    pendingPath = null;
    if (stale) return;
    const search = applyDelta(env.location.search, delta);
    if (search === env.location.search) return;
    lastWrite = env.now();
    try {
      env.replace(env.location.pathname + search);
    } catch {
      // WebKit throttle hit anyway; the overlay already served readers.
    }
  };

  // CACHED BY THE SEARCH STRING IT WAS PARSED FROM. Two reasons, and the second
  // is load-bearing: the search is re-parsed per key per render otherwise, and
  // `useSyncExternalStore` requires a snapshot whose identity is stable while
  // nothing has changed — a fresh object per read is an infinite render loop.
  let cachedSearch: string | null = null;
  let cached: ParamsSnapshot = {};
  const getAll = () => {
    const search = targetSearch();
    if (search !== cachedSearch) {
      cachedSearch = search;
      cached = snapshotOf(search);
    }
    return cached;
  };
  const onUrlChange = () => notifyIfChanged(getAll());
  const onPopState = () => {
    cancelPending();
    notifyIfChanged(getAll());
  };
  // THE FIVE WINDOW LISTENERS, AND WHO KEEPS THEM. Not the constructor: a store
  // built in a render React DISCARDS (StrictMode double-invokes a `useState`
  // initializer, a concurrent render can be interrupted, a Suspense retry
  // re-runs one) would then hold five capture-phase `window` listeners for the
  // life of the tab with nothing left to detach them — the `[]`-dep cleanup only
  // ever sees the store that got committed. So binding is driven by DEMAND:
  //   * a SUBSCRIBER (`onChange`) — the first one binds, the last one unbinds,
  //     which is what makes a discarded store cost nothing, and
  //   * an explicit `attach()` — a mount saying "this store is mine now", which
  //     holds the binding open even with no subscriber yet, because
  //     `pointerdown`/`keydown` have to be armed BEFORE the user's first gesture
  //     (that gesture is what unlocks the visit's one history push).
  // `dispose()` gives that ownership up. Both are idempotent.
  let listening = false;
  let owned = false;
  const sync = () => {
    const want = owned || listeners.size > 0;
    if (want === listening) return;
    listening = want;
    if (want) {
      env.events.addEventListener("fused:urlchange", onUrlChange);
      env.events.addEventListener("popstate", onPopState);
      env.events.addEventListener("pagehide", flush);
      env.events.addEventListener("pointerdown", markGesture, true);
      env.events.addEventListener("keydown", markGesture, true);
    } else {
      env.events.removeEventListener("fused:urlchange", onUrlChange);
      env.events.removeEventListener("popstate", onPopState);
      env.events.removeEventListener("pagehide", flush);
      env.events.removeEventListener("pointerdown", markGesture, true);
      env.events.removeEventListener("keydown", markGesture, true);
    }
  };
  /** Idempotent — a StrictMode remount re-attaches a store it has disposed
   *  rather than being left deaf to `fused:urlchange`/`popstate`. */
  const attach = () => {
    owned = true;
    sync();
  };
  notifyIfChanged(getAll()); // baseline so the first no-op change is silent

  return {
    attach,
    get: (key) => getAll()[key],
    getAll,
    set(rawPatch, opts) {
      const patch = withoutReserved(rawPatch);
      const delta = new Map(Object.entries(patch));
      const before = targetSearch();
      const after = applyDelta(before, delta);
      if (after !== before) {
        const state = env.history.state as Record<string, unknown> | null;
        const pristine = !(state && state[PARAM_ENTRY_FLAG]);
        // R:1254's three ways a bare `set` still takes the coalesced replace
        // path: an explicit `{history:"replace"}`, no gesture in this document
        // yet, or an entry this store has already pushed for.
        if (opts?.history !== "replace" && sawGesture && pristine) {
          // The once-per-visit push: immediate, so Back gets its entry.
          cancelPending();
          lastWrite = env.now();
          env.history.pushState({ ...(state ?? {}), [PARAM_ENTRY_FLAG]: true }, "", env.location.pathname + after);
        } else {
          if (!pending || pendingIsStale()) {
            pending = new Map();
            pendingPath = env.location.pathname;
          }
          for (const [k, v] of delta) pending.set(k, v);
          if (timer === null) {
            const wait = Math.max(0, HISTORY_MIN_INTERVAL_MS - (env.now() - lastWrite));
            if (wait === 0) flush();
            else timer = env.setTimeout(flush, wait);
          }
        }
      }
      notifyIfChanged(getAll());
      // Announced on EVERY set, landed or queued (R:1294): a consumer outside
      // this store reads the URL, and the overlay is already serving the new
      // value — waiting for the flush would leave them up to 400 ms behind.
      env.dispatchUrlChange();
    },
    onChange(cb) {
      listeners.add(cb);
      sync(); // the first subscriber binds (see `sync`)
      return () => {
        listeners.delete(cb);
        sync(); // …and the last one to leave unbinds
      };
    },
    dispose() {
      flush();
      owned = false;
      listeners.clear();
      sync();
    },
  };
}

/** In-memory store for cards / peek: same contract, no URL, `opts` ignored. */
export function createMemoryParamsStore(initial: ParamsSnapshot = {}): ParamsStore {
  const { listeners, notifyIfChanged } = makeNotifier();
  const map = new Map<string, string>(Object.entries(initial));
  // Cached, and replaced only when a write changes something: `useChatParams`
  // reads this on every React render, and a fresh object each time is a
  // snapshot no consumer can memo on.
  let snapshot: ParamsSnapshot = Object.fromEntries(map) as ParamsSnapshot;
  const getAll = () => snapshot;
  notifyIfChanged(getAll());
  return {
    get: (key) => map.get(key),
    getAll,
    set(rawPatch) {
      const patch = withoutReserved(rawPatch);
      let changed = false;
      for (const [k, v] of Object.entries(patch)) {
        if (v === null) changed = map.delete(k) || changed;
        else if (map.get(k) !== v) {
          map.set(k, v);
          changed = true;
        }
      }
      if (changed) snapshot = Object.fromEntries(map) as ParamsSnapshot;
      notifyIfChanged(getAll());
    },
    onChange(cb) {
      listeners.add(cb);
      return () => {
        listeners.delete(cb);
      };
    },
  };
}

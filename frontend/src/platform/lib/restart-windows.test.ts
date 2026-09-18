// TWO WINDOWS, ONE RESTART — the case the real-app test of PR #1214 failed on.
//
// Akshil had two Chrome windows on the packaged app. The blocking dialog came up
// in BOTH on its own (the proactive door works per window). He pressed Restart in
// one; the other ended up showing the red "fused-render isn't running" card
// instead of the stages, which is what a window that never learned of the press
// shows once its own probes start failing.
//
// The window that is not being clicked in is, by definition, the BACKGROUND one,
// and a background window is throttled and eventually frozen — so the
// `BroadcastChannel` message, a one-shot event, is exactly the thing it can
// miss. The durable localStorage record and the wake-up re-read are what make
// the second window's story survive that; the channel is only the fast path.
//
// Both stores here run against ONE fake channel bus and ONE fake storage, which
// is what two same-origin windows actually share.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";

const { createRestartStore, RESTART_STORAGE_KEY } = await import("@platform/lib/restart-store");
const { RESTART_GIVE_UP_MS } = await import("@platform/lib/restart-flow");
const { bannerSurface } = await import("@platform/lib/server-status");

type Store = ReturnType<typeof createRestartStore>;

/** One origin's shared state: the broadcast bus every awake window is attached
 *  to, and the one localStorage behind them all. */
function origin() {
  const attached: Array<(data: unknown) => void> = [];
  const items = new Map<string, string>();
  let clock = 1_000_000;
  const made: Store[] = [];
  /** What every window's `/api/config` answers. `null` = the server is not
   *  answering at all, which is what a restart in flight looks like. */
  let serving: string | null = "0.5.90";

  /** `awake: false` models a throttled/frozen background window — it holds a
   *  channel that never delivers, which is precisely what Chrome does to a
   *  window nobody is looking at. */
  function windowFor({ awake = true }: { awake?: boolean } = {}) {
    const wakers: Array<() => void> = [];
    const navigations: string[] = [];
    const store = createRestartStore({
      channel: () => {
        const ch = {
          postMessage: (data: unknown) => {
            // A real BroadcastChannel never echoes to its own sender.
            for (const deliver of attached) if (deliver !== own) deliver(data);
          },
          onmessage: null as ((ev: { data: unknown }) => void) | null,
          close: () => {
            const i = attached.indexOf(own);
            if (i >= 0) attached.splice(i, 1);
          },
        };
        const own = (data: unknown) => {
          if (awake) ch.onmessage?.({ data });
        };
        attached.push(own);
        return ch;
      },
      storage: () => ({
        getItem: (k) => items.get(k) ?? null,
        setItem: (k, v) => void items.set(k, v),
        removeItem: (k) => void items.delete(k),
      }),
      listen: (_type, fn) => wakers.push(fn),
      ask: async () =>
        serving === null ? { ok: false, version: null } : { ok: true, version: serving },
      navigate: (href) => void navigations.push(href),
      now: () => clock,
    });
    made.push(store);
    // Every real window MOUNTS `ServerStatusBanner`, which subscribes — which is
    // what starts the store, attaches its channel and reads the record. A test
    // window that never subscribed would be a window with no banner in it.
    store.subscribe(() => {});
    return {
      store,
      navigations,
      stage: () => store.snapshot().stage,
      /** What this window's banner would actually PUT ON SCREEN. */
      surface: (banner: Parameters<typeof bannerSurface>[0]["banner"]) =>
        bannerSurface({ banner, mode: "real", stage: store.snapshot().stage }),
      /** The user looking at this window again: every registered wake-up fires.
       *  Async, because adopting a record means asking the server first. */
      focus: async () => {
        wakers.forEach((fn) => fn());
        await store.wake();
      },
      /** A fresh document reads the record on start — the same await. */
      settle: () => store.wake(),
    };
  }

  return {
    windowFor,
    advance: (ms: number) => {
      clock += ms;
    },
    /** What the server answers from now on; `null` means it is down. */
    serve: (version: string | null) => {
      serving = version;
    },
    stored: () => items.get(RESTART_STORAGE_KEY) ?? null,
    disposeAll: () => made.forEach((s) => s.dispose()),
  };
}

const origins: Array<ReturnType<typeof origin>> = [];
function twoWindows(opts: { bAwake?: boolean } = {}) {
  const o = origin();
  origins.push(o);
  const a = o.windowFor();
  const b = o.windowFor({ awake: opts.bAwake ?? true });
  // Both have been talking to the same healthy server.
  a.store.noteRestartProbe({ ok: true, version: "0.5.90" });
  b.store.noteRestartProbe({ ok: true, version: "0.5.90" });
  return { o, a, b };
}
afterEach(() => {
  for (const o of origins.splice(0)) o.disposeAll();
});

test("a press in A puts B on the same stage, through the channel", () => {
  const { a, b } = twoWindows();
  a.store.requestRestart();
  expect(a.stage()).toBe("quitting");
  expect(b.stage()).toBe("quitting");
  expect(a.navigations).toEqual(["fused-render://relaunch"]);
  // B navigated nowhere — it only adopted the story.
  expect(b.navigations).toEqual([]);
});

test("BOTH windows reach reconnecting on their own probes, and NEITHER shows the down card", () => {
  const { o, a, b } = twoWindows();
  a.store.requestRestart();
  // The app goes away. Each window is polling for itself.
  for (const w of [a, b]) {
    o.advance(5_000);
    w.store.noteRestartProbe({ ok: false });
    w.store.noteRestartProbe({ ok: false });
  }
  expect(a.stage()).toBe("reconnecting");
  expect(b.stage()).toBe("reconnecting");
  // THE WHOLE POINT: the banner in both windows has reduced to "down" by now,
  // and neither is allowed to draw that card while the restart is in flight.
  expect(a.surface("down")).toBe("restart-dialog");
  expect(b.surface("down")).toBe("restart-dialog");
});

test("a BACKGROUND window that missed the broadcast still joins on wake-up", async () => {
  // The reported failure. B is frozen, so the one-shot `postMessage` lands on
  // nothing; without the durable record B would sit at `ready` and fall straight
  // through to the down card the moment its probes failed.
  const { o, a, b } = twoWindows({ bAwake: false });
  a.store.requestRestart();
  expect(b.stage()).toBe("ready");
  // …and that is exactly what it used to show.
  o.advance(10_000);
  b.store.noteRestartProbe({ ok: false });
  b.store.noteRestartProbe({ ok: false });
  expect(b.surface("down")).toBe("down");

  // The app is between processes, so nothing answers — which is itself evidence
  // the restart is still running.
  o.serve(null);
  // The user looks at it. It reads the record, adopts A's instant, and tells the
  // same story A is telling.
  await b.focus();
  expect(b.stage()).toBe("quitting");
  expect(b.surface("down")).toBe("restart-dialog");
});

test("a window that RELOADS mid-restart picks the story back up", async () => {
  // Akshil reloaded. A fresh document has no memory at all, so without the
  // record it would show the down card over a restart that is still in flight.
  const { o, a } = twoWindows();
  a.store.requestRestart();
  const at = JSON.parse(o.stored()!).at;
  o.advance(20_000);
  // Still the old process answering — the teardown has not finished.
  o.serve("0.5.90");
  const reloaded = o.windowFor();
  await reloaded.settle();
  expect(reloaded.stage()).toBe("quitting");
  expect(reloaded.store.snapshot().requestedAt).toBe(at);
  // And it inherits the ORIGINAL clock, so it gives up when A does, not 20s later.
  o.advance(RESTART_GIVE_UP_MS - 20_000 + 1);
  reloaded.store.noteRestartProbe({ ok: false });
  expect(reloaded.stage()).toBe("gave-up");
});

test("the record dies with the flow, so a finished restart is never re-adopted", async () => {
  const { o, a, b } = twoWindows();
  a.store.requestRestart();
  expect(o.stored()).not.toBeNull();
  o.advance(8_000);
  a.store.noteRestartProbe({ ok: false });
  // The successor answers on the new version: the flow is over.
  a.store.noteRestartProbe({ ok: true, version: "0.5.91" });
  expect(a.stage()).toBe("back");
  expect(o.stored()).toBeNull();
  // A window opening now — the page that reloads onto the new version — must
  // NOT put a blocking dialog up for a press that has already completed.
  o.serve("0.5.91");
  const fresh = o.windowFor();
  await fresh.settle();
  expect(fresh.stage()).toBe("ready");
  expect(b.stage()).not.toBe("back");
});

test("the cap clears the record too", () => {
  const { o, a } = twoWindows();
  a.store.requestRestart();
  o.advance(RESTART_GIVE_UP_MS + 1);
  a.store.noteRestartProbe({ ok: false });
  expect(a.stage()).toBe("gave-up");
  expect(o.stored()).toBeNull();
  // …and with the story over and the server still not answering, the down card
  // is what the page is allowed to show again.
  expect(a.surface("down")).toBe("down");
});

test("a stale record from a previous session is never adopted", async () => {
  const { o, a } = twoWindows();
  a.store.requestRestart();
  // Older than the cap by the time anyone looks: a press from a session that is
  // long over must not raise a blocking dialog on a healthy app.
  o.advance(RESTART_GIVE_UP_MS + 60_000);
  const fresh = o.windowFor();
  await fresh.settle();
  expect(fresh.stage()).toBe("ready");
  // And it tidies up after itself rather than leaving the key to be re-read.
  expect(o.stored()).toBeNull();
});

// ---- the record is evidence of a PRESS, not of a restart still running ------
// (bugbot, PR #1214). Clearing it when the flow ends cannot be the only guard: a
// document that reloads without ever passing through `back` — a press made
// before any healthy probe recorded a version, a discarded tab restoring onto
// the new server — starts from nothing and would adopt a finished press straight
// back off disk, raising a dialog with no ✕, no Esc and no backdrop over a
// server that is already fine.

test("a record is NOT adopted once the server is already on a newer version", async () => {
  const { o, a } = twoWindows();
  a.store.requestRestart();
  // The restart completed; this document is a fresh one on the new server, and
  // the record survived because nothing here ever reached `back`.
  o.advance(15_000);
  o.serve("0.5.91");
  const fresh = o.windowFor();
  await fresh.settle();
  expect(fresh.stage()).toBe("ready");
  // …and it tidies up, so the next document does not pay for the same request.
  expect(o.stored()).toBeNull();
});

test("a record IS adopted while the server is still on the old version", async () => {
  const { o, a } = twoWindows();
  a.store.requestRestart();
  o.advance(15_000);
  o.serve("0.5.90");
  const fresh = o.windowFor();
  await fresh.settle();
  expect(fresh.stage()).toBe("quitting");
  expect(o.stored()).not.toBeNull();
});

test("a server that does not answer at all counts as a restart in flight", async () => {
  const { o, a } = twoWindows();
  a.store.requestRestart();
  o.advance(15_000);
  o.serve(null);
  const fresh = o.windowFor();
  await fresh.settle();
  expect(fresh.stage()).toBe("quitting");
});

test("a record with no version on it is never adopted", async () => {
  // Nothing to compare against, so there is no way to tell a restart in flight
  // from one that finished — and the tie goes to NOT raising a dialog the user
  // cannot dismiss.
  const o = origin();
  origins.push(o);
  const a = o.windowFor();
  a.store.requestRestart(); // no probe ever ran, so `served` is null
  expect(JSON.parse(o.stored()!).served).toBeNull();
  // With the server DOWN there is nothing to compare against from either side —
  // the case where "ask the server" cannot save us, so the record itself has to
  // be refused. Adopting here would raise a dialog that can only ever end at the
  // 60s cap.
  o.serve(null);
  const fresh = o.windowFor();
  await fresh.settle();
  expect(fresh.stage()).toBe("ready");
  expect(o.stored()).toBeNull();
});

test("the reload path forgets the record on its way out", async () => {
  // `ServerStatusBanner` calls this before `location.reload()`, which is the end
  // of the restart whether or not the stage machine ever reached `back`.
  const { o, a } = twoWindows();
  a.store.requestRestart();
  expect(o.stored()).not.toBeNull();
  a.store.forget();
  expect(o.stored()).toBeNull();
});

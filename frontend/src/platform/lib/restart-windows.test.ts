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
      /** The user looking at this window again: every registered wake-up fires. */
      focus: () => wakers.forEach((fn) => fn()),
    };
  }

  return {
    windowFor,
    advance: (ms: number) => {
      clock += ms;
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

test("a BACKGROUND window that missed the broadcast still joins on wake-up", () => {
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

  // The user looks at it. It reads the record, adopts A's instant, and tells the
  // same story A is telling.
  b.focus();
  expect(b.stage()).toBe("quitting");
  expect(b.surface("down")).toBe("restart-dialog");
});

test("a window that RELOADS mid-restart picks the story back up", () => {
  // Akshil reloaded. A fresh document has no memory at all, so without the
  // record it would show the down card over a restart that is still in flight.
  const { o, a } = twoWindows();
  a.store.requestRestart();
  const at = JSON.parse(o.stored()!).at;
  o.advance(20_000);
  const reloaded = o.windowFor();
  expect(reloaded.stage()).toBe("quitting");
  expect(reloaded.store.snapshot().requestedAt).toBe(at);
  // And it inherits the ORIGINAL clock, so it gives up when A does, not 20s later.
  o.advance(RESTART_GIVE_UP_MS - 20_000 + 1);
  reloaded.store.noteRestartProbe({ ok: false });
  expect(reloaded.stage()).toBe("gave-up");
});

test("the record dies with the flow, so a finished restart is never re-adopted", () => {
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
  const fresh = o.windowFor();
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

test("a stale record from a previous session is never adopted", () => {
  const { o, a } = twoWindows();
  a.store.requestRestart();
  // Older than the cap by the time anyone looks: a press from a session that is
  // long over must not raise a blocking dialog on a healthy app.
  o.advance(RESTART_GIVE_UP_MS + 60_000);
  const fresh = o.windowFor();
  expect(fresh.stage()).toBe("ready");
  // And it tidies up after itself rather than leaving the key to be re-read.
  expect(o.stored()).toBeNull();
});

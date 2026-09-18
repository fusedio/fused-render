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

/** Let every pending microtask AND the timer queue turn over, which is what an
 *  in-flight `wake()` needs to finish: it awaits the host's `ask`, then re-reads
 *  the record it was verifying. */
const flush = () => new Promise<void>((done) => setTimeout(done, 0));

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
  /** When true, `ask()` never resolves on its own — the store's own timeout is
   *  the only thing that can end it. */
  let hold = false;
  /** How many times ANY window has asked the server. One record must cost one
   *  ask, however many wake-ups fire on it. */
  let asks = 0;

  /** A test can hold `ask()` open to model a slow or hung server. */
  let held: null | { release: (answer: { ok: boolean; version: string | null }) => void } = null;

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
      ask: () => {
        asks += 1;
        if (hold) {
          return new Promise<{ ok: boolean; version: string | null }>((resolve) => {
            held = { release: resolve };
          });
        }
        return Promise.resolve(
          serving === null ? { ok: false, version: null } : { ok: true, version: serving },
        );
      },
      askTimeoutMs: 40,
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
        await flush();
      },
      /** A fresh document reads the record on start (mounting the banner
       *  subscribes, which starts the store, which wakes). Draining the queue is
       *  how a test waits for THAT wake rather than starting a competing one —
       *  a second `wake()` would be refused as a duplicate and return before the
       *  first had answered. */
      settle: flush,
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
    /** Hang every `ask()` from now on. */
    hang: (on: boolean) => {
      hold = on;
    },
    releaseAsk: (answer: { ok: boolean; version: string | null }) => {
      held?.release(answer);
      held = null;
    },
    held: () => held !== null,
    asks: () => asks,
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

// ---- what a wake decides is STALE by the time it decides it ----------------
// (bugbot, PR #1214, HIGH). The request takes a moment, and in that moment a
// press can land here or a broadcast can arrive from another window — both NEWER
// than the record the wake read. Writing `null` then destroys the new press's
// record; latching the old `at` expires the cap early and drops other windows
// off the story.

test("a press landing during an in-flight wake wins, and its record survives", async () => {
  const o = origin();
  origins.push(o);
  const a = o.windowFor();
  a.store.noteRestartProbe({ ok: true, version: "0.5.90" });
  a.store.requestRestart();
  const firstAt = JSON.parse(o.stored()!).at;

  // A fresh document comes up and starts verifying that record.
  o.advance(5_000);
  o.hang(true);
  const b = o.windowFor();
  await flush();
  expect(o.held()).toBe(true);

  // While the answer is out, a NEWER press happens.
  o.advance(1_000);
  b.store.noteRestartProbe({ ok: true, version: "0.5.90" });
  b.store.requestRestart();
  const secondAt = JSON.parse(o.stored()!).at;
  expect(secondAt).toBeGreaterThan(firstAt);

  // The stale answer arrives saying "already on a new version" — which for the
  // OLD record would mean "clear it". It must not touch the new one.
  o.releaseAsk({ ok: true, version: "0.5.91" });
  await flush();
  expect(o.stored()).not.toBeNull();
  expect(JSON.parse(o.stored()!).at).toBe(secondAt);
  // …and the window is living the NEW press, on the NEW instant.
  expect(b.store.snapshot().requestedAt).toBe(secondAt);
  expect(b.stage()).toBe("quitting");
});

test("a hung server resolves on the store's own timeout, and adopts", async () => {
  // The banner's probe to the same endpoint has a 4s budget; a wake with none
  // would hold the dialog's button in limbo for as long as the socket stayed
  // open. A timeout means the same thing a refused connection does here.
  const o = origin();
  origins.push(o);
  const a = o.windowFor();
  a.store.noteRestartProbe({ ok: true, version: "0.5.90" });
  a.store.requestRestart();
  o.advance(5_000);
  o.hang(true);
  const b = o.windowFor();
  await flush();
  expect(b.store.snapshot().verifying).toBe(true);
  expect(b.stage()).toBe("ready");
  // Nothing ever answers; the store's own clock ends it.
  await new Promise((done) => setTimeout(done, 80));
  expect(b.store.snapshot().verifying).toBe(false);
  expect(b.stage()).toBe("quitting");
});

test("the button is not offered while a wake is in flight", async () => {
  // `stage` reads `ready` during the gap and may be about to say otherwise, so a
  // press in it would start a SECOND restart on top of the one being verified.
  const o = origin();
  origins.push(o);
  const a = o.windowFor();
  a.store.noteRestartProbe({ ok: true, version: "0.5.90" });
  a.store.requestRestart();
  o.advance(5_000);
  o.hang(true);
  const b = o.windowFor();
  await flush();
  expect(b.store.snapshot()).toMatchObject({ stage: "ready", verifying: true });
  o.releaseAsk({ ok: true, version: "0.5.90" });
  await flush();
  expect(b.store.snapshot()).toMatchObject({ stage: "quitting", verifying: false });
});

test("two overlapping wakes produce one outcome", async () => {
  // `focus` and `visibilitychange` both fire on the same gesture.
  const o = origin();
  origins.push(o);
  const a = o.windowFor();
  a.store.noteRestartProbe({ ok: true, version: "0.5.90" });
  a.store.requestRestart();
  const at = JSON.parse(o.stored()!).at;
  o.advance(5_000);
  o.hang(true);
  const b = o.windowFor();
  await flush();
  expect(o.asks()).toBe(1);
  // A second and third wake on the same record JOIN rather than racing — each
  // would otherwise open its own request, and only the last could ever be
  // answered.
  void b.store.wake();
  void b.store.wake();
  await flush();
  expect(o.asks()).toBe(1);
  expect(o.held()).toBe(true);
  o.releaseAsk({ ok: true, version: "0.5.90" });
  await flush();
  expect(b.store.snapshot().requestedAt).toBe(at);
  expect(b.stage()).toBe("quitting");
  expect(b.store.snapshot().verifying).toBe(false);
});

// ---- the reload path, for a press that could never reach `back` -------------
// (bugbot, PR #1214, MEDIUM, case (i)). `back` needs a `served` to compare
// against, so a press made before any healthy probe cannot reach it — and the
// clear-on-ending path therefore never runs for one.

test("the reload path forgets a record that could never have reached back", async () => {
  const o = origin();
  origins.push(o);
  const a = o.windowFor();
  // No probe ever ran, so `served` is null and `back` is unreachable for this
  // press by construction.
  a.store.requestRestart();
  expect(JSON.parse(o.stored()!).served).toBeNull();
  expect(a.stage()).toBe("quitting");
  // `reduceProbe` says reload; `ServerStatusBanner` forgets first.
  a.store.forget();
  expect(o.stored()).toBeNull();
  // The document that comes up on the new server has nothing to adopt.
  o.serve("0.5.91");
  const fresh = o.windowFor();
  await fresh.settle();
  expect(fresh.stage()).toBe("ready");
});

test("a discarded tab restoring onto the new server clears the record", async () => {
  // Case (ii): the tab was never the one that reloaded, so nothing called
  // `forget()` — the server's own answer is what ends it.
  const o = origin();
  origins.push(o);
  const a = o.windowFor();
  a.store.noteRestartProbe({ ok: true, version: "0.5.90" });
  a.store.requestRestart();
  o.advance(30_000);
  o.serve("0.5.91");
  const restored = o.windowFor();
  await restored.settle();
  expect(restored.stage()).toBe("ready");
  expect(o.stored()).toBeNull();
});

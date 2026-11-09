// The shared-chat registry (refcount, grace, re-key) and its host router
// (front reads, per-sender returns, broadcasts), on a fake `make`.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";

const {
  acquireSharedChat,
  adoptKey,
  _registry,
  GRACE_MS,
  CHAT_SHARED_KEYS,
  sharedChatKey,
  SEND_RECORD_CAP,
} = await import("./chat-store");
type HostSeat = import("./chat-store").HostSeat;
type AcquireOpts = import("./chat-store").AcquireOpts;
type ChatController = import("./controller-api").ChatController;
type ControllerDeps = import("./controller-api").ControllerDeps;
type SendOptions = import("./controller-api").SendOptions;

afterEach(() => _registry().disposeAll());

function last<T>(xs: T[]): T | undefined {
  return xs[xs.length - 1];
}

function fakeTimers() {
  let next = 1;
  const pending = new Map<number, { fn: () => void; ms: number }>();
  return {
    timers: {
      set: (fn: () => void, ms: number) => {
        const h = next++;
        pending.set(h, { fn, ms });
        return h;
      },
      clear: (h: unknown) => {
        pending.delete(h as number);
      },
    },
    pending,
    fire() {
      const all = [...pending.values()];
      pending.clear();
      for (const t of all) t.fn();
    },
  };
}

function stubController() {
  const sends: Array<{ road: string; text: string; opts?: SendOptions }> = [];
  let disposed = 0;
  const c = {
    sendMessage: (text: string, opts?: SendOptions) => {
      sends.push({ road: "sendMessage", text, opts });
      return Promise.resolve();
    },
    sendFollowUp: (text: string, opts?: SendOptions) => {
      sends.push({ road: "sendFollowUp", text, opts });
      return Promise.resolve();
    },
    dispose: () => {
      disposed++;
    },
  } as unknown as ChatController;
  return { c, sends, disposed: () => disposed };
}

function setup(key: string | null = "s1", extra: Partial<AcquireOpts> = {}) {
  const ft = fakeTimers();
  const stub = stubController();
  let deps: ControllerDeps | null = null;
  let made = 0;
  const opts: AcquireOpts = {
    key,
    make: (d) => {
      deps = d;
      made++;
      return stub.c;
    },
    config: { file: "/x/chat.html" },
    timers: ft.timers,
    ...extra,
  };
  const shared = acquireSharedChat(opts);
  return { shared, opts, ft, stub, deps: () => deps!, made: () => made };
}

/** A seat whose every call is logged as `[id, method, arg]`. */
function makeSeat(
  id: string,
  log: unknown[][],
  over: Partial<HostSeat> = {},
): HostSeat {
  return {
    id,
    model: () => "model-" + id,
    effort: () => "effort-" + id,
    backend: () => "backend-" + id,
    hasPane: () => id === "pane",
    appStateBlock: () => Promise.resolve("state-" + id),
    terminalHint: () => Promise.resolve("hint-" + id),
    focusComposer: () => log.push([id, "focusComposer"]),
    onActivity: () => log.push([id, "onActivity"]),
    onStranded: (lines) => log.push([id, "onStranded", lines]),
    onSendDelivered: (cid) => log.push([id, "onSendDelivered", cid]),
    onSendReturned: (info) => log.push([id, "onSendReturned", info]),
    onArtifactsTick: () => log.push([id, "onArtifactsTick"]),
    onRunEnded: () => log.push([id, "onRunEnded"]),
    onRunAbandoned: () => log.push([id, "onRunAbandoned"]),
    onCommandsChanged: () => log.push([id, "onCommandsChanged"]),
    ...over,
  };
}

describe("registry", () => {
  test("keys", () => {
    expect([...CHAT_SHARED_KEYS]).toEqual([
      "session_id",
      "run",
      "permission",
      "backend",
    ]);
  });

  test("same key → same instance, one controller", () => {
    const a = setup("s1");
    const b = acquireSharedChat(a.opts);
    expect(b).toBe(a.shared);
    expect(a.made()).toBe(1);
    expect(_registry().size()).toBe(1);
  });

  test("params is a memory store seeded and handed to make", () => {
    const a = setup("s1", { seed: { session_id: "s1", run: "r1" } });
    expect(a.shared.params.getAll()).toEqual({ session_id: "s1", run: "r1" });
    expect(a.deps().params).toBe(a.shared.params);
    expect(a.deps().file).toBe("/x/chat.html");
  });

  test("refcount: dispose only after the last release + grace", () => {
    const a = setup("s1");
    acquireSharedChat(a.opts);
    a.shared.release();
    expect(a.ft.pending.size).toBe(0);
    a.shared.release();
    expect(a.ft.pending.size).toBe(1);
    expect([...a.ft.pending.values()][0].ms).toBe(GRACE_MS);
    expect(a.stub.disposed()).toBe(0);
    a.ft.fire();
    expect(a.stub.disposed()).toBe(1);
    expect(_registry().size()).toBe(0);
    a.shared.release(); // extra release is a no-op
    expect(a.stub.disposed()).toBe(1);
  });

  test("re-acquire mid-grace revives the same chat", () => {
    const a = setup("s1");
    a.shared.release();
    expect(a.ft.pending.size).toBe(1);
    const b = acquireSharedChat(a.opts);
    expect(b).toBe(a.shared);
    expect(a.ft.pending.size).toBe(0);
    a.ft.fire();
    expect(a.stub.disposed()).toBe(0);
    expect(a.made()).toBe(1);
  });

  test("after dispose, the key builds a fresh chat", () => {
    const a = setup("s1");
    a.shared.release();
    a.ft.fire();
    const b = acquireSharedChat(a.opts);
    expect(b).not.toBe(a.shared);
    expect(a.made()).toBe(2);
  });

  test("key null is private: never registered, still graced", () => {
    const a = setup(null);
    const b = acquireSharedChat(a.opts);
    expect(b).not.toBe(a.shared);
    expect(_registry().size()).toBe(0);
    a.shared.release();
    expect(a.stub.disposed()).toBe(0);
    a.ft.fire();
    expect(a.stub.disposed()).toBe(1);
  });

  test("adoptKey registers a private chat; a later acquire shares it", () => {
    const a = setup(null);
    expect(adoptKey(a.shared, "s9")).toBe(true);
    expect(a.shared.key).toBe("s9");
    expect(acquireSharedChat({ ...a.opts, key: "s9" })).toBe(a.shared);
    expect(a.made()).toBe(1);
  });

  test("adoptKey conflict leaves the registry alone", () => {
    const owner = setup("s9");
    const a = setup(null);
    expect(adoptKey(a.shared, "s9")).toBe(false);
    expect(a.shared.key).toBeNull();
    expect(acquireSharedChat(owner.opts)).toBe(owner.shared);
    expect(adoptKey(owner.shared, "s9")).toBe(true); // own key is fine
  });

  test("calls after dispose are no-ops", () => {
    const a = setup("s1");
    const log: unknown[][] = [];
    a.shared.attachHost(makeSeat("p", log));
    a.shared.release();
    a.ft.fire();
    expect(a.shared.frontId()).toBeNull();
    const d = a.deps();
    d.onRunEnded?.();
    d.focusComposer?.();
    d.onSendReturned?.({ text: "x" });
    expect(log).toEqual([]);
    expect(d.model?.()).toBe("");
    expect(d.hasPane?.()).toBeNull();
    expect(adoptKey(a.shared, "s2")).toBe(false);
    a.shared.attachHost(makeSeat("q", log));
    expect(a.shared.frontId()).toBeNull();
  });
});

describe("seats and disposeAll", () => {
  test("seats() is a snapshot in attach order, newest last", () => {
    const a = setup();
    const offA = a.shared.attachHost(
      makeSeat("a", [], { hasCanvas: () => true }),
    );
    a.shared.attachHost(makeSeat("b", []));
    const snap = a.shared.seats();
    expect(snap.map((s) => s.id)).toEqual(["a", "b"]);
    expect(snap.some((s) => s.hasCanvas?.() === true)).toBe(true);
    offA();
    expect(snap.map((s) => s.id)).toEqual(["a", "b"]); // the old snapshot is unchanged
    expect(a.shared.seats().map((s) => s.id)).toEqual(["b"]);
    a.shared.release();
    a.ft.fire();
    expect(a.shared.seats()).toEqual([]);
  });

  test("disposeAll disposes registered and private chats and cancels grace", () => {
    const keyed = setup("s1");
    const priv = setup(null);
    const graced = setup("s2");
    graced.shared.release();
    expect(graced.ft.pending.size).toBe(1);
    _registry().disposeAll();
    expect(keyed.stub.disposed()).toBe(1);
    expect(priv.stub.disposed()).toBe(1);
    expect(graced.stub.disposed()).toBe(1);
    expect(graced.ft.pending.size).toBe(0);
    expect(_registry().size()).toBe(0);
    _registry().disposeAll(); // nothing left
    expect(keyed.stub.disposed()).toBe(1);
  });

  test("clear() forgets keys without disposing", () => {
    const a = setup("s1");
    _registry().clear();
    expect(_registry().size()).toBe(0);
    expect(a.stub.disposed()).toBe(0);
    expect(acquireSharedChat(a.opts)).not.toBe(a.shared);
  });
});

describe("review round 2", () => {
  test("composite key: one session on two files is two chats", () => {
    const a = setup("s1");
    const other = acquireSharedChat({
      ...a.opts,
      config: { file: "/y/other.html" },
    });
    expect(other).not.toBe(a.shared);
    expect(
      acquireSharedChat({ ...a.opts, config: { file: "/y/other.html" } }),
    ).toBe(other);
    expect(_registry().size()).toBe(2);
    expect(sharedChatKey("/x/chat.html", "s1")).toBe("/x/chat.html\0s1");
    expect(sharedChatKey(null, "s1")).toBe("\0s1");
  });

  test("adoptKey composes with the chat's own file", () => {
    const owner = setup("s9"); // on /x/chat.html
    const elsewhere = acquireSharedChat({
      ...owner.opts,
      key: null,
      config: { file: "/y/other.html" },
    });
    expect(adoptKey(elsewhere, "s9")).toBe(true); // different file → no conflict
    expect(elsewhere.key).toBe("s9");
    expect(
      acquireSharedChat({ ...owner.opts, config: { file: "/y/other.html" } }),
    ).toBe(elsewhere);
    expect(acquireSharedChat(owner.opts)).toBe(owner.shared);
  });

  test("reads during a bound send come from the sender seat", async () => {
    let deps: ControllerDeps | null = null;
    let release: () => void = () => {};
    const seen: string[] = [];
    const ctl = {
      sendMessage: async () => {
        seen.push(deps!.model!());
        await new Promise<void>((res) => (release = res));
        seen.push(deps!.model!());
      },
      sendFollowUp: () => Promise.resolve(),
      dispose: () => {},
    } as unknown as ChatController;
    const shared = acquireSharedChat({
      key: "s1",
      make: (d) => {
        deps = d;
        return ctl;
      },
      config: { file: null },
      timers: fakeTimers().timers,
    });
    shared.attachHost(makeSeat("popover", []));
    shared.attachHost(makeSeat("pane", [])); // front
    const p = shared.bindSender("popover").sendMessage("hi");
    expect(deps!.model!()).toBe("model-popover");
    expect(deps!.hasPane!()).toBe(false);
    release();
    await p;
    expect(seen).toEqual(["model-popover", "model-popover"]);
    expect(deps!.model!()).toBe("model-pane"); // restored after the send settles
  });

  test("send records are dropped once returned (+delivered when it has a clientId)", async () => {
    const a = setup();
    const log: unknown[][] = [];
    a.shared.attachHost(makeSeat("a", log));
    a.shared.attachHost(makeSeat("b", log));
    const d = a.deps();
    // no clientId: return alone drops it
    await a.shared.bindSender("a").sendMessage("x", { sendId: "S1" });
    await a.shared.bindSender("b").sendMessage("y");
    d.onSendReturned?.({ text: "x", sendId: "S1" });
    expect(last(log)?.[0]).toBe("a");
    d.onSendReturned?.({ text: "x", sendId: "S1" });
    expect(last(log)?.[0]).toBe("b"); // forgotten → last sender
    // with a clientId: kept after return until delivered fires too (either order)
    await a.shared
      .bindSender("a")
      .sendMessage("z", { sendId: "S2", clientId: "C2" });
    await a.shared.bindSender("b").sendMessage("w");
    d.onSendReturned?.({ text: "z", sendId: "S2", clientId: "C2" });
    d.onSendDelivered?.("C2");
    expect(last(log)).toEqual(["a", "onSendDelivered", "C2"]);
    d.onSendDelivered?.("C2");
    expect(last(log)?.[0]).toBe("b"); // both fired → dropped
  });

  test("send records are capped", async () => {
    const a = setup();
    const log: unknown[][] = [];
    a.shared.attachHost(makeSeat("a", log));
    a.shared.attachHost(makeSeat("b", log));
    const sendA = a.shared.bindSender("a");
    await sendA.sendMessage("first", { clientId: "C0" });
    for (let i = 1; i <= SEND_RECORD_CAP; i++)
      await sendA.sendMessage("m", { clientId: "C" + i });
    await a.shared.bindSender("b").sendMessage("last");
    a.deps().onSendDelivered?.("C0");
    expect(last(log)?.[0]).toBe("b"); // evicted → last sender
    a.deps().onSendDelivered?.("C" + SEND_RECORD_CAP);
    expect(last(log)?.[0]).toBe("a");
  });

  test("canvasResponderId: first seat with a canvas, else front", () => {
    const a = setup();
    expect(a.shared.canvasResponderId()).toBeNull();
    a.shared.attachHost(makeSeat("popover", []));
    a.shared.attachHost(makeSeat("kept", [], { hasCanvas: () => false }));
    expect(a.shared.canvasResponderId()).toBe("kept"); // front
    a.shared.attachHost(makeSeat("pane", [], { hasCanvas: () => true }));
    a.shared.setFront("popover");
    expect(a.shared.canvasResponderId()).toBe("pane");
  });

  test("onSeatsChange fires on attach/detach/setFront; seats() is stable between", () => {
    const a = setup();
    let n = 0;
    const off = a.shared.onSeatsChange(() => n++);
    expect(a.shared.hasSeats()).toBe(false);
    const offA = a.shared.attachHost(makeSeat("a", []));
    a.shared.attachHost(makeSeat("b", []));
    expect(n).toBe(2);
    expect(a.shared.hasSeats()).toBe(true);
    const snap = a.shared.seats();
    expect(a.shared.seats()).toBe(snap);
    a.shared.setFront("a");
    expect(n).toBe(3);
    a.shared.setFront("a"); // no change → no fire
    expect(n).toBe(3);
    offA();
    expect(n).toBe(4);
    expect(a.shared.seats()).not.toBe(snap);
    off();
    a.shared.attachHost(makeSeat("c", []));
    expect(n).toBe(4);
  });
});

describe("router", () => {
  test("no-seat fallbacks", async () => {
    const d = setup().deps();
    expect(d.model?.()).toBe("");
    expect(d.effort?.()).toBe("");
    expect(d.backend?.()).toBe("");
    expect(d.hasPane?.()).toBeNull();
    expect(await d.appStateBlock?.()).toBe("");
    expect(await d.terminalHint?.()).toBe("");
    d.focusComposer?.();
    d.onRunEnded?.();
    d.onStranded?.([{ text: "x" }]);
    d.onSendReturned?.({ text: "x" });
  });

  test("seat without terminalHint answers empty", async () => {
    const a = setup();
    a.shared.attachHost(makeSeat("p", [], { terminalHint: undefined }));
    expect(await a.deps().terminalHint?.()).toBe("");
  });

  test("reads go to the front seat; newest attached is front; setFront", async () => {
    const a = setup();
    const log: unknown[][] = [];
    a.shared.attachHost(makeSeat("popover", log));
    a.shared.attachHost(makeSeat("pane", log));
    const d = a.deps();
    expect(a.shared.frontId()).toBe("pane");
    expect(d.model?.()).toBe("model-pane");
    expect(d.hasPane?.()).toBe(true);
    expect(await d.appStateBlock?.()).toBe("state-pane");
    expect(await d.terminalHint?.()).toBe("hint-pane");
    a.shared.setFront("popover");
    expect(d.effort?.()).toBe("effort-popover");
    expect(d.backend?.()).toBe("backend-popover");
    expect(d.hasPane?.()).toBe(false);
    d.focusComposer?.();
    expect(log).toEqual([["popover", "focusComposer"]]);
    a.shared.setFront("nobody");
    expect(a.shared.frontId()).toBe("popover");
  });

  test("seat methods are read at call time", () => {
    const a = setup();
    const seat = makeSeat("p", []);
    a.shared.attachHost(seat);
    seat.model = () => "changed";
    expect(a.deps().model?.()).toBe("changed");
  });

  test("detach removes the seat and hands front to the newest left", () => {
    const a = setup();
    const log: unknown[][] = [];
    a.shared.attachHost(makeSeat("a", log));
    const offB = a.shared.attachHost(makeSeat("b", log));
    a.shared.attachHost(makeSeat("c", log));
    a.shared.setFront("b");
    offB();
    expect(a.shared.frontId()).toBe("c");
    a.deps().onRunEnded?.();
    expect(log).toEqual([
      ["a", "onRunEnded"],
      ["c", "onRunEnded"],
    ]);
    offB(); // idempotent
  });

  test("stale detach of a replaced seat id does not remove the new one", () => {
    const a = setup();
    const log: unknown[][] = [];
    const offOld = a.shared.attachHost(makeSeat("p", log));
    a.shared.attachHost(makeSeat("p", log, { model: () => "new" }));
    offOld();
    expect(a.shared.frontId()).toBe("p");
    expect(a.deps().model?.()).toBe("new");
  });

  test("broadcasts reach every seat", () => {
    const a = setup();
    const log: unknown[][] = [];
    a.shared.attachHost(makeSeat("a", log));
    a.shared.attachHost(makeSeat("b", log));
    const d = a.deps();
    d.onActivity?.();
    d.onArtifactsTick?.();
    d.onRunAbandoned?.();
    d.onCommandsChanged?.();
    expect(log).toEqual([
      ["a", "onActivity"],
      ["b", "onActivity"],
      ["a", "onArtifactsTick"],
      ["b", "onArtifactsTick"],
      ["a", "onRunAbandoned"],
      ["b", "onRunAbandoned"],
      ["a", "onCommandsChanged"],
      ["b", "onCommandsChanged"],
    ]);
  });

  test("bindSender delegates and routes returns by sendId, then forgets", async () => {
    const a = setup();
    const log: unknown[][] = [];
    a.shared.attachHost(makeSeat("a", log));
    a.shared.attachHost(makeSeat("b", log)); // front
    await a.shared
      .bindSender("a")
      .sendMessage("hi", { sendId: "S1", clientId: "C1" });
    await a.shared.bindSender("b").sendFollowUp("yo", { sendId: "S2" });
    expect(a.stub.sends.map((s) => [s.road, s.text])).toEqual([
      ["sendMessage", "hi"],
      ["sendFollowUp", "yo"],
    ]);
    const d = a.deps();
    d.onSendReturned?.({ text: "hi", sendId: "S1", clientId: "C1" });
    expect(last(log)).toEqual([
      "a",
      "onSendReturned",
      { text: "hi", sendId: "S1", clientId: "C1" },
    ]);
    // its clientId record waits for delivered; once that fires it is forgotten
    d.onSendDelivered?.("C1");
    expect(last(log)).toEqual(["a", "onSendDelivered", "C1"]);
    // forgotten: the same ids now fall back to the last sender (b)
    d.onSendReturned?.({ text: "hi", sendId: "S1", clientId: "C1" });
    expect(last(log)?.[0]).toBe("b");
  });

  test("returns route by clientId when there is no sendId", async () => {
    const a = setup();
    const log: unknown[][] = [];
    a.shared.attachHost(makeSeat("a", log));
    a.shared.attachHost(makeSeat("b", log));
    await a.shared.bindSender("a").sendMessage("hi", { clientId: "C1" });
    await a.shared.bindSender("b").sendMessage("yo");
    a.deps().onSendReturned?.({ text: "hi", clientId: "C1", refused: true });
    expect(last(log)?.[0]).toBe("a");
  });

  test("delivered routes by clientId and keeps the mapping for a later return", async () => {
    const a = setup();
    const log: unknown[][] = [];
    a.shared.attachHost(makeSeat("a", log));
    a.shared.attachHost(makeSeat("b", log));
    await a.shared.bindSender("a").sendMessage("hi", { clientId: "C1" });
    await a.shared.bindSender("b").sendMessage("yo");
    const d = a.deps();
    d.onSendDelivered?.("C1");
    expect(last(log)).toEqual(["a", "onSendDelivered", "C1"]);
    d.onSendDelivered?.("C1");
    expect(last(log)?.[0]).toBe("a");
    d.onSendReturned?.({ text: "hi", clientId: "C1" });
    expect(last(log)?.[0]).toBe("a");
    d.onSendDelivered?.("unknown");
    expect(last(log)).toEqual(["b", "onSendDelivered", "unknown"]);
  });

  test("stranded goes to the last sender, else front", async () => {
    const a = setup();
    const log: unknown[][] = [];
    a.shared.attachHost(makeSeat("a", log));
    a.shared.attachHost(makeSeat("b", log));
    const d = a.deps();
    d.onStranded?.([{ text: "x" }]);
    expect(last(log)?.[0]).toBe("b");
    await a.shared.bindSender("a").sendFollowUp("x");
    d.onStranded?.([{ text: "x", sendId: "S" }]);
    expect(last(log)).toEqual([
      "a",
      "onStranded",
      [{ text: "x", sendId: "S" }],
    ]);
  });

  test("a return mapped to a detached seat falls back to front", async () => {
    const a = setup();
    const log: unknown[][] = [];
    const offA = a.shared.attachHost(makeSeat("a", log));
    a.shared.attachHost(makeSeat("b", log));
    await a.shared.bindSender("a").sendMessage("hi", { sendId: "S1" });
    offA();
    a.deps().onSendReturned?.({ text: "hi", sendId: "S1" });
    expect(last(log)?.[0]).toBe("b");
  });

  test("a bound send after dispose is handed back refused", async () => {
    const a = setup();
    const log: unknown[][] = [];
    a.shared.attachHost(makeSeat("a", log));
    const send = a.shared.bindSender("a");
    a.shared.release();
    a.ft.fire();
    await send.sendMessage("late", { sendId: "S" });
    expect(a.stub.sends).toEqual([]);
    expect(log).toEqual([
      [
        "a",
        "onSendReturned",
        {
          text: "late",
          attachments: undefined,
          sendId: "S",
          clientId: undefined,
          refused: true,
        },
      ],
    ]);
  });
});

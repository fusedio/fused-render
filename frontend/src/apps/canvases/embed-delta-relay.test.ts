import { describe, expect, test } from "bun:test";
import type { CanvasDelta } from "./api";
import {
  BOOTING_REFUSAL,
  DELTA_RETRY_WINDOW_MS,
  FAILED_POLLS_BEFORE_HEAL,
  NODE_ABSENT_GRACE_MS,
  NODE_READY_WAIT_MS,
  SYNC_POLL_MS,
  WS_RETRY_MAX_MS,
  WS_RETRY_MIN_MS,
  acceptFrameMessage,
  connectSyncFeed,
  createEmbedDeltaRelay,
  deltaOwed,
  freshCursor,
  keepNewer,
  nodePresence,
  parseNodeState,
  type RelayTransport,
  type SocketLike,
} from "./embed-delta-relay";
import { emptySyncState, type SyncState } from "./sync-state";

// A transport that records everything and answers cuts from a script.
function fakeTransport(cuts: Array<CanvasDelta | { status: number }> = []) {
  const posted: Record<string, unknown>[] = [];
  const acks: Array<{ rev: number; ok: boolean; reason?: string; skipped?: string[] }> = [];
  const asked: boolean[] = [];
  const events: string[] = [];
  let clock = 0;
  let framed = true;
  const timers: Array<{ at: number; fn: () => void; id: number }> = [];
  let nextId = 1;
  const t: RelayTransport & {
    posted: typeof posted;
    acks: typeof acks;
    asked: typeof asked;
    events: typeof events;
    unframe(): void;
    advance(ms: number): void;
    pendingTimers(): number;
  } = {
    posted,
    acks,
    asked,
    events,
    async getDelta(full) {
      asked.push(full);
      events.push(`cut:${full ? "full" : "delta"}`);
      const next = cuts.shift();
      if (!next) return { rev: 0, ops: [] };
      if ("status" in next) {
        const e = new Error("busy") as Error & { status: number };
        e.status = next.status;
        throw e;
      }
      return next;
    },
    async ackDelta(rev, ok, reason, skipped) {
      acks.push({ rev, ok, reason, skipped });
      events.push(`ack:${rev}`);
    },
    async getToken() {
      return "tok";
    },
    post(msg) {
      if (!framed) return false;
      posted.push(msg);
      events.push(`post:${String(msg.type)}${typeof msg.rev === "number" ? ":" + msg.rev : ""}`);
      return true;
    },
    async sleep(ms) {
      clock += ms;
    },
    now: () => clock,
    setTimeout(fn, ms) {
      const id = nextId++;
      timers.push({ at: clock + ms, fn, id });
      return id;
    },
    clearTimeout(id) {
      const i = timers.findIndex((x) => x.id === id);
      if (i >= 0) timers.splice(i, 1);
    },
    unframe() {
      framed = false;
    },
    advance(ms) {
      clock += ms;
      for (const due of timers.filter((x) => x.at <= clock)) {
        timers.splice(timers.indexOf(due), 1);
        due.fn();
      }
    },
    pendingTimers: () => timers.length,
  };
  return t;
}

const state = (disk_rev: number, watcher_epoch: string | null = "e1") => ({ disk_rev, watcher_epoch });
const ops = (n: number) => Array.from({ length: n }, (_, i) => ({ op: "upsert" as const, name: `n${i}` }));

describe("acceptFrameMessage", () => {
  const win = {};
  const frame = { origin: "https://wb.example", window: win };
  test("only the frame's own window on the workbench's origin", () => {
    expect(acceptFrameMessage({ origin: "https://wb.example", source: win }, frame)).toBe(true);
    expect(acceptFrameMessage({ origin: "https://evil.example", source: win }, frame)).toBe(false);
    expect(acceptFrameMessage({ origin: "https://wb.example", source: {} }, frame)).toBe(false);
    expect(acceptFrameMessage({ origin: "https://wb.example", source: null }, frame)).toBe(false);
  });
  test("nothing before the frame exists", () => {
    expect(acceptFrameMessage({ origin: "https://wb.example", source: win }, { origin: null, window: win })).toBe(false);
    expect(acceptFrameMessage({ origin: "https://wb.example", source: null }, { origin: "https://wb.example", window: null })).toBe(false);
  });
});

describe("deltaOwed (the workspace's disk_rev effect)", () => {
  test("first sight records and cuts nothing", () => {
    const c = freshCursor();
    expect(deltaOwed(c, 4, "e1")).toBe(false);
    expect(c.lastRev).toBe(4);
  });
  test("a move past the last cut is a cut; a repeat is not", () => {
    const c = freshCursor();
    deltaOwed(c, 4, "e1");
    expect(deltaOwed(c, 5, "e1")).toBe(true);
    // The same state again (the socket re-sends while a leg is busy).
    expect(deltaOwed(c, 5, "e1")).toBe(false);
  });
  test("a new watcher asks at once, unless its counter is still 0", () => {
    const c = freshCursor();
    deltaOwed(c, 7, "e1");
    expect(deltaOwed(c, 7, "e2")).toBe(true);
    expect(c.lastRev).toBe(7);
    expect(deltaOwed(c, 0, "e3")).toBe(false);
    expect(deltaOwed(c, 1, "e3")).toBe(true);
  });
  test("a counter that went backwards on one watcher is a restart", () => {
    const c = freshCursor();
    deltaOwed(c, 9, "e1");
    expect(deltaOwed(c, 3, "e1")).toBe(true);
    expect(c.lastRev).toBe(3);
  });
  test("non-numeric revs are ignored", () => {
    const c = freshCursor();
    expect(deltaOwed(c, undefined, "e1")).toBe(false);
    expect(c.seenRev).toBe(null);
  });
});

describe("keepNewer", () => {
  test("drops an older state of the same watcher, keeps a new watcher's", () => {
    const a = { ...emptySyncState("x"), disk_rev: 5, watcher_epoch: "e1" } as SyncState;
    const older = { ...a, disk_rev: 4 };
    const other = { ...a, disk_rev: 1, watcher_epoch: "e2" };
    expect(keepNewer(a, older)).toBe(a);
    expect(keepNewer(a, other)).toBe(other);
    expect(keepNewer(null, older)).toBe(older);
  });
});

describe("parseNodeState", () => {
  test("reads NM-6's shape", () => {
    expect(
      parseNodeState({
        type: "fused-embed-node-state",
        udfName: "udf_1",
        present: true,
        kind: "udf",
        status: "error",
        error: { line: 12, text: "NameError: x" },
      }),
    ).toEqual({ udfName: "udf_1", present: true, kind: "udf", status: "error", error: { line: 12, text: "NameError: x" } });
  });
  test("tolerates what it does not know, refuses what it cannot read", () => {
    expect(parseNodeState({ udfName: "a", present: false })).toEqual({
      udfName: "a",
      present: false,
      kind: null,
      status: null,
      error: null,
    });
    expect(parseNodeState({ udfName: "a", present: true, kind: "weird", error: { line: -3, text: "x" } })?.error).toEqual({
      line: null,
      text: "x",
    });
    expect(parseNodeState({ udfName: "", present: true })).toBe(null);
    expect(parseNodeState({ udfName: "a" })).toBe(null);
    expect(parseNodeState(null)).toBe(null);
  });
  test("caps the error text at 8 KB, keeping the tail", () => {
    const text = "a".repeat(10_000) + "END";
    const got = parseNodeState({ udfName: "a", present: true, error: { text } })!;
    expect(got.error!.text.length).toBe(8 * 1024);
    expect(got.error!.text.endsWith("END")).toBe(true);
  });
});

describe("the relay", () => {
  test("auth-ready and auth-refresh: the token with no caps, at the frame", async () => {
    const t = fakeTransport();
    const r = createEmbedDeltaRelay(t, { publicOrigin: () => "https://www.fused.io" });
    r.handleMessage({ type: "fused-embed-auth-ready" });
    await r.idle();
    await Promise.resolve();
    expect(t.posted).toEqual([
      { type: "fused-embed-auth-token", accessToken: "tok", caps: [], publicOrigin: "https://www.fused.io" },
    ]);
    r.handleMessage({ type: "fused-embed-auth-refresh" });
    await Promise.resolve();
    await Promise.resolve();
    expect(t.posted.length).toBe(2);
    r.dispose();
  });

  test("hello is a full cut, posted with the server's gen/seq", async () => {
    const t = fakeTransport([{ rev: 3, base_rev: null, full: true, ops: ops(2), gen: 1, seq: 4 }]);
    const r = createEmbedDeltaRelay(t);
    r.handleMessage({ type: "fused-embed-hello" });
    await r.idle();
    expect(t.asked).toEqual([true]);
    expect(t.posted).toEqual([
      { type: "fused-embed-delta", rev: 3, base_rev: null, full: true, ops: ops(2), gen: 1, seq: 4 },
    ]);
  });

  test("a disk move is a cut; an empty cut posts nothing", async () => {
    const t = fakeTransport([
      { rev: 5, base_rev: 4, ops: ops(1) },
      { rev: 6, base_rev: 5, ops: [] },
    ]);
    const r = createEmbedDeltaRelay(t);
    r.onSyncState(state(4)); // first sight
    r.onSyncState(state(5));
    r.onSyncState(state(6));
    await r.idle();
    expect(t.asked).toEqual([false, false]);
    expect(t.posted.map((m) => m.rev)).toEqual([5]);
  });

  test("cuts go out in the order they were asked, one at a time", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const t = fakeTransport();
    const cuts = [
      { rev: 1, full: true, ops: ops(1) },
      { rev: 2, base_rev: 1, ops: ops(1) },
    ];
    let inFlight = 0;
    let maxInFlight = 0;
    t.getDelta = async () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      if (cuts.length === 2) await gate;
      inFlight -= 1;
      return cuts.shift()!;
    };
    const r = createEmbedDeltaRelay(t);
    void r.relayDelta(true);
    void r.relayDelta(false);
    release();
    await r.idle();
    expect(maxInFlight).toBe(1);
    expect(t.posted.map((m) => m.rev)).toEqual([1, 2]);
  });

  test("a held cut and a 409 are asked again, within the window", async () => {
    const t = fakeTransport([{ rev: 1, held: "row without file" }, { status: 409 }, { rev: 1, full: true, ops: ops(1) }]);
    const r = createEmbedDeltaRelay(t);
    await r.relayDelta(true);
    expect(t.asked.length).toBe(3);
    expect(t.posted.length).toBe(1);
  });

  test("past the window it gives up; another status gives up at once", async () => {
    const total = Math.ceil(DELTA_RETRY_WINDOW_MS / 1000) + 5;
    const held = Array.from({ length: total }, () => ({ rev: 1, held: "x" }));
    const t = fakeTransport(held);
    const r = createEmbedDeltaRelay(t);
    await r.relayDelta(false);
    expect(t.posted).toEqual([]);
    // One ask per DELTA_RETRY_MS of fake clock, then it stops.
    expect(t.asked.length).toBe(DELTA_RETRY_WINDOW_MS / 1000 + 2);
    expect(t.asked.length).toBeLessThan(total);
    const t2 = fakeTransport([{ status: 400 }, { rev: 1, ops: ops(1) }]);
    await createEmbedDeltaRelay(t2).relayDelta(false);
    expect(t2.asked.length).toBe(1);
  });

  test("no frame: the cut is dropped unacked", async () => {
    const t = fakeTransport([{ rev: 2, full: true, ops: ops(1) }]);
    t.unframe();
    const r = createEmbedDeltaRelay(t);
    await r.relayDelta(true);
    expect(t.posted).toEqual([]);
    expect(t.acks).toEqual([]);
  });

  test("the cut's rev becomes the cursor's, so the same rev is not cut again", async () => {
    const t = fakeTransport([{ rev: 8, full: true, ops: ops(1) }]);
    const r = createEmbedDeltaRelay(t);
    r.onSyncState(state(5));
    await r.relayDelta(true); // hello at rev 8
    r.onSyncState(state(8));
    await r.idle();
    expect(t.asked).toEqual([true]);
  });

  test("acks are relayed in the frame's order, skipped names and reasons kept", async () => {
    const t = fakeTransport();
    const r = createEmbedDeltaRelay(t);
    r.handleMessage({ type: "fused-embed-delta-applied", rev: 3, skipped: ["a", 7, "b"] });
    r.handleMessage({ type: "fused-embed-delta-rejected", rev: 4, reason: BOOTING_REFUSAL });
    r.handleMessage({ type: "fused-embed-delta-applied", rev: "5" });
    await r.idle();
    expect(t.acks).toEqual([
      { rev: 3, ok: true, reason: undefined, skipped: ["a", "b"] },
      { rev: 4, ok: false, reason: BOOTING_REFUSAL, skipped: undefined },
    ]);
    // A booting refusal heals nothing: its hello will ask.
    expect(t.asked).toEqual([]);
  });

  test("a real refusal: acked first, then one healing full cut, and only one", async () => {
    const t = fakeTransport([{ rev: 6, full: true, ops: ops(1) }, { rev: 6, full: true, ops: ops(1) }]);
    const r = createEmbedDeltaRelay(t);
    r.handleMessage({ type: "fused-embed-delta-rejected", rev: 5, reason: "bad op" });
    await r.idle();
    expect(t.events).toEqual(["ack:5", "cut:full", "post:fused-embed-delta:6"]);
    // The heal refused again: no second heal.
    r.handleMessage({ type: "fused-embed-delta-rejected", rev: 6 });
    await r.idle();
    expect(t.asked).toEqual([true]);
    expect(t.acks.map((a) => [a.rev, a.reason])).toEqual([[5, "bad op"], [6, "refused"]]);
    // An applied delta re-arms the heal.
    r.handleMessage({ type: "fused-embed-delta-applied", rev: 7 });
    r.handleMessage({ type: "fused-embed-delta-rejected", rev: 8, reason: "bad op" });
    await r.idle();
    expect(t.asked).toEqual([true, true]);
  });

  test("a failing ack does not stop the next one", async () => {
    const t = fakeTransport();
    let n = 0;
    t.ackDelta = async (rev) => {
      n += 1;
      if (n === 1) throw new Error("down");
      t.acks.push({ rev, ok: true });
    };
    const r = createEmbedDeltaRelay(t);
    r.handleMessage({ type: "fused-embed-delta-applied", rev: 1 });
    r.handleMessage({ type: "fused-embed-delta-applied", rev: 2 });
    await r.idle();
    expect(t.acks.map((a) => a.rev)).toEqual([2]);
  });

  test("sync state goes to the frame as fused-embed-sync-state", () => {
    const t = fakeTransport();
    const r = createEmbedDeltaRelay(t);
    r.postSyncState({ worst: "ok" });
    expect(t.posted).toEqual([{ type: "fused-embed-sync-state", state: { worst: "ok" } }]);
  });

  test("node state: this node's only, and it does NOT stop the NM-7 clock", () => {
    const t = fakeTransport();
    const seen: string[] = [];
    let missing = 0;
    const r = createEmbedDeltaRelay(t, {
      node: "udf_1",
      onNodeState: (s) => seen.push(`${s.udfName}:${s.status}`),
      onNoNodeMode: () => (missing += 1),
    });
    r.handleMessage({ type: "fused-embed-auth-ready" });
    r.handleMessage({ type: "fused-embed-node-state", udfName: "other", present: true, status: "ready" });
    r.handleMessage({ type: "fused-embed-node-state", udfName: "udf_1", present: true, status: "running" });
    expect(seen).toEqual(["udf_1:running"]);
    // Only node-ready answers NM-7's question.
    t.advance(NODE_READY_WAIT_MS + 1);
    expect(missing).toBe(1);
  });

  test("NM-7: node-ready for this node stops the clock, and for good", () => {
    const t = fakeTransport();
    let missing = 0;
    const r = createEmbedDeltaRelay(t, { node: "udf_1", onNoNodeMode: () => (missing += 1) });
    r.handleMessage({ type: "fused-embed-auth-ready" });
    // Another node's ready, or a malformed one, is not this view's answer.
    r.handleMessage({ type: "fused-embed-node-ready", udfName: "other" });
    r.handleMessage({ type: "fused-embed-node-ready" });
    t.advance(NODE_READY_WAIT_MS - 1);
    r.handleMessage({ type: "fused-embed-node-ready", udfName: "udf_1" });
    t.advance(NODE_READY_WAIT_MS * 2);
    expect(missing).toBe(0);
    // A reload of a frame that said it knows node mode is not doubted.
    r.handleMessage({ type: "fused-embed-auth-ready" });
    t.advance(NODE_READY_WAIT_MS * 2);
    expect(missing).toBe(0);
  });

  test("NM-7: no node-ready within the wait after auth-ready", () => {
    const t = fakeTransport();
    let missing = 0;
    const r = createEmbedDeltaRelay(t, { node: "udf_1", onNoNodeMode: () => (missing += 1) });
    r.handleMessage({ type: "fused-embed-auth-ready" });
    t.advance(NODE_READY_WAIT_MS - 1);
    expect(missing).toBe(0);
    // A reload restarts the clock.
    r.handleMessage({ type: "fused-embed-auth-ready" });
    t.advance(NODE_READY_WAIT_MS - 1);
    expect(missing).toBe(0);
    t.advance(2);
    expect(missing).toBe(1);
  });

  test("NV-9: present:false is pending until 2 s after the first delta ack", () => {
    const t = fakeTransport();
    const seen: string[] = [];
    const r = createEmbedDeltaRelay(t, { node: "udf_1", onPresence: (p) => seen.push(p) });
    r.handleMessage({ type: "fused-embed-auth-ready" });
    r.handleMessage({ type: "fused-embed-node-state", udfName: "udf_1", present: false, kind: null, status: null });
    t.advance(NODE_ABSENT_GRACE_MS * 5);
    // No ack yet: still the loading state, however long.
    expect(seen).toEqual(["pending"]);
    r.handleMessage({ type: "fused-embed-delta-applied", rev: 1 });
    t.advance(NODE_ABSENT_GRACE_MS - 1);
    expect(seen).toEqual(["pending"]);
    t.advance(1);
    expect(seen).toEqual(["pending", "absent"]);
  });

  test("NV-9: a node that arrives within the grace never reads absent", () => {
    const t = fakeTransport();
    const seen: string[] = [];
    const r = createEmbedDeltaRelay(t, { node: "udf_1", onPresence: (p) => seen.push(p) });
    r.handleMessage({ type: "fused-embed-auth-ready" });
    r.handleMessage({ type: "fused-embed-node-state", udfName: "udf_1", present: false, kind: null, status: null });
    r.handleMessage({ type: "fused-embed-delta-applied", rev: 1 });
    t.advance(NODE_ABSENT_GRACE_MS / 2);
    r.handleMessage({ type: "fused-embed-node-state", udfName: "udf_1", present: true, kind: "udf", status: "running" });
    t.advance(NODE_ABSENT_GRACE_MS * 2);
    expect(seen).toEqual(["pending", "present"]);
    // Gone later (deleted on disk): after the first ack's grace, at once.
    r.handleMessage({ type: "fused-embed-node-state", udfName: "udf_1", present: false, kind: null, status: null });
    expect(seen).toEqual(["pending", "present", "absent"]);
    // A reload starts over: pending until its own first ack.
    r.handleMessage({ type: "fused-embed-auth-ready" });
    expect(seen[seen.length - 1]).toBe("pending");
  });

  test("nodePresence (the pure rule)", () => {
    const absent = { udfName: "a", present: false, kind: null, status: null, error: null };
    const here = { ...absent, present: true };
    expect(nodePresence(null, 0, 5)).toEqual({ presence: "pending", recheckAt: null });
    expect(nodePresence(here, null, 5)).toEqual({ presence: "present", recheckAt: null });
    expect(nodePresence(absent, null, 99_999)).toEqual({ presence: "pending", recheckAt: null });
    expect(nodePresence(absent, 1000, 1500)).toEqual({ presence: "pending", recheckAt: 1000 + NODE_ABSENT_GRACE_MS });
    expect(nodePresence(absent, 1000, 1000 + NODE_ABSENT_GRACE_MS)).toEqual({ presence: "absent", recheckAt: null });
  });

  test("present:false carries no kind and no status, whatever was sent", () => {
    expect(parseNodeState({ udfName: "a", present: false, kind: "udf", status: "error", error: { text: "x" } })).toEqual({
      udfName: "a",
      present: false,
      kind: null,
      status: null,
      error: null,
    });
  });

  test("after dispose nothing is posted and no clock runs", async () => {
    const t = fakeTransport([{ rev: 1, full: true, ops: ops(1) }]);
    let missing = 0;
    const r = createEmbedDeltaRelay(t, { onNoNodeMode: () => (missing += 1) });
    r.handleMessage({ type: "fused-embed-auth-ready" });
    r.dispose();
    r.handleMessage({ type: "fused-embed-hello" });
    r.postSyncState({});
    await r.idle();
    await Promise.resolve();
    t.advance(NODE_READY_WAIT_MS * 2);
    expect(t.posted).toEqual([]);
    expect(missing).toBe(0);
  });

  test("an unknown message is ignored", async () => {
    const t = fakeTransport();
    const r = createEmbedDeltaRelay(t);
    r.handleMessage({ type: "fused-embed-snapshot", gen: 1, seq: 1, body: {} });
    r.handleMessage({ type: "fused-embed-body", seq: 1 });
    r.handleMessage("nope");
    await r.idle();
    expect(t.posted).toEqual([]);
    expect(t.acks).toEqual([]);
    expect(t.asked).toEqual([]);
  });
});

describe("connectSyncFeed", () => {
  function harness(pollAnswers: Array<SyncState | Error> = []) {
    const sockets: Array<SocketLike & { url: string; closed: boolean }> = [];
    const timeouts: Array<{ fn: () => void; ms: number; id: number }> = [];
    const intervals: Array<{ fn: () => void; ms: number; id: number }> = [];
    let id = 1;
    const states: SyncState[] = [];
    const live: boolean[] = [];
    let heals = 0;
    let polls = 0;
    const close = connectSyncFeed({
      url: "ws://x/api/canvases/sync/events?name=a&env=prod&frame=f1",
      poll: async () => {
        polls += 1;
        const next = pollAnswers.shift();
        if (next instanceof Error) throw next;
        return next ?? ({ ...emptySyncState("a"), disk_rev: polls } as SyncState);
      },
      onState: (s) => states.push(s),
      onLive: (v) => live.push(v),
      onHeal: () => (heals += 1),
      openSocket: (url) => {
        const s = { url, closed: false, onopen: null, onmessage: null, onerror: null, onclose: null, close() { this.closed = true; } };
        sockets.push(s as never);
        return s as never;
      },
      setTimeout: (fn, ms) => (timeouts.push({ fn, ms, id: id++ }), id - 1),
      clearTimeout: (x) => {
        const i = timeouts.findIndex((t) => t.id === x);
        if (i >= 0) timeouts.splice(i, 1);
      },
      setInterval: (fn, ms) => (intervals.push({ fn, ms, id: id++ }), id - 1),
      clearInterval: (x) => {
        const i = intervals.findIndex((t) => t.id === x);
        if (i >= 0) intervals.splice(i, 1);
      },
    });
    return { sockets, timeouts, intervals, states, live, close, heals: () => heals, polls: () => polls };
  }
  const flush = () => new Promise((r) => setTimeout(r, 0));

  test("polls once at once, opens the socket on the frame's url, and reads its states", async () => {
    const h = harness();
    await flush();
    expect(h.polls()).toBe(1);
    expect(h.sockets[0].url).toContain("frame=f1");
    h.sockets[0].onopen!();
    expect(h.live).toEqual([true]);
    await flush(); // the catch-up poll the open makes
    h.sockets[0].onmessage!({ data: JSON.stringify({ type: "state", state: { ...emptySyncState("a"), disk_rev: 9 } }) });
    h.sockets[0].onmessage!({ data: JSON.stringify({ type: "ping" }) });
    h.sockets[0].onmessage!({ data: "not json" });
    await flush();
    expect(h.states.map((s) => s.disk_rev)).toEqual([1, 2, 9]);
    h.close();
    expect(h.sockets[0].closed).toBe(true);
  });

  test("a dropped socket polls every 2 s and reconnects with a doubling backoff", () => {
    const h = harness();
    h.sockets[0].onclose!();
    expect(h.live).toEqual([false]);
    expect(h.intervals.map((i) => i.ms)).toEqual([SYNC_POLL_MS]);
    expect(h.timeouts.map((t) => t.ms)).toEqual([WS_RETRY_MIN_MS]);
    h.timeouts.shift()!.fn();
    h.sockets[1].onclose!();
    expect(h.timeouts.map((t) => t.ms)).toEqual([WS_RETRY_MIN_MS * 2]);
    for (let i = 0; i < 10; i += 1) {
      h.timeouts.shift()!.fn();
      h.sockets[h.sockets.length - 1].onclose!();
    }
    expect(h.timeouts[0].ms).toBe(WS_RETRY_MAX_MS);
    // Back up: polling stops, the backoff resets.
    h.timeouts.shift()!.fn();
    h.sockets[h.sockets.length - 1].onopen!();
    expect(h.intervals).toEqual([]);
    h.sockets[h.sockets.length - 1].onclose!();
    expect(h.timeouts[0].ms).toBe(WS_RETRY_MIN_MS);
    h.close();
  });

  test(`${FAILED_POLLS_BEFORE_HEAL} failed polls in a row ask for a heal`, async () => {
    const err = new Error("down");
    const h = harness([err, err, err]);
    h.sockets[0].onclose!();
    h.intervals[0].fn();
    h.intervals[0].fn();
    await flush();
    expect(h.heals()).toBe(1);
    h.close();
  });
});

describe("review round", () => {
  test("NV-9's grace starts on the first APPLIED ack, not on a booting refusal", () => {
    const t = fakeTransport();
    const seen: string[] = [];
    const r = createEmbedDeltaRelay(t, { node: "udf_1", onPresence: (p) => seen.push(p) });
    r.handleMessage({ type: "fused-embed-auth-ready" });
    r.handleMessage({ type: "fused-embed-node-state", udfName: "udf_1", present: false, kind: null, status: null });
    r.handleMessage({ type: "fused-embed-delta-rejected", rev: 1, reason: BOOTING_REFUSAL });
    r.handleMessage({ type: "fused-embed-delta-rejected", rev: 2, reason: "bad op" });
    t.advance(NODE_ABSENT_GRACE_MS * 3);
    expect(seen).toEqual(["pending"]);
    r.handleMessage({ type: "fused-embed-delta-applied", rev: 3 });
    t.advance(NODE_ABSENT_GRACE_MS);
    expect(seen).toEqual(["pending", "absent"]);
  });

  test("acks still queued when the relay is disposed are not posted", async () => {
    const t = fakeTransport();
    let release!: () => void;
    const gate = new Promise<void>((res) => (release = res));
    let n = 0;
    t.ackDelta = async (rev) => {
      n += 1;
      if (n === 1) await gate;
      t.acks.push({ rev, ok: true });
    };
    const r = createEmbedDeltaRelay(t);
    r.handleMessage({ type: "fused-embed-delta-applied", rev: 1 });
    r.handleMessage({ type: "fused-embed-delta-applied", rev: 2 });
    r.handleMessage({ type: "fused-embed-delta-applied", rev: 3 });
    await Promise.resolve();
    r.dispose();
    release();
    await r.idle();
    // The one in flight lands; the two behind it never leave.
    expect(t.acks.map((a) => a.rev)).toEqual([1]);
  });
});

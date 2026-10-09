// The plain-JS events client (fused_render/static/events-client.js), driven
// through its `_reset` seam with a fake WebSocket and hand-run timers. One
// socket per document, and the liveness story around it: a half-open socket
// is closed and redialled by the watchdog, and the OLD socket's own `onclose`
// — which lands a tick later, by which time the redial may already have said
// hello — must not be mistaken for the new one closing (Bugbot, PR #1537).
import { afterEach, beforeEach, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { FusedEvents } from "@static/events-client.js";

type Timer = { fn: () => void; ms: number; id: number };

class FakeSocket {
  static opened: FakeSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.opened.push(this);
  }
  send(text: string) {
    this.sent.push(text);
  }
  close() {
    this.closed = true;
    this.readyState = 3;
  }
  // The server's side of the wire.
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  frame(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  hello(bootId = "b1") {
    this.frame({ t: "hello", boot_id: bootId, topics: { "t.a": { hidden_ok: true } } });
  }
  /** The browser's late `close` event for a socket that was closed by hand. */
  closeEvent() {
    this.readyState = 3;
    this.onclose?.();
  }
}

let timers: Timer[] = [];
let nextTimer = 1;
const fakeSetTimeout = (fn: () => void, ms: number) => {
  const id = nextTimer++;
  timers.push({ fn, ms, id });
  return id;
};
const fakeClearTimeout = (id: number | null) => {
  timers = timers.filter((t) => t.id !== id);
};
/** Run every pending timer whose delay is at least `ms` (the watchdog, the redial). */
const fire = (pred: (t: Timer) => boolean) => {
  const due = timers.filter(pred);
  timers = timers.filter((t) => !pred(t));
  for (const t of due) t.fn();
};

// A PRIVATE INSTANCE of the client, not the process-wide one. `bun test` runs
// every suite in one process and the IIFE attaches to whatever `window` is at
// first import; a suite that swaps the DOM shim's window afterwards leaves the
// shared instance unreachable, and its socket/timer state would leak between
// files either way. Evaluating the source with a root of our own gives this
// file a client nothing else can see or disturb.
const SOURCE = readFileSync(join(import.meta.dir, "..", "..", "..", "..", "fused_render", "static", "events-client.js"), "utf8");
const root: { fusedEvents?: FusedEvents } = {};
new Function("window", SOURCE)(root);
const client = () => {
  if (!root.fusedEvents) throw new Error("events client did not attach");
  return root.fusedEvents;
};

beforeEach(() => {
  FakeSocket.opened = [];
  timers = [];
  client()._reset({
    WebSocket: FakeSocket,
    location: { host: "127.0.0.1:8765", protocol: "http:" },
    document: null,
    setTimeout: fakeSetTimeout,
    clearTimeout: fakeClearTimeout,
  });
});

afterEach(() => {
  client()._reset({ WebSocket: null, location: null });
});

const parsed = (s: FakeSocket) => s.sent.map((x) => JSON.parse(x) as Record<string, unknown>);

test("a subscribe dials one socket, subscribes after hello, and the snapshot is delivered", () => {
  const got: unknown[] = [];
  const off = client().subscribe("t.a", { k: 1 }, (snap) => got.push(snap));
  expect(FakeSocket.opened.length).toBe(1);
  const s = FakeSocket.opened[0];
  s.open();
  expect(s.sent).toEqual([]); // nothing before hello
  s.hello();
  expect(parsed(s)).toEqual([{ t: "sub", id: 1, topic: "t.a", params: { k: 1 } }]);
  s.frame({ t: "snap", id: 1, gen: 3, body: { n: 1 } });
  expect(got).toEqual([{ n: 1 }]);
  expect(client().connected()).toBe(true);
  off();
  expect(client()._subscriptions()).toBe(0);
});

test("the watchdog closes a silent socket and redials; the old socket's late close does not kill the new one", () => {
  const got: unknown[] = [];
  client().subscribe("t.a", {}, (snap) => got.push(snap));
  const old = FakeSocket.opened[0];
  old.open();
  old.hello();
  old.frame({ t: "snap", id: 1, gen: 1, body: { n: 1 } });
  // 45 s of nothing: the watchdog closes the socket by hand and arms a redial.
  fire((t) => t.ms === 45000);
  expect(old.closed).toBe(true);
  expect(client().connected()).toBe(false);
  fire((t) => t.ms === 1000);
  expect(FakeSocket.opened.length).toBe(2);
  const fresh = FakeSocket.opened[1];
  fresh.open();
  fresh.hello();
  expect(client().connected()).toBe(true);
  expect(parsed(fresh)).toEqual([{ t: "sub", id: 2, topic: "t.a", params: {}, since: 1 }]);
  // THE OLD SOCKET'S OWN CLOSE EVENT LANDS NOW. It must be ignored: it is not
  // the current socket. Before the fix it ran onClosed for the fresh socket —
  // opened=false, wire ids forgotten, no redial because `sock` was set — and
  // every later frame was dropped until a reload.
  old.closeEvent();
  expect(client().connected()).toBe(true);
  expect(client()._wire()).toBe(1);
  fresh.frame({ t: "snap", id: 2, gen: 2, body: { n: 2 } });
  expect(got).toEqual([{ n: 1 }, { n: 2 }]);
  expect(FakeSocket.opened.length).toBe(2); // no stray redial either
});

test("the current socket closing redials with backoff and resubscribes with `since`", () => {
  client().subscribe("t.a", {}, () => {});
  const s1 = FakeSocket.opened[0];
  s1.open();
  s1.hello();
  s1.frame({ t: "snap", id: 1, gen: 9, body: {} });
  s1.closeEvent();
  expect(client().connected()).toBe(false);
  fire((t) => t.ms === 1000);
  const s2 = FakeSocket.opened[1];
  s2.open();
  s2.hello();
  expect(parsed(s2)).toEqual([{ t: "sub", id: 2, topic: "t.a", params: {}, since: 9 }]);
  // A hello resets the backoff; a dial that drops BEFORE hello doubles it.
  s2.closeEvent();
  expect(timers.some((t) => t.ms === 1000)).toBe(true);
  fire((t) => t.ms === 1000);
  const s3 = FakeSocket.opened[2];
  s3.open();
  s3.closeEvent();
  expect(timers.some((t) => t.ms === 2000)).toBe(true);
});

// The Bots page's status stream (store.ts): ONE events-bus subscription to topic `bots`, whose snapshot and delta
// frames are both whole `/api/bots` replies folded in by applyStatus. Driven through a scripted events client
// (`setEventsClientForTests`) — never `mock.module` (process-wide in bun) and never a stubbed poll: there is no poll.
// `fetch` is stubbed only for the `open` POST the page sends for the bot on screen.
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import { setEventsClientForTests } from "@platform/lib/events";
import type { Bot, BotEvent, StatusReply } from "../lib/api";
import { askNotify } from "../lib/notify";
import { setBotsRoot } from "../lib/root";
import * as store from "./store";

interface Sub { topic: string; params: Record<string, unknown>; cb: (s: unknown, d: unknown, m: Record<string, unknown>) => void; opts?: { hiddenOk?: boolean }; live: boolean }
let subs: Sub[] = [];
let resyncs: { topic: string; params: unknown }[] = [];
const fakeClient = {
  subscribe: ((topic: string, params: Record<string, unknown>, cb: Sub["cb"], opts?: Sub["opts"]) => {
    const s: Sub = { topic, params, cb, opts, live: true };
    subs.push(s);
    return () => { s.live = false; };
  }) as never,
  resync: ((topic: string, params: unknown) => { resyncs.push({ topic, params }); return true; }) as never,
};
const live = () => subs.filter((s) => s.live && s.topic === "bots");
const cur = () => { const l = live(); expect(l.length).toBe(1); return l[0]; };
const reply = (bots: Bot[]): StatusReply => ({ bots, ts: 0, usage: null, imessage: null });
const snap = (bots: Bot[]) => cur().cb(reply(bots), null, { gen: null });
const delta = (bots: Bot[]) => cur().cb(null, reply(bots), { gen: null });
const ev = (seq: number, role: BotEvent["role"], text = `line ${seq}`): BotEvent => ({ seq, ts: Date.now() / 1000, role, text });
const bot = (id: string, seq: number, events: BotEvent[], o: Partial<Bot> = {}): Bot =>
  ({ id, name: id.toUpperCase(), model: "sonnet", effort: "low", status: "idle", seq, events, ...o }) as unknown as Bot;
const tick = () => new Promise((r) => setTimeout(r, 0));

// OS notifications: a stand-in the page may construct (notify.ts asks once, then `new Notification`).
const notes: string[] = [];
class FakeNotification {
  static permission = "granted";
  onclick: (() => void) | null = null;
  constructor(title: string) { notes.push(title); }
  close() {}
}

const g = globalThis as unknown as Record<string, unknown>;
const w = window as unknown as Record<string, unknown>;
let realFetch: unknown;
beforeAll(() => {
  realFetch = g.fetch;
  g.fetch = async () => new Response(JSON.stringify({ ok: true, setup: "none" }), { status: 200 });
  g.Notification = FakeNotification;
  w.Notification = FakeNotification;
  askNotify();
});
afterAll(() => {
  g.fetch = realFetch;
  delete g.Notification;
  delete w.Notification;
});

let teardown: () => void = () => {};
beforeEach(() => {
  subs = []; resyncs = []; notes.length = 0;
  store.clearToast();
  store.setState({
    bots: [], events: {}, cursors: {}, sel: null, fast: false, toasts: [], noteFor: null,
    banner: { show: false, text: "" }, ui: { dialog: null, panel: null, menu: null },
  });
  setEventsClientForTests(fakeClient);
  teardown = store.startStore();
});
afterEach(() => {
  teardown();
  store.clearToast();
  setEventsClientForTests(null);
});

describe("the status stream", () => {
  test("one subscription to `bots`, not hidden-ok (the hidden window's chime), with the page's params; teardown closes it", () => {
    const s = cur();
    expect(s.params).toEqual({ fast: false, shot_for: "" });
    expect(s.opts).toEqual({ hiddenOk: false });
    teardown();
    expect(live().length).toBe(0);
    teardown = () => {};
  });

  test("N readers, selection moves and Stage toggles: never more than one live subscription", () => {
    const offs = [store.subscribe(() => {}), store.subscribe(() => {}), store.subscribe(() => {})];
    snap([bot("a", 1, []), bot("b", 1, [])]);
    expect(cur().params).toEqual({ fast: false, shot_for: "a" });  // the first frame picked a bot: re-keyed
    store.select("b");
    expect(cur().params).toEqual({ fast: false, shot_for: "b" });
    const before = subs.length;
    store.select("b");                                              // same params: no re-key
    expect(subs.length).toBe(before);
    store.setFast(true);
    expect(cur().params).toEqual({ fast: true, shot_for: "b" });
    store.setFast(false);
    expect(cur().params).toEqual({ fast: false, shot_for: "b" });
    for (const off of offs) off();
  });

  test("first frame is silent; a later frame notifies its new events; a re-key snapshot neither duplicates nor re-notifies", () => {
    snap([bot("s", 2, [ev(1, "user"), ev(2, "question")], { kind: "super" }), bot("o", 1, [ev(1, "question")])]);
    expect(notes).toEqual([]);
    expect(store.getState().sel).toBe("s");                         // Super Bot is where a new user lands
    // The selection re-keyed the subscription; its snapshot restarts every bot at seq 0.
    snap([bot("s", 2, [ev(1, "user"), ev(2, "question")], { kind: "super" }), bot("o", 1, [ev(1, "question")])]);
    expect(store.eventsOf("s").map((e) => e.seq)).toEqual([1, 2]);
    expect(store.eventsOf("o").map((e) => e.seq)).toEqual([1]);
    expect(notes).toEqual([]);
    // A delta with something new on a bot you are not looking at: the OS notification.
    delta([bot("s", 2, [], { kind: "super" }), bot("o", 2, [ev(2, "question", "Which one?")])]);
    expect(store.eventsOf("o").map((e) => e.seq)).toEqual([1, 2]);
    expect(store.getState().cursors.o).toBe(2);
    expect(notes).toEqual(["O asks you"]);
  });

  test("the per-bot event list is capped at EVENT_CAP", () => {
    snap([bot("a", 0, [])]);
    const many = Array.from({ length: store.EVENT_CAP + 100 }, (_, i) => ev(i + 1, "action"));
    delta([bot("a", many.length, many)]);
    const evs = store.eventsOf("a");
    expect(evs.length).toBe(store.EVENT_CAP);
    expect(evs[evs.length - 1].seq).toBe(many.length);
  });

  test("a fresh system note on the bot on screen toasts once; a snapshot carrying it again does not re-toast", () => {
    snap([bot("a", 1, [ev(1, "user")])]);
    delta([bot("a", 2, [ev(2, "system", "Paused")])]);
    expect(store.getState().toasts.map((t) => t.label)).toEqual(["Paused"]);
    const ids = store.getState().toasts.map((t) => t.id);
    snap([bot("a", 2, [ev(1, "user"), ev(2, "system", "Paused")])]);
    expect(store.getState().toasts.map((t) => t.id)).toEqual(ids);
  });

  test("a hand-over on the selected bot opens Stage; its end closes the Stage it opened", async () => {
    const d = document as unknown as Record<string, unknown>;
    const hadGet = "getElementById" in d, hadTitle = "title" in d;
    d.getElementById = () => null;
    if (!hadTitle) d.title = "Bots";
    setBotsRoot({ isConnected: true } as unknown as HTMLElement);
    const opened: { focus: boolean }[] = [];
    store.onHandover((o) => { opened.push(o); store.setFast(true); }, () => store.setFast(false));
    try {
      snap([bot("a", 1, [], { status: "running" })]);
      delta([bot("a", 1, [], { status: "running", control: true, control_by: "bot" } as Partial<Bot>)]);
      await tick();
      expect(opened).toEqual([{ focus: true }]);
      expect(cur().params).toEqual({ fast: true, shot_for: "a" });  // Stage open: the stream re-keyed to `fast`
      delta([bot("a", 1, [], { status: "running", control: false, control_by: null } as Partial<Bot>)]);
      expect(store.getState().fast).toBe(false);
      expect(cur().params).toEqual({ fast: false, shot_for: "a" });
    } finally {
      setBotsRoot(null);
      store.onHandover(() => {}, () => store.setFast(false));
      if (!hadGet) delete d.getElementById;
      if (!hadTitle) delete d.title;
    }
  });

  test("act(): the mutation, then ONE resync of the stream, resolving when the next frame has landed", async () => {
    snap([bot("a", 1, [ev(1, "user")])]);
    await tick();
    resyncs = [];
    let done = false;
    const p = store.act(async () => 42).then((r) => { done = true; return r; });
    await tick();
    expect(resyncs).toEqual([{ topic: "bots", params: { fast: false, shot_for: "a" } }]);
    expect(done).toBe(false);
    snap([bot("a", 2, [ev(1, "user"), ev(2, "done")])]);
    expect(await p).toBe(42);
    expect(store.eventsOf("a").map((e) => e.seq)).toEqual([1, 2]);
  });

  test("poll() with no subscription (the route unmounted) resolves at once and asks nothing", async () => {
    teardown();
    teardown = () => {};
    resyncs = [];
    await store.poll();
    expect(resyncs).toEqual([]);
  });

  test("a refusal shows the banner; the next frame hides it", () => {
    cur().cb(null, null, { error: "boom", status: 500 });
    expect(store.getState().banner).toEqual({ show: true, text: "Worker unreachable: boom" });
    snap([bot("a", 1, [])]);
    expect(store.getState().banner.show).toBe(false);
  });
});

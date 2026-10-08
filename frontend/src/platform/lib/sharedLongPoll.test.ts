// sharedLongPoll: one upstream long-poll per URL for every document of the
// origin. Documents are simulated as separate instances over an in-memory
// BroadcastChannel bus and a fake Web Locks table; no mock.module (it is
// process-wide in bun), everything is injected through the env.
import { expect, test } from "bun:test";

import {
  createSharedLongPoll,
  FOLLOWER_TIMEOUT_MS,
  type ChannelLike,
  type SharedLongPollEnv,
} from "@platform/lib/sharedLongPoll";

const flush = () => new Promise<void>((r) => setTimeout(r, 0));

function makeBus() {
  const endpoints: ChannelLike[] = [];
  return {
    channel(): ChannelLike {
      const ep: ChannelLike = {
        onmessage: null,
        postMessage(m) {
          for (const other of endpoints) {
            if (other === ep) continue;
            queueMicrotask(() => other.onmessage?.({ data: structuredClone(m) }));
          }
        },
        close() {
          const i = endpoints.indexOf(ep);
          if (i >= 0) endpoints.splice(i, 1);
        },
      };
      endpoints.push(ep);
      return ep;
    },
  };
}

/** One lock name, granted to one holder at a time in request order, or to
 *  everyone at once when `grantAll` (a split-brain). */
function makeLocks(grantAll = false, startHeld = false) {
  const holders: { cb: () => Promise<unknown>; granted: boolean }[] = [];
  let held = startHeld;
  const pump = () => {
    for (const h of holders) {
      if (h.granted) continue;
      if (held && !grantAll) return;
      held = true;
      h.granted = true;
      queueMicrotask(() => void h.cb());
    }
  };
  return {
    request(_name: string, cb: () => Promise<unknown>) {
      holders.push({ cb, granted: false });
      pump();
      return new Promise<unknown>(() => {});
    },
    /** An outside holder (a document not under test) lets go. */
    open() {
      held = false;
      queueMicrotask(pump);
    },
    /** The document holding lock `i` goes away. */
    release(i: number) {
      holders[i]!.granted = true;
      held = false;
      queueMicrotask(pump);
    },
  };
}

interface Upstream {
  url: string;
  signal: AbortSignal;
  respond(status: number, body: string): void;
  fail(e: unknown): void;
}

function makeFetch() {
  const calls: Upstream[] = [];
  const fn: SharedLongPollEnv["fetch"] = (url, init) =>
    new Promise<Response>((resolve, reject) => {
      const call: Upstream = {
        url,
        signal: init.signal,
        respond: (status, body) => resolve(new Response(body, { status })),
        fail: reject,
      };
      init.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      calls.push(call);
    });
  return { fn, calls };
}

function makeTimers() {
  const fns = new Map<number, () => void>();
  let n = 0;
  return {
    setTimeout: (fn: () => void) => {
      fns.set(++n, fn);
      return n;
    },
    clearTimeout: (h: unknown) => void fns.delete(h as number),
    fireAll: () => {
      for (const [k, f] of [...fns]) {
        fns.delete(k);
        f();
      }
    },
    count: () => fns.size,
  };
}

function world(opts: { grantAll?: boolean; startHeld?: boolean } = {}) {
  const bus = makeBus();
  const locks = makeLocks(opts.grantAll, opts.startHeld);
  const upstream = makeFetch();
  const timers = makeTimers();
  let ids = 0;
  const docs: ReturnType<typeof createSharedLongPoll>[] = [];
  return {
    locks,
    upstream,
    timers,
    doc(name: string) {
      const d = createSharedLongPoll({
        channel: bus.channel(),
        locks,
        fetch: upstream.fn,
        setTimeout: timers.setTimeout,
        clearTimeout: timers.clearTimeout,
        newId: () => `${name}-${++ids}`,
      });
      docs.push(d);
      return d;
    },
  };
}

const URL1 = "/api/tasks/changes?since=1";

test("three documents asking for one URL cause exactly one upstream fetch", async () => {
  const w = world();
  const [a, b, c] = [w.doc("a"), w.doc("b"), w.doc("c")];
  const ra = a.fetch(URL1);
  await flush(); // a is the leader
  const rb = b.fetch(URL1);
  const rc = c.fetch(URL1);
  await flush();
  expect(w.upstream.calls).toHaveLength(1);
  w.upstream.calls[0]!.respond(200, '{"gen":2}');
  const [xa, xb, xc] = await Promise.all([ra, rb, rc]);
  for (const x of [xa, xb, xc]) {
    expect(x.status).toBe(200);
    expect(await x.json()).toEqual({ gen: 2 });
  }
  expect(w.timers.count()).toBe(0);
});

test("different URLs are separate upstream fetches", async () => {
  const w = world();
  const [a, b] = [w.doc("a"), w.doc("b")];
  void a.fetch(URL1);
  await flush();
  void b.fetch("/api/tasks/changes?since=2");
  await flush();
  expect(w.upstream.calls.map((c) => c.url).sort()).toEqual([URL1, "/api/tasks/changes?since=2"]);
});

test("the upstream aborts only when every waiter has aborted", async () => {
  const w = world();
  const [a, b] = [w.doc("a"), w.doc("b")];
  const ca = new AbortController();
  const cb = new AbortController();
  const ra = a.fetch(URL1, { signal: ca.signal });
  await flush();
  const rb = b.fetch(URL1, { signal: cb.signal });
  await flush();
  expect(w.upstream.calls).toHaveLength(1);
  const up = w.upstream.calls[0]!;

  cb.abort(); // the follower leaves; the leader's own caller still waits
  await expect(rb).rejects.toMatchObject({ name: "AbortError" });
  await flush();
  expect(up.signal.aborted).toBe(false);

  ca.abort();
  await expect(ra).rejects.toMatchObject({ name: "AbortError" });
  expect(up.signal.aborted).toBe(true);
});

test("a network error rejects every waiter with a TypeError and is broadcast", async () => {
  const w = world();
  const [a, b] = [w.doc("a"), w.doc("b")];
  const ra = a.fetch(URL1);
  await flush();
  const rb = b.fetch(URL1);
  await flush();
  const errOf = (p: Promise<Response>) => p.then(() => null, (e: unknown) => e);
  const outcomes = Promise.all([errOf(ra), errOf(rb)]);
  w.upstream.calls[0]!.fail(new TypeError("Failed to fetch"));
  for (const e of await outcomes) expect(e).toBeInstanceOf(TypeError);
});

test("failover: the next document becomes leader and serves the pending requests with one new fetch", async () => {
  const w = world();
  const [a, b, c] = [w.doc("a"), w.doc("b"), w.doc("c")];
  const ra = a.fetch(URL1); // lock holder 0
  await flush();
  const rb = b.fetch(URL1); // holder 1
  const rc = c.fetch(URL1); // holder 2
  await flush();
  expect(w.upstream.calls).toHaveLength(1);

  a.dispose(); // the leader's document closes ...
  await expect(ra).rejects.toMatchObject({ name: "AbortError" });
  w.locks.release(0); // ... and the browser frees its lock
  await flush();
  await flush();

  const live = w.upstream.calls.filter((x) => !x.signal.aborted);
  expect(live).toHaveLength(1);
  live[0]!.respond(200, "{}");
  expect((await rb).status).toBe(200);
  expect((await rc).status).toBe(200);
});

test("a request made before any leader is known is sent once a leader arrives", async () => {
  const w = world({ startHeld: true }); // someone else holds the lock for now
  const [a, b] = [w.doc("a"), w.doc("b")];
  void a.fetch("/unrelated").catch(() => {}); // a's lock request is first in line
  const rb = b.fetch(URL1);
  await flush();
  expect(w.upstream.calls).toHaveLength(0); // no leader: nothing sent, nothing fetched
  w.locks.open(); // a wins, announces itself, and b's request is then sent to it
  await flush();
  await flush();
  const forUrl = w.upstream.calls.filter((x) => x.url === URL1);
  expect(forUrl).toHaveLength(1);
  forUrl[0]!.respond(200, "ok");
  expect(await (await rb).text()).toBe("ok");
});

test("split-brain: a third document's request is fetched once, by the latest announced leader", async () => {
  const w = world({ grantAll: true });
  const [a, b, c] = [w.doc("a"), w.doc("b"), w.doc("c")];
  // a and b both end up leaders. Their own pending sets are empty-free.
  void a.fetch("/other-a");
  void b.fetch("/other-b");
  await flush();
  const before = w.upstream.calls.length;
  const rc = c.fetch(URL1);
  await flush();
  await flush();
  const forUrl = w.upstream.calls.slice(before).filter((x) => x.url === URL1);
  expect(forUrl).toHaveLength(1);
  forUrl[0]!.respond(200, "x");
  expect((await rc).status).toBe(200);
});

test("without a channel or locks it is a plain no-store fetch", async () => {
  for (const missing of ["channel", "locks"] as const) {
    const upstream = makeFetch();
    const seen: unknown[] = [];
    const d = createSharedLongPoll({
      channel: missing === "channel" ? null : makeBus().channel(),
      locks: missing === "locks" ? null : makeLocks(),
      fetch: (u, i) => {
        seen.push(i.cache);
        return upstream.fn(u, i);
      },
      setTimeout: () => 0,
      clearTimeout: () => {},
      newId: () => "x",
    });
    const r = d.fetch(URL1);
    expect(upstream.calls).toHaveLength(1);
    upstream.calls[0]!.respond(200, "p");
    expect(await (await r).text()).toBe("p");
    expect(seen).toEqual(["no-store"]);
  }
});

test("a follower with no answer gives up with a TypeError after FOLLOWER_TIMEOUT_MS", async () => {
  expect(FOLLOWER_TIMEOUT_MS).toBeGreaterThan(25_000);
  const w = world();
  const [a, b] = [w.doc("a"), w.doc("b")];
  a.fetch("/warm").catch(() => {}); // a is leader
  await flush();
  const rb = b.fetch(URL1);
  await flush();
  a.dispose(); // the leader dies and no one takes over (b's lock request is still queued)
  w.timers.fireAll();
  await expect(rb).rejects.toBeInstanceOf(TypeError);
});

test("an already-aborted signal rejects at once", async () => {
  const w = world();
  const d = w.doc("a");
  const ctl = new AbortController();
  ctl.abort();
  await expect(d.fetch(URL1, { signal: ctl.signal })).rejects.toMatchObject({ name: "AbortError" });
  expect(w.upstream.calls).toHaveLength(0);
});

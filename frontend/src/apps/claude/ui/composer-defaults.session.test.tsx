// WHICH CONVERSATION THE PILLS ARE ABOUT (Akshil, 2026-09-18: "what I select as
// a user stays").
//
// The composer ranked `param > detected > pref > constant`, and `detected` used
// to be a question about a FOLDER: `agent._defaults(file)` reads the model last
// used anywhere in it. Every door into a chat asked it that way, so the same
// conversation reached from the Tasks peek, from that peek's Open button, from a
// row, from the chat list or from a bare URL could each be told a different
// thing — and a model the reader had picked in THIS chat lost to whatever some
// other chat in the same folder ran with more recently.
//
// Two halves to the fix, and both are pinned below. The question gains a
// SUBJECT: every route ends at this hook and this hook names the session, so all
// of them ask about one conversation. And the answer gains a RECORD, which the
// app writes itself on every spawn, every send and every pill pick — so it is
// complete where the transcript is not, exists before the transcript does, and
// ranks above the `?model=`/`?effort=` params a deep link seeded. The agent half
// is pinned in `tests/test_claude_sessions_merged.py`; this is the half that has
// to ASK, and now also the half that has to WRITE.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";

const { useComposerDefaults } = await import("./composer-defaults");
const { createMemoryParamsStore } = await import("../params/store");

const realFetch = globalThis.fetch;
const mounted: ReactTestRenderer[] = [];

afterEach(async () => {
  for (const r of mounted.splice(0)) await act(async () => r.unmount());
  globalThis.fetch = realFetch;
});

/** What the agent's `defaults` action answers with. `recorded` is the app's own
 *  per-session record, which outranks every param. */
type Defaults = {
  model?: string;
  effort?: string;
  recorded?: { model: string; effort: string };
};

/** Every `/api/run` and `/api/tasks/settings` body this render posted, newest
 *  last. Both go through one shim so a test can assert on the ORDER of a pick:
 *  it re-asks nothing and writes once. */
function record(answer: Defaults = {}): Record<string, unknown>[] {
  const seen: Record<string, unknown>[] = [];
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    const target = String(url);
    if ((target.startsWith("/api/run")
         || target.startsWith("/api/tasks/settings")) && init?.body) {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      if (target.startsWith("/api/tasks/settings")) body.__settings = true;
      seen.push(body);
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        ok: true,
        result: {
          model: answer.model ?? "",
          effort: answer.effort ?? "",
          recorded: answer.recorded ?? { model: "", effort: "" },
        },
        model: "", effort: "",
      }),
    } as unknown as Response);
  }) as unknown as typeof fetch;
  return seen;
}

/** Every record write this render made, newest last. */
function posted(seen: Record<string, unknown>[]) {
  return seen.filter((b) => b.__settings);
}

/** The params the `defaults` call went out with, or null if it never went. */
function askedWith(seen: Record<string, unknown>[]) {
  const call = seen.find(
    (b) => (b.params as { action?: string } | undefined)?.action === "defaults",
  );
  return call ? (call.params as Record<string, unknown>) : null;
}

/** The live hook result, so a test can read the pills and move them. */
type Pills = ReturnType<typeof useComposerDefaults>;

async function mount(params: ReturnType<typeof createMemoryParamsStore>) {
  const box: { pills: Pills | null } = { pills: null };
  await act(async () => {
    const r = create(createElement(function Probe() {
      box.pills = useComposerDefaults("/w/p/.fused/claude", "/w/p", params);
      return null;
    }));
    mounted.push(r);
  });
  await act(async () => { await new Promise((done) => setTimeout(done, 0)); });
  return box;
}

test("a chat that HAS a session asks about that conversation", async () => {
  const seen = record();
  await mount(createMemoryParamsStore({ session_id: "sess-abc" }));
  expect(askedWith(seen)).toMatchObject({
    action: "defaults", file: "/w/p", session_id: "sess-abc",
  });
});

test("a chat with no session asks the folder's question, exactly as before", async () => {
  // "" is a real state — a conversation that has not started. The agent answers
  // the folder for it, and the HOST's own seed (`ChatMount`'s `model`/`effort`,
  // which the Tasks peek fills from the task's stored setting) is what speaks
  // for that window. Asserted as the ABSENCE of the key, so an empty string can
  // never be sent and read as "the session literally named ''".
  const seen = record();
  await mount(createMemoryParamsStore({}));
  const asked = askedWith(seen);
  expect(asked).toMatchObject({ action: "defaults", file: "/w/p" });
  expect(asked && "session_id" in asked).toBe(false);
});

test("a session that arrives LATE is asked about as soon as it exists", async () => {
  // A chat started from the composer has no id for the two to four seconds
  // before the CLI reports one, and the first answer was therefore about the
  // folder. The conversation's own settings have to appear the moment it has an
  // identity, or "what I select stays" holds for every chat except a new one.
  const seen = record();
  const params = createMemoryParamsStore({});
  await mount(params);
  expect(askedWith(seen) && "session_id" in askedWith(seen)!).toBe(false);

  await act(async () => { params.set({ session_id: "sess-late" }); });
  await act(async () => { await new Promise((done) => setTimeout(done, 0)); });

  const calls = seen.filter(
    (b) => (b.params as { action?: string } | undefined)?.action === "defaults",
  );
  expect(calls.length).toBe(2);
  expect(calls[1].params).toMatchObject({ session_id: "sess-late" });
});


// ---- the chat's own RECORD, above the URL params -----------------------------
//
// The params are a SEED. "Fix with AI" and the New task card build deep links
// carrying `?model=`/`?effort=`, and the Tasks peek states the task's own two
// the same way — all of which answer for a chat that does not exist yet and must
// stand down the moment it does. A seed that kept outranking the record is how a
// pill moved mid-chat came back undone on the next open.

test("the record outranks the param a deep link seeded", async () => {
  record({ recorded: { model: "haiku", effort: "low" } });
  const box = await mount(
    createMemoryParamsStore({ session_id: "sess-a", model: "opus", effort: "max" }),
  );
  expect([box.pills!.model, box.pills!.effort]).toEqual(["haiku", "low"]);
});

test("the param still speaks for a chat with no record of its own", async () => {
  // The seeded window, and the reason the peek states the pair at all: a task
  // set up in the New task card and not yet run.
  record({ recorded: { model: "", effort: "" } });
  const box = await mount(
    createMemoryParamsStore({ session_id: "sess-a", model: "opus", effort: "max" }),
  );
  expect([box.pills!.model, box.pills!.effort]).toEqual(["opus", "max"]);
});

test("a record of ONE field leaves the other to the param", async () => {
  // Per field everywhere: Claude Code writes the transcript's effort only
  // sometimes, and the whole bug was one half of an answer coming from
  // somewhere else.
  record({ recorded: { model: "haiku", effort: "" } });
  const box = await mount(
    createMemoryParamsStore({ session_id: "sess-a", model: "opus", effort: "max" }),
  );
  expect([box.pills!.model, box.pills!.effort]).toEqual(["haiku", "max"]);
});

test("a pick is WRITTEN, and the pill shows it before the write lands", async () => {
  // The param alone dies with the address bar. The record is what every other
  // door into this chat reads first — and it is set optimistically so the pill
  // does not flicker back while the POST is in flight.
  const seen = record({ recorded: { model: "", effort: "" } });
  const box = await mount(createMemoryParamsStore({ session_id: "sess-a" }));

  await act(async () => { box.pills!.setEffort("max"); });
  expect(box.pills!.effort).toBe("max");
  expect(posted(seen).map((b) => ({ session_id: b.session_id, effort: b.effort })))
    .toEqual([{ session_id: "sess-a", effort: "max" }]);

  // …one field per pick, so moving the effort cannot erase the model the spawn
  // recorded.
  await act(async () => { box.pills!.setModel("haiku"); });
  expect(box.pills!.model).toBe("haiku");
  expect(posted(seen)[1]).toMatchObject({ session_id: "sess-a", model: "haiku" });
  expect("effort" in posted(seen)[1]).toBe(false);
});

test("a chat with no session writes nothing — there is nothing to key on", async () => {
  // The first send mints the id and records the pair server-side
  // (`agent._start`), so the pick is not lost; it simply has no subject yet.
  const seen = record();
  const box = await mount(createMemoryParamsStore({}));
  await act(async () => { box.pills!.setModel("haiku"); });
  expect(posted(seen)).toEqual([]);
  // The param still moved, so the pill — and the send it is about to make —
  // carry the pick.
  expect(box.pills!.model).toBe("haiku");
});

test("a pick made while the defaults read is in flight is not undone by its answer", async () => {
  // THE STALE-READ RACE. The `defaults` read goes out at mount; the reader
  // moves a pill before it comes back; the answer — composed before that pick
  // was recorded — lands last. Left to overwrite `recorded`, it snaps the pill
  // back to the old value, and because the record outranks the param the next
  // send would run (and re-record) the value the reader just left.
  const seen: Record<string, unknown>[] = [];
  let answer: ((v: unknown) => void) | null = null;
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    const target = String(url);
    if (target.startsWith("/api/tasks/settings") && init?.body) {
      seen.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      return Promise.resolve({
        ok: true, status: 200, json: () => Promise.resolve({ ok: true }),
      } as unknown as Response);
    }
    if (target.startsWith("/api/run")) {
      // Held open until the test lets it land.
      return new Promise((resolve) => {
        answer = resolve;
      });
    }
    return Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as unknown as Response);
  }) as unknown as typeof fetch;

  const box = await mount(createMemoryParamsStore({ session_id: "sess-a" }));
  expect(answer).not.toBeNull();

  await act(async () => { box.pills!.setEffort("low"); });
  expect(box.pills!.effort).toBe("low");
  expect(seen).toEqual([{ session_id: "sess-a", effort: "low" }]);

  // Now the read lands, carrying the record as it was BEFORE the pick.
  await act(async () => {
    answer!({
      ok: true, status: 200,
      json: () => Promise.resolve({
        ok: true,
        result: { model: "opus", effort: "max",
                  recorded: { model: "opus", effort: "max" } },
      }),
    });
    await new Promise((done) => setTimeout(done, 0));
  });
  // The picked field holds; the field the reader did not touch takes the read.
  expect(box.pills!.effort).toBe("low");
  expect(box.pills!.model).toBe("opus");
});


// ---- THE FAST READ, AND WHY THE PILLS WAIT FOR IT ----------------------------
//
// "It takes some time to load in these model and effort … when I come to the
// page after 2-3 seconds it flips, same when I reload" (Akshil, 2026-09-19).
//
// The record above is the rank that outranks every other, and it used to arrive
// on the SLOW read: `runAgent(agentDir, "defaults")` is a POST /api/run that
// spawns agent.py as a subprocess to scan a transcript tail. Two to three
// seconds — during which the pills had already painted the constant default or
// the URL seed, and then swapped it.
//
// So the record gets a door of its own: `GET /api/tasks/settings`, one JSON file
// the server already reads on every listing, answered in milliseconds. The slow
// read stays for the one thing only it knows — the transcript/folder ladder —
// and the pills are held (`pillsReady`) until nothing still in flight can change
// them. A pill that has never shown a value cannot flip to a different one.

/** The two reads, separately steerable: `fast` is `GET /api/tasks/settings`,
 *  `slow` is the agent's `defaults` action. Either can be HELD — answered only
 *  when the test says so — which is how the order of the two is asserted. */
function reads(opts: {
  fast?: Defaults["recorded"];
  slow?: Defaults;
  holdFast?: boolean;
  holdSlow?: boolean;
} = {}) {
  const posts: Record<string, unknown>[] = [];
  let letFast: (() => void) | null = null;
  let letSlow: (() => void) | null = null;
  const answer = (body: unknown) =>
    ({ ok: true, status: 200, json: () => Promise.resolve(body) }) as unknown as Response;
  const fastBody = () => ({
    model: opts.fast?.model ?? "",
    effort: opts.fast?.effort ?? "",
  });
  const slowBody = () => ({
    ok: true,
    result: {
      model: opts.slow?.model ?? "",
      effort: opts.slow?.effort ?? "",
      recorded: opts.slow?.recorded ?? { model: "", effort: "" },
    },
  });
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    const target = String(url);
    if (target.startsWith("/api/tasks/settings")) {
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return Promise.resolve(answer({ ok: true }));
      }
      if (!opts.holdFast) return Promise.resolve(answer(fastBody()));
      return new Promise<Response>((done) => {
        letFast = () => done(answer(fastBody()));
      });
    }
    if (target.startsWith("/api/run")) {
      if (!opts.holdSlow) return Promise.resolve(answer(slowBody()));
      return new Promise<Response>((done) => {
        letSlow = () => done(answer(slowBody()));
      });
    }
    return Promise.resolve(answer({}));
  }) as unknown as typeof fetch;
  const settle = async (open: (() => void) | null) => {
    await act(async () => {
      open?.();
      await new Promise((done) => setTimeout(done, 0));
    });
  };
  return {
    posts,
    landFast: () => settle(letFast),
    landSlow: () => settle(letSlow),
  };
}

test("the record arrives on the FAST read, with the slow one still in flight", async () => {
  // The whole fix in one assertion: the pills hold their final value before the
  // subprocess has said anything at all.
  const wire = reads({ fast: { model: "haiku", effort: "low" }, holdSlow: true });
  const box = await mount(createMemoryParamsStore({ session_id: "sess-a" }));
  expect([box.pills!.model, box.pills!.effort]).toEqual(["haiku", "low"]);
  expect(box.pills!.pillsReady).toBe(true);
  // …and the slow read landing afterwards changes nothing.
  await wire.landSlow();
  expect([box.pills!.model, box.pills!.effort]).toEqual(["haiku", "low"]);
});

test("the pills are NOT ready while the fast read is in flight", async () => {
  // The composer draws a wash for this window rather than a value, so there is
  // nothing on screen for the answer to overturn.
  const wire = reads({
    fast: { model: "haiku", effort: "low" },
    holdFast: true,
    slow: { recorded: { model: "", effort: "" } },
  });
  const box = await mount(createMemoryParamsStore({ session_id: "sess-a" }));
  expect(box.pills!.pillsReady).toBe(false);
  await wire.landFast();
  expect(box.pills!.pillsReady).toBe(true);
  expect([box.pills!.model, box.pills!.effort]).toEqual(["haiku", "low"]);
});

test("a field the record left empty waits for the slow read", async () => {
  // Per FIELD, like every other question about this pair. The model is settled
  // the moment the record answers; the effort has no record and no param, so
  // detection is the next rank down and the pill has to wait for it.
  const wire = reads({
    fast: { model: "haiku", effort: "" },
    slow: { effort: "max" },
    holdSlow: true,
  });
  const box = await mount(createMemoryParamsStore({ session_id: "sess-a" }));
  expect(box.pills!.model).toBe("haiku");
  expect(box.pills!.pillsReady).toBe(false);
  await wire.landSlow();
  expect(box.pills!.pillsReady).toBe(true);
  expect([box.pills!.model, box.pills!.effort]).toEqual(["haiku", "max"]);
});

test("a seeded param settles its pill without waiting for the slow read", async () => {
  // The rank below a record is the URL param, and nothing still in flight can
  // outrank it — so a deep-linked chat with no record of its own paints at once
  // rather than washing for the whole of the subprocess.
  reads({ fast: { model: "", effort: "" }, holdSlow: true });
  const box = await mount(
    createMemoryParamsStore({ session_id: "sess-a", model: "opus", effort: "max" }),
  );
  expect(box.pills!.pillsReady).toBe(true);
  expect([box.pills!.model, box.pills!.effort]).toEqual(["opus", "max"]);
});

test("a chat with NO session needs no fast read, and still resolves", async () => {
  // There is no conversation to have a record, so the record is "" for both
  // fields and known without asking. Detection answers the folder's question for
  // it, and the pills wait for exactly that.
  const wire = reads({ slow: { model: "opus", effort: "max" }, holdSlow: true });
  const box = await mount(createMemoryParamsStore({}));
  expect(box.pills!.pillsReady).toBe(false);
  await wire.landSlow();
  expect(box.pills!.pillsReady).toBe(true);
  expect([box.pills!.model, box.pills!.effort]).toEqual(["opus", "max"]);
});

test("a pick made while the FAST read is in flight is not undone by its answer", async () => {
  // The same stale-read race the slow read has, and it is tighter here rather
  // than gone: the read is milliseconds, but it is asked again the moment the
  // chat learns its id, and a pill can be moved in that window. The pick is
  // recorded server-side; the answer already on the wire was composed before
  // that write and lands after it.
  const wire = reads({ fast: { model: "opus", effort: "max" }, holdFast: true,
                       holdSlow: true });
  const box = await mount(createMemoryParamsStore({ session_id: "sess-a" }));

  await act(async () => { box.pills!.setEffort("low"); });
  expect(box.pills!.effort).toBe("low");
  expect(wire.posts).toEqual([{ session_id: "sess-a", effort: "low" }]);

  await wire.landFast();
  // The picked field holds; the one the reader did not touch takes the read.
  expect(box.pills!.effort).toBe("low");
  expect(box.pills!.model).toBe("opus");
});

test("`ready` waits on the record too — the automatic send cannot outrun it", async () => {
  // `ready` is what the "Fix with AI" boot branch awaits before its automatic
  // send, and that is the one send a human cannot hold back. It must not launch
  // on a constant the record was about to overturn.
  const wire = reads({ fast: { model: "haiku", effort: "low" }, holdFast: true });
  const box = await mount(createMemoryParamsStore({ session_id: "sess-a" }));
  expect(box.pills!.ready).toBe(false);
  await wire.landFast();
  expect(box.pills!.ready).toBe(true);
});

test("a field settled by a param is reported settled on its own, while the other still waits", async () => {
  // What a send carries is decided PER FIELD: a task peek opens with
  // `?model=haiku`, so the model is settled the moment the record read answers
  // ("" — nothing recorded yet) even though the effort still waits on the slow
  // read. Tying both to `pillsReady` sent that turn with no model at all.
  let answerSlow: ((v: unknown) => void) | null = null;
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    const target = String(url);
    if (target.startsWith("/api/tasks/settings")) {
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ model: "", effort: "" }),
      } as unknown as Response);
    }
    if (target.startsWith("/api/run") && init?.body) {
      return new Promise((resolve) => { answerSlow = resolve; });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) } as unknown as Response);
  }) as unknown as typeof fetch;
  const box = await mount(createMemoryParamsStore({ session_id: "sess-a", model: "haiku" }));
  expect(answerSlow).not.toBeNull();
  expect(box.pills!.modelSettled).toBe(true);
  expect(box.pills!.model).toBe("haiku");
  expect(box.pills!.effortSettled).toBe(false);
  expect(box.pills!.pillsReady).toBe(false);
});

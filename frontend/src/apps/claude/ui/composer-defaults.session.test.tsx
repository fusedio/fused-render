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

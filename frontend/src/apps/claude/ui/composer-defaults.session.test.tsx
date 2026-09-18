// WHICH CONVERSATION THE PILLS ARE ABOUT (Akshil, 2026-09-18: "what I select as
// a user stays").
//
// The composer ranks `param > detected > pref > constant`, and `detected` used
// to be a question about a FOLDER: `agent._defaults(file)` reads the model last
// used anywhere in it. Every door into a chat asked it that way, so the same
// conversation reached from the Tasks peek, from that peek's Open button, from a
// row, from the chat list or from a bare URL could each be told a different
// thing — and a model the reader had picked in THIS chat lost to whatever some
// other chat in the same folder ran with more recently.
//
// The session names the transcript that records what this conversation actually
// ran with. Every route ends at this hook and this hook now names the session,
// so all of them get one answer. The agent half is pinned in
// `tests/test_claude_sessions_merged.py`; this is the half that has to ASK.
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

/** Every `/api/run` body this render posted, newest last. */
function record(): Record<string, unknown>[] {
  const seen: Record<string, unknown>[] = [];
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    if (String(url).startsWith("/api/run") && init?.body) {
      seen.push(JSON.parse(String(init.body)) as Record<string, unknown>);
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true, result: { model: "", effort: "" } }),
    } as unknown as Response);
  }) as unknown as typeof fetch;
  return seen;
}

/** The params the `defaults` call went out with, or null if it never went. */
function askedWith(seen: Record<string, unknown>[]) {
  const call = seen.find(
    (b) => (b.params as { action?: string } | undefined)?.action === "defaults",
  );
  return call ? (call.params as Record<string, unknown>) : null;
}

async function mount(params: ReturnType<typeof createMemoryParamsStore>) {
  await act(async () => {
    const r = create(createElement(function Probe() {
      useComposerDefaults("/w/p/.fused/claude", "/w/p", params);
      return null;
    }));
    mounted.push(r);
  });
  await act(async () => { await new Promise((done) => setTimeout(done, 0)); });
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

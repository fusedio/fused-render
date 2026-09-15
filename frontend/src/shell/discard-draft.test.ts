// THE DISCARD GESTURE'S THREE PROMISES (Bugbot #1166), which the row's own
// render test cannot see because all three are about ORDER and FAILURE:
//
//   * nothing is deleted while a write on the same record is still in the air —
//     the New task modal's debounced PUT lands after the DELETE and puts the row
//     straight back (`markTaskDraftSpent`, the task half of the chat half that
//     already existed);
//   * a DELETE that fails undoes every optimistic step, or the page says the
//     draft is gone while the store still holds it and every composer on the key
//     answers empty for the rest of the session;
//   * and a move out of the chat's Recent list acts on the WHOLE record, never
//     on the row's clipped preview.
//
// Driven through the real `discardDraft` over a stubbed `fetch`, because what is
// being asserted is the sequence of requests it makes and what it does with
// their answers.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";

import type { Task } from "@platform/lib/api";

const { discardDraft } = await import("./ScheduleTaskViews");
const {
  dropListingKeys,
  readListing,
  resetListingFeedForTests,
  restoreListingRows,
  subscribeListing,
} = await import("./tasksPulse");
const {
  fetchChatDraft,
  onTaskDraftSpent,
  unmarkChatDraftSpent,
} = await import("@platform/lib/drafts");
type ListingEnv = import("./tasksPulse").ListingEnv;

/** One `new:<file>` chat draft as `/api/tasks` emits it. */
const chatRow = (key = "new:/repo/x.py"): Task =>
  ({
    key,
    task_id: "TASK-900",
    kind: "draft",
    draft_kind: "chat",
    draft_id: "",
    project: "/repo",
    target: key.slice("new:".length),
    file: key.slice("new:".length),
    session_id: "",
    title: "ship the thing",
    last_active: 10,
  }) as unknown as Task;

/** …and one half-filled New task form. */
const formRow = (id = "d-7"): Task =>
  ({
    key: `draft:${id}`,
    task_id: "TASK-901",
    kind: "draft",
    draft_kind: "task",
    draft_id: id,
    project: "/repo",
    target: "/repo",
    session_id: "",
    title: "Nightly report",
    last_active: 9,
  }) as unknown as Task;

interface Call {
  method: string;
  url: string;
}

const realFetch = globalThis.fetch;
let calls: Call[] = [];
/** Requests whose URL contains one of these answer `ok: false`. */
let refuse: string[] = [];
/** `/api/drafts` answers this. */
let store: Record<string, unknown> = {};

function stubFetch(): void {
  (globalThis as { fetch: unknown }).fetch = async (
    input: unknown,
    init?: { method?: string },
  ): Promise<Response> => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({ method, url });
    if (refuse.some((frag) => url.includes(frag))) {
      return { ok: false, status: 500, json: async () => ({}) } as unknown as Response;
    }
    if (url === "/api/drafts") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ chat: store, task: {} }),
      } as unknown as Response;
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) } as unknown as Response;
  };
}

/** A listing feed holding `rows`, so the optimistic drop has something to drop
 *  FROM — `dropListingKeys` is a no-op against an empty store, and then the
 *  restore would have nothing to prove. */
function seedListing(rows: Task[]): () => void {
  const env: ListingEnv = {
    fetch: () => new Promise(() => {}), // the change-poll parks
    hidden: () => false,
    whenVisible: () => ({ promise: new Promise<void>(() => {}), cancel: () => {} }),
    sleep: () => Promise.resolve(),
    tasks: () => Promise.resolve({ tasks: rows }),
    pokes: () => () => {},
    every: () => () => {},
  };
  return subscribeListing(() => {}, env);
}

const settle = async () => {
  for (let i = 0; i < 60; i++) await Promise.resolve();
};

afterEach(() => {
  (globalThis as { fetch: unknown }).fetch = realFetch;
  resetListingFeedForTests();
  calls = [];
  refuse = [];
  store = {};
});

describe("discardDraft", () => {
  test("a TASK draft waits out the card's in-flight write before the DELETE", async () => {
    // THE RACE (Bugbot #1166). The modal is open on this very form with a
    // debounced PUT already on the wire; `stop()` disarms the NEXT write and
    // cannot cancel that one, so it lands after the DELETE and the row is back.
    // The row has no handle on the card's autosave — this signal is the handle.
    stubFetch();
    const seen: Array<string | boolean> = [];
    let release: () => void = () => {};
    const held = new Promise<void>((r) => {
      release = r;
    });
    const off = onTaskDraftSpent((id, spent) => {
      seen.push(`${id}:${spent}`);
      // What the card hands back is its write settling.
      return spent ? held : undefined;
    });
    const off2 = seedListing([formRow()]);
    await settle();

    const done = discardDraft(formRow());
    await settle();
    expect(seen).toEqual(["d-7:true"]);
    // NOT YET: the card's PUT has not finished, so neither may the delete.
    expect(calls.filter((c) => c.method === "DELETE")).toEqual([]);

    release();
    await done;
    expect(calls.filter((c) => c.method === "DELETE").map((c) => c.url)).toEqual([
      "/api/drafts/task/d-7",
    ]);
    off();
    off2();
  });

  test("a DELETE that fails puts the row back and un-spends the key", async () => {
    // The draft is still on the server. A page that goes on saying otherwise —
    // and a key left reading as spent, so every composer on it answers empty
    // until a reload — is the page lying about what the reader still has.
    stubFetch();
    refuse = ["/api/drafts/chat/"];
    store = { "new:/repo/x.py": { text: "ship the thing", attachments: [], updated_at: 1 } };
    const off = seedListing([chatRow(), chatRow("new:/repo/other.py")]);
    await settle();
    expect(readListing()?.map((t) => t.key)).toContain("new:/repo/x.py");

    const gone = await discardDraft(chatRow());
    await settle();
    expect(gone).toBe(false);
    // The row is back…
    expect(readListing()?.map((t) => t.key)).toContain("new:/repo/x.py");
    // …and so are the words: a spent key answers null whatever the server holds.
    expect((await fetchChatDraft("new:/repo/x.py"))?.text).toBe("ship the thing");
    off();
  });

  test("a DELETE that lands leaves the row gone and the key spent", async () => {
    stubFetch();
    store = { "new:/repo/x.py": { text: "ship the thing", attachments: [], updated_at: 1 } };
    const off = seedListing([chatRow(), chatRow("new:/repo/other.py")]);
    await settle();

    const gone = await discardDraft(chatRow());
    await settle();
    expect(gone).toBe(true);
    expect(readListing()?.map((t) => t.key)).toEqual(["new:/repo/other.py"]);
    // Spent: nothing may hand these words back, whatever a stale store says.
    expect(await fetchChatDraft("new:/repo/x.py")).toBe(null);
    unmarkChatDraftSpent("new:/repo/x.py"); // this suite's own tidy-up
    off();
  });

  test("a task row with no draft id sends nothing and keeps its row", async () => {
    stubFetch();
    const row = { ...formRow(), draft_id: "" } as Task;
    const off = seedListing([row]);
    await settle();

    expect(await discardDraft(row)).toBe(false);
    expect(calls.filter((c) => c.method === "DELETE")).toEqual([]);
    expect(readListing()?.map((t) => t.key)).toEqual(["draft:d-7"]);
    off();
  });
});

describe("restoreListingRows", () => {
  test("puts a dropped row back in order, and is a no-op with nothing held", async () => {
    const off = seedListing([chatRow("a"), chatRow("b")]);
    await settle();
    dropListingKeys(["a"]);
    expect(readListing()?.map((t) => t.key)).toEqual(["b"]);

    restoreListingRows([chatRow("a")]);
    expect(readListing()?.map((t) => t.key).sort()).toEqual(["a", "b"]);
    // Nothing to put a row back INTO is not an error; the next read answers it.
    restoreListingRows([]);
    off();
    resetListingFeedForTests();
    restoreListingRows([chatRow("a")]);
    expect(readListing()).toBe(null);
  });
});

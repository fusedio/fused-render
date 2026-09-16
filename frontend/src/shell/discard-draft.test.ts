// THE DISCARD GESTURE'S PROMISES, which the row's own render test cannot see
// because they are about ORDER and FAILURE:
//
//   * the DELETE states the version it read, so a write still on the wire from
//     the card open on the same record is refused rather than landing after and
//     putting the row back. That used to take a signal the row fired and the
//     card answered (`markTaskDraftSpent`); the version does it for every
//     document at once, the other tab included (design "one record", §2);
//   * a DELETE that fails undoes every optimistic step, or the page says the
//     draft is gone while the store still holds it;
//   * and the CHIP case deletes the words without dropping the row: an ordinary
//     task whose composer is holding something is still a task (design §5).
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
  draftVersion,
  fetchChatDraft,
  forgetDraftVersion,
  rememberDraftVersion,
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
  ifMatch: string | null;
}

/** One stored chat record, contract-shaped (§1). */
const record = (text: string, version = 3) =>
  ({ text, attachments: [], updated_at: 1, version, form: {} });

const draftVersionOf = (key: string) => draftVersion(key);

const realFetch = globalThis.fetch;
let calls: Call[] = [];
/** Requests whose URL contains one of these answer `ok: false`. */
let refuse: string[] = [];
/** `/api/drafts` answers this. */
let store: Record<string, unknown> = {};

function stubFetch(): void {
  (globalThis as { fetch: unknown }).fetch = async (
    input: unknown,
    init?: { method?: string; headers?: Record<string, string> },
  ): Promise<Response> => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({ method, url, ifMatch: init?.headers?.["If-Match"] ?? null });
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
  // Versions are module state and deliberately outlive one test.
  for (const key of ["new:/repo/x.py", "new:/repo/other.py", "draft:d-7", "sess-4"]) {
    forgetDraftVersion(key);
  }
});

describe("discardDraft", () => {
  test("the DELETE states the version this client last read", async () => {
    // THE RACE IT CLOSES (Bugbot #1166). The New task modal is open on this very
    // form with a debounced PUT already on the wire; nothing can cancel that
    // write, so it used to land after the DELETE and put the row straight back.
    // The row had no handle on the card's autosave, so the row FIRED A SIGNAL
    // and the card answered with its in-flight write — a whole protocol for an
    // ordering the server can simply decide. The DELETE names the version, the
    // stale PUT names an older one, and only one of them is allowed to land.
    stubFetch();
    rememberDraftVersion("draft:d-7", 11);
    const off = seedListing([formRow()]);
    await settle();

    expect(await discardDraft(formRow())).toBe(true);
    const del = calls.find((c) => c.method === "DELETE");
    expect(del?.url).toBe("/api/drafts/task/d-7");
    expect(del?.ifMatch).toBe("11");
    off();
  });

  test("a DELETE that fails puts the row back, and the words are still there", async () => {
    // The draft is still on the server. A page that goes on saying otherwise is
    // a page lying about what the reader still has.
    stubFetch();
    refuse = ["/api/drafts/chat/"];
    store = { "new:/repo/x.py": record("ship the thing") };
    const off = seedListing([chatRow(), chatRow("new:/repo/other.py")]);
    await settle();
    expect(readListing()?.map((t) => t.key)).toContain("new:/repo/x.py");

    const gone = await discardDraft(chatRow());
    await settle();
    expect(gone).toBe(false);
    // The row is back…
    expect(readListing()?.map((t) => t.key)).toContain("new:/repo/x.py");
    // …and so are the words. Nothing client-side ever claimed otherwise: there
    // is no `spent` set to undo, which is the other half of what a version
    // replaced.
    expect((await fetchChatDraft("new:/repo/x.py"))?.text).toBe("ship the thing");
    off();
    forgetDraftVersion("new:/repo/x.py");
  });

  test("a DELETE that lands leaves the row gone and the key forgotten", async () => {
    stubFetch();
    store = { "new:/repo/x.py": record("ship the thing") };
    const off = seedListing([chatRow(), chatRow("new:/repo/other.py")]);
    await settle();

    const gone = await discardDraft(chatRow());
    await settle();
    expect(gone).toBe(true);
    expect(readListing()?.map((t) => t.key)).toEqual(["new:/repo/other.py"]);
    // …and the key reads as one this client has never seen, which is what stops
    // a later `gone` announcement for it from being acted on a second time
    // (contract §3).
    expect(draftVersionOf("new:/repo/x.py")).toBeUndefined();
    off();
  });

  test("the CHIP case deletes the words and keeps the row (design §5)", async () => {
    // The Cards wall's only draft: a card is a transcript, so a draft ROW has no
    // card, and what a wall can carry is an ordinary task whose composer is
    // holding something. Discarding that must not take the task with it.
    stubFetch();
    const chip = {
      ...formRow(),
      kind: "task",
      draft_kind: "",
      draft_id: "",
      session_id: "sess-4",
      draft: { preview: "half a thought" },
    } as unknown as Task;
    const off = seedListing([chip]);
    await settle();

    expect(await discardDraft(chip)).toBe(true);
    expect(calls.filter((c) => c.method === "DELETE").map((c) => c.url)).toEqual([
      "/api/drafts/chat/sess-4",
    ]);
    // The row stays: it is a task, and only its chip has gone.
    expect(readListing()?.map((t) => t.key)).toEqual(["draft:d-7"]);
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

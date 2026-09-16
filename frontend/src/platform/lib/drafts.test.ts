// ONE RECORD, VERSIONED — the drafts module's whole concurrency story (design
// "Drafts: one record, one key, versioned, pushed", §2).
//
// What this file used to test was a coordination layer: a `spent` set, an
// in-flight map, a `stop`/`settle`/`resume` protocol, an `era` counter — all of
// which existed to order one document's writes against its own reads, and all of
// which are gone. There is ONE writer per key now (`draftSyncer`), and what is
// tested here is the thing that replaced them: a desired state, one request in
// flight, a version between documents and a sequence within one.
//
// Driven through the real hook — react-test-renderer, no DOM, the same tool
// JobRow.test.tsx and apps/explorer/listing/hook-harness.ts both use — because
// what matters is a SEQUENCE that grepping the source cannot show. The harness
// below is a small reimplementation of hook-harness.ts's own `renderHook`, not
// an import of it: `platform/` may not import `apps/` (scripts/check-boundaries).
//
// Every test drives the write through `flushNow()` rather than waiting out the
// real debounce timer: `bun test` runs every suite in one process, and a real
// `setTimeout` left pending past a test's own assertions is exactly the kind of
// leftover that lands during whichever OTHER file happens to be running when it
// fires. `flush()` dispatches synchronously, so nothing here waits on a clock.
import { beforeEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";
import {
  chatDraftKey,
  deleteChatDraft,
  draftSyncer,
  draftVersion,
  fetchChatDraft,
  fetchDrafts,
  forgetDraftVersion,
  isChatDraftKey,
  newChatFile,
  resetDraftSyncers,
  saveChatDraft,
  saveTaskDraft,
  taskDraftKey,
  useAutosave,
  type Autosave,
} from "@platform/lib/drafts";

// `useAutosave`'s unload effect reaches for `window`/`document` — real globals
// in a browser, absent in bun's DOM-less test runtime. Neither is touched at
// drafts.ts's MODULE scope (only inside the hook's own effects, which run after
// this file's synchronous top level), so installing the shim here is enough.
installDomShim();

/** Mount `useAutosave` and expose its latest handle. */
function renderAutosave<T>(
  value: T,
  push: (value: T) => void,
  options?: { key?: string },
): { current: () => Autosave<T>; rerender: (next: T) => void; unmount: () => void } {
  let latest!: Autosave<T>;
  let renderer!: ReactTestRenderer;
  const Probe = (props: { value: T }): null => {
    latest = useAutosave(props.value, push, options);
    return null;
  };
  act(() => {
    renderer = create(createElement(Probe, { value }));
  });
  return {
    current: () => latest,
    rerender: (next: T) => {
      act(() => {
        renderer.update(createElement(Probe, { value: next }));
      });
    },
    unmount: () => {
      act(() => {
        renderer.unmount();
      });
    },
  };
}

/** One request, as this file's stub records it. */
interface Seen {
  method: string;
  url: string;
  body: unknown;
  ifMatch: string | null;
}

/** A `fetch` that answers whatever `reply` says and records what it was asked.
 *  Every test restores the real one, because bun runs every suite in one
 *  process. */
function serve(reply: (seen: Seen) => { status?: number; json: unknown }) {
  const calls: Seen[] = [];
  const real = globalThis.fetch;
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const seen: Seen = {
      method: init?.method ?? "GET",
      url,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      ifMatch: headers["If-Match"] ?? null,
    };
    calls.push(seen);
    const answer = reply(seen);
    const status = answer.status ?? 200;
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(answer.json),
    } as unknown as Response);
  }) as typeof fetch;
  return { calls, restore: () => { globalThis.fetch = real; } };
}

const chatRecord = (text: string, version: number) => ({
  text, attachments: [], updated_at: 1, version, form: {},
});

// ---- the version, stated and taken ------------------------------------------
describe("If-Match", () => {
  test("is omitted on a first write and stated on every one after it", async () => {
    const key = "new:/Users/me/if-match";
    forgetDraftVersion(key);
    const f = serve(() => ({ json: { ok: true, key, draft: chatRecord("a", 4) } }));
    await saveChatDraft(key, "a");
    // NOTHING TO CLOBBER YET. A key this client has never seen has no version to
    // state, and the server reads a missing header as unconditional (contract §2)
    // — which is right for a create and wrong for nothing.
    expect(f.calls[0]!.ifMatch).toBeNull();
    // …and the answer's version is adopted, so the NEXT write is conditional
    // without a GET in between.
    expect(draftVersion(key)).toBe(4);
    await saveChatDraft(key, "ab");
    expect(f.calls[1]!.ifMatch).toBe("4");
    f.restore();
    forgetDraftVersion(key);
  });

  test("a task write is versioned under the key the LISTING uses", async () => {
    // `draft:<id>`, not `<id>`: that is the key `/api/tasks/changes` pushes this
    // record's version under, and two spellings of one record is the class of bug
    // this design ends.
    const id = "d-version";
    forgetDraftVersion(taskDraftKey(id));
    const f = serve(() => ({
      json: { ok: true, draft_id: id, draft: { ...chatRecord("", 2), title: "x" } },
    }));
    await saveTaskDraft(id, {} as never);
    expect(draftVersion(taskDraftKey(id))).toBe(2);
    expect(draftVersion(id)).toBeUndefined();
    f.restore();
    forgetDraftVersion(taskDraftKey(id));
  });

  test("a DELETE states it too, and forgets the key afterwards", async () => {
    const key = "new:/Users/me/deleted";
    forgetDraftVersion(key);
    const f = serve((seen) =>
      seen.method === "GET"
        ? { json: { chat: { [key]: chatRecord("words", 9) }, task: {} } }
        : { json: { ok: true, key, removed: true } });
    await fetchDrafts();
    expect(draftVersion(key)).toBe(9);
    await deleteChatDraft(key);
    expect(f.calls[1]!.ifMatch).toBe("9");
    // A key with no version reads as "this client has never seen it", which is
    // what stops a later `gone` for it from being acted on (contract §3).
    expect(draftVersion(key)).toBeUndefined();
    f.restore();
  });

  test("a stale GET cannot drag the version backwards", async () => {
    const key = "new:/Users/me/backwards";
    forgetDraftVersion(key);
    const f = serve((seen) =>
      seen.method === "GET"
        ? { json: { chat: { [key]: chatRecord("old", 2) }, task: {} } }
        : { json: { ok: true, key, draft: chatRecord("new", 7) } });
    await saveChatDraft(key, "new");
    expect(draftVersion(key)).toBe(7);
    // The read was dispatched before the write and answers after it. Adopting
    // its number would make the next write state a version the server has
    // already moved past — refused for ever, on a record nobody else touched.
    await fetchDrafts();
    expect(draftVersion(key)).toBe(7);
    f.restore();
    forgetDraftVersion(key);
  });
});

// ---- 409, and the two honest things to do with it ---------------------------

// ---- the chat record's form ---------------------------------------------------

describe("saveChatDraft's form", () => {
  test("is left off the wire entirely when the caller has no opinion", async () => {
    // The composer never sends one, and the contract makes `form` a patch for
    // exactly that reason: a keystroke save must not wipe the time and repeat a
    // Schedule hop put on the same record (contract §2).
    const key = "new:/Users/me/no-form";
    forgetDraftVersion(key);
    const f = serve(() => ({ json: { ok: true, key, draft: chatRecord("a", 1) } }));
    await saveChatDraft(key, "a");
    expect(f.calls[0]!.body).toEqual({ text: "a", attachments: [] });
    expect("form" in (f.calls[0]!.body as object)).toBe(false);
    f.restore();
    forgetDraftVersion(key);
  });

  test("and rides along when the New task card has one", async () => {
    const key = "new:/Users/me/with-form";
    forgetDraftVersion(key);
    const f = serve(() => ({ json: { ok: true, key, draft: chatRecord("a", 1) } }));
    await saveChatDraft(key, "Ship it\n\nand run the tests", [], undefined, {
      when: "2026-09-17T09:00", repeat: "none", model: "opus",
    });
    const body = f.calls[0]!.body as { text: string; form: Record<string, unknown> };
    // THE WORDS ARE IN `text`, NEVER IN `form.description` (contract §1) — one
    // string both editors open on, which is what makes the round trip lossless.
    expect(body.text).toBe("Ship it\n\nand run the tests");
    expect(body.form.when).toBe("2026-09-17T09:00");
    expect("description" in body.form).toBe(false);
    f.restore();
    forgetDraftVersion(key);
  });
});

// ---- the syncer: one writer, and it owns the order ---------------------------
//
// A TABLE OF INTERLEAVINGS AGAINST A FAKE VERSIONED SERVER, because that is what
// the bugs were: never one call being wrong, always two correct calls landing in
// the wrong order. The lab below holds every request until a test lands it, so a
// test can say "the send's DELETE arrives before the autosave's PUT" and mean it.
//
// The fake server is the contract in thirty lines: a version per key, `If-Match`
// refused with the record, and a `(client, seq)` that is not newer than the last
// one applied dropped with `{ok: true, dropped: true}` (drafts-seq-contract.md).

interface Stored {
  text: string;
  attachments: unknown[];
  version: number;
}

function lab() {
  const store = new Map<string, Stored>();
  const notes = new Map<string, { client: string; seq: number }>();
  const held: Array<{
    seen: Seen;
    settle: (answer: unknown) => void;
    landed?: boolean;
  }> = [];
  const real = globalThis.fetch;
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const seen: Seen = {
      method: init?.method ?? "GET",
      url,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      ifMatch: headers["If-Match"] ?? null,
    };
    return new Promise<Response>((resolve) => {
      held.push({
        seen,
        settle: (answer) =>
          resolve({
            ok: (answer as { status?: number }).status === undefined
              || ((answer as { status: number }).status >= 200
                && (answer as { status: number }).status < 300),
            status: (answer as { status?: number }).status ?? 200,
            json: () => Promise.resolve((answer as { json: unknown }).json),
          } as unknown as Response),
      });
    });
  }) as typeof fetch;

  /** Answer request `i` the way the contract says, against the store. */
  const land = (i: number): Promise<void> => {
    const req = held[i];
    if (!req) throw new Error(`no request ${i}`);
    const key = decodeURIComponent(req.seen.url.split("/api/drafts/chat/")[1] ?? "");
    const body = (req.seen.body ?? {}) as {
      text?: string;
      attachments?: unknown[];
      client?: string;
      seq?: number;
    };
    const record = store.get(key);
    const note = notes.get(key);
    // SEQUENCE FIRST: a straggler from this same page is not a conflict for
    // anybody to resolve, whatever its `If-Match` says.
    if (body.client && typeof body.seq === "number") {
      if (note && note.client === body.client && body.seq <= note.seq) {
        req.settle({ json: { ok: true, dropped: true, key, draft: record ?? null } });
        return Promise.resolve();
      }
      notes.set(key, { client: body.client, seq: body.seq });
    }
    const want = req.seen.ifMatch === null ? null : Number(req.seen.ifMatch);
    if (want !== null && want !== (record?.version ?? 0)) {
      req.settle({
        status: 409,
        json: {
          error: "version",
          record: record ? { ...record, updated_at: 1, form: {} } : null,
          version: record?.version ?? 0,
          key,
        },
      });
      return Promise.resolve();
    }
    if (req.seen.method === "DELETE") {
      store.delete(key);
      req.settle({ json: { ok: true, key, removed: !!record } });
      return Promise.resolve();
    }
    const text = body.text ?? "";
    const attachments = body.attachments ?? [];
    if (!text.trim() && !attachments.length) {
      store.delete(key);
      req.settle({ json: { ok: true, key, draft: null } });
      return Promise.resolve();
    }
    const next: Stored = {
      text,
      attachments,
      version: (record?.version ?? 0) + 1,
    };
    store.set(key, next);
    req.settle({
      json: { ok: true, key, draft: { ...next, updated_at: 1, form: {} } },
    });
    return Promise.resolve();
  };

  return {
    held,
    store,
    /** Land everything outstanding, oldest first, until nothing new appears. */
    async settle(): Promise<void> {
      for (let i = 0; i < held.length; i += 1) {
        if (!held[i]!.landed) {
          held[i]!.landed = true;
          await land(i);
          await flushMicrotasks();
        }
      }
    },
    async landOne(i: number): Promise<void> {
      held[i]!.landed = true;
      await land(i);
      await flushMicrotasks();
    },
    text: (key: string) => store.get(key)?.text,
    version: (key: string) => store.get(key)?.version ?? 0,
    /** Somebody ELSE wrote this record — the other tab, the Board, a row's
     *  trash. No sequence of ours is touched by it, which is the point. */
    elsewhere(key: string, text: string): void {
      const record = store.get(key);
      store.set(key, {
        text,
        attachments: [],
        version: (record?.version ?? 0) + 1,
      });
    },
    restore: () => {
      globalThis.fetch = real;
      resetDraftSyncers();
    },
  };
}

/** Let every already-resolved promise run. Four turns: a settled request wakes
 *  the syncer, which may dispatch the next one, whose own `then` is another
 *  turn. */
async function flushMicrotasks(): Promise<void> {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

describe("the syncer's table of interleavings", () => {
  const KEY = "new:/Users/me/lab";
  // THE REGISTRY IS MODULE SCOPE, which is the point of it — so a test starts by
  // forgetting every syncer and every version, or it inherits the last one's
  // desired state and reads it as its own.
  beforeEach(() => {
    resetDraftSyncers();
    forgetDraftVersion(KEY);
    forgetDraftVersion("new:/Users/me/lab-two");
  });

  test("(a) type, type, blur — one request at a time, and the last word wins", async () => {
    const it = lab();
    forgetDraftVersion(KEY);
    const sync = draftSyncer(KEY);
    sync.setText("hel");
    sync.flushNow();
    expect(it.held.length).toBe(1);
    // A SECOND FLUSH WHILE THE FIRST IS OUT DISPATCHES NOTHING. This is the
    // whole one-in-flight rule: what used to happen here was two PUTs on the
    // wire whose landing order the network chose.
    sync.setText("hello");
    sync.flushNow();
    expect(it.held.length).toBe(1);
    await it.landOne(0);
    // …and the moment it clears, the newest state goes — not the state that was
    // current when the flush was asked for.
    expect(it.held.length).toBe(2);
    expect((it.held[1]!.seen.body as { text: string }).text).toBe("hello");
    await it.landOne(1);
    expect(it.text(KEY)).toBe("hello");
    expect(it.held.length).toBe(2);
    it.restore();
  });

  test("(b) Send with a PUT in flight, then a follow-up — no resurrection", async () => {
    const it = lab();
    forgetDraftVersion(KEY);
    const sync = draftSyncer(KEY);
    sync.setText("the message");
    sync.flushNow();
    // Send: the record should not exist. It waits for the PUT that is out.
    sync.markDeleted();
    expect(it.held.length).toBe(1);
    // …and the reader types the next thing before any of it has landed. That is
    // a NEWER statement about the same key, so it supersedes the delete instead
    // of racing it.
    sync.setText("a follow-up");
    await it.settle();
    sync.flushNow(); // the follow-up's own debounce, made to fire now
    await it.settle();
    // ONE RECORD, HOLDING THE FOLLOW-UP. The sent sentence is not in it, and no
    // delete raced a write that had not been made yet.
    expect(it.text(KEY)).toBe("a follow-up");
    it.restore();
  });

  test("(c) the hop hands off while a PUT is in flight, and states the version it made", async () => {
    const it = lab();
    forgetDraftVersion(KEY);
    const sync = draftSyncer(KEY);
    sync.setText("half a sentence");
    sync.flushNow();
    // Continue, pressed with that PUT still out and one more keystroke in.
    sync.setText("half a sentence, and the rest");
    const handed = sync.handoff();
    await it.settle();
    const out = await handed;
    expect(out.ok).toBe(true);
    expect(it.text(KEY)).toBe("half a sentence, and the rest");
    // The hop navigates with the version the server now holds — the one its own
    // write earned, not a number read out of a map somebody else has moved.
    expect(out.version).toBe(it.version(KEY));
    // …and the second request stated the version the first one made.
    expect(it.held[1]!.seen.ifMatch).toBe("1");
    it.restore();
  });

  test("(d) pagehide with a PUT in flight sends IMMEDIATELY, keepalive, newest text", async () => {
    const it = lab();
    forgetDraftVersion(KEY);
    const sync = draftSyncer(KEY);
    sync.setText("first");
    sync.flushNow();
    expect(it.held.length).toBe(1);
    sync.setText("first, then the last thing typed");
    // THE ONE FLUSH THAT MAY PASS AN IN-FLIGHT REQUEST (Bugbot 4026181414). A
    // keepalive flush that queued itself behind the PUT never left at all: the
    // browser cancels the PUT as the document goes, and the queued write dies
    // with it.
    sync.flushNow({ keepalive: true });
    expect(it.held.length).toBe(2);
    expect((it.held[1]!.seen.body as { text: string }).text)
      .toBe("first, then the last thing typed");
    it.restore();
  });

  test("(e) a remote change is adopted while idle, and kept over a reader mid-sentence", async () => {
    const it = lab();
    forgetDraftVersion(KEY);
    const sync = draftSyncer(KEY);
    let box = "";
    let focused = false;
    let kept = 0;
    let typedSince = "";
    sync.watch({
      focused: () => focused,
      localText: () => typedSince,
      adopt: (record) => {
        box = ((record as { text?: string } | null)?.text) ?? "";
      },
      onKept: () => {
        kept += 1;
      },
    });
    // The record is written by somebody else first, so this page's own write is
    // the one that loses.
    it.elsewhere(KEY, "their words");
    sync.setText("my words");
    sync.flushNow();
    await it.settle();
    // NOT FOCUSED: theirs are simply the newer words, and the box takes them.
    expect(box).toBe("their words");
    expect(kept).toBe(0);
    // …and nothing is written back: conceding a conflict and then winning it
    // anyway is not conceding it.
    expect(it.text(KEY)).toBe("their words");

    // Now the reader IS mid-sentence over a record somebody else moved again.
    focused = true;
    typedSince = "mine";
    it.elsewhere(KEY, "theirs again");
    sync.setText("mine");
    sync.flushNow();
    // …AND THEY GO ON TYPING WHILE IT IS IN THE AIR, which is what "mid-
    // sentence" means: the box says something else now than it did when the
    // request left, so the answer that comes back is about older words.
    typedSince = "mine, still being typed";
    sync.setText("mine, still being typed");
    await it.settle();
    expect(kept).toBe(1);
    expect(box).toBe("their words"); // the box was never overwritten
    expect(it.text(KEY)).toBe("mine, still being typed");
    it.restore();
  });

  test("(f) one page's sequence is per key, and says nothing about another page", async () => {
    const it = lab();
    const other = "new:/Users/me/lab-two";
    forgetDraftVersion(KEY);
    forgetDraftVersion(other);
    draftSyncer(KEY).setText("one");
    draftSyncer(KEY).flushNow();
    draftSyncer(other).setText("two");
    draftSyncer(other).flushNow();
    const first = it.held[0]!.seen.body as { client: string; seq: number };
    const second = it.held[1]!.seen.body as { client: string; seq: number };
    // ONE CLIENT ID FOR THE DOCUMENT, one counter per key: a busy chat must not
    // make the New task card's writes look stale.
    expect(first.client).toBe(second.client);
    expect(first.seq).toBe(1);
    expect(second.seq).toBe(1);
    await it.settle();
    // A write from ANOTHER document — no sequence of ours — lands on its own
    // version and is arbitrated by `If-Match` alone.
    it.elsewhere(KEY, "the other window");
    draftSyncer(KEY).setText("ours");
    draftSyncer(KEY).flushNow();
    expect((it.held[2]!.seen.body as { seq: number }).seq).toBe(2);
    await it.settle();
    expect(it.text(KEY)).toBe("ours");
    it.restore();
  });

  test("(g) a straggler that arrives after a newer write is dropped, not applied", async () => {
    const it = lab();
    forgetDraftVersion(KEY);
    const sync = draftSyncer(KEY);
    sync.setText("older");
    sync.flushNow();
    sync.setText("newer");
    sync.flushNow({ keepalive: true }); // two on the wire, deliberately
    expect(it.held.length).toBe(2);
    // The newer one arrives FIRST, which is the whole case: the older request is
    // now a straggler and the record must not go backwards.
    await it.landOne(1);
    expect(it.text(KEY)).toBe("newer");
    await it.landOne(0);
    expect(it.text(KEY)).toBe("newer");
    // …and the page learned nothing from the dropped answer: the version it
    // holds is the one the write that landed made.
    expect(draftVersion(KEY)).toBe(it.version(KEY));
    it.restore();
  });

  test("the first write of an unknown key expects no record (If-Match: 0)", async () => {
    const it = lab();
    forgetDraftVersion(KEY);
    const sync = draftSyncer(KEY);
    sync.setText("hello");
    sync.flushNow();
    // Zero is "I expect nothing here" (contract §2): a page that could not read
    // the store must not silently overwrite a draft it has never seen.
    expect(it.held[0]!.seen.ifMatch).toBe("0");
    await it.settle();
    expect(it.held[0]!.seen.body).toMatchObject({ text: "hello", attachments: [] });
    it.restore();
  });

  test("a DELETE never states zero — the trash is not a create", async () => {
    const it = lab();
    forgetDraftVersion(KEY);
    const sync = draftSyncer(KEY);
    sync.markDeleted();
    expect(it.held[0]!.seen.method).toBe("DELETE");
    expect(it.held[0]!.seen.ifMatch).toBeNull();
    // …and it carries the sequence, which is what keeps a keepalive PUT from
    // landing behind it and putting the record back.
    expect((it.held[0]!.seen.body as { seq: number }).seq).toBe(1);
    await it.settle();
    it.restore();
  });

  test("seeding a box from its record writes nothing at all", async () => {
    const it = lab();
    forgetDraftVersion(KEY);
    const sync = draftSyncer(KEY);
    sync.seedText("what the server already holds", []);
    sync.flushNow();
    expect(it.held.length).toBe(0);
    // …and the next keystroke does write, because that is a change.
    sync.setText("what the server already holds, plus more");
    sync.flushNow();
    expect(it.held.length).toBe(1);
    await it.settle();
    it.restore();
  });

  test("forget() drops a pending write — the record is out of this page's hands", async () => {
    const it = lab();
    forgetDraftVersion(KEY);
    const sync = draftSyncer(KEY);
    sync.setText("about to become a task");
    sync.forget();
    sync.flushNow();
    expect(it.held.length).toBe(0);
    it.restore();
  });

  test("the three moments the document may be going away are listened for once", () => {
    // Not per editor: the debounce belongs to the KEY, so the unload listeners
    // do too. Read off the source because an event nobody can dispatch in this
    // runtime is still a promise this module makes.
    const src = readFileSync(join(import.meta.dir, "drafts.ts"), "utf8");
    expect(src).toContain('window.addEventListener("pagehide", leaving)');
    expect(src).toContain('document.addEventListener("visibilitychange"');
    expect(src).toContain('window.addEventListener("blur"');
    // …and the one that leaves carries keepalive, which is what makes it leave.
    expect(src).toContain("sync.flushNow({ keepalive: true })");
  });
});

// ---- the hook over it ----------------------------------------------------------

describe("nothing is minted by merely opening an editor", () => {
  test("a mount writes nothing, however full the form it mounts on", () => {
    // design §4: "no PUT until title or text non-empty. Open+close empty leaves
    // nothing." The hook's half of that is the baseline — the opening value
    // counts as already-written — and it is per MOUNT, so a second composer
    // opening on a key this document has already written does not read its own
    // empty box as an instruction to delete.
    const pushed: string[] = [];
    const box = renderAutosave({ title: "Ship it", text: "already here" }, (value) => {
      pushed.push(JSON.stringify(value));
    });
    box.current().flush();
    box.unmount();
    expect(pushed).toEqual([]);
  });

  test("a change is pushed once, and a re-render of the same value is not a change", () => {
    const pushed: string[] = [];
    const box = renderAutosave({ text: "" }, (value) => {
      pushed.push(JSON.stringify(value));
    });
    box.rerender({ text: "one" });
    box.rerender({ text: "one" });
    box.rerender({ text: "two" });
    expect(pushed).toEqual(['{"text":"one"}', '{"text":"two"}']);
    box.unmount();
  });

  test("reset() leaves the send nothing to write", () => {
    const pushed: string[] = [];
    const box = renderAutosave({ text: "" }, (value) => {
      pushed.push(JSON.stringify(value));
    });
    // A sentence typed, then sent: `reset` is told what the box is ABOUT to
    // hold, because the state write that empties it has not landed yet.
    box.rerender({ text: "ship the release notes" });
    pushed.length = 0;
    box.current().reset({ text: "" });
    box.rerender({ text: "" });
    box.unmount();
    expect(pushed).toEqual([]);
  });

  test("…and the hook's handle is two calls, with no ordering left in it", async () => {
    const mod = await import("@platform/lib/drafts");
    for (const gone of [
      "writeInitial",
      "markChatDraftSpent",
      "unmarkChatDraftSpent",
      "onChatDraftSpent",
      "markTaskDraftSpent",
      "onTaskDraftSpent",
      "unmarkTaskDraftSpent",
      "readChatDraft",
    ]) {
      expect(Object.keys(mod)).not.toContain(gone);
    }
    // `settle` is gone with the thing it ordered: there is one writer per key
    // now, so a caller has nothing left to wait for except the server itself
    // (`handoff`).
    const box = renderAutosave({ n: 0 }, () => {});
    expect(Object.keys(box.current()).sort()).toEqual(["flush", "reset"]);
    box.unmount();
  });
});


// ---- keys ---------------------------------------------------------------------

describe("the chat does not rekey its own draft", () => {
  const chat = () =>
    readFileSync(join(import.meta.dir, "../../apps/claude/ClaudeChat.tsx"), "utf8");
  const composer = () =>
    readFileSync(join(import.meta.dir, "../../apps/claude/ui/Composer.tsx"), "utf8");
  const drafts = () => readFileSync(join(import.meta.dir, "drafts.ts"), "utf8");

  test("no rekey call, no note, no route — on either side", () => {
    const c = chat();
    expect(c).not.toContain("rekeyChatDraft");
    expect(c).not.toContain("pendingRekey");
    const d = drafts();
    expect(d).not.toContain("export async function rekeyChatDraft");
    // The URL as it would be WRITTEN, not as the module's own prose names it.
    expect(d).not.toContain('"/api/drafts/chat/rekey"');
  });

  test("the send still spends its own key, which is all it ever owed", () => {
    // …and it spends it by SAYING SO to the one writer of that record, rather
    // than firing a DELETE beside whatever the box has on the wire. The
    // ordering that used to live here — take the in-flight promise, wait for
    // it, state the version it made — is the syncer's now, so none of it is
    // left in this file to get subtly wrong again.
    const src = composer();
    expect(src).toContain("draftSyncer(draftKeyRef.current).markDeleted();");
    expect(src).not.toContain("autosaveRef.current.settle()");
    expect(src).not.toContain("deleteChatDraft");
    expect(src).not.toContain("settleDraft");
  });
});

describe("the two shapes of a chat key", () => {
  test("reads the file back out of a `new:` key, and nothing out of a session", () => {
    // The way BACK to a never-sent chat is built out of this string — the task
    // card's "Back to chat" and the draft row's own href both have to land on
    // the same `file` the composer keys on, or the chat that opens seeds from a
    // key nothing wrote.
    expect(newChatFile(chatDraftKey(null, "/Users/me/news"))).toBe("/Users/me/news");
    expect(newChatFile("new:/a/b")).toBe("/a/b");
    expect(newChatFile("sess-9")).toBe("");
    expect(newChatFile("")).toBe("");
    // Nothing is trimmed or normalised — chatDraftKey's rule, held here too.
    expect(newChatFile("new:/Users/me/news/")).toBe("/Users/me/news/");
  });

  test("and tells a chat draft's listing key from every other row's", () => {
    expect(isChatDraftKey(chatDraftKey("sess-9", null))).toBe(true);
    expect(isChatDraftKey(chatDraftKey(null, "/Users/me/news"))).toBe(true);
    expect(isChatDraftKey("new:")).toBe(true);
    expect(isChatDraftKey("draft:d-7")).toBe(false);
    expect(isChatDraftKey("pending:e-3")).toBe(false);
    expect(isChatDraftKey("")).toBe(false);
  });

  test("a key is a PATH on the wire, segment by segment", () => {
    const key = "new:/Users/me/a b/x.py";
    forgetDraftVersion(key);
    const f = serve(() => ({ json: { ok: true, key, draft: chatRecord("a", 1) } }));
    void saveChatDraft(key, "a");
    // The separators stand; each segment is encoded. `encodeURIComponent` on the
    // whole key would send `%2F`, which every layer between here and the route
    // gets to normalise differently.
    expect(f.calls[0]!.url).toBe("/api/drafts/chat/new%3A/Users/me/a%20b/x.py");
    f.restore();
    forgetDraftVersion(key);
  });
});

describe("fetchChatDraft", () => {
  test("answers the record the store holds, and remembers its version", async () => {
    const key = "new:/Users/me/seed";
    forgetDraftVersion(key);
    const f = serve(() => ({ json: { chat: { [key]: chatRecord("words", 6) }, task: {} } }));
    expect((await fetchChatDraft(key))?.text).toBe("words");
    expect(draftVersion(key)).toBe(6);
    // A KEY WITH NOTHING UNDER IT IS `null` — the contract's "no record", and
    // the instruction a reader adopts by emptying its box.
    expect(await fetchChatDraft("new:/Users/me/nothing")).toBeNull();
    f.restore();
    forgetDraftVersion(key);
  });

  test("and a read that never answered is UNDEFINED, not null", async () => {
    // The third answer (Bugbot, PR #1180). Collapsed into `null` it read as
    // "the draft was deleted", and the change feed's adopt path cleared a
    // composer on a network blip. Still not a throw: this is awaited inside a
    // mount effect, where a rejection costs the mount.
    const real = globalThis.fetch;
    globalThis.fetch = (() => Promise.reject(new Error("offline"))) as unknown as typeof fetch;
    expect(await fetchChatDraft("new:/Users/me/offline")).toBeUndefined();
    expect(await fetchDrafts()).toBeNull();
    globalThis.fetch = real;
  });
});

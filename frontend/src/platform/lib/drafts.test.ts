// `useAutosave`'s `settle()` — the fix for the composer-drafts resurrection
// race (Akshil, 2026-09-11): `reset()`/`stop()` can cancel the PENDING debounce
// timer, but nothing short of the network can cancel a `fetch` already sent,
// so Send/Discard/Schedule need a way to wait that one write out before firing
// the delete that would otherwise land BEFORE it and get resurrected.
//
// Driven through the real hook — react-test-renderer, no DOM, the same tool
// JobRow.test.tsx and apps/explorer/listing/hook-harness.ts both use — because
// what matters is a SEQUENCE (write starts, write is still running, write
// resolves, `settle()` resolves only then) that grepping the source cannot
// show. The harness below is a small reimplementation of hook-harness.ts's own
// `renderHook`/`Deferred`, not an import of it: `platform/` may not import
// `apps/` (scripts/check-boundaries; see this module's own header for why).
//
// Every test drives the write through `flush()` rather than waiting out the
// real debounce timer: `bun test` runs every suite in one process, and a real
// `setTimeout` left pending past a test's own assertions is exactly the kind
// of leftover that lands during whichever OTHER file happens to be running
// when it fires — this file's first draft did that (a 30ms real wait per
// test) and it was enough to shift a completely unrelated suite's mocked
// `fetch` count elsewhere in the same run. `flush()` dispatches synchronously,
// so nothing here ever waits on the clock.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";
import {
  chatDraftKey,
  newChatFile,
  rekeyChatDraft,
  saveTaskDraft,
  useAutosave,
  type Autosave,
  type AutosaveOptions,
  type DraftWriteOptions,
  type TaskDraftForm,
} from "@platform/lib/drafts";

// `useAutosave`'s unload effect reaches for `window`/`document` — real
// globals in a browser, absent in bun's DOM-less test runtime. Neither is
// touched at drafts.ts's MODULE scope (only inside the hook's own effects,
// which run after this file's synchronous top level), so installing the shim
// here — rather than via the dynamic-import dance router.test.ts needs — is
// enough.
installDomShim();

/** A promise this test resolves by hand, standing in for a PUT in flight. */
function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/** Mount `useAutosave` and expose its latest handle. */
function renderAutosave<T>(
  value: T,
  save: (value: T, opts: DraftWriteOptions) => unknown,
  options?: AutosaveOptions,
): { current: () => Autosave<T>; rerender: (next: T) => void; unmount: () => void } {
  let latest!: Autosave<T>;
  let renderer!: ReactTestRenderer;
  const Probe = (props: { value: T }): null => {
    latest = useAutosave(props.value, save, options);
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

describe("useAutosave().settle()", () => {
  test("resolves at once when nothing has ever been written", async () => {
    // Discard on a card nobody touched: no write was ever dispatched, so
    // there is nothing to wait out.
    const box = renderAutosave({ n: 0 }, () => true);
    let settled = false;
    await box.current()
      .settle()
      .then(() => {
        settled = true;
      });
    expect(settled).toBe(true);
    box.unmount();
  });

  test("waits for a write already in flight before resolving", async () => {
    const calls: string[] = [];
    const put = deferred<boolean>();
    const box = renderAutosave({ n: 0 }, (value) => {
      calls.push(JSON.stringify(value));
      return put.promise;
    });

    box.rerender({ n: 1 });
    box.current().flush();
    expect(calls).toEqual(['{"n":1}']);

    let settled = false;
    const settling = box.current()
      .settle()
      .then(() => {
        settled = true;
      });
    // The PUT has not answered yet — settle must not have resolved either.
    await Promise.resolve();
    await Promise.resolve();
    expect(settled).toBe(false);

    put.resolve(true);
    await settling;
    expect(settled).toBe(true);
    box.unmount();
  });

  test(
    "reset() forgets the pending write's bookkeeping but settle() still " +
      "waits for a write already dispatched",
    async () => {
      const put = deferred<boolean>();
      const box = renderAutosave({ n: 0 }, () => put.promise);

      box.rerender({ n: 1 });
      // The composer's send: dispatch (standing in for a debounced write that
      // already went out), then `reset` to the value the box is about to
      // hold. `reset` cannot reach back and cancel the fetch above.
      box.current().flush();
      box.current().reset({ n: 1 });

      let settled = false;
      const settling = box.current()
        .settle()
        .then(() => {
          settled = true;
        });
      await Promise.resolve();
      await Promise.resolve();
      expect(settled).toBe(false);

      put.resolve(false);
      await settling;
      expect(settled).toBe(true);
      box.unmount();
    },
  );

  test(
    "stop() disarms future writes but settle() still waits for one already " +
      "running",
    async () => {
      const put = deferred<boolean>();
      const calls: string[] = [];
      const box = renderAutosave({ n: 0 }, (value) => {
        calls.push(JSON.stringify(value));
        return put.promise;
      });

      box.rerender({ n: 1 });
      box.current().flush();
      expect(calls).toEqual(['{"n":1}']);

      box.current().stop();
      // A later change, even flushed by hand, must NOT queue a new write —
      // stop is permanent for this mount.
      box.rerender({ n: 2 });
      box.current().flush();
      expect(calls).toEqual(['{"n":1}']);

      let settled = false;
      const settling = box.current()
        .settle()
        .then(() => {
          settled = true;
        });
      await Promise.resolve();
      expect(settled).toBe(false);
      put.resolve(true);
      await settling;
      expect(settled).toBe(true);
      box.unmount();
    },
  );

  test("a save that rejects still lets settle() resolve, not hang or throw", async () => {
    const box = renderAutosave({ n: 0 }, () => Promise.reject(new Error("network")));
    box.rerender({ n: 1 });
    box.current().flush();

    let settled = false;
    // If `settle()` propagated the rejection this `await` would throw and
    // fail the test — same contract every write in this module already keeps
    // (see drafts.ts's own header).
    await box.current()
      .settle()
      .then(() => {
        settled = true;
      });
    expect(settled).toBe(true);
    box.unmount();
  });
});

// ---- the opening value, when it arrived already typed -------------------------
// design.md, Round 2: the Schedule hop hands the task form the sentence the
// composer was holding, so the card mounts on words that are already a draft.
// `writeInitial` is what lets the first write happen with nobody having typed.

describe("useAutosave({ writeInitial })", () => {
  test("off by default: mounting alone never writes", () => {
    // "Nothing minted for an untouched modal" is made of exactly this — the
    // form's own initial state is not something the user typed.
    let writes = 0;
    const box = renderAutosave({ n: 0 }, () => {
      writes += 1;
      return true;
    });
    box.current().flush();
    expect(writes).toBe(0);
    box.unmount();
  });

  test("on: the mount value counts as unwritten, so the first flush writes it", () => {
    const seen: unknown[] = [];
    const box = renderAutosave({ text: "from the chat" }, (v) => {
      seen.push(v);
      return true;
    }, { writeInitial: true });
    box.current().flush();
    expect(seen).toEqual([{ text: "from the chat" }]);
    box.unmount();
  });

  test("…and only once — the second flush has nothing new to say", () => {
    let writes = 0;
    const box = renderAutosave({ text: "from the chat" }, () => {
      writes += 1;
      return true;
    }, { writeInitial: true });
    box.current().flush();
    box.current().flush();
    expect(writes).toBe(1);
    box.unmount();
  });

  test("a null opening value writes nothing of substance and still settles", async () => {
    // An Edit hands `null` here. The hook has no opinion about the value; the
    // caller's own `save` is what refuses it.
    const seen: unknown[] = [];
    const box = renderAutosave<{ n: number } | null>(null, (v) => {
      seen.push(v);
      return true;
    }, { writeInitial: true });
    box.current().flush();
    expect(seen).toEqual([null]);
    await box.current().settle();
    box.unmount();
  });
});

// ---- the wire: a draft that MOVES rather than duplicating ---------------------

const FORM: TaskDraftForm = {
  title: "", description: "half a thought", target: "~/news",
  when: null, repeat: null, model: "", effort: "", permission: "",
  attachments: [], new_task_each_run: null,
};

/** Swap `fetch` for a recorder, and put the real one back afterwards. */
function recordFetch(): { calls: { url: string; init: RequestInit }[]; restore: () => void } {
  const calls: { url: string; init: RequestInit }[] = [];
  const real = globalThis.fetch;
  globalThis.fetch = ((url: string, init: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve({ ok: true } as Response);
  }) as typeof fetch;
  return { calls, restore: () => { globalThis.fetch = real; } };
}

describe("saveTaskDraft's from_chat_key", () => {
  test("is absent unless the caller names one", async () => {
    const f = recordFetch();
    await saveTaskDraft("d1", FORM);
    expect(JSON.parse(String(f.calls[0].init.body))).not.toHaveProperty("from_chat_key");
    f.restore();
  });

  test("rides the body when it does, beside the form", async () => {
    // The server deletes that chat draft as it stores this one, so the sentence
    // is in exactly one place at every instant (design.md, Round 2).
    const f = recordFetch();
    await saveTaskDraft("d1", FORM, undefined, "new:/Users/me/news");
    const body = JSON.parse(String(f.calls[0].init.body));
    expect(body.from_chat_key).toBe("new:/Users/me/news");
    expect(body.description).toBe("half a thought");
    f.restore();
  });
});

describe("rekeyChatDraft", () => {
  test("moves the number from the `new:<file>` key onto the session", async () => {
    const f = recordFetch();
    await rekeyChatDraft(chatDraftKey(null, "/Users/me/news"), "sess-9");
    expect(f.calls[0].url).toBe("/api/drafts/chat/rekey");
    expect(f.calls[0].init.method).toBe("POST");
    expect(JSON.parse(String(f.calls[0].init.body)))
      .toEqual({ from: "new:/Users/me/news", to: "sess-9" });
    f.restore();
  });

  test("says nothing at all when there is nothing to move", async () => {
    // The server answers 400 on a bad shape or `from === to`; a request that
    // can only be refused is one not worth making.
    const f = recordFetch();
    await rekeyChatDraft("", "sess-9");
    await rekeyChatDraft("new:/x", "");
    await rekeyChatDraft("sess-9", "sess-9");
    expect(f.calls).toEqual([]);
    f.restore();
  });
});

// ---- the number follows the session the chat turns out to be -----------------
// design.md, Round 2: "Every draft has a TASK number." A brand-new chat keys its
// draft `new:<file>` and is given one under that key; the first send creates the
// session, and the composer keys on the session id from the next render. Without
// the rekey, the number — and the row wearing it — is stranded on a key nothing
// reads again. Source reads: the whole claim is about WHEN this fires and how
// many times, which a mounted chat cannot be asked without a whole run.

describe("the chat rekeys its draft when it learns its session", () => {
  const chat = () =>
    readFileSync(join(import.meta.dir, "../../apps/claude/ClaudeChat.tsx"), "utf8");
  const effect = () => {
    const s = chat();
    const at = s.indexOf("const rekeyed = useRef(false);");
    return s.slice(at, s.indexOf("}, [file, state.sessionId]);", at));
  };

  test("posts the move once, from the `new:<file>` key onto the session", () => {
    expect(effect()).toContain("void rekeyChatDraft(chatDraftKey(null, file), id);");
    // The same key the composer autosaves under — one function, so the two
    // halves cannot spell the path differently.
    expect(chat()).toContain('import { chatDraftKey, rekeyChatDraft } from "@platform/lib/drafts";');
  });

  test("only for a chat that STARTED without a session", () => {
    // A chat opened ON a session (a recent row, a deep link, the Tasks page)
    // never had a `new:<file>` key; renaming one would at best be a no-op and at
    // worst claim a key belonging to a different unsent chat in the same folder.
    const e = effect();
    expect(e).toContain("if (startedWithoutSession.current === null) startedWithoutSession.current = !id;");
    expect(e).toContain("if (!startedWithoutSession.current || rekeyed.current || !id) return;");
    expect(e).toContain("rekeyed.current = true;");
  });

  test("fire and forget, like every other write in this module", () => {
    // A refusal costs the number's continuity and nothing the reader is doing —
    // and the draft it renames is one the send is about to delete anyway.
    expect(effect()).toContain("void rekeyChatDraft(");
    expect(effect()).not.toContain("await ");
  });
});

describe("the two shapes of a chat key", () => {
  test("reads the file back out of a `new:` key, and nothing out of a session", () => {
    // The way BACK to a never-sent chat is built out of this string — a reopened
    // task draft's "Back to chat" and the draft row's own href both have to land
    // on the same `file` the composer keys on, or the chat that opens seeds from
    // a key nothing wrote.
    expect(newChatFile(chatDraftKey(null, "/Users/me/news"))).toBe("/Users/me/news");
    expect(newChatFile("new:/a/b")).toBe("/a/b");
    expect(newChatFile("sess-9")).toBe("");
    expect(newChatFile("")).toBe("");
    // Nothing is trimmed or normalised — chatDraftKey's rule, held here too.
    expect(newChatFile("new:/Users/me/news/")).toBe("/Users/me/news/");
  });
});

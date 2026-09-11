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
  deleteChatDraft,
  fetchChatDraft,
  newChatFile,
  rekeyChatDraft,
  saveChatDraft,
  saveTaskDraft,
  useAutosave,
  type Autosave,
  type AutosaveOptions,
  type ChatDraft,
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

// ---- the unmount flush that follows the first send --------------------------
//
// THE BUG (Bugbot, PR #1118, 2026-09-11). The first send from a session-less
// chat calls `reset({ text: "", attachments: [] })` and then, one tick later,
// gains a session — which remounts the whole chat. The composer going away runs
// the unmount flush, and that flush reads the value REF, which at that instant
// still holds the render before the clear: the sentence that was just sent. It
// differed from the empty value `reset` had just recorded, so it was written —
// a PUT with content, which un-spends the key and puts the message back on the
// server under it. `reset` has to move the value too, not only the bookkeeping.

describe("the unmount flush after a send", () => {
  test("reset() leaves it nothing to write", () => {
    const calls: string[] = [];
    const box = renderAutosave({ text: "" }, (value) => {
      calls.push(JSON.stringify(value));
      return true;
    });
    // A sentence typed, then sent: `reset` is told what the box is ABOUT to
    // hold, because the state write that empties it has not landed yet.
    box.rerender({ text: "ship the release notes" });
    box.current().reset({ text: "" });
    // The session arrives and the chat remounts.
    box.unmount();
    expect(calls).toEqual([]);
  });

  test("…and so does a flush by hand in the same tick", () => {
    const calls: string[] = [];
    const box = renderAutosave({ text: "" }, (value) => {
      calls.push(JSON.stringify(value));
      return true;
    });
    box.rerender({ text: "ship the release notes" });
    box.current().reset({ text: "" });
    box.current().flush();
    expect(calls).toEqual([]);
    box.unmount();
    expect(calls).toEqual([]);
  });

  test("stop() disarms it outright, words in the box or not", () => {
    // Schedule's half: the server deletes the draft as it creates the task, so
    // the teardown must not write one back either.
    const calls: string[] = [];
    const box = renderAutosave({ text: "" }, (value) => {
      calls.push(JSON.stringify(value));
      return true;
    });
    box.rerender({ text: "half a thought" });
    box.current().stop();
    box.unmount();
    expect(calls).toEqual([]);
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
  when: null, repeat: null, custom_rule: null, model: "", effort: "",
  permission: "", attachments: [], new_task_each_run: null,
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

// ---- a spent chat key never hands its draft back -----------------------------
//
// THE BUG (Bugbot, PR #1118). The first send from a session-less chat deletes
// the draft under `new:<file>` AND gives the landing a session, which remounts
// the composer. The fresh mount seeds itself from `fetchChatDraft`, and that GET
// can overtake the DELETE still in flight: the answer is the sentence that was
// just sent, put back into the box the send had emptied — and the rekey that
// follows walks it onto the session, so the message the reader sent is sitting
// on their own task row as an unsent draft.
//
// Nothing inside one mount can close that window (`reset`/`stop`/`settle` all
// belong to the component being thrown away, and the seed runs in the NEXT one),
// so the fact lives at module scope. These drive the module's own functions,
// which is where it lives; every test uses a key of its own, because the set is
// module state and deliberately outlives any one of them.

describe("a spent chat key reads back as empty", () => {
  const held = (text: string): ChatDraft => ({ text, attachments: [], updated_at: 1 });

  /** `fetch` answering `GET /api/drafts` out of `chat`, and recording every call
   *  — the writes here never reach a server, which is the point: what is being
   *  asserted is what the module answers WHILE the DELETE is still in the air. */
  function serve(chat: Record<string, ChatDraft>) {
    const calls: string[] = [];
    const real = globalThis.fetch;
    globalThis.fetch = ((url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ chat, task: {} }),
      } as unknown as Response);
    }) as typeof fetch;
    return { calls, restore: () => { globalThis.fetch = real; } };
  }

  test("the remount's seed gets nothing, though the server still holds the words", async () => {
    const key = "new:/Users/me/sent";
    const f = serve({ [key]: held("ship the release notes") });
    // Before the send: the draft is real and the composer would restore it.
    expect(await fetchChatDraft(key)).not.toBeNull();
    // The send. Deliberately NOT awaited — the DELETE being in flight is the
    // whole of the race.
    void deleteChatDraft(key);
    expect(await fetchChatDraft(key)).toBeNull();
    f.restore();
  });

  test("and answers without a request at all", async () => {
    const key = "new:/Users/me/sent-quietly";
    const f = serve({ [key]: held("ship it") });
    void deleteChatDraft(key);
    const before = f.calls.length;
    expect(await fetchChatDraft(key)).toBeNull();
    expect(f.calls.length).toBe(before);
    f.restore();
  });

  test("typing into the key again brings it back", async () => {
    // Spent is not dead: a reader who starts a second unsent message in the same
    // folder has a draft again, and the next composer to open there must see it.
    const key = "new:/Users/me/typed-on";
    const f = serve({ [key]: held("and one more thing") });
    void deleteChatDraft(key);
    expect(await fetchChatDraft(key)).toBeNull();
    await saveChatDraft(key, "and one more thing");
    expect(await fetchChatDraft(key)).not.toBeNull();
    f.restore();
  });

  test("an attachment alone is content enough", async () => {
    const key = "new:/Users/me/dropped-a-file";
    const f = serve({ [key]: held("") });
    void deleteChatDraft(key);
    await saveChatDraft(key, "", [{ path: "/tmp/a.png", name: "a.png", kind: "image" }]);
    expect(await fetchChatDraft(key)).not.toBeNull();
    f.restore();
  });

  test("an empty write does NOT un-spend it — an empty write is itself a delete", async () => {
    // The composer's autosave fires on the pause after the send cleared the box.
    // Treating that as "there are words here again" would re-open the window.
    const key = "new:/Users/me/cleared";
    const f = serve({ [key]: held("ghost") });
    void deleteChatDraft(key);
    await saveChatDraft(key, "   ");
    expect(await fetchChatDraft(key)).toBeNull();
    f.restore();
  });

  test("one key's spending says nothing about any other", async () => {
    const sent = "new:/Users/me/one";
    const other = "new:/Users/me/two";
    const f = serve({ [sent]: held("sent"), [other]: held("still unsent") });
    void deleteChatDraft(sent);
    expect(await fetchChatDraft(sent)).toBeNull();
    expect((await fetchChatDraft(other))?.text).toBe("still unsent");
    f.restore();
  });
});

// ---- the rekey cannot pass the send's delete ---------------------------------
//
// THE BUG (Bugbot, PR #1118, 2026-09-11). `spent` covers `new:<file>` and only
// that key. The first send fires `DELETE new:<file>` and, on learning the
// session, `POST /api/drafts/chat/rekey` — two requests in no order at all. If
// the rekey is served first the record is still there, so the route copies it
// onto the session id, and the composer that just remounted seeds from the
// SESSION key, which nothing had marked spent. Same resurrection, one key over.

describe("the rekey cannot pass the send's delete", () => {
  const held = (text: string): ChatDraft => ({ text, attachments: [], updated_at: 1 });

  /** `fetch` that records every call and answers `GET /api/drafts` out of
   *  `chat` — but holds every request until `release()`, which is how one is
   *  kept "in flight" without a clock. After the release the gate is open:
   *  later requests answer at once, so a test can order what it cares about
   *  and then let the rest run. */
  function gate(chat: Record<string, ChatDraft> = {}) {
    const calls: string[] = [];
    const waiting: (() => void)[] = [];
    let holding = true;
    const answer = () => ({ ok: true, json: () => Promise.resolve({ chat, task: {} }) }) as unknown as Response;
    const real = globalThis.fetch;
    globalThis.fetch = ((url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (!holding) return Promise.resolve(answer());
      return new Promise<Response>((resolve) => {
        waiting.push(() => resolve(answer()));
      });
    }) as typeof fetch;
    return {
      calls,
      release: () => {
        holding = false;
        for (const unblock of waiting.splice(0)) unblock();
      },
      restore: () => {
        globalThis.fetch = real;
      },
    };
  }

  const posts = (calls: string[]) => calls.filter((c) => c.startsWith("POST /api/drafts/chat/rekey"));

  test("it waits for a DELETE still in flight before posting the move", async () => {
    const g = gate();
    const from = "new:/Users/me/moving";
    // The send. Not awaited — the DELETE being in the air is the whole race.
    void deleteChatDraft(from);
    const moved = rekeyChatDraft(from, "sess-moving");
    await Promise.resolve();
    await Promise.resolve();
    // Nothing posted while the delete is unanswered: the route would have found
    // the record and copied the sent words onto the session.
    expect(posts(g.calls)).toEqual([]);

    g.release();
    await moved;
    expect(posts(g.calls).length).toBe(1);
    g.restore();
  });

  test("a spent `from` hands its spent-ness to `to`", async () => {
    // Belt and braces for the copy that happens anyway (another tab, a server
    // that already moved it): whatever is under the new key, it is not restored
    // into the box the send just emptied.
    const from = "new:/Users/me/handed";
    const to = "sess-handed";
    const g = gate({ [to]: held("ship the release notes") });
    void deleteChatDraft(from);
    const moved = rekeyChatDraft(from, to);
    g.release();
    await moved;
    expect(await fetchChatDraft(to)).toBeNull();
    g.restore();
  });

  test("…until the reader types under the new key, which brings it back", async () => {
    const from = "new:/Users/me/typed-on-after";
    const to = "sess-typed-on-after";
    const g = gate({ [to]: held("a second thought") });
    void deleteChatDraft(from);
    const moved = rekeyChatDraft(from, to);
    g.release();
    await moved;
    await saveChatDraft(to, "a second thought");
    expect((await fetchChatDraft(to))?.text).toBe("a second thought");
    g.restore();
  });

  test("a `from` nobody spent says nothing about `to`", async () => {
    // The ordinary rekey: a chat that learned its session without a send. Its
    // draft is a real unsent draft and must survive the move.
    const from = "new:/Users/me/never-sent";
    const to = "sess-never-sent";
    const g = gate({ [to]: held("still unsent") });
    const moved = rekeyChatDraft(from, to);
    g.release();
    await moved;
    expect((await fetchChatDraft(to))?.text).toBe("still unsent");
    g.restore();
  });
});

// ---- a read already in the air when the send lands ---------------------------

describe("fetchChatDraft re-checks spent after the answer", () => {
  const held = (text: string): ChatDraft => ({ text, attachments: [], updated_at: 1 });

  test("a GET dispatched before the send still answers null", async () => {
    // THE BUG (Bugbot, PR #1118, 2026-09-11): the guard ran before the request
    // only. A seed that passed it while the key was still live answers out of a
    // snapshot taken before the DELETE landed — and hands the composer the
    // sentence that was sent in the meantime.
    const key = "new:/Users/me/mid-flight";
    const waiting: (() => void)[] = [];
    const real = globalThis.fetch;
    globalThis.fetch = (() =>
      new Promise<Response>((resolve) => {
        waiting.push(() =>
          resolve({
            ok: true,
            json: () => Promise.resolve({ chat: { [key]: held("ship the release notes") }, task: {} }),
          } as unknown as Response),
        );
      })) as unknown as typeof fetch;

    // The remount's seed goes out while the key is still live…
    const reading = fetchChatDraft(key);
    await Promise.resolve();
    // …and the send marks it spent while that GET is still unanswered.
    void deleteChatDraft(key);
    for (const answer of waiting.splice(0)) answer();

    expect(await reading).toBeNull();
    globalThis.fetch = real;
  });
});

// ONE RECORD, VERSIONED — the drafts module's whole concurrency story (design
// "Drafts: one record, one key, versioned, pushed", §2).
//
// What this file used to test was a coordination layer: a `spent` set, an
// in-flight map, and a `stop`/`settle`/`resume` protocol, all of which existed
// to order one document's writes against its own reads. A version orders them
// against every document at once — the other tab included — so the tests here
// are about the version: that it is stated, that it is taken from every answer
// the server gives, and that a refusal is resolved the way the design says.
//
// Driven through the real hook — react-test-renderer, no DOM, the same tool
// JobRow.test.tsx and apps/explorer/listing/hook-harness.ts both use — because
// what matters is a SEQUENCE that grepping the source cannot show. The harness
// below is a small reimplementation of hook-harness.ts's own `renderHook`, not
// an import of it: `platform/` may not import `apps/` (scripts/check-boundaries).
//
// Every test drives the write through `flush()` rather than waiting out the
// real debounce timer: `bun test` runs every suite in one process, and a real
// `setTimeout` left pending past a test's own assertions is exactly the kind of
// leftover that lands during whichever OTHER file happens to be running when it
// fires. `flush()` dispatches synchronously, so nothing here waits on a clock.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";
import {
  chatDraftKey,
  deleteChatDraft,
  draftVersion,
  fetchChatDraft,
  fetchDrafts,
  forgetDraftVersion,
  isChatDraftKey,
  newChatFile,
  saveChatDraft,
  saveTaskDraft,
  taskDraftKey,
  useAutosave,
  type Autosave,
  type AutosaveOptions,
  type DraftWriteOptions,
} from "@platform/lib/drafts";

// `useAutosave`'s unload effect reaches for `window`/`document` — real globals
// in a browser, absent in bun's DOM-less test runtime. Neither is touched at
// drafts.ts's MODULE scope (only inside the hook's own effects, which run after
// this file's synchronous top level), so installing the shim here is enough.
installDomShim();

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

describe("a refused write", () => {
  const conflicted = (key: string, version: number, text: string) => ({
    status: 409,
    json: { error: "version", record: chatRecord(text, version), version, key },
  });

  test("hands the server's record back and takes its version", async () => {
    const key = "new:/Users/me/clash";
    forgetDraftVersion(key);
    const f = serve(() => conflicted(key, 12, "theirs"));
    const out = await saveChatDraft(key, "mine");
    expect(out.ok).toBe(false);
    expect((out.conflict as { text: string } | null)?.text).toBe("theirs");
    // Whatever the caller decides, the NEXT write has to state a version that
    // exists or it is refused for ever.
    expect(draftVersion(key)).toBe(12);
    f.restore();
    forgetDraftVersion(key);
  });

  test("adopts, when the editor is not focused", async () => {
    const key = "new:/Users/me/adopt";
    forgetDraftVersion(key);
    const f = serve(() => conflicted(key, 3, "theirs"));
    const adopted: unknown[] = [];
    let kept = 0;
    const box = renderAutosave(
      { text: "mine" },
      (value, opts) => saveChatDraft(key, value.text, [], opts),
      {
        conflict: {
          focused: () => false,
          localText: () => "mine",
          adopt: (record) => adopted.push(record),
          onKept: () => { kept += 1; },
        },
      },
    );
    box.rerender({ text: "mine typed" });
    await act(async () => {
      box.current().flush();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect((adopted[0] as { text: string }).text).toBe("theirs");
    // One write, not two: a conceded conflict is not retried.
    expect(f.calls.length).toBe(1);
    expect(kept).toBe(0);
    box.unmount();
    f.restore();
    forgetDraftVersion(key);
  });

  test("…and when the reader is focused but has typed nothing since the save", async () => {
    const key = "new:/Users/me/adopt-idle";
    forgetDraftVersion(key);
    const f = serve(() => conflicted(key, 3, "theirs"));
    const adopted: unknown[] = [];
    // `localText` never moves, so the words on screen ARE the words last
    // written: a caret sitting in an unchanged box is not somebody mid-sentence.
    const box = renderAutosave(
      { text: "mine" },
      (value, opts) => saveChatDraft(key, value.text, [], opts),
      {
        conflict: {
          focused: () => true,
          localText: () => "settled",
          adopt: (record) => adopted.push(record),
        },
      },
    );
    box.rerender({ text: "mine typed" });
    await act(async () => {
      box.current().flush();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(adopted.length).toBe(1);
    expect(f.calls.length).toBe(1);
    box.unmount();
    f.restore();
    forgetDraftVersion(key);
  });

  test("keeps the local text and retries ONCE when the reader is mid-sentence", async () => {
    const key = "new:/Users/me/keep";
    forgetDraftVersion(key);
    // Refused every time: the point is that the retry stops at one. Two tabs
    // both typing would otherwise write past each other for as long as they go
    // on.
    const f = serve(() => conflicted(key, 5, "theirs"));
    const adopted: unknown[] = [];
    let kept = 0;
    // "Mid-sentence" is a question about the ROUND TRIP, not about the caret:
    // the box said one thing when the save went out and says another by the time
    // the refusal comes back, so the reader typed while it was in the air. A
    // caret sitting in an unchanged box is not somebody whose words are at risk,
    // which is the case the test above covers.
    let typed = "half a th";
    const box = renderAutosave(
      { text: "mine" },
      (value, opts) => {
        const out = saveChatDraft(key, value.text, [], opts);
        typed += "ought";
        return out;
      },
      {
        conflict: {
          focused: () => true,
          localText: () => typed,
          adopt: (record) => adopted.push(record),
          onKept: () => { kept += 1; },
        },
      },
    );
    box.rerender({ text: "mine typed" });
    await act(async () => {
      box.current().flush();
      for (let i = 0; i < 8; i += 1) await Promise.resolve();
    });
    expect(adopted).toEqual([]);
    expect(kept).toBe(1);
    expect(f.calls.length).toBe(2);
    // …and the retry states the version it has just been told about, which is
    // the whole reason it can be expected to land.
    expect(f.calls[1]!.ifMatch).toBe("5");
    box.unmount();
    f.restore();
    forgetDraftVersion(key);
  });
});

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

// ---- the hook's own promises ---------------------------------------------------

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
});

describe("nothing is minted by merely opening an editor", () => {
  test("a mount writes nothing, however full the form it mounts on", () => {
    // design §4: "no PUT until title or text non-empty. Open+close empty leaves
    // nothing." The hook's half of that is the seed — `written` starts as the
    // opening value — and there is no `writeInitial` escape hatch any more,
    // because a Schedule hop no longer arrives holding words that exist nowhere
    // else.
    const calls: string[] = [];
    const box = renderAutosave({ title: "Ship it", text: "already here" }, (value) => {
      calls.push(JSON.stringify(value));
      return true;
    });
    box.current().flush();
    box.unmount();
    expect(calls).toEqual([]);
  });

  test("…and the hook exposes no way to override it", async () => {
    // Asserted on the EXPORTS rather than on the source text: the module's prose
    // still names what it used to do and why, which is the record of the
    // decision, not a survival of the code.
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
    // …and the hook's handle is three calls, not five: `settle` came back for
    // the one thing a version cannot order — two requests already on the wire
    // stating the same `If-Match` (Bugbot, PR #1180).
    const box = renderAutosave({ n: 0 }, () => true);
    expect(Object.keys(box.current()).sort()).toEqual(["flush", "reset", "settle"]);
    box.unmount();
  });
});

// ---- settle: the one ordering a version cannot do -----------------------------

describe("settle", () => {
  // THE HOLE A VERSION DOES NOT FILL (Bugbot, PR #1180). Versioning refuses a
  // LATER write that states a STALE number. It says nothing about two requests
  // that were BOTH dispatched against version 7 — the autosave's PUT and the
  // send's DELETE — because neither is late when it leaves; whichever the
  // server takes second simply wins. Taken second, the PUT recreated the message
  // the user had just sent as a live draft. So the send waits for what is
  // already on the wire and lets its own answer supply the version.
  const holdable = (key: string, version: number) => {
    let release!: () => void;
    const held = new Promise<void>((r) => {
      release = r;
    });
    const calls: { method: string; ifMatch: string | null }[] = [];
    const real = globalThis.fetch;
    globalThis.fetch = ((_url: string, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      const method = init?.method ?? "GET";
      calls.push({ method, ifMatch: headers["If-Match"] ?? null });
      const answer = {
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve(
            method === "DELETE"
              ? { ok: true, key, removed: true }
              : { ok: true, key, draft: chatRecord("half a line", version) },
          ),
      } as unknown as Response;
      // Only the PUT is held: the DELETE has to be free to answer, or the test
      // could not tell "waited" from "never sent".
      return method === "PUT" ? held.then(() => answer) : Promise.resolve(answer);
    }) as typeof fetch;
    return { calls, release, restore: () => { globalThis.fetch = real; } };
  };

  test("a send's DELETE waits for the PUT already out, and states the version it made", async () => {
    const key = "new:/Users/me/settle";
    forgetDraftVersion(key);
    const f = holdable(key, 8);
    const box = renderAutosave({ text: "" }, (value: { text: string }, opts) =>
      saveChatDraft(key, value.text, [], opts),
    );
    box.rerender({ text: "half a line" });
    act(() => box.current().flush());
    // The PUT is on the wire and nothing has answered it. This is the moment the
    // send happens.
    expect(f.calls).toEqual([{ method: "PUT", ifMatch: null }]);
    const handle = box.current();
    handle.reset({ text: "" });
    let deleted = false;
    const spent = handle.settle().then(async () => {
      await deleteChatDraft(key);
      deleted = true;
    });
    // …and it has NOT gone out: the old code deleted here, against the same
    // (absent) version the PUT was carrying.
    await Promise.resolve();
    await Promise.resolve();
    expect(f.calls.map((c) => c.method)).toEqual(["PUT"]);
    expect(deleted).toBe(false);
    f.release();
    await spent;
    // Now — and stating 8, the version this client learnt FROM the PUT it waited
    // for, so the record that is deleted is the one the PUT had just written.
    expect(f.calls.map((c) => c.method)).toEqual(["PUT", "DELETE"]);
    expect(f.calls[1]!.ifMatch).toBe("8");
    expect(deleted).toBe(true);
    box.unmount();
    f.restore();
    forgetDraftVersion(key);
  });

  /**
   * A FAKE SERVER THAT ACTUALLY KEEPS A VERSION, and answers each request only
   * when this test says so — which is the only way to write down an
   * INTERLEAVING (a PUT out, a second PUT out, then a DELETE) rather than a
   * sequence.
   */
  const versioned = (key: string) => {
    const state = { version: 0, text: "", gone: true };
    const calls: { method: string; ifMatch: string | null; text?: string }[] = [];
    const queued: (() => void)[] = [];
    const real = globalThis.fetch;
    globalThis.fetch = ((_url: string, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      const method = init?.method ?? "GET";
      const body = init?.body
        ? (JSON.parse(String(init.body)) as { text?: string })
        : undefined;
      calls.push({ method, ifMatch: headers["If-Match"] ?? null, ...(body ? { text: body.text } : {}) });
      return new Promise<Response>((resolve) => {
        queued.push(() => {
          const now = state.gone ? 0 : state.version;
          const want = headers["If-Match"];
          const answer = (status: number, json: unknown) =>
            resolve({ ok: status < 300, status, json: () => Promise.resolve(json) } as unknown as Response);
          if (want !== undefined && Number(want) !== now) {
            answer(409, {
              error: "version",
              key,
              record: state.gone ? null : chatRecord(state.text, state.version),
              version: now,
            });
            return;
          }
          if (method === "DELETE") {
            state.gone = true;
            answer(200, { ok: true, key, removed: true });
            return;
          }
          state.gone = false;
          state.version = now + 1;
          state.text = body?.text ?? "";
          answer(200, { ok: true, key, draft: chatRecord(state.text, state.version) });
        });
      });
    }) as typeof fetch;
    return {
      calls,
      state,
      release: async (at: number) => {
        queued[at]!();
        await act(async () => {
          await Promise.resolve();
          await Promise.resolve();
        });
      },
      restore: () => { globalThis.fetch = real; },
    };
  };

  test("a follow-up typed through the wait is not what the send deletes", async () => {
    // THE SECOND ROUND OF THE SAME BUG (Bugbot, PR #1180). Waiting is not
    // enough on its own: the reader can type a FOLLOW-UP while the send's PUT
    // is still out, and a DELETE that reads its version at fire time then
    // states the FOLLOW-UP's and spends words nobody sent.
    const key = "new:/Users/me/follow-up";
    forgetDraftVersion(key);
    const f = versioned(key);
    let local = "";
    const box = renderAutosave(
      { text: "" },
      (value: { text: string }, opts) => saveChatDraft(key, value.text, [], opts),
      {
        conflict: {
          // The reader IS in this box — they are typing the follow-up — so a
          // refusal keeps their words and retries once against the version it
          // has just learnt.
          focused: () => true,
          localText: () => local,
          adopt: () => {},
        },
      },
    );
    // A draft already on the server, so every write below is conditional.
    local = "the sent line";
    box.rerender({ text: local });
    act(() => box.current().flush());
    await f.release(0);
    expect(draftVersion(key)).toBe(1);

    // …and now the send, with that same line still in the box: a second write
    // goes out (the tray changed, say) and is still on the wire.
    local = "the sent line, and a comma";
    box.rerender({ text: local });
    act(() => box.current().flush());
    const handle = box.current();
    const held = draftVersion(key);
    const pending = handle.settle();
    handle.reset({ text: "" });

    // THE FOLLOW-UP, typed while that PUT is out. It states the version this
    // client knows (1), which the sent write is about to move past.
    local = "one more thing";
    box.rerender({ text: local });
    act(() => box.current().flush());
    expect(f.calls.map((c) => c.method)).toEqual(["PUT", "PUT", "PUT"]);
    expect(f.calls[2]!.ifMatch).toBe("1");

    // …and the reader keeps typing while that PUT is out, which is what makes
    // the refusal below a KEPT sentence rather than an adopted one.
    local = "one more thing —";

    let spent: { ok: boolean } | null = null;
    const send = pending.then(async (made) => {
      const at = made ?? held;
      spent = await deleteChatDraft(key, at === undefined ? undefined : { ifMatch: at });
    });
    // The sent write lands: version 2, and that is the number the send states.
    await f.release(1);
    expect(f.calls[3]!.method).toBe("DELETE");
    expect(f.calls[3]!.ifMatch).toBe("2");
    // The follow-up's own write is refused (it stated 1) and retries once
    // against the version it has just learnt…
    await f.release(2);
    expect(f.calls[4]).toEqual({ method: "PUT", ifMatch: "2", text: "one more thing" });
    // …which lands: the record now holds the follow-up, at version 3.
    await f.release(4);
    // AND THE DELETE IS REFUSED, because the record is no longer the one the
    // send was about. The follow-up survives on the server, and nothing told
    // this composer its key was gone.
    await f.release(3);
    await send;
    expect(spent!.ok).toBe(false);
    expect(f.state.gone).toBe(false);
    expect(f.state.text).toBe("one more thing");
    box.unmount();
    f.restore();
    forgetDraftVersion(key);
  });

  test("a write reset out from under speaks for nobody when it answers", async () => {
    // The other half of the send: `reset` disowns what is already out. Without
    // it the held PUT's 409 would resolve into `adopt` and put the sent sentence
    // back on screen — the same resurrection by the other road.
    const key = "new:/Users/me/disowned";
    forgetDraftVersion(key);
    let release!: () => void;
    const held = new Promise<void>((r) => { release = r; });
    const real = globalThis.fetch;
    globalThis.fetch = (() =>
      held.then(() => ({
        ok: false,
        status: 409,
        json: () => Promise.resolve({
          error: "version",
          record: chatRecord("somebody else's line", 5),
          version: 5,
          key,
        }),
      } as unknown as Response))) as unknown as typeof fetch;
    const adopted: unknown[] = [];
    const box = renderAutosave(
      { text: "" },
      (value: { text: string }, opts) => saveChatDraft(key, value.text, [], opts),
      {
        conflict: {
          focused: () => false,
          localText: () => "",
          adopt: (record) => adopted.push(record),
        },
      },
    );
    box.rerender({ text: "mine" });
    act(() => box.current().flush());
    const handle = box.current();
    handle.reset({ text: "" });
    release();
    await act(async () => {
      await handle.settle();
    });
    expect(adopted).toEqual([]);
    box.unmount();
    globalThis.fetch = real;
    forgetDraftVersion(key);
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
    // …and it spends it AFTER the write already on the wire has answered
    // (Bugbot, PR #1180): a PUT and a DELETE dispatched against the same
    // version are ordered by whoever answers first, and when that was the PUT
    // it put the sent message back as a live draft.
    const src = composer();
    expect(src).toContain("const key = draftKeyRef.current;");
    // The handle is taken BEFORE the reset — `settle` answers for what is on
    // the wire as of the call — and the DELETE names the version that write
    // made rather than reading one at fire time (Bugbot, second round: a
    // follow-up typed through the wait is the record a late read would spend).
    expect(src).toContain("const pending = autosaveRef.current.settle();");
    expect(src).toContain("const spent = made ?? held;");
    expect(src).toContain(
      "return deleteChatDraft(key, spent === undefined ? undefined : { ifMatch: spent });");
    expect(src).not.toContain("void deleteChatDraft(draftKeyRef.current);");
    expect(src).not.toContain("settle().then(() => deleteChatDraft(key))");
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

// The confirm in front of the Schedule button: which control the keyboard lands
// on when it opens, and the two gestures that close it that no portal library
// can see. Both were unimplemented STATED intent — `SchedConfirm`'s own header
// named the dismissal contract it did not have (B-06, B-29).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement, useCallback } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { SchedButton } = await import("./SchedButton");
const { SchedConfirm, SchedConfirmBody } = await import("./SchedConfirm");
const { draftSyncer, forgetDraftVersion, resetDraftSyncers, useAutosave } =
  await import("@platform/lib/drafts");
const { getPopupNotification, _resetNotificationsForTest } =
  await import("@platform/lib/notifications");
const { useDismissOnWindow } = await import("./useDismissOnWindow");

/** A REAL LISTENER REGISTRY on the shim's `window`, which otherwise no-ops. The
 *  gestures under test ARE window-level bindings, so a test that cannot fire
 *  them can only assert the hook compiled. Installed and removed around this
 *  file: `bun test` runs every suite in one process and this is a global. */
const winListeners: Record<string, ((ev: unknown) => void)[]> = {};
const win = window as unknown as {
  addEventListener(t: string, fn: (ev: unknown) => void): void;
  removeEventListener(t: string, fn: (ev: unknown) => void): void;
};
const realWin = { add: win.addEventListener, remove: win.removeEventListener };
win.addEventListener = (t, fn) => {
  (winListeners[t] ||= []).push(fn);
};
win.removeEventListener = (t, fn) => {
  winListeners[t] = (winListeners[t] || []).filter((f) => f !== fn);
};
async function fireWindow(type: string): Promise<void> {
  await act(async () => {
    for (const fn of [...(winListeners[type] || [])]) fn({ type });
  });
}
const bound = (type: string): number => (winListeners[type] || []).length;

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  // THE SYNCER REGISTRY IS MODULE SCOPE — one writer per key for the whole
  // document, which is the point of it — so a test that leaves a desired state
  // behind is a test the next one inherits.
  resetDraftSyncers();
});

// ── the hook, on its own ─────────────────────────────────────────────────────

test("blur and resize close an open overlay, and nothing is bound while it is shut", async () => {
  const closes: number[] = [];
  function Probe({ open }: { open: boolean }) {
    const close = useCallback(() => closes.push(1), []);
    useDismissOnWindow(open, close);
    return null;
  }
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(Probe, { open: false }));
  });
  mounted.push(r);
  // A LISTENER THAT LIVES FOR THE LIFE OF THE COMPOSER is a listener that fires
  // on every click into the pane for the whole session.
  expect(bound("blur")).toBe(0);
  expect(bound("resize")).toBe(0);

  await act(async () => r.update(createElement(Probe, { open: true })));
  expect(bound("blur")).toBe(1);
  expect(bound("resize")).toBe(1);

  // A click into the preview iframe never reaches this document but does blur
  // this window (T:12145-12146).
  await fireWindow("blur");
  expect(closes.length).toBe(1);
  // Nothing repositions an open popup, so a resize under it takes it away
  // rather than leaving it pointing at a button that has moved (T:12147-12149).
  await fireWindow("resize");
  expect(closes.length).toBe(2);

  // And the bindings come off with the overlay.
  await act(async () => r.update(createElement(Probe, { open: false })));
  expect(bound("blur")).toBe(0);
  expect(bound("resize")).toBe(0);
});

// ── the confirm's own content ───────────────────────────────────────────────

interface Focused {
  count: number;
  preventScroll?: boolean;
}

/** The sub-lines the popover shows, in order — the two things the calendar glyph
 *  cannot say for itself. */
function subLines(r: ReactTestRenderer): string[] {
  return r.root
    .findAll(
      (n) =>
        typeof n.type === "string" &&
        String((n.props as { className?: string }).className || "") === "c-schedpop-sub",
    )
    .map((n) => String(n.props.children).trim());
}

/** Renders the body (the portal above it has no container here — see the note
 *  in `SchedConfirm.tsx`) and hands back what Continue was told. */
async function openBody(): Promise<{
  renderer: ReactTestRenderer;
  go: Focused;
  presses: string[];
}> {
  const go: Focused = { count: 0 };
  const presses: string[] = [];
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      createElement(SchedConfirmBody, {
        onGo: () => presses.push("go"),
        onCancel: () => presses.push("cancel"),
      }),
      {
        createNodeMock: (el) => {
          const cls = String((el.props as { className?: string }).className || "");
          if (cls.includes("is-go")) {
            return {
              focus: (o?: { preventScroll?: boolean }) => {
                go.count += 1;
                go.preventScroll = o?.preventScroll;
              },
            };
          }
          return { focus: () => {} };
        },
      },
    );
  });
  mounted.push(renderer);
  return { renderer, go, presses };
}

const btnClasses = (r: ReactTestRenderer): string[] =>
  r.root
    .findAll(
      (n) =>
        typeof n.type === "string" &&
        String((n.props as { className?: string }).className || "").includes(
          "c-schedpop-btn",
        ),
    )
    .map((n) => String((n.props as { className?: string }).className || ""));

test("ENTER CONTINUES: the confirm opens with the caret on Continue (T:12072)", async () => {
  const { go } = await openBody();
  // Base UI would otherwise land on the popup's FIRST focusable, which is
  // Cancel — so a keyboard user's Enter dismissed the question they just asked.
  expect(go.count).toBe(1);
  // The composer is pinned to the bottom of a scrolling pane: focusing into an
  // overlay above it must not move the transcript underneath.
  expect(go.preventScroll).toBe(true);
});

test("DOM ORDER STAYS CANCEL-THEN-CONTINUE — that is T's reading order", async () => {
  const { renderer } = await openBody();
  const row = btnClasses(renderer);
  expect(row.length).toBe(2);
  expect(row[0].includes("is-go")).toBe(false);
  expect(row[1].includes("is-go")).toBe(true);
});

test("both answers still reach the caller", async () => {
  const { renderer, presses } = await openBody();
  const [cancel, go] = renderer.root.findAll(
    (n) =>
      typeof n.type === "string" &&
      String((n.props as { className?: string }).className || "").includes(
        "c-schedpop-btn",
      ),
  );
  await act(async () => (cancel.props as { onClick(): void }).onClick());
  await act(async () => (go.props as { onClick(): void }).onClick());
  expect(presses).toEqual(["cancel", "go"]);
});

// ── the button WIRES the dismissal ──────────────────────────────────────────

test("the Schedule button binds blur/resize only while its confirm is open", async () => {
  function Host() {
    return createElement(SchedButton, {
      file: "/w/app/page.html",
      sessionId: "s1",
      draft: () => "a scheduled line",
      back: "/w/app/page.html",
      onNavigate: () => {},
    });
  }
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(createElement(Host), {
      createNodeMock: () => ({ focus: () => {} }),
    });
  });
  mounted.push(renderer);
  expect(bound("blur")).toBe(0);

  const trigger = renderer.root.findAll(
    (n) => typeof n.type === "string" && n.type === "button",
  )[0];
  await act(async () => {
    // Base UI's button reads `detail` (0 means "from the keyboard") and
    // `pointerType` off the event, so the synthetic press carries both.
    (trigger.props as { onClick?: (ev: unknown) => void }).onClick?.({
      detail: 1,
      pointerType: "mouse",
      currentTarget: null,
    });
  });
  // OPEN — and this popover's Continue NAVIGATES AWAY FROM THE CONVERSATION, so
  // an orphaned confirm over a pane the reader has since clicked into is one
  // Enter from leaving the chat.
  expect(bound("blur")).toBe(1);
  expect(bound("resize")).toBe(1);

  await fireWindow("blur");
  expect(bound("blur")).toBe(0);
  expect(bound("resize")).toBe(0);
});

// ── the hop waits for its own save ──────────────────────────────────────────
//
// Bugbot, PR #1180: Continue used to fire the PUT and navigate in the same tick.
// The card on the other side seeds from `GET /api/drafts`, so the two raced —
// and when the GET won, the reader arrived at a card holding the words as they
// were 600 ms ago, or nothing at all on a first hop. The words are the whole
// point of the handoff, so the navigation is now the save's ANSWER.

// A SESSION'S key, and it has to be one (Akshil, 2026-09-16). These three tests
// are about the ORDER of two writers on ONE record — the box's debounced save
// and the hop's own statement — and that is now only true of a chat that has a
// session: a never-sent one writes nothing on its own and its Continue mints a
// `draft:<id>` nobody else holds (see "a never-sent chat" below).
const HOP_SESSION = "sess-hop";
const HOP_KEY = HOP_SESSION;

/** Mount the real button and hand back the confirm's own `onGo` — the Continue
 *  press, without asking a portal to render in a DOM-less runtime. */
function pressContinue(onNavigate: (url: string) => void) {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      createElement(SchedButton, {
        file: "/w/app/page.html",
        sessionId: HOP_SESSION,
        draft: () => "a scheduled line",
        back: "/w/app/page.html",
        onNavigate,
      }),
      { createNodeMock: () => ({ focus: () => {} }) },
    );
  });
  mounted.push(renderer);
  return (renderer.root.findByType(SchedConfirm).props as { onGo(): void }).onGo;
}

test("Continue navigates only once the draft's own PUT has answered", async () => {
  forgetDraftVersion(HOP_KEY);
  let release!: () => void;
  const held = new Promise<void>((r) => {
    release = r;
  });
  const methods: string[] = [];
  const real = globalThis.fetch;
  globalThis.fetch = ((_url: string, init?: RequestInit) => {
    methods.push(init?.method ?? "GET");
    return held.then(
      () =>
        ({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              ok: true,
              key: HOP_KEY,
              draft: { text: "a scheduled line", attachments: [], updated_at: 1,
                       version: 2, form: {} },
            }),
        }) as unknown as Response,
    );
  }) as typeof fetch;

  const went: string[] = [];
  const go = pressContinue((url) => went.push(url));
  await act(async () => {
    go();
  });
  // The write is out; the reader is still in the chat.
  expect(methods).toEqual(["PUT"]);
  expect(went).toEqual([]);
  await act(async () => {
    release();
    await held;
  });
  expect(went.length).toBe(1);
  expect(went[0]).toContain("draft=" + encodeURIComponent(HOP_KEY));
  globalThis.fetch = real;
  forgetDraftVersion(HOP_KEY);
});

test("a refused save keeps the reader in the chat, and says so", async () => {
  // Navigating with nothing saved is the same empty card by another road — and
  // this side is the only one still holding the words.
  forgetDraftVersion(HOP_KEY);
  _resetNotificationsForTest();
  const real = globalThis.fetch;
  globalThis.fetch = (() =>
    Promise.resolve({
      ok: false,
      status: 500,
      json: () => Promise.resolve({}),
    } as unknown as Response)) as unknown as typeof fetch;

  const went: string[] = [];
  const go = pressContinue((url) => went.push(url));
  await act(async () => {
    go();
  });
  expect(went).toEqual([]);
  expect(getPopupNotification()?.title).toContain("Could not save that draft");
  _resetNotificationsForTest();
  globalThis.fetch = real;
  forgetDraftVersion(HOP_KEY);
});

test("the hop waits for the write the box already had out", async () => {
  // Bugbot, PR #1180 (second round): waiting for THIS button's PUT is not
  // enough, because the box beside it writes the same record on a debounce. A
  // keystroke a moment before Continue is a PUT that lands AFTER the hop's —
  // with the older words, and with the chat tempdir paths `POST /api/schedule`
  // refuses.
  //
  // It is not a WAIT any more, it is an ORDER: the box and this button are the
  // same writer now (`draftSyncer(key)`), so the hop's state simply queues
  // behind whatever that writer has out and goes when it clears, stating the
  // version that write earned.
  forgetDraftVersion(HOP_KEY);
  let release!: () => void;
  const held = new Promise<void>((r) => { release = r; });
  const seen: (string | null)[] = [];
  let version = 0;
  const real = globalThis.fetch;
  globalThis.fetch = ((_url: string, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const at = seen.length;
    seen.push(headers["If-Match"] ?? null);
    const answer = (): Response => {
      version += 1;
      return {
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          ok: true,
          key: HOP_KEY,
          draft: { text: "a scheduled line", attachments: [], updated_at: 1,
                   version, form: {} },
        }),
      } as unknown as Response;
    };
    return at === 0 ? held.then(answer) : Promise.resolve(answer());
  }) as typeof fetch;

  // The box's own keystroke save, already on the wire when Continue is pressed.
  const box = draftSyncer(HOP_KEY);
  box.setText("a scheduled li");
  box.flushNow();
  expect(seen.length).toBe(1);

  const went: string[] = [];
  const go = pressContinue((url) => went.push(url));
  await act(async () => {
    go();
  });
  // NOTHING NEW HAS LEFT, and the reader is still in the chat.
  expect(seen.length).toBe(1);
  expect(went).toEqual([]);

  await act(async () => {
    release();
    await held;
    for (let i = 0; i < 6; i += 1) await Promise.resolve();
  });
  // The hop's own write went second, stating the version the box's write made,
  // and the navigation is its answer.
  // `0` on the first: this document had never seen a record under this key, and
  // "I expect nothing here" is what it honestly holds (contract §2).
  expect(seen).toEqual(["0", "1"]);
  expect(went.length).toBe(1);
  globalThis.fetch = real;
  forgetDraftVersion(HOP_KEY);
});

test("a real autosave PUT held open does not race the hop — the two coalesce", async () => {
  // THE SAME BUG through the REAL hook this time. What used to happen here was
  // three requests in a row, each waiting for the last, because the box's flush
  // and the hop's PUT were two different writers being put in order by hand.
  // They are one writer now, so the keystroke typed during the held write and
  // the hop's own state are ONE request: the last statement wins, which is what
  // a desired state means.
  forgetDraftVersion(HOP_KEY);
  const calls: { ifMatch: string | null; text?: string }[] = [];
  let releaseFirst!: () => void;
  const heldFirst = new Promise<void>((r) => { releaseFirst = r; });
  let version = 0;
  const real = globalThis.fetch;
  globalThis.fetch = ((_url: string, init?: RequestInit) => {
    const body = init?.body
      ? (JSON.parse(String(init.body)) as { text?: string })
      : undefined;
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const at = calls.length;
    calls.push({ ifMatch: headers["If-Match"] ?? null, text: body?.text });
    const answer = (): Response => {
      version += 1;
      return {
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          ok: true,
          key: HOP_KEY,
          draft: { text: body?.text ?? "", attachments: [], updated_at: 1,
                   version, form: {} },
        }),
      } as unknown as Response;
    };
    return at === 0 ? heldFirst.then(answer) : Promise.resolve(answer());
  }) as typeof fetch;

  const went: string[] = [];
  let renderer!: ReactTestRenderer;
  function Host({ value }: { value: { text: string } }) {
    useAutosave(value, (v: { text: string }) => draftSyncer(HOP_KEY).setText(v.text),
                { key: HOP_KEY });
    return createElement(SchedButton, {
      file: "/w/app/page.html",
      sessionId: HOP_SESSION,
      draft: () => value.text,
      back: "/w/app/page.html",
      onNavigate: (url: string) => went.push(url),
    });
  }
  // A mount alone mints nothing (design §4), so the box opens empty and the
  // first keystroke is what the held write below is about.
  await act(async () => {
    renderer = create(createElement(Host, { value: { text: "" } }), {
      createNodeMock: () => ({ focus: () => {} }),
    });
  });
  mounted.push(renderer);
  await act(async () => {
    renderer.update(createElement(Host, { value: { text: "first" } }));
  });
  act(() => draftSyncer(HOP_KEY).flushNow());
  await Promise.resolve();
  expect(calls.length).toBe(1);
  expect(calls[0]!.ifMatch).toBe("0");

  // A further keystroke, then Continue — exactly the moment the bug fired.
  await act(async () => {
    renderer.update(createElement(Host, { value: { text: "first and second" } }));
  });
  const go = (renderer.root.findByType(SchedConfirm).props as { onGo(): void }).onGo;
  await act(async () => {
    go();
  });
  expect(calls.length).toBe(1);
  expect(went).toEqual([]);

  releaseFirst();
  await act(async () => {
    await heldFirst;
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });
  // ONE request follows, not two: the newest state is the hop's, and it states
  // the version the held write made.
  expect(calls.length).toBe(2);
  expect(calls[1]).toEqual({ ifMatch: "1", text: "first and second" });
  expect(went.length).toBe(1);

  globalThis.fetch = real;
  forgetDraftVersion(HOP_KEY);
});

// ── a never-sent chat mints a NEW draft, every press ────────────────────────
//
// Akshil, 2026-09-16: type, Schedule, Continue; Back to chat; type something
// else, Schedule, Continue — and the second draft replaced the first. It had to:
// a session-less composer's key was `new:<file>`, ONE record per folder, and
// Continue stated the whole of it. A chat that has never been sent is not a
// conversation with an unsent message in it, so there is no single record for it
// to be: each press mints a `draft:<id>` task draft, the shape "+ New task"
// makes, and each one is its own Upcoming row.

interface Wrote {
  url: string;
  method: string;
  body: Record<string, unknown>;
}

/** Records every request and answers each one as a task-draft write. */
function watchTaskWrites(): Wrote[] {
  const seen: Wrote[] = [];
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    const body = init?.body
      ? (JSON.parse(String(init.body)) as Record<string, unknown>)
      : {};
    seen.push({ url: String(url), method: init?.method ?? "GET", body });
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        ok: true,
        draft_id: String(url).split("/").pop(),
        draft: { ...body, created_at: 1, updated_at: 1, version: 1 },
      }),
    } as unknown as Response);
  }) as typeof fetch;
  return seen;
}

/** One press of Continue on a composer with no session, and the URL it left on. */
async function pressNewChat(text: string): Promise<{ wrote: Wrote[]; went: string[] }> {
  const wrote = watchTaskWrites();
  const went: string[] = [];
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      createElement(SchedButton, {
        file: "/w/app",
        sessionId: "",
        draft: () => text,
        back: "/explorer/view/w/app?_side=claude",
        onNavigate: (url: string) => went.push(url),
      }),
      { createNodeMock: () => ({ focus: () => {} }) },
    );
  });
  mounted.push(renderer);
  const go = (renderer.root.findByType(SchedConfirm).props as { onGo(): void }).onGo;
  await act(async () => {
    go();
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });
  return { wrote, went };
}

test("Continue from a never-sent chat writes a TASK draft, not `new:<file>`", async () => {
  const real = globalThis.fetch;
  const { wrote, went } = await pressNewChat("port the parquet reader\nstart with paths");
  expect(wrote).toHaveLength(1);
  // The record is a task draft under an id this press minted…
  expect(wrote[0]!.method).toBe("PUT");
  expect(wrote[0]!.url.startsWith("/api/drafts/task/")).toBe(true);
  expect(wrote[0]!.url).not.toContain("new%3A");
  // …carrying the box's prose cut the card's way (`splitDraft`), the folder the
  // chat is mounted on, and no answer at all to the questions a composer cannot
  // answer.
  expect(wrote[0]!.body.title).toBe("port the parquet reader");
  expect(wrote[0]!.body.description).toBe("start with paths");
  expect(wrote[0]!.body.target).toBe("/w/app");
  expect(wrote[0]!.body.when).toBe(null);
  expect(wrote[0]!.body.repeat).toBe(null);
  expect(wrote[0]!.body.session_id).toBe("");
  // …and the card opens through the arm every draft row presses, with the way
  // back on it.
  const id = wrote[0]!.url.slice("/api/drafts/task/".length);
  // `hop=1`: this opening is a Schedule PRESS, so the card lands on now+2m and
  // planning like the session hop's `?new=1` does — a draft ROW presses the same
  // arm without it and keeps the reopen rule (Bugbot 4028344051).
  expect(went).toEqual([
    `/tasks?draft=${encodeURIComponent(id)}&hop=1`
      + "&from=" + encodeURIComponent("/explorer/view/w/app?_side=claude"),
  ]);
  globalThis.fetch = real;
  resetDraftSyncers();
});

test("…and a SECOND Continue out of the same folder is a SECOND draft", async () => {
  // The bug, in one assertion: two presses, two ids, two records. The old shape
  // wrote `new:/w/app` both times and the first draft was simply gone.
  const real = globalThis.fetch;
  const first = await pressNewChat("the first thing");
  resetDraftSyncers();
  const second = await pressNewChat("the second thing");
  expect(first.wrote).toHaveLength(1);
  expect(second.wrote).toHaveLength(1);
  expect(second.wrote[0]!.url).not.toBe(first.wrote[0]!.url);
  expect(first.went[0]).not.toBe(second.went[0]);
  for (const w of [...first.wrote, ...second.wrote]) {
    expect(w.url).toContain("/api/drafts/task/");
  }
  globalThis.fetch = real;
  resetDraftSyncers();
});

test("an EMPTY never-sent composer mints nothing and opens a blank card", async () => {
  // A draft with no words and no files is an Upcoming row saying nothing. The
  // press still travels — the folder and the way back are what it knows — and
  // the card is filled in there.
  const real = globalThis.fetch;
  const { wrote, went } = await pressNewChat("   ");
  expect(wrote).toEqual([]);
  expect(went).toHaveLength(1);
  expect(went[0]).toContain("new=1");
  expect(went[0]).toContain("target=" + encodeURIComponent("/w/app"));
  expect(went[0]).not.toContain("new%3A");
  globalThis.fetch = real;
  resetDraftSyncers();
});

// The patch comes off with the file (see the registry note above).
process.on("beforeExit", () => {
  win.addEventListener = realWin.add;
  win.removeEventListener = realWin.remove;
});

// ---- what Continue is about to do, and what it no longer claims -----------

test("the confirm NEVER says it is replacing a saved draft", async () => {
  // Akshil, 2026-09-16: it used to, and the sentence was true of a bug. Continue
  // from a never-sent chat wrote `new:<file>` — one record per FOLDER — so a
  // second draft out of the same folder really did land on top of the first, and
  // the line was the page apologising for it. It mints `draft:<id>` per press
  // now (`composerTaskDraft`), nothing is ever replaced, and the confirm is two
  // lines again: what it is, and what happens next.
  const { renderer } = await openBody();
  expect(subLines(renderer)).toEqual([
    "This task will be scheduled to run at a specific time.",
  ]);
  expect(JSON.stringify(renderer.toJSON())).not.toContain("replaces");
});

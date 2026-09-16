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
const { forgetDraftVersion, saveChatDraft, useAutosave } =
  await import("@platform/lib/drafts");
const { getPopupNotification, _resetNotificationsForTest } =
  await import("@platform/lib/notifications");
const { useDismissOnWindow } = await import("./useDismissOnWindow");
type Autosave<T> = ReturnType<typeof useAutosave<T>>;

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

const HOP_KEY = "new:/w/app/page.html";

/** Mount the real button and hand back the confirm's own `onGo` — the Continue
 *  press, without asking a portal to render in a DOM-less runtime. */
function pressContinue(
  onNavigate: (url: string) => void,
  settleDraft?: () => Promise<number | undefined>,
) {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      createElement(SchedButton, {
        file: "/w/app/page.html",
        sessionId: "",
        draft: () => "a scheduled line",
        back: "/w/app/page.html",
        onNavigate,
        ...(settleDraft ? { settleDraft } : {}),
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

test("the hop writes only after the composer's own autosave has finished", async () => {
  // Bugbot, PR #1180 (second round): waiting for THIS button's PUT is not
  // enough, because the box beside it writes the same record on a debounce. A
  // keystroke a moment before Continue is a PUT that lands AFTER the hop's —
  // with the older words, and with the chat tempdir paths `POST /api/schedule`
  // refuses. So the composer is settled first, and the version it answers with
  // is what the hop states: a straggler is then the write that gets refused.
  forgetDraftVersion(HOP_KEY);
  let finishAutosave!: () => void;
  const autosaved = new Promise<number | undefined>((r) => {
    finishAutosave = () => r(9);
  });
  const seen: (string | null)[] = [];
  const real = globalThis.fetch;
  globalThis.fetch = ((_url: string, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    seen.push(headers["If-Match"] ?? null);
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          ok: true,
          key: HOP_KEY,
          draft: { text: "a scheduled line", attachments: [], updated_at: 1,
                   version: 10, form: {} },
        }),
    } as unknown as Response);
  }) as typeof fetch;

  const went: string[] = [];
  const go = pressContinue((url) => went.push(url), () => autosaved);
  await act(async () => {
    go();
  });
  // NOTHING HAS LEFT. The composer's write is still out, so the hop's has not
  // been dispatched at all — there is no order to get wrong.
  expect(seen).toEqual([]);
  expect(went).toEqual([]);
  await act(async () => {
    finishAutosave();
    await autosaved;
  });
  // …and it carries the version that write earned, not "whatever this client
  // last heard of".
  expect(seen).toEqual(["9"]);
  expect(went.length).toBe(1);
  globalThis.fetch = real;
  forgetDraftVersion(HOP_KEY);
});

test("a real autosave PUT held open queues the flush behind it, and the hop's own PUT goes third", async () => {
  // THE SAME BUG, through the REAL `useAutosave` this time rather than a
  // stand-in `settleDraft` (Bugbot, PR #1180, this round): `settleDraft` is
  // `flush()` then `settle()` (Composer.tsx), and `flush` used to dispatch a
  // brand-new PUT the instant it was called even while an earlier autosave
  // was still on the wire — the abandoned one could land AFTER the hop's and
  // put stale text back on the record the task card was about to open. Now
  // every write from this box queues behind whatever it already has out
  // (`useAutosave`, drafts.ts), so the debounced edit's flush waits its turn
  // and the hop's own PUT — asked for only once `settleDraft` resolves — is
  // never racing either one.
  forgetDraftVersion(HOP_KEY);
  const calls: { ifMatch: string | null; text?: string }[] = [];
  let releaseFirst!: () => void;
  const heldFirst = new Promise<void>((r) => { releaseFirst = r; });
  let version = 0;
  const real = globalThis.fetch;
  globalThis.fetch = ((_url: string, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const body = init?.body
      ? (JSON.parse(String(init.body)) as { text?: string })
      : undefined;
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
    // Only the FIRST request — the autosave already out when the test begins
    // — is held; everything after answers as soon as it is asked to, so the
    // test controls order by CALLING, not by releasing.
    return at === 0 ? heldFirst.then(answer) : Promise.resolve(answer());
  }) as typeof fetch;

  let latest!: Autosave<{ text: string }>;
  const went: string[] = [];
  let renderer!: ReactTestRenderer;
  function Host({ value }: { value: { text: string } }) {
    latest = useAutosave(value, (v: { text: string }, opts) =>
      saveChatDraft(HOP_KEY, v.text, [], opts));
    const settleDraft = useCallback((): Promise<number | undefined> => {
      latest.flush();
      return latest.settle();
    }, []);
    return createElement(SchedButton, {
      file: "/w/app/page.html",
      sessionId: "",
      draft: () => value.text,
      back: "/w/app/page.html",
      onNavigate: (url: string) => went.push(url),
      settleDraft,
    });
  }
  // A mount alone mints nothing (design §4: `written` seeds with the opening
  // value), so the box opens empty and the first keystroke is what the held
  // write below is about.
  await act(async () => {
    renderer = create(createElement(Host, { value: { text: "" } }), {
      createNodeMock: () => ({ focus: () => {} }),
    });
  });
  mounted.push(renderer);
  await act(async () => {
    renderer.update(createElement(Host, { value: { text: "first" } }));
  });

  // The autosave's own write — held open, per the mock above.
  act(() => latest.flush());
  await Promise.resolve();
  expect(calls.length).toBe(1);
  expect(calls[0]!.ifMatch).toBe(null);

  // A further keystroke, then Continue — exactly the moment the bug fired:
  // the debounced edit's flush and the hop's settle both want this box's
  // pending write answered.
  await act(async () => {
    renderer.update(createElement(Host, { value: { text: "first and second" } }));
  });
  const go = (renderer.root.findByType(SchedConfirm).props as { onGo(): void }).onGo;
  await act(async () => {
    go();
  });
  // NOTHING NEW HAS LEFT: the edit's own flush is queued behind the held
  // autosave, and the hop asked `settleDraft` to wait rather than firing
  // beside it.
  expect(calls.length).toBe(1);
  expect(went).toEqual([]);

  releaseFirst();
  await act(async () => {
    await heldFirst;
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  // THE QUEUED FLUSH GOES OUT SECOND — carrying the version the held write
  // just made, and the newer text — and only once IT lands does the hop's own
  // PUT follow: THIRD, carrying THAT write's version in turn. Never two on the
  // wire at once, and never out of order.
  expect(calls.length).toBe(3);
  expect(calls[1]).toEqual({ ifMatch: "1", text: "first and second" });
  expect(calls[2]!.ifMatch).toBe("2");
  expect(went.length).toBe(1);

  globalThis.fetch = real;
  forgetDraftVersion(HOP_KEY);
});

// The patch comes off with the file (see the registry note above).
process.on("beforeExit", () => {
  win.addEventListener = realWin.add;
  win.removeEventListener = realWin.remove;
});

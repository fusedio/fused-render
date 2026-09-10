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
const { SchedConfirmBody } = await import("./SchedConfirm");
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

// The patch comes off with the file (see the registry note above).
process.on("beforeExit", () => {
  win.addEventListener = realWin.add;
  win.removeEventListener = realWin.remove;
});

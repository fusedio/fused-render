// THE CHASSIS AS A POPOVER (Modal `popover`; owner, 2026-11-07: the rail's +
// opens the New task card "like the popover for task chats"): no scrim, not
// aria-modal; and where it stands (popover-place.ts).
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createRef } from "react";

import { Modal } from "./Modal";
import { POPOVER_GAP, POPOVER_INSET, popoverPlacement } from "./popover-place";

afterEach(() => {
  removePortalContainer();
});

function mount(popover: boolean): ReactTestRenderer {
  installPortalContainer();
  const anchor = createRef<HTMLElement>();
  let r!: ReactTestRenderer;
  act(() => {
    r = create(
      <Modal title="New task" onClose={() => {}} {...(popover ? { popover: { anchor } } : {})}>
        <span>body</span>
      </Modal>,
    );
  });
  return r;
}

describe("Modal popover presentation", () => {
  test("no scrim and not modal: the overlay is the click-through popover layer, no aria-modal", () => {
    const r = mount(true);
    const overlay = r.root.find((n) => typeof n.type === "string" && String(n.props.className ?? "").includes("modal-overlay"));
    expect(overlay.props.className).toContain("modal-popover");
    expect(overlay.props.className).not.toContain("deploy-overlay");
    const dialog = r.root.find((n) => n.props.role === "dialog");
    expect(dialog.props["aria-modal"]).toBeUndefined();
    expect(dialog.props.style.position).toBe("fixed");
    act(() => r.unmount());
  });

  test("without it, the modal it always was: scrim and aria-modal", () => {
    const r = mount(false);
    const overlay = r.root.find((n) => typeof n.type === "string" && String(n.props.className ?? "").includes("modal-overlay"));
    expect(overlay.props.className).toContain("deploy-overlay");
    expect(overlay.props.className).not.toContain("modal-popover");
    const dialog = r.root.find((n) => n.props.role === "dialog");
    expect(dialog.props["aria-modal"]).toBe("true");
    act(() => r.unmount());
  });
});

describe("Modal popover pinned (the rail's +: hover previews, a press pins)", () => {
  type Listener = (e: unknown) => void;
  const doc = (globalThis as unknown as { document: { addEventListener: unknown; removeEventListener: unknown } }).document;
  let listeners: { type: string; fn: Listener }[] = [];
  const realAdd = doc.addEventListener;
  const realRemove = doc.removeEventListener;
  function capture(): void {
    listeners = [];
    doc.addEventListener = (type: string, fn: Listener) => listeners.push({ type, fn });
    doc.removeEventListener = (type: string, fn: Listener) => {
      listeners = listeners.filter((l) => !(l.type === type && l.fn === fn));
    };
  }
  afterEach(() => {
    doc.addEventListener = realAdd;
    doc.removeEventListener = realRemove;
  });
  const fire = (type: string, e: unknown) => listeners.filter((l) => l.type === type).forEach((l) => l.fn(e));
  const outside = { nodeType: 1, closest: () => null, parentElement: null };

  function mountPinned(pinned: boolean) {
    installPortalContainer();
    capture();
    const anchor = createRef<HTMLElement>();
    const closes: number[] = [];
    const pins: number[] = [];
    const el = (p: boolean) => (
      <Modal title="New task" onClose={() => closes.push(1)} popover={{ anchor, pinned: p, onPin: () => pins.push(1) }}>
        <span>body</span>
      </Modal>
    );
    let r!: ReactTestRenderer;
    act(() => {
      r = create(el(pinned));
    });
    const closing = () =>
      String(
        r.root.find((n) => typeof n.type === "string" && String(n.props.className ?? "").includes("modal-overlay")).props
          .className,
      ).includes("closing");
    return { r, closing, closes, pins, rerender: (p: boolean) => act(() => r.update(el(p))) };
  }

  test("pinned: a press outside and Escape leave it up — only its × closes it", () => {
    const m = mountPinned(true);
    act(() => fire("pointerdown", { target: outside }));
    act(() => fire("keydown", { key: "Escape" }));
    expect(m.closing()).toBe(false);
    act(() => m.r.unmount());
  });

  test("a preview (pinned: false): a press outside closes it, a press inside pins it", () => {
    const m = mountPinned(false);
    const dialog = m.r.root.find((n) => n.props.role === "dialog");
    act(() => dialog.props.onPointerDownCapture());
    expect(m.pins.length).toBe(1);
    act(() => fire("pointerdown", { target: outside }));
    expect(m.closing()).toBe(true);
    act(() => m.r.unmount());
  });

  test("a preview closes on Escape; once pinned, Escape does nothing and the pin is not asked again", () => {
    const m = mountPinned(false);
    m.rerender(true);
    act(() => fire("keydown", { key: "Escape" }));
    expect(m.closing()).toBe(false);
    const dialog = m.r.root.find((n) => n.props.role === "dialog");
    act(() => dialog.props.onFocusCapture());
    expect(m.pins.length).toBe(0);
    m.rerender(false);
    act(() => fire("keydown", { key: "Escape" }));
    expect(m.closing()).toBe(true);
    act(() => m.r.unmount());
  });
});

describe("popoverPlacement", () => {
  const view = { width: 1400, height: 900 };
  test("left of the rail's edge, a gap away, top level with the anchor", () => {
    const at = popoverPlacement({ left: 1360, top: 60 }, { left: 1352, top: 0 }, { width: 580, height: 400 }, view);
    expect(at.width).toBe(580);
    expect(at.left).toBe(1352 - POPOVER_GAP - 580);
    expect(at.top).toBe(60);
  });
  test("a card taller than the room under the anchor slides up, inside the window", () => {
    const at = popoverPlacement({ left: 1360, top: 700 }, { left: 1352, top: 0 }, { width: 580, height: 400 }, view);
    expect(at.top).toBe(900 - POPOVER_INSET - 400);
  });
  test("a narrow window: the width gives way, never past the left inset", () => {
    const at = popoverPlacement({ left: 460, top: 60 }, { left: 452, top: 0 }, { width: 580, height: 400 }, { width: 500, height: 900 });
    expect(at.left).toBeGreaterThanOrEqual(POPOVER_INSET);
    expect(at.left + at.width).toBeLessThanOrEqual(500 - POPOVER_INSET);
    expect(at.width).toBeLessThan(580);
  });
  test("a SIZED card (the rail's +, window A's size) takes its height, clamped inside the window", () => {
    const tall = popoverPlacement({ left: 1360, top: 60 }, { left: 1352, top: 0 }, { width: 420, height: 640 }, view, true);
    expect(tall).toEqual({ left: 1352 - POPOVER_GAP - 420, top: 60, width: 420, height: 640 });
    const short = popoverPlacement({ left: 1360, top: 60 }, { left: 1352, top: 0 }, { width: 420, height: 640 }, { width: 1400, height: 500 }, true);
    expect(short.height).toBe(500 - 2 * POPOVER_INSET);
    expect(short.top).toBe(POPOVER_INSET);
    expect(short.top + short.height!).toBeLessThanOrEqual(500 - POPOVER_INSET);
  });
  test("an unsized card says no height", () => {
    const at = popoverPlacement({ left: 1360, top: 60 }, { left: 1352, top: 0 }, { width: 580, height: 400 }, view);
    expect(at.height).toBeUndefined();
  });
});

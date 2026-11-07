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
});

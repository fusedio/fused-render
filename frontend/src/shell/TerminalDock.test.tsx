// PLAN-status-bar-terminal.md Task 5's own test list: chip label per state,
// click toggles, hovering the chip does NOT open the drawer (there is no
// hover wiring at all — see terminalDockStore.ts's header for why), and the
// chip does not close Models' panel (it never calls `useExclusiveSection`,
// so it cannot enter the arbitration `entries` map ModelsDock's chip does).
// Drawer geometry gets no test here (PLAN Decisions) — this file only covers
// the chip and the shared open/close store.
//
// Every tree this file creates is unmounted in the same test — react-test-
// renderer trees hold DOM-ish nodes and (for the store test) a live
// `useSyncExternalStore` subscription, both real resources per the bun-test
// memory discipline for this feature.
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";

import { TerminalDockView } from "@shell/TerminalDock";
import { resetExclusiveSectionsForTests, useExclusiveSection } from "@platform/lib/exclusiveSection";
import { resetTerminalDockForTests, toggleTerminalDock, useTerminalDockOpen } from "@shell/terminalDockStore";

function findAll(node: ReactTestRendererJSON | null, className: string): ReactTestRendererJSON[] {
  if (node === null || typeof node === "string") return [];
  const hits: ReactTestRendererJSON[] = [];
  if (
    typeof node.props?.className === "string" &&
    node.props.className.split(" ").includes(className)
  ) {
    hits.push(node);
  }
  for (const child of node.children ?? []) {
    if (typeof child !== "string") hits.push(...findAll(child, className));
  }
  return hits;
}

function text(node: ReactTestRendererJSON | null): string {
  if (node === null) return "";
  if (typeof node === "string") return node;
  return (node.children ?? []).map((c) => text(c as ReactTestRendererJSON)).join("");
}

// Every renderer this file creates is tracked here and unmounted in
// afterEach, so a failing assertion mid-test still tears the tree down
// rather than leaking it into the next test (bun runs the whole file's
// tests in one process/module registry).
let renderers: ReactTestRenderer[] = [];
function renderTracked(node: Parameters<typeof create>[0]): ReactTestRenderer {
  const renderer = create(node);
  renderers.push(renderer);
  return renderer;
}

afterEach(() => {
  for (const renderer of renderers) renderer.unmount();
  renderers = [];
  resetTerminalDockForTests();
  resetExclusiveSectionsForTests();
});

test("idle (closed) draws a real, clickable, muted chip labelled Terminal", () => {
  const tree = renderTracked(<TerminalDockView open={false} onToggle={() => {}} />).toJSON() as ReactTestRendererJSON;
  const toggles = findAll(tree, "dl-toggle");
  expect(toggles).toHaveLength(1);
  expect(toggles[0].type).toBe("button");
  expect((toggles[0].props.className as string).split(" ")).toContain("is-idle");
  expect(toggles[0].props["aria-expanded"]).toBe(false);
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Terminal");
});

test("open drops the idle tone but keeps the same label", () => {
  const tree = renderTracked(<TerminalDockView open={true} onToggle={() => {}} />).toJSON() as ReactTestRendererJSON;
  const toggles = findAll(tree, "dl-toggle");
  expect((toggles[0].props.className as string).split(" ")).not.toContain("is-idle");
  expect(toggles[0].props["aria-expanded"]).toBe(true);
  expect(text(findAll(tree, "dl-summary")[0])).toBe("Terminal");
});

test("clicking the chip calls onToggle", () => {
  let calls = 0;
  const renderer = renderTracked(<TerminalDockView open={false} onToggle={() => (calls += 1)} />);
  const tree = renderer.toJSON() as ReactTestRendererJSON;
  const button = findAll(tree, "dl-toggle")[0];
  act(() => {
    (button.props as { onClick: () => void }).onClick();
  });
  expect(calls).toBe(1);
});

// The store itself (not the view): clicking the real chip must flip the
// shared open state `TerminalDrawer.tsx` reads, with no hover wiring at all
// — there is no `onPointerEnter`/`onPointerLeave` prop on the chip's host at
// all, unlike ModelsDock's `hostProps` spread, so "hover opens it" is not
// merely untested here, it is structurally absent.
test("toggling the store flips the shared open state, with no hover affordance on the host", () => {
  function Probe() {
    const open = useTerminalDockOpen();
    return <TerminalDockView open={open} onToggle={toggleTerminalDock} />;
  }
  let renderer!: ReactTestRenderer;
  // `useSyncExternalStore`'s subscription is registered in a passive effect,
  // which react-test-renderer only flushes inside `act()` — an un-acted
  // `create()` leaves the store's listener set empty, so a click right after
  // would mutate the module value with nothing subscribed to notice.
  act(() => {
    renderer = create(<Probe />);
  });
  renderers.push(renderer);
  let tree = renderer.toJSON() as ReactTestRendererJSON;
  const host = findAll(tree, "dl-host")[0];
  expect(host.props.onPointerEnter).toBeUndefined();
  expect(host.props.onPointerLeave).toBeUndefined();

  expect((findAll(tree, "dl-toggle")[0].props.className as string).split(" ")).toContain("is-idle");

  act(() => {
    (findAll(tree, "dl-toggle")[0].props as { onClick: () => void }).onClick();
  });
  tree = renderer.toJSON() as ReactTestRendererJSON;
  expect((findAll(tree, "dl-toggle")[0].props.className as string).split(" ")).not.toContain(
    "is-idle",
  );

  act(() => {
    (findAll(tree, "dl-toggle")[0].props as { onClick: () => void }).onClick();
  });
  tree = renderer.toJSON() as ReactTestRendererJSON;
  expect((findAll(tree, "dl-toggle")[0].props.className as string).split(" ")).toContain("is-idle");
});

// The exclusivity guarantee: a section that participates in
// `useExclusiveSection` (like ModelsDock's own chip) gets force-closed by a
// SECOND participating section opening (exclusiveSection.test.tsx covers
// that case at the hook level). TerminalDock must never be that second
// section. Rather than mounting the real, network-polling `ModelsDock`
// default export (which would need a fetch/window shim this file has no
// other reason to carry — see ModelsDock.test.tsx's own preference for the
// pure `ModelsCardView`), this stands in a minimal probe that calls
// `useExclusiveSection` directly, the same call ModelsDock's chip makes, and
// asserts its `forceClose` is never invoked by a terminal-chip click.
test("opening the terminal chip never triggers another section's forceClose", () => {
  let forceCloseCalls = 0;
  function OtherSectionProbe() {
    useExclusiveSection("models", true, () => {
      forceCloseCalls += 1;
    });
    return null;
  }
  function TerminalProbe() {
    const open = useTerminalDockOpen();
    return <TerminalDockView open={open} onToggle={toggleTerminalDock} />;
  }

  let terminalRenderer!: ReactTestRenderer;
  act(() => {
    renderTracked(<OtherSectionProbe />);
    terminalRenderer = create(<TerminalProbe />);
  });
  renderers.push(terminalRenderer);

  act(() => {
    const tree = terminalRenderer.toJSON() as ReactTestRendererJSON;
    (findAll(tree, "dl-toggle")[0].props as { onClick: () => void }).onClick();
  });

  expect(forceCloseCalls).toBe(0);
});

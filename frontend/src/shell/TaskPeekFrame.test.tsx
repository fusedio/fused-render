// THE FRAME THE PEEK LIVES IN, on both of its hosts (TaskPeekFrame.tsx).
//
// Two things worth proving without a browser: that an off frame renders its
// children bare (the page is exactly the page it was), and that an on frame
// hands the row element to whatever is mounted inside it — which is how the
// app page's Tasks tab gets its panel beside the WHOLE page rather than under
// the tab bar (`useTaskPeekSlot`). The rest — widths, floors, the click that
// closes — is the store's arithmetic, pinned in task-peek-store.test.ts.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement, type ReactElement } from "react";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

// Dynamic, AFTER the shim: a static import is hoisted above `installDomShim()`
// and router.ts reads `location` at module scope.
const { TaskPeekFrame, useTaskPeekSlot } = await import("./TaskPeekFrame");
const { resetPeekStoreForTests } = await import("./task-peek-store");

const read = (name: string) => readFileSync(join(import.meta.dir, name), "utf8");

/** The measuring apparatus the frame reaches for on mount, inert, and only
 *  for this suite (schedule-hop.render.test.tsx says why not the shim). */
const MISSING = Symbol("missing");
const was = new Map<string, unknown>();
function installMeasuring() {
  const g = globalThis as Record<string, unknown>;
  const inert = class {
    observe() {}
    disconnect() {}
  };
  for (const name of ["ResizeObserver", "MutationObserver"]) {
    was.set(name, name in g ? g[name] : MISSING);
    g[name] = inert;
  }
}
function uninstallMeasuring() {
  const g = globalThis as Record<string, unknown>;
  for (const [name, prior] of was) {
    if (prior === MISSING) delete g[name];
    else g[name] = prior;
  }
  was.clear();
}

/** What a child inside the frame is told about its slot. */
let seenSlot: unknown = MISSING;
function SlotReader() {
  seenSlot = useTaskPeekSlot();
  return createElement("p", { className: "page" }, "the page");
}

/** A fake element per host node, so the `ref={setHost}` callback has a node
 *  to hand over (react-test-renderer resolves refs through `createNodeMock`). */
const nodeMock = (el: ReactElement) => ({
  nodeType: 1,
  tag: el.type,
  className: (el.props as Record<string, unknown>).className,
});

let box: ReactTestRenderer | null = null;

/** The frame, whose class also carries `is-instant` when the store says so. */
const findFrame = (host: ReactTestInstance) =>
  host.find(
    (n) => typeof n.props.className === "string" && n.props.className.split(" ")[0] === "tasks-frame",
  );

describe("TaskPeekFrame", () => {
  beforeEach(() => {
    installMeasuring();
    resetPeekStoreForTests();
    seenSlot = MISSING;
  });
  afterEach(() => {
    act(() => box?.unmount());
    box = null;
    uninstallMeasuring();
  });

  it("off, renders its children bare: no host, no frame, no slot", () => {
    act(() => {
      box = create(
        createElement(TaskPeekFrame, { peekable: false, children: createElement(SlotReader) }),
        { createNodeMock: nodeMock },
      );
    });
    expect(box!.root.findAllByProps({ className: "tasks-peek-host" })).toHaveLength(0);
    expect(
      box!.root.findAll((n) => typeof n.props.className === "string" && /tasks-frame/.test(n.props.className)),
    ).toHaveLength(0);
    expect(box!.root.findByProps({ className: "page" })).toBeTruthy();
    expect(seenSlot).toBeNull();
  });

  it("on, wraps the page in the row + frame and hands the row to what is inside", () => {
    act(() => {
      box = create(
        createElement(TaskPeekFrame, { peekable: true, children: createElement(SlotReader) }),
        { createNodeMock: nodeMock },
      );
    });
    const host = box!.root.findByProps({ className: "tasks-peek-host" });
    const frame = findFrame(host);
    // The page is INSIDE the frame — the half that shrinks — not beside it.
    expect(frame.findByProps({ className: "page" })).toBeTruthy();
    // Closed: the frame takes the whole row and nothing is floored or tight.
    expect(frame.props.style.width).toBe("calc(100% - 0px)");
    expect(frame.props["data-floored"]).toBeUndefined();
    expect(frame.props["data-tight"]).toBeUndefined();
    // …and the slot a child sees IS the row — the element a portalled panel
    // mounts into, so it lands as the frame's sibling.
    expect(seenSlot).toMatchObject({ nodeType: 1, className: "tasks-peek-host" });
  });

  it("puts a panel handed in as `peek` beside the frame, not inside it", () => {
    act(() => {
      box = create(
        createElement(TaskPeekFrame, {
          peekable: true,
          peek: createElement("aside", { className: "panel" }),
          children: createElement(SlotReader),
        }),
        { createNodeMock: nodeMock },
      );
    });
    const host = box!.root.findByProps({ className: "tasks-peek-host" });
    const frame = findFrame(host);
    expect(host.findAllByProps({ className: "panel" })).toHaveLength(1);
    expect(frame.findAllByProps({ className: "panel" })).toHaveLength(0);
  });
});

describe("the two hosts", () => {
  const PAGE = read("Scheduled.tsx");
  const APP = read("AppPage.tsx");

  it("the Tasks page arms the peek scoped or not, and portals its panel into a frame someone else drew", () => {
    // The scope used to be half the gate; the app page's Tasks tab is a host now.
    expect(PAGE).toContain("const peekable = peekOn;");
    expect(PAGE).not.toContain("!scope && peekOn");
    expect(PAGE).toContain("const slot = useTaskPeekSlot();");
    expect(PAGE).toContain("{createPortal(panel, slot)}");
    // …and NEVER a frame of its own while scoped: the slot arrives one commit
    // late, and a frame drawn in that gap would nest inside the app page's.
    expect(PAGE).toContain("if (scope) {\n    if (!slot) return page;");
    // …and draws its own frame where there is none: `/tasks`.
    expect(PAGE).toContain("<TaskPeekFrame peekable peek={panel}>");
  });

  it("the app page frames the WHOLE page, only on the Tasks tab, only with the flag", () => {
    expect(APP).toContain('const peekable = peekOn === true && tab === "tasks";');
    // The wrap encloses `.app-page` — header, tab strip and panels alike.
    expect(APP).toContain('<TaskPeekFrame peekable={peekable}>\n    <div className="app-page">');
  });

  it("the app page's tab links never carry `?peek=`: a switch away is a close", () => {
    expect(APP).toContain(
      "const tabUrl = (next: AppPageTab) => appPageUrl(dir, next, peekSearch(location.search, null));",
    );
    expect(APP).toContain("href={tabUrl(id)}");
    expect(APP).toContain("if (next !== tab) navigateUrl(tabUrl(next));");
    expect(APP).not.toContain("appPageUrl(dir, id, location.search)");
  });

  it("clicking the app page's icon, or inside its picker, is not a click on blank frame", () => {
    const STORE = read("task-peek-store.ts");
    expect(STORE).toContain(".app-page-icon-toggle, [${PEEK_KEEP_ATTR}]");
    expect(read("../platform/ui/IconPicker.tsx")).toContain('data-peek-keep="1"');
  });
});

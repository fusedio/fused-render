// The chat frame's contract with the template it hosts: `data-chat-ready="1"`
// on the framed document's <html>, and nothing else. Two things are worth a
// test here and the rest is browser behaviour — the READ of that attribute
// (pure, exhaustively checkable) and the REVEAL it drives (a class on the
// iframe, a cover that leaves), which react-test-renderer can drive with a
// mocked frame node.
//
// Not tested here: the 8s fallback timer and the MutationObserver path. The
// first is real time nobody should spend in a suite (and bun's runtime has no
// fake-timer contract this file can lean on); the second needs a live
// MutationObserver, which bun does not have — both are exercised in the browser
// pass instead.
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";

import { ChatFrame, ChatFramePlaceholder, isChatDocReady } from "./ChatFrame";

/** A document whose <html> carries whatever `dataset` this is handed. */
function doc(dataset: Record<string, string>): Document {
  return { documentElement: { dataset } } as unknown as Document;
}

/** An iframe node standing in for the DOM one react-test-renderer never makes.
 *  `contentDocument` is what the component reads; the listeners are recorded so
 *  a test can fire `load` the way a browser would. */
function frameNode(contentDocument: Document | null) {
  const listeners: Record<string, Array<() => void>> = {};
  return {
    contentDocument,
    addEventListener(type: string, fn: () => void) {
      (listeners[type] ??= []).push(fn);
    },
    removeEventListener(type: string, fn: () => void) {
      listeners[type] = (listeners[type] ?? []).filter((f) => f !== fn);
    },
    fire(type: string) {
      for (const fn of [...(listeners[type] ?? [])]) fn();
    },
  };
}

function find(
  node: ReactTestRendererJSON | null,
  cls: string,
): ReactTestRendererJSON | undefined {
  if (!node) return undefined;
  if (String(node.props?.className ?? "").split(" ").includes(cls)) return node;
  for (const k of node.children ?? []) {
    if (typeof k === "string") continue;
    const hit = find(k as ReactTestRendererJSON, cls);
    if (hit) return hit;
  }
  return undefined;
}

/** Mount a ChatFrame over `node`, effects flushed, and register it for
 *  teardown — every mounted frame holds at least the fallback timer, and a
 *  renderer left up fires it (and the fade timer) into a finished test, which
 *  React reports as an unwrapped state update. */
const mounted: Array<ReturnType<typeof create>> = [];
function mount(el: React.ReactElement, node: unknown): ReturnType<typeof create> {
  let r!: ReturnType<typeof create>;
  act(() => {
    r = create(el, { createNodeMock: () => node });
  });
  mounted.push(r);
  return r;
}
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

// ---- the read -------------------------------------------------------------

test("only the exact stamp reads as ready", () => {
  expect(isChatDocReady(doc({ chatReady: "1" }))).toBe(true);
  expect(isChatDocReady(doc({}))).toBe(false);
  // Anything else on the attribute is a template that has not finished (or one
  // saying something this host does not understand): not ready either way.
  expect(isChatDocReady(doc({ chatReady: "" }))).toBe(false);
  expect(isChatDocReady(doc({ chatReady: "0" }))).toBe(false);
  expect(isChatDocReady(doc({ chatReady: "true" }))).toBe(false);
});

test("no document is NOT ready — the cross-origin case is decided on load, not here", () => {
  expect(isChatDocReady(null)).toBe(false);
  expect(isChatDocReady(undefined)).toBe(false);
  // A document mid-teardown, whose documentElement is already gone.
  expect(isChatDocReady({} as Document)).toBe(false);
});

test("a document that throws on read answers false rather than propagating", () => {
  const hostile = {
    get documentElement(): never {
      throw new Error("gone");
    },
  } as unknown as Document;
  expect(isChatDocReady(hostile)).toBe(false);
});

// ---- the reveal -----------------------------------------------------------

test("an unready frame is mounted invisible under the cover, with the caller's class on it", () => {
  const node = frameNode(doc({}));
  const r = mount(<ChatFrame src="/render?a=1" title="Chat" className="task-card-frame" />, node);
  const tree = r.toJSON() as ReactTestRendererJSON;
  const iframe = find(tree, "chat-frame-iframe");
  expect(iframe?.type).toBe("iframe");
  // The consumer's own class rides the iframe — that is what keeps the card's
  // scaled fit working — and `is-ready` is absent, so the CSS holds opacity 0.
  expect(String(iframe?.props.className).split(" ")).toContain("task-card-frame");
  expect(String(iframe?.props.className).split(" ")).not.toContain("is-ready");
  const cover = find(tree, "chat-frame-placeholder");
  expect(cover?.props["aria-busy"]).toBe("true");
  expect(cover?.props.role).toBe("status");
  expect(String(cover?.props.className).split(" ")).not.toContain("is-out");
});

test("a document already carrying the stamp at mount is revealed without waiting for load", () => {
  // The warm case: the frame finished (and painted) before this component's
  // effect ran. Nothing will fire a `load` for it any more, so the mount-time
  // read is the only thing that can uncover it.
  const node = frameNode(doc({ chatReady: "1" }));
  const r = mount(<ChatFrame src="/render?a=1" title="Chat" />, node);
  const tree = r.toJSON() as ReactTestRendererJSON;
  expect(String(find(tree, "chat-frame-iframe")?.props.className).split(" ")).toContain("is-ready");
  // Still mounted, now fading: the cover leaves in two beats so the crossfade
  // has something to cross with.
  expect(String(find(tree, "chat-frame-placeholder")?.props.className).split(" ")).toContain(
    "is-out",
  );
});

test("load with a readable-but-unready document keeps the cover; the stamp then reveals", () => {
  const d = doc({}) as Document & { documentElement: { dataset: Record<string, string> } };
  const node = frameNode(d);
  const r = mount(<ChatFrame src="/render?a=1" title="Chat" />, node);
  act(() => node.fire("load"));
  expect(
    String(find(r.toJSON() as ReactTestRendererJSON, "chat-frame-iframe")?.props.className),
  ).not.toContain("is-ready");
  // The template finishes and stamps; in a browser the observer catches this,
  // and a second `load` (a re-navigation of the same frame) must too.
  d.documentElement.dataset.chatReady = "1";
  act(() => node.fire("load"));
  expect(
    String(find(r.toJSON() as ReactTestRendererJSON, "chat-frame-iframe")?.props.className),
  ).toContain("is-ready");
});

test("load with no readable document reveals at once — a cross-origin frame never stamps", () => {
  const node = frameNode(null);
  const r = mount(<ChatFrame src="https://elsewhere/x" title="Chat" />, node);
  act(() => node.fire("load"));
  expect(
    String(find(r.toJSON() as ReactTestRendererJSON, "chat-frame-iframe")?.props.className),
  ).toContain("is-ready");
});

test("a new src starts a new wait — the previous document's answer does not uncover it", () => {
  // One iframe element, as React keeps it across a src change, navigated to a
  // second conversation: the element is the same, the document is not.
  const node = frameNode(doc({ chatReady: "1" }));
  const r = mount(<ChatFrame src="/render?s=1" title="Chat" />, node);
  expect(
    String(find(r.toJSON() as ReactTestRendererJSON, "chat-frame-iframe")?.props.className),
  ).toContain("is-ready");
  node.contentDocument = doc({});
  act(() => {
    r.update(<ChatFrame src="/render?s=2" title="Chat" />);
  });
  const tree = r.toJSON() as ReactTestRendererJSON;
  expect(String(find(tree, "chat-frame-iframe")?.props.className)).not.toContain("is-ready");
  // And the cover is back over it, not left behind by the previous reveal.
  expect(find(tree, "chat-frame-placeholder")).toBeDefined();
});

test("frameRef reaches the iframe element itself, not the box around it", () => {
  // TaskPeek attaches an Esc listener to the framed document and hands this ref
  // to the modal chassis as its initial focus — both want the iframe.
  const node = frameNode(doc({}));
  const ref = { current: null } as React.MutableRefObject<HTMLIFrameElement | null>;
  mount(<ChatFrame src="/render?a=1" title="Chat" frameRef={ref} />, node);
  expect(ref.current).toBe(node as unknown as HTMLIFrameElement);
});

test("the standalone placeholder is the same skeleton, in its own box and with no frame", () => {
  const tree = create(<ChatFramePlaceholder />).toJSON() as ReactTestRendererJSON;
  expect(String(tree.props.className).split(" ")).toContain("chat-frame");
  expect(find(tree, "chat-frame-placeholder")?.props["aria-label"]).toBe("Loading chat");
  expect(find(tree, "chat-frame-iframe")).toBeUndefined();
  // The template's own two turns: the user pill, then the avatar and its lines.
  expect(find(tree, "chat-frame-skel-user")).toBeDefined();
  expect(find(tree, "chat-frame-skel-assistant")).toBeDefined();
});

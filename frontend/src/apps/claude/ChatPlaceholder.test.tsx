// The one skeleton every chat wait shows (ChatMount's Suspense, the tasks
// wall's resolving tiles).
import { expect, test } from "bun:test";
import { create, type ReactTestRendererJSON } from "react-test-renderer";

import { ChatFramePlaceholder } from "./ChatPlaceholder";

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

test("the placeholder is the two-turn skeleton, in its own box", () => {
  const tree = create(<ChatFramePlaceholder />).toJSON() as ReactTestRendererJSON;
  expect(String(tree.props.className).split(" ")).toContain("chat-frame");
  expect(find(tree, "chat-frame-placeholder")?.props["aria-label"]).toBe("Loading chat");
  // One user pill, then the avatar and its lines.
  expect(find(tree, "chat-frame-skel-user")).toBeDefined();
  expect(find(tree, "chat-frame-skel-assistant")).toBeDefined();
});

test("a host class rides the box beside `chat-frame`", () => {
  const tree = create(<ChatFramePlaceholder className="x" />).toJSON() as ReactTestRendererJSON;
  expect(String(tree.props.className).split(" ")).toEqual(["chat-frame", "x"]);
});

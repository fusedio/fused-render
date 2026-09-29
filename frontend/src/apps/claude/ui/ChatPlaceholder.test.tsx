// The skeleton every chat embed shows while there is no transcript on screen
// yet. One test, because there is one claim: the same two turns, in a box of
// its own, whatever the host that draws it.
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

test("the placeholder is a skeleton of two turns, in its own box", () => {
  const tree = create(<ChatFramePlaceholder />).toJSON() as ReactTestRendererJSON;
  expect(String(tree.props.className).split(" ")).toContain("chat-frame");
  expect(find(tree, "chat-frame-placeholder")?.props["aria-label"]).toBe("Loading chat");
  // Two turns: the user pill, then the avatar and its lines.
  expect(find(tree, "chat-frame-skel-user")).toBeDefined();
  expect(find(tree, "chat-frame-skel-assistant")).toBeDefined();
});

test("a host class rides the box, for a host that has already sized it", () => {
  const tree = create(
    <ChatFramePlaceholder className="task-card-frame" />,
  ).toJSON() as ReactTestRendererJSON;
  expect(String(tree.props.className).split(" ")).toContain("chat-frame");
  expect(String(tree.props.className).split(" ")).toContain("task-card-frame");
});

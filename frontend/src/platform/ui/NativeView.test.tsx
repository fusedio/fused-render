// NativeView mounts a registered native mode's component, and gives each FILE
// its own instance: state a view holds for one file must not follow the pane
// to the next (the node view's frame id is scoped server-side to one node).
import { expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import { createElement, useRef } from "react";
import { NativeView, registerNativeView, type NativeViewProps } from "./NativeView";

let minted = 0;
function Probe({ path }: NativeViewProps) {
  const id = useRef(++minted).current;
  return createElement("span", null, `${path}#${id}`);
}

test("a native view is remounted when its pane moves to another file", async () => {
  registerNativeView("probe", async () => ({ default: Probe }));
  const at = (path: string) =>
    createElement(NativeView, { mode: "probe", path, placement: "content" as const });
  let tree!: ReturnType<typeof create>;
  await act(async () => {
    tree = create(at("/c/udf_1.py"));
  });
  await act(async () => {});
  const text = () => JSON.stringify(tree.toJSON());
  // The id is whatever render minted it (a suspended render may spend one):
  // what matters is whether it CHANGES.
  const idOf = (path: string) => {
    const m = new RegExp(`${path.replace(/[./]/g, "\\$&")}#(\\d+)`).exec(text());
    expect(m).not.toBeNull();
    return m![1];
  };
  const first = idOf("/c/udf_1.py");
  await act(async () => {
    tree.update(at("/c/udf_1.py"));
  });
  expect(idOf("/c/udf_1.py")).toBe(first); // same file: same instance
  await act(async () => {
    tree.update(at("/c/udf_2.py"));
  });
  expect(idOf("/c/udf_2.py")).not.toBe(first); // another file: a new one
});

test("an unregistered mode renders the fallback, never a view", () => {
  const tree = create(
    createElement(NativeView, { mode: "nobody", path: "/x", placement: "content" as const, fallback: "none" }),
  );
  expect(JSON.stringify(tree.toJSON())).toBe(JSON.stringify("none"));
});

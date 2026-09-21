// `onExplain` (SPEC-doctor-git-ai-errors.md Part A): opt-in per call site,
// since ErrorBanner itself has no idea whether its children describe a
// system/runtime error (gets the action) or a plain input-validation
// message (never gets it). This file only tests ErrorBanner's own
// rendering contract — whether a given call site SHOULD pass `onExplain`
// is that call site's own decision, tested there.
import { expect, test } from "bun:test";
import { act, create, type ReactTestRendererJSON } from "react-test-renderer";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

function findAll(
  node: ReactTestRendererJSON | null,
  match: (n: ReactTestRendererJSON) => boolean,
): ReactTestRendererJSON[] {
  if (node === null || typeof node === "string") return [];
  const hits: ReactTestRendererJSON[] = [];
  if (match(node)) hits.push(node);
  for (const child of node.children ?? []) {
    if (typeof child !== "string") hits.push(...findAll(child, match));
  }
  return hits;
}

function text(node: ReactTestRendererJSON): string {
  return (node.children ?? []).filter((c): c is string => typeof c === "string").join("");
}

test("no onExplain: renders the error with no explain action", async () => {
  let renderer: ReturnType<typeof create> | null = null;
  await act(async () => {
    renderer = create(<ErrorBanner>Could not save the file.</ErrorBanner>);
  });
  const tree = renderer!.toJSON() as ReactTestRendererJSON;
  expect(findAll(tree, (n) => typeof n.props?.onClick === "function")).toHaveLength(0);
  await act(async () => {
    renderer!.unmount();
  });
});

test("onExplain given: renders an action that calls it, without altering the error text", async () => {
  let explained = 0;
  let renderer: ReturnType<typeof create> | null = null;
  await act(async () => {
    renderer = create(
      <ErrorBanner onExplain={() => (explained += 1)}>Could not save the file.</ErrorBanner>,
    );
  });
  const tree = renderer!.toJSON() as ReactTestRendererJSON;
  const clickable = findAll(tree, (n) => typeof n.props?.onClick === "function");
  expect(clickable).toHaveLength(1);
  const action = clickable[0];
  expect(text(action).toLowerCase()).toContain("explain");

  (action.props as { onClick: () => void }).onClick();
  expect(explained).toBe(1);

  await act(async () => {
    renderer!.unmount();
  });
});

test("null/false children still render nothing, onExplain or not", async () => {
  let renderer: ReturnType<typeof create> | null = null;
  await act(async () => {
    renderer = create(<ErrorBanner onExplain={() => {}}>{false}</ErrorBanner>);
  });
  expect(renderer!.toJSON()).toBeNull();
  await act(async () => {
    renderer!.unmount();
  });
});

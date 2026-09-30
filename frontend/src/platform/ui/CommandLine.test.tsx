// CommandLine.tsx's own rendering/behaviour contract: the Copy button always
// shows, the Run button only where `useCanRunInTerminal()` says there is a
// drawer to hand the command to, Run opens the drawer with the command, and
// `disabled` keeps Run from being pressed while the row's own action is busy.
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";

import { CommandLine } from "@platform/ui/CommandLine";
import {
  peekPendingTerminalRequest,
  registerTerminalDrawerMounted,
  resetTerminalDockForTests,
  useTerminalDockOpen,
} from "@platform/lib/terminalDockStore";

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
});

test("always renders the command and a Copy button", () => {
  const renderer = renderTracked(<CommandLine command="claude update" />);
  const tree = renderer.toJSON() as ReactTestRendererJSON;
  expect(findAll(tree, (n) => n.type === "code").map(text)).toEqual(["claude update"]);
  const buttons = findAll(tree, (n) => n.type === "button");
  expect(buttons.map(text)).toContain("Copy");
});

test("no Run button when no drawer is mounted", () => {
  const renderer = renderTracked(<CommandLine command="claude update" />);
  const tree = renderer.toJSON() as ReactTestRendererJSON;
  const buttons = findAll(tree, (n) => n.type === "button");
  expect(buttons.map(text)).not.toContain("Run");
});

describe("with a terminal drawer mounted", () => {
  test("shows a Run button that opens the drawer with the command", () => {
    let unregister!: () => void;
    act(() => {
      unregister = registerTerminalDrawerMounted();
    });
    const renderer = renderTracked(<CommandLine command="claude update" />);
    const tree = renderer.toJSON() as ReactTestRendererJSON;
    const run = findAll(tree, (n) => n.type === "button" && text(n) === "Run")[0];
    expect(run).toBeDefined();

    act(() => {
      (run.props as { onClick: () => void }).onClick();
    });
    expect(peekPendingTerminalRequest()).toEqual({ command: "claude update" });
    act(() => unregister());
  });

  test("Run also opens the drawer itself", () => {
    let unregister!: () => void;
    act(() => {
      unregister = registerTerminalDrawerMounted();
    });
    let open = false;
    function Probe() {
      open = useTerminalDockOpen();
      return null;
    }
    const probeRenderer = renderTracked(<Probe />);
    const renderer = renderTracked(<CommandLine command="claude update" />);
    const tree = renderer.toJSON() as ReactTestRendererJSON;
    const run = findAll(tree, (n) => n.type === "button" && text(n) === "Run")[0];
    act(() => {
      (run.props as { onClick: () => void }).onClick();
    });
    expect(open).toBe(true);
    void probeRenderer;
    act(() => unregister());
  });

  test("Run is disabled while the row's own action is busy", () => {
    let unregister!: () => void;
    act(() => {
      unregister = registerTerminalDrawerMounted();
    });
    const renderer = renderTracked(<CommandLine command="claude update" disabled />);
    const tree = renderer.toJSON() as ReactTestRendererJSON;
    const run = findAll(tree, (n) => n.type === "button" && text(n) === "Run")[0];
    expect(run).toBeDefined();
    expect((run.props as { disabled?: boolean }).disabled).toBe(true);
    act(() => unregister());
  });
});

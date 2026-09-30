// CommandLine.tsx's own rendering/behaviour contract: the Copy button always
// shows, the Run button only where `canRunInTerminal()` says there is a
// drawer to hand the command to, and Run opens the drawer with exactly the
// command/cwd/execute it was given.
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";

import { CommandLine } from "@platform/ui/CommandLine";
import {
  canRunInTerminal,
  peekPendingTerminalRequest,
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

describe("when canRunInTerminal() is true", () => {
  test.if(canRunInTerminal())(
    "shows a Run button that opens the drawer with the command",
    () => {
      const renderer = renderTracked(<CommandLine command="claude update" cwd="/tmp/x" />);
      const tree = renderer.toJSON() as ReactTestRendererJSON;
      const run = findAll(tree, (n) => n.type === "button" && text(n) === "Run")[0];
      expect(run).toBeDefined();

      act(() => {
        (run.props as { onClick: () => void }).onClick();
      });
      expect(peekPendingTerminalRequest()).toEqual({
        cwd: "/tmp/x",
        command: "claude update",
        execute: true,
      });
    },
  );

  test.if(canRunInTerminal())("Run with execute={false} types without running", () => {
    const renderer = renderTracked(<CommandLine command="claude update" execute={false} />);
    const tree = renderer.toJSON() as ReactTestRendererJSON;
    const run = findAll(tree, (n) => n.type === "button" && text(n) === "Run")[0];
    act(() => {
      (run.props as { onClick: () => void }).onClick();
    });
    expect(peekPendingTerminalRequest()).toEqual({
      cwd: undefined,
      command: "claude update",
      execute: false,
    });
  });

  test.if(canRunInTerminal())("Run calls onRun once the request is handed to the drawer", () => {
    let ran = 0;
    const renderer = renderTracked(
      <CommandLine command="claude update" onRun={() => (ran += 1)} />,
    );
    const tree = renderer.toJSON() as ReactTestRendererJSON;
    const run = findAll(tree, (n) => n.type === "button" && text(n) === "Run")[0];
    act(() => {
      (run.props as { onClick: () => void }).onClick();
    });
    expect(ran).toBe(1);
  });

  test.if(canRunInTerminal())("Run also opens the drawer itself", () => {
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
  });
});

test.if(!canRunInTerminal())("no Run button when canRunInTerminal() is false", () => {
  const renderer = renderTracked(<CommandLine command="claude update" />);
  const tree = renderer.toJSON() as ReactTestRendererJSON;
  const buttons = findAll(tree, (n) => n.type === "button");
  expect(buttons.map(text)).not.toContain("Run");
});

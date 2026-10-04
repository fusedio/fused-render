// The "Open in Terminal" row this hook contributes to both the folder menu
// and the file menu's `open` group (bar-menus.ts) — the one piece of
// useAppActionRows worth a dedicated test, since it is a plain function of
// `fsPath` (via `dir`) with no probe of its own to mock out. `isEntry: false`
// and `snapshotSha: null` below keep every OTHER effect in this hook
// (getAppEntry, useAppDoctorChecks, useAppVersionLabel) from firing a fetch
// this file has no server behind — none of that machinery bears on the
// terminal row, which reads only `dir`/`name` and `IS_EMBED`.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";

import { useAppActionRows, type AppActionRows } from "@apps/explorer/EntryActionsMenu";
import {
  registerTerminalDrawerMounted,
  resetTerminalDockForTests,
  peekPendingTerminalRequest,
} from "@platform/lib/terminalDockStore";

let renderers: ReactTestRenderer[] = [];
let unregisterDrawer: () => void = () => {};
afterEach(() => {
  for (const renderer of renderers) renderer.unmount();
  renderers = [];
  unregisterDrawer();
  resetTerminalDockForTests();
});

function renderRows(fsPath: string): AppActionRows {
  unregisterDrawer = registerTerminalDrawerMounted();
  let rows!: AppActionRows;
  function Probe() {
    rows = useAppActionRows({ fsPath, isEntry: false, onOpenMcp: () => {} });
    return null;
  }
  act(() => {
    renderers.push(create(createElement(Probe)));
  });
  return rows;
}

describe("useAppActionRows — Open in Terminal", () => {
  test("a folder listing's row (fsPath = <folder>/index.html) opens the folder itself", () => {
    const rows = renderRows("/repo/myapp/index.html");
    expect(rows.terminal).toHaveLength(1);
    expect(rows.terminal[0]).toMatchObject({ label: "Open in Terminal" });
    (rows.terminal[0] as { onClick: () => void }).onClick();
    expect(peekPendingTerminalRequest()).toEqual({ cwd: "/repo/myapp" });
  });

  test("a file preview's row opens the file's PARENT directory, not the file", () => {
    const rows = renderRows("/repo/myapp/notes.txt");
    expect(rows.terminal).toHaveLength(1);
    (rows.terminal[0] as { onClick: () => void }).onClick();
    expect(peekPendingTerminalRequest()).toEqual({ cwd: "/repo/myapp" });
  });
});

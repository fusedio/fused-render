import { expect, test } from "bun:test";

import { initialOscState, isAskSelectionChord, reduceShellOsc, selectionPillTop } from "@platform/lib/terminalAi";

const run = (...steps: Array<[133 | 633, string]>) =>
  steps.reduce((s, [i, d]) => reduceShellOsc(s, i, d), initialOscState());

test("633;E records the command, decoding the shim escapes", () => {
  expect(run([633, "E;ls \\x3b echo a\\\\b\\x0ax"]).pendingCommand).toBe("ls ; echo a\\b\nx");
  expect(run([633, "E;plain"]).pendingCommand).toBe("plain");
  expect(run([633, "P;Cwd=/x"]).pendingCommand).toBe("");
});

test("nonzero exit becomes a failure with the pending command", () => {
  expect(run([633, "E;false"], [133, "C"], [133, "D;1"]).failure).toEqual({ command: "false", exitCode: 1 });
});

test("exit 0, 130 and a missing exit clear the failure", () => {
  const failed: Array<[133 | 633, string]> = [[633, "E;x"], [133, "C"], [133, "D;2"]];
  expect(run(...failed, [133, "D;0"]).failure).toBeNull();
  expect(run(...failed, [133, "D;130"]).failure).toBeNull();
  expect(run(...failed, [133, "D"]).failure).toBeNull();
  expect(run(...failed, [133, "D;abc"]).failure).toBeNull();
});

test("133;C clears a failure; other marks leave state unchanged", () => {
  const failed: Array<[133 | 633, string]> = [[633, "E;x"], [133, "D;2"]];
  expect(run(...failed, [133, "C"]).failure).toBeNull();
  const s = run(...failed);
  expect(reduceShellOsc(s, 133, "A")).toBe(s);
});

const base = { viewportY: 100, rows: 10, surfaceHeight: 200, pillHeight: 22 };
test("pill sits under the selection end row", () => {
  expect(selectionPillTop({ ...base, endRow: 102 })).toBe(3 * 20 + 2);
});
test("pill is null when the end row is scrolled out", () => {
  expect(selectionPillTop({ ...base, endRow: 99 })).toBeNull();
  expect(selectionPillTop({ ...base, endRow: 110 })).toBeNull();
});
test("pill clamps to the bottom edge", () => {
  expect(selectionPillTop({ ...base, endRow: 109 })).toBe(178);
});

const ev = (o: Partial<Record<string, unknown>>) =>
  ({ key: "l", metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, type: "keydown", ...o }) as never;
test("ask chord per platform", () => {
  expect(isAskSelectionChord(ev({ metaKey: true }), true)).toBe(true);
  expect(isAskSelectionChord(ev({ metaKey: true, key: "L" }), true)).toBe(true);
  expect(isAskSelectionChord(ev({ ctrlKey: true, shiftKey: true }), false)).toBe(true);
  expect(isAskSelectionChord(ev({ ctrlKey: true }), false)).toBe(false);
  expect(isAskSelectionChord(ev({ metaKey: true, type: "keyup" }), true)).toBe(false);
  expect(isAskSelectionChord(ev({ metaKey: true, shiftKey: true }), true)).toBe(false);
  expect(isAskSelectionChord(ev({ ctrlKey: true, shiftKey: true }), true)).toBe(false);
});

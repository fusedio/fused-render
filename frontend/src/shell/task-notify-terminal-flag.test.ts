// The flag's own contract — same idiom check as task-peek-flag.test.ts's
// "the flag module" describe block. The DECISION this flag gates (whether a
// terminal session's finished-task notice fires) is tested in
// task-status-notify.test.ts, since `notificationForTransition` is pure and
// takes the flag's value as a plain argument rather than reading this module
// itself.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "bun:test";

const SHELL = new URL(".", import.meta.url).pathname;
const FLAG = readFileSync(join(SHELL, "task-notify-terminal-flag.ts"), "utf8");

describe("the flag module", () => {
  it("is the card-title flag's idiom, down to the tri-state", () => {
    expect(FLAG).toContain("export function useTaskNotifyTerminalSessionsFlag(): boolean | null");
    expect(FLAG).toContain("export function useTaskNotifyTerminalSessions(): boolean");
    expect(FLAG).toContain("export function publishTaskNotifyTerminalSessions");
    expect(FLAG).toContain("export function resetTaskNotifyTerminalSessionsForTests");
    // One shared read, a generation guard, and a bounded retry — the three
    // things that make two mounts cost one GET.
    expect(FLAG).toContain("let reading: Promise<void> | null = null;");
    expect(FLAG).toContain("let generation = 0;");
    expect(FLAG).toContain(".catch(() => getPrefs())");
  });

  it("reads the pref strictly: only a stored true is on", () => {
    expect(FLAG).toContain("p.task_notify?.terminal_sessions === true");
  });

  it("settles a failed read on OFF rather than leaving it unknown", () => {
    expect(FLAG).toContain("if (generation === departed) set(false);");
  });
});

describe("useTaskNotifyTerminalSessionsFlag / useTaskNotifyTerminalSessions", () => {
  it("starts at null (not asked) and flattens null to false", async () => {
    const {
      resetTaskNotifyTerminalSessionsForTests,
      taskNotifyTerminalSessionsNow,
      publishTaskNotifyTerminalSessions,
    } = await import("./task-notify-terminal-flag");
    resetTaskNotifyTerminalSessionsForTests();
    expect(taskNotifyTerminalSessionsNow()).toBeNull();
    publishTaskNotifyTerminalSessions(true);
    expect(taskNotifyTerminalSessionsNow()).toBe(true);
    publishTaskNotifyTerminalSessions(false);
    expect(taskNotifyTerminalSessionsNow()).toBe(false);
    resetTaskNotifyTerminalSessionsForTests();
  });
});

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
const FLAG = readFileSync(join(SHELL, "project-peek-flag.ts"), "utf8");

describe("the flag module", () => {
  it("is the terminal-sessions flag's idiom, down to the tri-state", () => {
    expect(FLAG).toContain("export function useProjectPeekFlag(): boolean | null");
    expect(FLAG).toContain("export function useProjectPeekEnabled(): boolean");
    expect(FLAG).toContain("export function publishProjectPeekEnabled");
    expect(FLAG).toContain("export function resetProjectPeekFlagForTests");
    // One shared read, a generation guard, and a bounded retry — the three
    // things that make two mounts cost one GET.
    expect(FLAG).toContain("let reading: Promise<void> | null = null;");
    expect(FLAG).toContain("let generation = 0;");
    expect(FLAG).toContain(".catch(() => getPrefs())");
  });

  it("reads the pref strictly: only a stored true is on", () => {
    expect(FLAG).toContain("p.task_peek?.project === true");
  });

  it("settles a failed read on OFF rather than leaving it unknown", () => {
    expect(FLAG).toContain("if (generation === departed) set(false);");
  });
});

describe("useProjectPeekFlag / useProjectPeekEnabled", () => {
  it("starts at null (not asked) and flattens null to false", async () => {
    const {
      resetProjectPeekFlagForTests,
      projectPeekEnabledNow,
      publishProjectPeekEnabled,
    } = await import("./project-peek-flag");
    resetProjectPeekFlagForTests();
    expect(projectPeekEnabledNow()).toBeNull();
    publishProjectPeekEnabled(true);
    expect(projectPeekEnabledNow()).toBe(true);
    publishProjectPeekEnabled(false);
    expect(projectPeekEnabledNow()).toBe(false);
    resetProjectPeekFlagForTests();
  });
});

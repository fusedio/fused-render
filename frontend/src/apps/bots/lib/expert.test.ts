import { describe, expect, test } from "bun:test";
import type { ExpertSession } from "./api";
import { changedMemory, costLabel, diffLines, diffStat, duration, filterSessions, kindLabel, memoryUsage, oneLine, originOf, statusOf } from "./expert";

const row = (p: Partial<ExpertSession>): ExpertSession => ({ id: "s", kind: "ask", started: 100, ended: 112, status: "done", memory_diff: "", ...p });
const DIFF = "--- a/memory.md\n+++ b/memory.md\n@@ -1,2 +1,2 @@\n ## Works\n-- [1 · 2026-11-16] Pull first.\n+- [2 · 2026-11-17] Pull first.\n+- [1 · 2026-11-17] New fact.\n";

describe("diffLines / diffStat", () => {
  test("each line gets its tone; the file headers are meta, not counted", () => {
    const l = diffLines(DIFF);
    expect(l.map((x) => x.tone)).toEqual(["meta", "meta", "hunk", "ctx", "del", "add", "add"]);
    expect(l[4].text).toBe("-- [1 · 2026-11-16] Pull first.");
    expect(diffStat(DIFF)).toBe("+2 −1");
  });
  test("an empty diff is no lines and no stat", () => {
    expect(diffLines("")).toEqual([]);
    expect(diffLines("  \n")).toEqual([]);
    expect(diffLines(undefined)).toEqual([]);
    expect(diffStat("")).toBe("");
  });
});

describe("originOf", () => {
  test("one line per kind, from the trigger", () => {
    expect(originOf("chat", { message: "Remember:\n  pull first" })).toBe("“Remember: pull first”");
    expect(originOf("chat", {})).toBe("your chat");
    expect(originOf("ask", { asker: "Claude Code task", question: "How do I share a canvas?" })).toBe("Claude Code task: How do I share a canvas?");
    expect(originOf("ask", { question: "Why?" })).toBe("Why?");
    expect(originOf("task-learn", { target: "/Users/a/Fused/canvas/maps/", backend: "claude" })).toBe("task in maps (claude)");
    expect(originOf("task-learn", { backend: "codex" })).toBe("codex task");
    expect(originOf("task-learn", null)).toBe("a task");
    expect(originOf("compact", { lines: 230, bytes: 14336 })).toBe("memory at 230 lines, 14 KB");
    expect(originOf("compact", {})).toBe("memory over the limit");
    expect(originOf("other", { message: "x" })).toBe("");
  });
  test("long text is cut to one line", () => {
    const o = originOf("chat", { message: "x".repeat(200) });
    expect(o.length).toBeLessThan(80);
    expect(o).toContain("…");
    expect(oneLine("a  b\n c", 80)).toBe("a b c");
  });
});

describe("rows", () => {
  test("kind and status words", () => {
    expect(kindLabel("task-learn")).toBe("task-learn");
    expect(kindLabel("chat")).toBe("chat");
    expect(kindLabel("")).toBe("session");
    expect(statusOf({ status: "running" })).toEqual({ text: "running", tone: "accent" });
    expect(statusOf({ status: "error" }).tone).toBe("err");
    expect(statusOf({ status: "interrupted" }).tone).toBe("warn");
    expect(statusOf({ status: "done" }).tone).toBe("ok");
  });
  test("changed memory = a non-empty diff; the filter keeps those, newest first", () => {
    const rows = [row({ id: "a", started: 1, memory_diff: DIFF }), row({ id: "b", started: 3 }), row({ id: "c", started: 2, memory_diff: DIFF })];
    expect(changedMemory(rows[1])).toBe(false);
    expect(changedMemory(row({ memory_diff: "\n" }))).toBe(false);
    expect(filterSessions(rows, "all").map((s) => s.id)).toEqual(["b", "c", "a"]);
    expect(filterSessions(rows, "changed").map((s) => s.id)).toEqual(["c", "a"]);
  });
  test("duration and cost", () => {
    expect(duration(row({}))).toBe("12 s");
    expect(duration(row({ ended: null }))).toBe("");
    expect(costLabel(0.034)).toBe(" · $0.03");
    expect(costLabel(0.004)).toBe(" · $0.004");
    expect(costLabel(undefined)).toBe("");
    expect(costLabel(0)).toBe("");
  });
});

describe("memoryUsage", () => {
  test("lines and bytes against the limit; over when either passes it", () => {
    expect(memoryUsage({ lines: 42, bytes: 3072, limit_lines: 200, limit_bytes: 12288 })).toEqual({ text: "42 of 200 lines · 3 KB of 12 KB", over: false });
    expect(memoryUsage({ lines: 201, bytes: 3072, limit_lines: 200, limit_bytes: 12288 }).over).toBe(true);
    expect(memoryUsage({ lines: 10, bytes: 13000, limit_lines: 200, limit_bytes: 12288 }).over).toBe(true);
    expect(memoryUsage({ lines: 1, bytes: 10, limit_lines: 0, limit_bytes: 0 })).toEqual({ text: "1 line · 10 B", over: false });
  });
});

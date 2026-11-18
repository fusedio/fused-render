import { describe, expect, test } from "bun:test";
import type { ExpertSession } from "./api";
import { agoShort, changedMemory, diffDoc, duration, kindLabel, memoryUsage, newestFirst, oneLine, originOf, settingsWords, skillFormWords, statusOf, turnIsMarkdown } from "./expert";

const row = (p: Partial<ExpertSession>): ExpertSession => ({ id: "s", kind: "ask", started: 100, ended: 112, status: "done", memory_diff: "", ...p });
const DIFF = "--- a/memory.md\n+++ b/memory.md\n@@ -1,2 +1,2 @@\n ## Works\n-- [1 · 2026-11-16] Pull first.\n+- [2 · 2026-11-17] Pull first.\n+- [1 · 2026-11-17] New fact.\n";

describe("diffDoc", () => {
  test("the file headers and hunk header go; a one-for-one removed + added is a pair; a lone added line stays", () => {
    expect(diffDoc(DIFF)).toEqual([
      { tone: "ctx", text: "## Works" },
      { tone: "pair", del: "- [1 · 2026-11-16] Pull first.", add: "- [2 · 2026-11-17] Pull first." },
      { tone: "add", text: "- [1 · 2026-11-17] New fact." },
    ]);
  });
  test("added, removed and context lines; a removed run with no added run after it stays removed", () => {
    const d = "@@ -1,4 +1,3 @@\n ## Works\n-- [1 · 2026-11-01] Old.\n-- [1 · 2026-11-02] Older.\n \n ## Does not work\n+- [1 · 2026-11-17] Force push.\n\\ No newline at end of file\n";
    expect(diffDoc(d).map((r) => r.tone)).toEqual(["ctx", "del", "del", "ctx", "ctx", "add"]);
  });
  test("longer runs pair by fact (a bump next to a new fact); different facts stay apart", () => {
    const bumps = "@@ -1,2 +1,2 @@\n-- [1 · 2026-11-01] A.\n-- [3 · 2026-11-01] B `x`.\n+- [2 · 2026-11-17] A.\n+- [4 · 2026-11-17] B `x`.\n";
    expect(diffDoc(bumps).map((r) => r.tone)).toEqual(["pair", "pair"]);
    const rewrite = "@@ -1,2 +1,2 @@\n-- [1 · 2026-11-01] A.\n-- [1 · 2026-11-01] B.\n+- [1 · 2026-11-17] C.\n+- [1 · 2026-11-17] D.\n";
    expect(diffDoc(rewrite).map((r) => r.tone)).toEqual(["del", "del", "add", "add"]);
    expect(diffDoc("@@ -1 +1,2 @@\n-a\n+b\n+c\n").map((r) => r.tone)).toEqual(["del", "add", "add"]);
    const mixed = "@@ -1,2 +1,3 @@\n-- [1 · 2026-11-01] Gone.\n-- [1 · 2026-11-01] Kept.\n+- [1 · 2026-11-17] New.\n+- [2 · 2026-11-17] Kept.\n";
    expect(diffDoc(mixed)).toEqual([{ tone: "pair", del: "- [1 · 2026-11-01] Kept.", add: "- [2 · 2026-11-17] Kept." },
      { tone: "del", text: "- [1 · 2026-11-01] Gone." }, { tone: "add", text: "- [1 · 2026-11-17] New." }]);
  });
  test("inside a hunk the first character decides: a removed `---` and an added `+++` are lines, not headers", () => {
    const d = "--- a/memory.md\n+++ b/memory.md\n@@ -1,3 +1,3 @@\n ## Works\n----\n+++++\n";
    expect(diffDoc(d)).toEqual([{ tone: "ctx", text: "## Works" }, { tone: "pair", del: "---", add: "++++" }]);
    expect(diffDoc("@@ -1 +0,0 @@\n----\n")).toEqual([{ tone: "del", text: "---" }]);
    expect(diffDoc("@@ -0,0 +1 @@\n++++\n")).toEqual([{ tone: "add", text: "+++" }]);
  });
  test("a second hunk starts after a gap; a headerless diff still reads", () => {
    expect(diffDoc("@@ -1 +1 @@\n a\n@@ -9 +9 @@\n+b\n").map((r) => r.tone)).toEqual(["ctx", "gap", "add"]);
    expect(diffDoc("+x\n y\n")).toEqual([{ tone: "add", text: "x" }, { tone: "ctx", text: "y" }]);
  });
  test("an empty diff is no rows", () => {
    expect(diffDoc("")).toEqual([]);
    expect(diffDoc("  \n")).toEqual([]);
    expect(diffDoc(undefined)).toEqual([]);
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
    expect(originOf("dream", { lines: 230, bytes: 14336 })).toBe("memory at 230 lines, 14 KB");
    expect(originOf("dream", {})).toBe("memory over the limit");
    expect(originOf("compact", { lines: 230 })).toBe("");  // the old name is gone
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
    expect(kindLabel("dream")).toBe("dream");
    expect(kindLabel("")).toBe("session");
    expect(statusOf({ status: "running" })).toEqual({ text: "running", tone: "accent" });
    expect(statusOf({ status: "error" }).tone).toBe("err");
    expect(statusOf({ status: "interrupted" }).tone).toBe("warn");
    expect(statusOf({ status: "done" }).tone).toBe("ok");
  });
  test("changed memory = a non-empty diff; rows newest first", () => {
    const rows = [row({ id: "a", started: 1, memory_diff: DIFF }), row({ id: "b", started: 3 }), row({ id: "c", started: 2, memory_diff: DIFF })];
    expect(changedMemory(rows[1])).toBe(false);
    expect(changedMemory(row({ memory_diff: "\n" }))).toBe(false);
    expect(changedMemory(rows[0])).toBe(true);
    expect(newestFirst(rows).map((s) => s.id)).toEqual(["b", "c", "a"]);
    expect(rows.map((s) => s.id)).toEqual(["a", "b", "c"]);  // not sorted in place
  });
  test("duration and cost", () => {
    expect(duration(row({}))).toBe("12s");
    expect(duration(row({ ended: null }))).toBe("");
    expect(duration(row({ started: 100, ended: 100 + 190 }))).toBe("3m");
    expect(duration(row({ started: 100, ended: 100 + 2 * 3600 }))).toBe("2h");
    const now = 1_000_000_000, older = () => "12 Nov";
    expect(agoShort(0, now, older)).toBe("");
    expect(agoShort(now / 1000 - 20, now, older)).toBe("now");
    expect(agoShort(now / 1000 - 125, now, older)).toBe("2m ago");
    expect(agoShort(now / 1000 - 3 * 3600, now, older)).toBe("3h ago");
    expect(agoShort(now / 1000 - 4 * 86400, now, older)).toBe("4d ago");
    expect(agoShort(now / 1000 - 9 * 86400, now, older)).toBe("12 Nov");
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

describe("follow-ups", () => {
  test("an added or removed blank line is left out (no empty coloured strip); blank context stays", () => {
    const d = "@@ -1,3 +1,4 @@\n ## Works\n+\n+- [1 · 2026-11-18] New.\n \n-\n ## Does not work\n";
    expect(diffDoc(d)).toEqual([
      { tone: "ctx", text: "## Works" },
      { tone: "add", text: "- [1 · 2026-11-18] New." },
      { tone: "ctx", text: "" },
      { tone: "ctx", text: "## Does not work" },
    ]);
    expect(diffDoc("@@ -1,2 +1,3 @@\n ## Works\n+\n ## Does not work\n")).toEqual([]);  // only blank lines changed
  });

  test("only the expert's own transcript text renders as markdown", () => {
    expect(turnIsMarkdown("expert")).toBe(true);
    for (const r of ["prompt", "tool", "refused", ""]) expect(turnIsMarkdown(r)).toBe(false);
  });

  test("an expert's settings speak of reference files; an ordinary bot's words are unchanged", () => {
    const x = settingsWords("expert"), b = settingsWords("bot"), s = settingsWords("super");
    expect(x.instructionsPlaceholder).toContain("Answer questions about writing canvases");
    expect(x.description).not.toMatch(/browser|playbooks|routines/);
    expect(b.instructionsPlaceholder).toBe("e.g. “Browse LinkedIn for me. Answer in bullets. Never like or comment.”");
    expect(b.description).toBe("How this bot thinks, what it may do without asking, the services it may use, its browser, its playbooks, its routines, its memory.");
    expect(s.description.endsWith(" and your phone.")).toBe(true);
    const xs = skillFormWords(true), bs = skillFormWords(false);
    expect(xs.bodyLabel).toBe("Text");
    expect(xs.triggerHint).toContain("help the expert find");
    expect(bs.bodyLabel).toBe("Steps");
    expect(bs.triggerHint).toBe("Comma-separated; any one appearing in a task mounts this playbook.");
  });
});

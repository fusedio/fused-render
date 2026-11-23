import { describe, expect, test } from "bun:test";
import { agoShort, diffDoc } from "./memdiff";

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
});

describe("agoShort", () => {
  test("now, minutes, hours, days, then the date `older` gives", () => {
    const now = 1_000_000_000, older = () => "12 Nov";
    expect(agoShort(0, now, older)).toBe("");
    expect(agoShort(now / 1000 - 20, now, older)).toBe("now");
    expect(agoShort(now / 1000 - 125, now, older)).toBe("2m ago");
    expect(agoShort(now / 1000 - 3 * 3600, now, older)).toBe("3h ago");
    expect(agoShort(now / 1000 - 4 * 86400, now, older)).toBe("4d ago");
    expect(agoShort(now / 1000 - 9 * 86400, now, older)).toBe("12 Nov");
  });
});

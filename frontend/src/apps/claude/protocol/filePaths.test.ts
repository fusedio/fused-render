import { expect, test } from "bun:test";

import { pathCandidate, resolveAgainst, touchedFiles } from "./filePaths";
import type { Segment } from "./types";

const tool = (name: string, input: Record<string, unknown>, children?: Segment[]): Segment =>
  ({ kind: "tool", id: "", name, input, status: "done", output: null, images: [], children }) as unknown as Segment;

test("touchedFiles: newest first, deduped, notebook_path, non-edit tools ignored", () => {
  const turns = [
    { segments: [tool("Write", { file_path: "/p/a.ts" }), tool("Read", { file_path: "/p/r.ts" })] },
    { segments: [tool("Edit", { file_path: "/p/b.ts" }), tool("NotebookEdit", { notebook_path: "/p/n.ipynb" })] },
    { segments: [tool("MultiEdit", { file_path: "/p/a.ts" }), tool("Bash", { command: "x" })] },
    { segments: undefined },
  ];
  expect(touchedFiles(turns)).toEqual(["/p/a.ts", "/p/n.ipynb", "/p/b.ts"]);
});

test("touchedFiles: reads subagent children too", () => {
  const turns = [{ segments: [tool("Agent", {}, [tool("Write", { file_path: "/p/c.ts" })])] }];
  expect(touchedFiles(turns)).toEqual(["/p/c.ts"]);
});

test("resolveAgainst", () => {
  expect(resolveAgainst("/a/b.ts", "/w")).toBe("/a/b.ts");
  expect(resolveAgainst("src/x.ts", "/w")).toBe("/w/src/x.ts");
  expect(resolveAgainst("./src/../x.ts", "/w/")).toBe("/w/x.ts");
  expect(resolveAgainst("x.ts", null)).toBe("x.ts");
});

test("pathCandidate", () => {
  expect(pathCandidate("src/a.ts")).toEqual({ path: "src/a.ts" });
  expect(pathCandidate("a.ts:42")).toEqual({ path: "a.ts", line: 42 });
  expect(pathCandidate("a.ts:42:7")).toEqual({ path: "a.ts", line: 42 });
  expect(pathCandidate("README.md")).toEqual({ path: "README.md" });
  expect(pathCandidate("foo")).toBeNull();
  expect(pathCandidate("two words.ts")).toBeNull();
  expect(pathCandidate("https://x.com/a.html")).toBeNull();
  expect(pathCandidate("foo()")).toBeNull();
  expect(pathCandidate("a/b|c")).toBeNull();
});

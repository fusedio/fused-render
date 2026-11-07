import { expect, test } from "bun:test";

import { localPathFromHref, pathCandidate, resolveAgainst, touchedFiles } from "./filePaths";
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
  expect(resolveAgainst("C:\\p\\a.ts", "/w")).toBe("C:\\p\\a.ts");
  expect(resolveAgainst("c:/p/a.ts", "/w")).toBe("c:/p/a.ts");
  expect(resolveAgainst("\\\\host\\share\\a.ts", "/w")).toBe("\\\\host\\share\\a.ts");
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

test("pathCandidate: a leading ~/ is a path, any other ~ is not", () => {
  expect(pathCandidate("~/a/b.html")).toEqual({ path: "~/a/b.html" });
  expect(pathCandidate("~/a.ts:3")).toEqual({ path: "~/a.ts", line: 3 });
  expect(pathCandidate("~")).toBeNull();
  expect(pathCandidate("~/")).toBeNull();
  expect(pathCandidate("a~b")).toBeNull();
  expect(pathCandidate("a/~/b.ts")).toBeNull();
  expect(pathCandidate("~user/x.ts")).toBeNull();
  expect(pathCandidate("~/a~b.ts")).toBeNull();
  expect(pathCandidate("echo ~/x")).toBeNull();
});

test("resolveAgainst: ~/ expands to home, never joins cwd", () => {
  expect(resolveAgainst("~/a/b.html", "/w", "/home/u")).toBe("/home/u/a/b.html");
  expect(resolveAgainst("~/a.html", "/w", "/home/u/")).toBe("/home/u/a.html");
  expect(resolveAgainst("~/a/b.html", "/w", "C:\\Users\\x")).toBe("C:/Users/x/a/b.html");
  expect(resolveAgainst("~/a.html", "/w", undefined)).toBe("~/a.html");
});

test("localPathFromHref: absolute, ~/ and file:// hrefs are local files, decoded", () => {
  expect(localPathFromHref("/Users/u/.claude/skills/x/SKILL.md")).toBe("/Users/u/.claude/skills/x/SKILL.md");
  expect(localPathFromHref("~/notes/a.md")).toBe("~/notes/a.md");
  expect(localPathFromHref("file:///Users/u/a%20b.md")).toBe("/Users/u/a b.md");
  expect(localPathFromHref("file://localhost/Users/u/a.md")).toBe("/Users/u/a.md");
  expect(localPathFromHref("file:///C:/x/a.md")).toBe("C:/x/a.md");
  expect(localPathFromHref("C:\\x\\a.md")).toBe("C:\\x\\a.md");
  expect(localPathFromHref("/p/100%25.md")).toBe("/p/100%.md");
  // Undecodable stays as written rather than throwing out of the renderer.
  expect(localPathFromHref("/p/%E0%A4%A.md")).toBe("/p/%E0%A4%A.md");
});

test("localPathFromHref: web URLs, relative links, fragments and the shell's own routes are not", () => {
  expect(localPathFromHref("https://x.test/a.md")).toBeNull();
  expect(localPathFromHref("http://127.0.0.1:1777/render?path=%2Fa.md")).toBeNull();
  expect(localPathFromHref("src/a.ts")).toBeNull();
  expect(localPathFromHref("./a.md")).toBeNull();
  expect(localPathFromHref("#top")).toBeNull();
  expect(localPathFromHref("mailto:a@b.c")).toBeNull();
  expect(localPathFromHref("")).toBeNull();
  expect(localPathFromHref("/render?path=%2Fa.md")).toBeNull();
  expect(localPathFromHref("/api/fs/stat?path=x")).toBeNull();
  // …but a folder that merely starts with those letters is a path.
  expect(localPathFromHref("/renderings/a.md")).toBe("/renderings/a.md");
});


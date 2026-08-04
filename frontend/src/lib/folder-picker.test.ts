// The pure path logic inside the shared template folder picker
// (fused_render/templates/shared/folder-picker.js).
//
// That file is an IIFE that hangs itself off `window`, like ro-badge.js and
// graph-canvas.js — deliberately not an ES module, because templates load it
// with a plain <script src="/template-shared/…"> and have no bundler. So it is
// loaded here the way a browser would: evaluate the source against a stub
// global and read the object it exported. Importing it as a module instead
// would mean changing the shipped file's shape to suit the test.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join as joinPath } from "node:path";

const SOURCE = readFileSync(
  joinPath(import.meta.dir, "../../../fused_render/templates/shared/folder-picker.js"),
  "utf8"
);

interface Paths {
  parent(p: string): string;
  join(dir: string, name: string): string;
  isRoot(p: string): boolean;
  freeName(base: string, taken: string[]): string;
}

function loadPicker(): { paths: Paths } {
  const globals: Record<string, unknown> = {};
  // No document touched at load time (CSS injects lazily, as in ro-badge.js),
  // so a bare object is a sufficient window for this.
  new Function("window", SOURCE)(globals);
  return globals.fusedFolderPicker as { paths: Paths };
}

const { paths } = loadPicker();

// ------------------------------------------------------------------ parent

test("parent walks one level up", () => {
  expect(paths.parent("/a/b/c")).toBe("/a/b");
  expect(paths.parent("/a/b/c/")).toBe("/a/b");
});

test("the root is its own parent, so Up can't escape the filesystem", () => {
  expect(paths.parent("/")).toBe("/");
  expect(paths.parent("/a")).toBe("/");
});

test("a Windows drive root is its own parent too", () => {
  expect(paths.parent("C:/Users/ada")).toBe("C:/Users");
  expect(paths.parent("C:/Users")).toBe("C:/");
  expect(paths.parent("C:/")).toBe("C:/");
});

// -------------------------------------------------------------------- join

test("join does not double the separator at a root", () => {
  expect(paths.join("/", "repo")).toBe("/repo");
  expect(paths.join("/a/b", "repo")).toBe("/a/b/repo");
  expect(paths.join("C:/", "repo")).toBe("C:/repo");
});

// ------------------------------------------------------------------ isRoot

test("isRoot recognises both kinds of root", () => {
  expect(paths.isRoot("/")).toBe(true);
  expect(paths.isRoot("C:/")).toBe(true);
  expect(paths.isRoot("/a")).toBe(false);
});

// ---------------------------------------------------------------- freeName

test("an unused name is proposed as-is", () => {
  expect(paths.freeName("project", ["other", "notes.txt"])).toBe("project");
});

test("a taken name gains the same numeric suffix the server derives", () => {
  // Mirrors reader.py's _free_dest, so the name the picker proposes is the one
  // the clone would have picked by itself.
  expect(paths.freeName("project", ["project"])).toBe("project 2");
  expect(paths.freeName("project", ["project", "project 2"])).toBe("project 3");
});

test("a clash only counts an exact name, not a prefix", () => {
  expect(paths.freeName("project", ["projector", "project-old"])).toBe("project");
});

test("freeName gives up rather than looping forever", () => {
  const taken = ["project", ...Array.from({ length: 200 }, (_, i) => `project ${i + 2}`)];
  const chosen = paths.freeName("project", taken);
  // Whatever it lands on, it must terminate and stay a usable name; the
  // server's own existence check is the backstop.
  expect(typeof chosen).toBe("string");
  expect(chosen.length).toBeGreaterThan(0);
});

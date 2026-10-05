// The folder a transcript "run" button `cd`s into: agent.py's `_workdir` rule.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";

const { chatWorkdir } = await import("./ClaudeChat");

test("a folder target is its own working folder", () => {
  expect(chatWorkdir("/w/p", "folder")).toBe("/w/p");
  expect(chatWorkdir("/w/app", "project")).toBe("/w/app");
});

test("a file target works in its parent", () => {
  expect(chatWorkdir("/w/p/notes.md", "file")).toBe("/w/p");
  expect(chatWorkdir("/notes.md", "file")).toBe("/");
  expect(chatWorkdir("C:\\w\\p\\x.py", "file")).toBe("C:\\w\\p");
});

test("no target, or a kind not decided yet, gives no cwd", () => {
  expect(chatWorkdir(null, "file")).toBe(null);
  expect(chatWorkdir("/w/p", "")).toBe(null);
});

import { describe, expect, test } from "bun:test";
import { fileUrlToPath } from "./path-url";

describe("fileUrlToPath", () => {
  test("a Windows file URL drops the authority slash before the drive", () => {
    expect(fileUrlToPath("file:///C:/Users/x")).toBe("C:/Users/x");
    expect(fileUrlToPath("file:///C:")).not.toBe("C:/Users/x");
  });

  test("a POSIX path whose first segment looks like 'a:notes' stays rooted", () => {
    expect(fileUrlToPath("file:///a:notes/x.md")).toBe("/a:notes/x.md");
  });

  test("a POSIX path is untouched", () => {
    expect(fileUrlToPath("file:///Users/x")).toBe("/Users/x");
  });
});

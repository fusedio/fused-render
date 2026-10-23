import { describe, expect, test } from "bun:test";
import { canonFsPath, isDriveAbs, fileUrlToPath } from "./path-url";

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

describe("canonFsPath", () => {
  test("folds backslashes on drive paths only", () => {
    expect(canonFsPath("C:\\Users\\x")).toBe("C:/Users/x");
    expect(canonFsPath("C:/Users\\x")).toBe("C:/Users/x");
    expect(canonFsPath("/tmp/a\\b")).toBe("/tmp/a\\b");
    expect(canonFsPath("\\\\srv\\share")).toBe("\\\\srv\\share");
  });
  test("isDriveAbs needs a separator after the colon", () => {
    expect(isDriveAbs("C:\\")).toBe(true);
    expect(isDriveAbs("C:/")).toBe(true);
    expect(isDriveAbs("a:notes")).toBe(false);
  });
});

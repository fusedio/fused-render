import { describe, expect, test } from "bun:test";
import {
  canonFsPath,
  encodeFsPathSegments,
  fileUrlToPath,
  isDriveAbs,
  parentFsPath,
  rootedFsPath,
} from "./path-url";

describe("fileUrlToPath", () => {
  test("a Windows file URL drops the authority slash before the drive", () => {
    expect(fileUrlToPath("file:///C:/Users/x")).toBe("C:/Users/x");
    expect(fileUrlToPath("file:///C:")).toBe("C:/");
    expect(fileUrlToPath("file:///C:/")).toBe("C:/");
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

describe("parentFsPath", () => {
  test("a drive path never drops below its root", () => {
    expect(parentFsPath("C:/Users")).toBe("C:/");
    expect(parentFsPath("C:\\notes.md")).toBe("C:/");
    expect(parentFsPath("C:/Users/x")).toBe("C:/Users");
    expect(parentFsPath("C:\\Users\\x\\")).toBe("C:/Users");
    expect(parentFsPath("C:/")).toBe("C:/");
  });
  test("POSIX bottoms out at / and keeps a literal backslash", () => {
    expect(parentFsPath("/a/b")).toBe("/a");
    expect(parentFsPath("/a")).toBe("/");
    expect(parentFsPath("/")).toBe("/");
    expect(parentFsPath("/tmp/a\\b.ipynb")).toBe("/tmp");
  });
  test("UNC bottoms out at the share", () => {
    expect(parentFsPath("\\\\srv\\share\\a\\b")).toBe("\\\\srv\\share\\a");
    expect(parentFsPath("\\\\srv\\share\\a")).toBe("\\\\srv\\share");
    expect(parentFsPath("\\\\srv\\share")).toBe("\\\\srv\\share");
  });
});

describe("rootedFsPath / encodeFsPathSegments", () => {
  test("drive rooting", () => {
    expect(rootedFsPath("C:")).toBe("C:/");
    expect(rootedFsPath("C:/x")).toBe("C:/x");
    expect(rootedFsPath("tmp/x")).toBe("/tmp/x");
  });
  test("encoding", () => {
    expect(encodeFsPathSegments("C:\\Users\\x y")).toBe("C%3A/Users/x%20y");
    expect(encodeFsPathSegments("/tmp/a\\b")).toBe("tmp/a%5Cb");
  });
});

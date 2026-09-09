import { describe, expect, test } from "bun:test";
import { completionTarget, displayDir } from "@apps/explorer/listing/completion-target";

const FS_PATH = "/home/dev/project";
const HOME = "/home/dev";

describe("completionTarget", () => {
  test("a bare filter word gets no dropdown", () => {
    expect(completionTarget("readme", FS_PATH, HOME)).toBeNull();
  });

  test("a glob gets no dropdown", () => {
    expect(completionTarget("*.py", FS_PATH, HOME)).toBeNull();
    expect(completionTarget("src/*.py", FS_PATH, HOME)).toBeNull();
  });

  test("an empty query gets no dropdown", () => {
    expect(completionTarget("", FS_PATH, HOME)).toBeNull();
  });

  test("bare ~ lists home with no partial", () => {
    expect(completionTarget("~", FS_PATH, HOME)).toEqual({
      dir: HOME,
      partial: "",
    });
  });

  test("~ with nothing home is unresolved returns null", () => {
    expect(completionTarget("~", FS_PATH, undefined)).toBeNull();
  });

  test("~/ lists home with no partial", () => {
    expect(completionTarget("~/", FS_PATH, HOME)).toEqual({
      dir: HOME,
      partial: "",
    });
  });

  test("~/Doc narrows home by a partial name", () => {
    expect(completionTarget("~/Doc", FS_PATH, HOME)).toEqual({
      dir: HOME,
      partial: "Doc",
    });
  });

  test("a bare absolute slash lists the root", () => {
    expect(completionTarget("/", FS_PATH, HOME)).toEqual({
      dir: "/",
      partial: "",
    });
  });

  test("an absolute path with one segment narrows the root", () => {
    expect(completionTarget("/us", FS_PATH, HOME)).toEqual({
      dir: "/",
      partial: "us",
    });
  });

  test("an absolute path with a trailing slash lists that directory", () => {
    expect(completionTarget("/usr/local/", FS_PATH, HOME)).toEqual({
      dir: "/usr/local",
      partial: "",
    });
  });

  test("an absolute path narrows the last segment", () => {
    expect(completionTarget("/usr/loc", FS_PATH, HOME)).toEqual({
      dir: "/usr",
      partial: "loc",
    });
  });

  test("a relative query is scoped to the folder being searched", () => {
    expect(completionTarget("src/ap", FS_PATH, HOME)).toEqual({
      dir: FS_PATH + "/src",
      partial: "ap",
    });
  });

  test("a relative query with a trailing slash lists that subfolder", () => {
    expect(completionTarget("src/", FS_PATH, HOME)).toEqual({
      dir: FS_PATH + "/src",
      partial: "",
    });
  });
});

describe("displayDir", () => {
  test("home itself renders as ~", () => {
    expect(displayDir(HOME, HOME)).toBe("~");
  });

  test("a directory under home renders as ~/...", () => {
    expect(displayDir(FS_PATH, HOME)).toBe("~/project");
  });

  test("a directory outside home renders unchanged", () => {
    expect(displayDir("/usr/local", HOME)).toBe("/usr/local");
  });

  test("an unresolved home renders the directory unchanged", () => {
    expect(displayDir(FS_PATH, undefined)).toBe(FS_PATH);
  });
});

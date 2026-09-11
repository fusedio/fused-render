import { describe, expect, test } from "bun:test";
import {
  applyQueryNotation,
  completionTarget,
  displayDir,
  isExactSingleMatch,
} from "@apps/explorer/listing/completion-target";

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

  test("a drive-letter path narrows the last segment, backslashes folded to /", () => {
    expect(completionTarget("C:/Users/me", FS_PATH, HOME)).toEqual({
      dir: "C:/Users",
      partial: "me",
    });
    expect(completionTarget("C:\\Users\\me", FS_PATH, HOME)).toEqual({
      dir: "C:/Users",
      partial: "me",
    });
  });

  test("a bare drive root lists that drive, keeping its own slash in dir", () => {
    expect(completionTarget("C:/", FS_PATH, HOME)).toEqual({
      dir: "C:/",
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

describe("applyQueryNotation", () => {
  test("a tilde-relative query stays tilde-relative", () => {
    expect(
      applyQueryNotation(HOME + "/work/ai_utils/", "~/work/ai", FS_PATH, HOME),
    ).toBe("~/work/ai_utils/");
  });

  test("bare ~ stays tilde-relative", () => {
    expect(applyQueryNotation(HOME + "/Documents/", "~", FS_PATH, HOME)).toBe(
      "~/Documents/",
    );
  });

  test("a tilde query resolving outside home falls back to absolute", () => {
    // Can't happen via completionTarget's own resolution today, but
    // displayDir's own fallback is what applyQueryNotation defers to, so it
    // stays honest rather than fabricating a "~" that doesn't apply.
    expect(applyQueryNotation("/usr/local/", "~/x", FS_PATH, HOME)).toBe(
      "/usr/local/",
    );
  });

  test("an absolute query stays absolute", () => {
    expect(applyQueryNotation("/usr/local/", "/usr/loc", FS_PATH, HOME)).toBe(
      "/usr/local/",
    );
  });

  test("a relative query stays relative to the folder being searched", () => {
    expect(
      applyQueryNotation(FS_PATH + "/src/app.py", "src/ap", FS_PATH, HOME),
    ).toBe("src/app.py");
  });

  test("a relative query resolving to the folder itself writes back empty", () => {
    expect(applyQueryNotation(FS_PATH, "", FS_PATH, HOME)).toBe("");
  });

  test("the trailing slash marking a directory survives every notation", () => {
    expect(
      applyQueryNotation(FS_PATH + "/src/", "src/", FS_PATH, HOME),
    ).toBe("src/");
    expect(applyQueryNotation("/usr/local/", "/usr/loc", FS_PATH, HOME)).toBe(
      "/usr/local/",
    );
  });
});

describe("isExactSingleMatch", () => {
  const target = { dir: FS_PATH, partial: "working_as_a_team" };

  test("one row whose name equals the typed partial exactly is redundant", () => {
    expect(isExactSingleMatch([{ name: "working_as_a_team" }], target)).toBe(true);
  });

  test("one row that only PREFIXES the partial still has more to type", () => {
    expect(isExactSingleMatch([{ name: "working_as_a_team_2" }], target)).toBe(false);
  });

  test("more than one row is never redundant, even with an exact match among them", () => {
    expect(
      isExactSingleMatch(
        [{ name: "working_as_a_team" }, { name: "working_as_a_team_2" }],
        target,
      ),
    ).toBe(false);
  });

  test("no target (dropdown not path-shaped at all) is never redundant", () => {
    expect(isExactSingleMatch([{ name: "working_as_a_team" }], null)).toBe(false);
  });

  test("zero rows is never redundant — that's a plain empty dropdown", () => {
    expect(isExactSingleMatch([], target)).toBe(false);
  });
});

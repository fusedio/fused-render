import { describe, expect, it } from "bun:test";
import {
  HOME_RESULT_CAP,
  activeRow,
  corpusFrom,
  homeCountNote,
  homeHitsFrom,
  pathShortcut,
  redirectsToSearch,
  stepHighlight,
} from "./home-search";
import type { IndexSearchResult, WalkEntry } from "@platform/lib/api";
import type { SearchHit } from "@apps/explorer/listing/types";

const HOME = "/Users/me";

function walkEntry(rel: string, over: Partial<WalkEntry> = {}): WalkEntry {
  return { rel, is_dir: false, size: 10, mtime: 1_800_000_000, ...over };
}

function hit(rel: string, over: Partial<WalkEntry> = {}): SearchHit {
  return { entry: walkEntry(rel, over), positions: [], score: 1, longestRun: 1, tier: 1, depth: 1 };
}

function indexResult(over: Partial<IndexSearchResult> = {}): IndexSearchResult {
  return {
    covered: true,
    fresh: true,
    root: HOME,
    entries: [walkEntry("Downloads/a.csv")],
    truncated: false,
    total: 1,
    updated: null,
    age_s: null,
    ...over,
  };
}

describe("pathShortcut", () => {
  it("expands ~ and ~/… against home", () => {
    expect(pathShortcut("~", HOME)).toBe(HOME);
    expect(pathShortcut("~/Downloads", HOME)).toBe(`${HOME}/Downloads`);
  });

  it("keeps absolute posix paths and normalizes trailing slashes", () => {
    expect(pathShortcut("/etc", HOME)).toBe("/etc");
    expect(pathShortcut("/etc/", HOME)).toBe("/etc");
    // The root itself must survive being stripped.
    expect(pathShortcut("/", HOME)).toBe("/");
  });

  it("normalizes drive-letter paths and keeps a drive root's slash", () => {
    expect(pathShortcut("C:\\Users\\me", HOME)).toBe("C:/Users/me");
    // Bare "C:" reads as cwd-relative, so a drive root keeps its slash.
    expect(pathShortcut("C:/", HOME)).toBe("C:/");
  });

  it("is null for anything that is not a path — that is a search, not a jump", () => {
    expect(pathShortcut("weather csv", HOME)).toBeNull();
    expect(pathShortcut("Downloads", HOME)).toBeNull();
    // A backslash is a legal POSIX filename char, so this is not a path.
    expect(pathShortcut("a\\b", HOME)).toBeNull();
    expect(pathShortcut("  ", HOME)).toBeNull();
  });
});

describe("homeHitsFrom", () => {
  it("absolutizes rel paths against home and carries the entry's facts", () => {
    const rows = homeHitsFrom([hit("Downloads/a.csv", { size: 42, is_dir: false })], HOME);
    expect(rows).toEqual([
      {
        path: `${HOME}/Downloads/a.csv`,
        rel: "Downloads/a.csv",
        is_dir: false,
        size: 42,
        mtime: 1_800_000_000,
      },
    ]);
  });

  it("caps the rendered rows without touching the ranking behind them", () => {
    const many = Array.from({ length: HOME_RESULT_CAP + 25 }, (_, i) => hit(`f${i}.txt`));
    expect(homeHitsFrom(many, HOME)).toHaveLength(HOME_RESULT_CAP);
  });
});

describe("homeCountNote", () => {
  it("states the true total and owns up to the display cap", () => {
    expect(homeCountNote(1, false)).toBe("1 match");
    expect(homeCountNote(HOME_RESULT_CAP, false)).toBe(`${HOME_RESULT_CAP} matches`);
    expect(homeCountNote(HOME_RESULT_CAP + 60, false)).toBe(
      `Showing top ${HOME_RESULT_CAP} of ${HOME_RESULT_CAP + 60}`,
    );
    // Four figures read as a number, not a digit run.
    expect(homeCountNote(4690, false)).toBe(`Showing top ${HOME_RESULT_CAP} of 4,690`);
    // A truncated corpus is a second, independent "there was more than this".
    expect(homeCountNote(3, true)).toBe("3+ matches");
  });
});

describe("corpusFrom", () => {
  it("is ok for a covered root", () => {
    expect(corpusFrom(indexResult())).toEqual({
      status: "ok",
      entries: [walkEntry("Downloads/a.csv")],
      truncated: false,
    });
  });

  it("is cold — not empty — when the index has not covered the root yet", () => {
    // The honest answer is "still building", never "no matches": the home page
    // has no live walk to fall back on, so a miss here is the app's state.
    expect(corpusFrom(indexResult({ covered: false, entries: [] }))).toEqual({ status: "cold" });
  });
});

describe("keyboard rows", () => {
  // Rows are the file hits followed by ONE action row (Search with AI), so the
  // AI row's index is always the file count.
  it("steps down from nothing to the first row and wraps at both ends", () => {
    expect(stepHighlight(null, 3, 1)).toBe(0);
    expect(stepHighlight(2, 3, 1)).toBe(3); // the AI row
    expect(stepHighlight(3, 3, 1)).toBe(0); // wrapped past the AI row
    expect(stepHighlight(null, 3, -1)).toBe(3); // up from nothing = the AI row
    expect(stepHighlight(0, 3, -1)).toBe(3);
  });

  it("walks only the AI row when there are no file hits", () => {
    expect(stepHighlight(null, 0, 1)).toBe(0);
    expect(stepHighlight(0, 0, 1)).toBe(0);
  });

  it("pre-selects the AI row on zero matches, and nothing otherwise", () => {
    expect(activeRow(null, 0)).toBe(0); // the AI row is the only content
    expect(activeRow(null, 5)).toBeNull(); // Enter does nothing yet
    expect(activeRow(2, 5)).toBe(2);
    // A highlight past the end of a shrinking list clamps to the AI row rather
    // than addressing a row that is no longer on screen.
    expect(activeRow(9, 3)).toBe(3);
  });
});

describe("redirectsToSearch", () => {
  const key = (over: Partial<Parameters<typeof redirectsToSearch>[0]> = {}) => ({
    key: "a",
    ctrlKey: false,
    altKey: false,
    metaKey: false,
    tagName: "DIV",
    isContentEditable: false,
    isSearchInput: false,
    ...over,
  });

  it("claims a printable keystroke aimed at the page", () => {
    expect(redirectsToSearch(key())).toBe(true);
    expect(redirectsToSearch(key({ key: "7" }))).toBe(true);
    expect(redirectsToSearch(key({ key: "~" }))).toBe(true);
    expect(redirectsToSearch(key({ key: "/" }))).toBe(true);
    // Shift is part of typing, not a command.
    expect(redirectsToSearch(key({ key: "A" }))).toBe(true);
    // Correcting a query has to reach the box too.
    expect(redirectsToSearch(key({ key: "Backspace" }))).toBe(true);
  });

  it("leaves shortcuts alone", () => {
    expect(redirectsToSearch(key({ ctrlKey: true }))).toBe(false);
    expect(redirectsToSearch(key({ metaKey: true }))).toBe(false);
    expect(redirectsToSearch(key({ altKey: true }))).toBe(false);
  });

  it("leaves navigation and other non-printable keys alone", () => {
    for (const k of ["Enter", "Escape", "Tab", "ArrowDown", "ArrowUp", "F5", "Shift"])
      expect(redirectsToSearch(key({ key: k }))).toBe(false);
  });

  it("never steals from another field", () => {
    expect(redirectsToSearch(key({ tagName: "INPUT" }))).toBe(false);
    expect(redirectsToSearch(key({ tagName: "TEXTAREA" }))).toBe(false);
    expect(redirectsToSearch(key({ tagName: "SELECT" }))).toBe(false);
    expect(redirectsToSearch(key({ isContentEditable: true }))).toBe(false);
  });

  it("is a no-op when the search box already has the caret", () => {
    // Not merely redundant: focusing on every keystroke would reset the caret
    // to the end, so editing the middle of a query would be impossible.
    expect(redirectsToSearch(key({ tagName: "INPUT", isSearchInput: true }))).toBe(false);
  });
});

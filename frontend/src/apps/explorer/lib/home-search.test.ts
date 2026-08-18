import { describe, expect, it } from "bun:test";
import {
  HOME_RESULT_CAP,
  activeRow,
  answerFrom,
  homeCountNote,
  nameStart,
  pathShortcut,
  positionsWithin,
  rankingSettled,
  redirectsToSearch,
  stepHighlight,
  submitRow,
  type HomeAnswer,
} from "./home-search";
import type { IndexRankHit, IndexRankResult } from "@platform/lib/api";

const HOME = "/Users/me";

function rankHit(rel: string, over: Partial<IndexRankHit> = {}): IndexRankHit {
  return {
    rel,
    is_dir: false,
    size: 10,
    mtime: 1_800_000_000,
    score: 1,
    longest_run: 1,
    tier: 1,
    depth: 1,
    ...over,
  };
}

function rankResult(over: Partial<IndexRankResult> = {}): IndexRankResult {
  return {
    covered: true,
    fresh: true,
    reason: "",
    root: HOME,
    hits: [rankHit("Downloads/a.csv")],
    truncated: false,
    total: 1,
    updated: null,
    age_s: null,
    ...over,
  };
}

function answer(over: Partial<HomeAnswer> = {}): HomeAnswer {
  return { query: "a", hits: [], truncated: false, total: 0, covered: true, ...over };
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

describe("answerFrom", () => {
  it("absolutizes rel paths against home and carries the row's facts", () => {
    const res = rankResult({ hits: [rankHit("Downloads/a.csv", { size: 42 })] });
    expect(answerFrom(res, "a.csv", HOME).hits).toEqual([
      {
        path: `${HOME}/Downloads/a.csv`,
        rel: "Downloads/a.csv",
        is_dir: false,
        size: 42,
        mtime: 1_800_000_000,
        positions: [10, 11, 12, 13, 14],
      },
    ]);
  });

  it("re-runs the matcher for highlights rather than trusting the wire", () => {
    // fuzzy.ts is the single source of truth for what highlights; the server
    // deliberately does not send positions (index/rank.py's docstring).
    const [row] = answerFrom(rankResult({ hits: [rankHit("docs/README.md")] }), "readme", HOME).hits;
    expect(row.positions!.map((i) => "docs/README.md"[i]).join("")).toBe("README");
  });

  it("caps the rendered rows but keeps the server's true total", () => {
    const many = Array.from({ length: HOME_RESULT_CAP + 25 }, (_, i) => rankHit(`f${i}.txt`));
    const out = answerFrom(rankResult({ hits: many, total: many.length }), "f", HOME);
    expect(out.hits).toHaveLength(HOME_RESULT_CAP);
    expect(out.total).toBe(HOME_RESULT_CAP + 25);
  });

  it("carries the query it answers, which is what stops the list blanking", () => {
    expect(answerFrom(rankResult(), "down", HOME).query).toBe("down");
  });

  it("reports an uncovered root as such, never as zero matches", () => {
    // The honest answer is "still building": the home page has no live walk to
    // fall back on, so a miss here is the app's state, not the user's files.
    const out = answerFrom(rankResult({ covered: false, hits: [], total: 0 }), "x", HOME);
    expect(out.covered).toBe(false);
    expect(out.hits).toEqual([]);
  });
});

describe("highlight rebasing", () => {
  it("keeps only the positions that land in the cell, rebased to it", () => {
    const rel = "docs/readme.md";
    const positions = [0, 1, 5, 6, 7]; // "do" in docs, "rea" in readme.md
    expect(nameStart(rel)).toBe(5);
    expect(positionsWithin(positions, 5, rel.length - 5)).toEqual([0, 1, 2]);
  });

  it("drops out-of-range positions instead of clamping them", () => {
    // A match entirely on the parent directory has nothing to mark in the name
    // cell, and marking the wrong character is worse than marking none.
    expect(positionsWithin([0, 1], 5, 9)).toEqual([]);
  });

  it("has nothing to rebase for a name-only rel", () => {
    expect(nameStart("file.txt")).toBe(0);
    expect(positionsWithin([0, 1], 0, 8)).toEqual([0, 1]);
  });
});

describe("rankingSettled over a failure", () => {
  it("is not settled while the rows on screen answer an older query", () => {
    // The residual path the pending check cannot see. A CURRENT failure is
    // reported correctly, and `settled` then licensed acting on rows that
    // answer something else: type "read", get ten rows, type "readme", the
    // request fails, press Enter — and submitRow opens "read"'s top hit.
    expect(rankingSettled(answer({ query: "read" }), "readme", false, true)).toBe(false);
  });

  it("IS settled with nothing on screen, which is what arms the AI row", () => {
    // Deliberate and unchanged: no answer is coming and the AI row really is
    // the only content left.
    expect(rankingSettled(null, "readme", false, true)).toBe(true);
  });

  it("IS settled when the failure is a refresh over the current query's rows", () => {
    expect(rankingSettled(answer({ query: "readme" }), "readme", false, true)).toBe(true);
  });

  it("still checks pending first, whatever the rows say", () => {
    expect(rankingSettled(answer({ query: "readme" }), "readme", true, true)).toBe(false);
  });
});

describe("submitRow over a failure with stale rows", () => {
  it("commits nothing when Enter has no explicit choice", () => {
    // The whole point of the rule above: with rows for a previous query on
    // screen, the top-hit fallthrough opens a file the user did not ask for.
    const settled = rankingSettled(answer({ query: "read" }), "readme", false, true);
    expect(submitRow(null, 10, settled)).toBeNull();
  });

  it("still commits a row the user pointed at", () => {
    const settled = rankingSettled(answer({ query: "read" }), "readme", false, true);
    expect(submitRow(3, 10, settled)).toBe(3);
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
    expect(activeRow(null, 0, true)).toBe(0); // the AI row is the only content
    expect(activeRow(null, 5, true)).toBeNull(); // no highlight to render
    expect(activeRow(2, 5, true)).toBe(2);
    // A highlight past the end of a shrinking list clamps to the AI row rather
    // than addressing a row that is no longer on screen.
    expect(activeRow(9, 3, true)).toBe(3);
  });

  it("does not pre-select the AI row until ranking has settled on zero", () => {
    // "Nothing scored yet" and "zero matches" look identical as a count, and
    // pre-selecting on the first made Enter during the corpus load or the
    // 120ms debounce spend a model call on a query with instant matches.
    expect(activeRow(null, 0, false)).toBeNull();
    // An explicit arrow-key choice is the user's, settled or not.
    expect(activeRow(1, 0, false)).toBe(0);
  });
});

describe("submitRow", () => {
  it("opens the top hit when Enter is pressed with no highlight", () => {
    // Previously a silent no-op: every other search box in the app commits on
    // Enter, and the top hit is what the list is offering.
    expect(submitRow(null, 5, true)).toBe(0);
  });

  it("commits NOTHING while the rows on screen answer the previous query", () => {
    // The list is never blanked, so hits are on screen for a query that has
    // not been answered yet. Enter used to open the top one — type "read",
    // then "readme", press Enter before the answer lands, and the app
    // navigated to "read"'s best match. Opening a file is now gated on
    // `settled` exactly as the AI row already was.
    expect(submitRow(null, 5, false)).toBeNull();
    // An explicit arrow-key choice still commits: the user pointed at a row
    // they can see.
    expect(submitRow(2, 5, false)).toBe(2);
  });

  it("runs the AI row only once ranking has settled on zero hits", () => {
    expect(submitRow(null, 0, true)).toBe(0); // fileCount 0 → the AI row
    // Mid-scan: nothing to commit yet, and the AI row must not be armed.
    expect(submitRow(null, 0, false)).toBeNull();
  });

  it("honours an explicit highlight, including the AI row", () => {
    expect(submitRow(2, 5, true)).toBe(2);
    expect(submitRow(5, 5, true)).toBe(5);
  });
});

describe("rankingSettled", () => {
  it("is false while a request for this query is in flight", () => {
    expect(rankingSettled(null, "read", true, false)).toBe(false);
    expect(rankingSettled(answer({ query: "read" }), "read", true, false)).toBe(false);
  });

  it("is false while the rows on screen answer the PREVIOUS query", () => {
    // The list is deliberately never blanked, so hits being present is not
    // evidence that this query has been answered — and pre-arming the AI row
    // here would spend a model call on a query that was about to answer itself.
    expect(rankingSettled(answer({ query: "rea" }), "read", false, false)).toBe(false);
    expect(rankingSettled(null, "read", false, false)).toBe(false);
  });

  it("is false while a request is in flight even after an EARLIER failure", () => {
    // `failed` must not outrank `pending`. It did, and the consequence was a
    // paid model call: after one transient failure every later keystroke read
    // as settled while its request was still out, so the AI row pre-selected
    // itself and Enter committed it for a query that was about to answer.
    expect(rankingSettled(null, "read", true, true)).toBe(false);
  });

  it("is true for an answer to THIS query, and for one that will never come", () => {
    expect(rankingSettled(answer({ query: "read" }), "read", false, false)).toBe(true);
    // A failed request is settled: nothing further is coming, and the AI row is
    // the only content left — so Enter must reach it.
    expect(rankingSettled(null, "read", false, true)).toBe(true);
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

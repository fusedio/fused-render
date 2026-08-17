import { describe, expect, it } from "bun:test";
import { discoverChrome, resultsSummary, suggestedSummary } from "./discoverView";

// What the Discover tab is SHOWING, from the settled query alone. Three pieces
// of chrome that must move together — the grid, the "these are suggestions"
// preamble, and the "searching huggingface.co" caption — because every way
// they can disagree is a page making a false claim about itself.
describe("discoverChrome", () => {
  it("shows the curated shortlist when nothing has been asked for", () => {
    expect(discoverChrome("", "")).toEqual({
      view: "suggested",
      heading: "Suggested models",
      showsPreamble: true,
      showsSearchNote: false,
    });
  });

  it("swaps to results as soon as there is a query", () => {
    expect(discoverChrome("whisper", "")).toEqual({
      view: "results",
      heading: "Search results",
      showsPreamble: false,
      showsSearchNote: true,
    });
  });

  it("names the grid on screen, and never the other one", () => {
    // The complaint this answers: the two states differed only by a line of
    // prose appearing and disappearing, so somebody who searched, scrolled and
    // looked back up could not tell a page of Hub hits from the vetted
    // shortlist. Each state now says its own name in the same place, and
    // exactly one name exists at a time.
    for (const [q, task] of [["", ""], ["whisper", ""], ["", "text-to-image"], ["a", "t"]]) {
      const chrome = discoverChrome(q, task);
      expect(chrome.heading).toBe(chrome.view === "results" ? "Search results" : "Suggested models");
    }
  });
});

// The muted right-hand fact beside each heading — the thing that makes two
// ALL-CAPS titles read as siblings rather than as one replacing the other.
describe("heading summaries", () => {
  it("says what was asked and how many came back", () => {
    expect(resultsSummary("whisper", 24, "huggingface.co")).toBe(
      '"whisper" · 24 on huggingface.co',
    );
  });

  it("leaves out the query when the task filter is the whole question", () => {
    // Picking "Speech to text" with an empty box is a search, and quoting an
    // empty string ('""') would be the heading reporting a query nobody typed.
    expect(resultsSummary("", 8, "huggingface.co")).toBe("8 on huggingface.co");
    expect(resultsSummary("  ", 8, "hf-mirror.com")).toBe("8 on hf-mirror.com");
  });

  it("holds off on a count nobody has yet", () => {
    // While the request is in flight there is no number. A "0 on
    // huggingface.co" beside the heading would be a wrong answer rather than a
    // missing one.
    expect(resultsSummary("whisper", null, "huggingface.co")).toBe('"whisper"');
    expect(resultsSummary("", null, "huggingface.co")).toBe(null);
  });

  it("counts the shortlist the same way the results are counted", () => {
    expect(suggestedSummary(11)).toBe("11 picked for this machine");
    expect(suggestedSummary(1)).toBe("1 picked for this machine");
  });

  it("treats a task filter on its own as a search", () => {
    // Picking "Speech to text" with an empty box is a question about the Hub
    // exactly as much as typing is, and the curated sections are not the
    // answer to it.
    expect(discoverChrome("", "automatic-speech-recognition").view).toBe("results");
  });

  it("takes the preamble away with the shortlist it describes", () => {
    // The bug this rule exists to prevent: "Suggested models — picked to run
    // on this machine" left standing over a grid of search results, which is a
    // sentence describing cards that are no longer on screen.
    for (const [q, task] of [["qwen", ""], ["", "text-to-image"], ["qwen", "text-to-image"]]) {
      const chrome = discoverChrome(q, task);
      expect(chrome.showsPreamble).toBe(false);
      expect(chrome.view).toBe("results");
    }
  });

  it("never shows the preamble and the search caption at once", () => {
    // They describe two different pages. Whichever is true, the other is not.
    for (const [q, task] of [["", ""], ["a", ""], ["", "t"], ["a", "t"]]) {
      const chrome = discoverChrome(q, task);
      expect(chrome.showsPreamble && chrome.showsSearchNote).toBe(false);
    }
  });

  it("is not fooled by whitespace", () => {
    // A box holding a space is a box nobody has typed a query into. Without
    // this, a stray keystroke swaps the shortlist out for "nothing matches
    // that", which reads as the app having lost its own catalog.
    expect(discoverChrome("   ", "").view).toBe("suggested");
    expect(discoverChrome(" \t ", " ").showsPreamble).toBe(true);
  });
});

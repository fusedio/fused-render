import { describe, expect, it } from "bun:test";
import { discoverChrome } from "./discoverView";

// What the Discover tab is SHOWING, from the settled query alone. Three pieces
// of chrome that must move together — the grid, the "these are suggestions"
// preamble, and the "searching huggingface.co" caption — because every way
// they can disagree is a page making a false claim about itself.
describe("discoverChrome", () => {
  it("shows the curated shortlist when nothing has been asked for", () => {
    expect(discoverChrome("", "")).toEqual({
      view: "suggested",
      showsPreamble: true,
      showsSearchNote: false,
    });
  });

  it("swaps to results as soon as there is a query", () => {
    expect(discoverChrome("whisper", "")).toEqual({
      view: "results",
      showsPreamble: false,
      showsSearchNote: true,
    });
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

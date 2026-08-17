import { describe, expect, it } from "bun:test";
import { discoverChrome, gateChrome, localCopy, resultsSummary, suggestedSummary } from "./discoverView";

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
      showsReset: false,
    });
  });

  it("swaps to results as soon as there is a query", () => {
    expect(discoverChrome("whisper", "")).toEqual({
      view: "results",
      heading: "Search results",
      showsPreamble: false,
      showsSearchNote: true,
      showsReset: true,
    });
  });

  it("offers the way back for a TASK FILTER on its own", () => {
    // The complaint, exactly: pick "Text generation" from the select with an
    // empty box and the shortlist is gone, with no visible way back — the only
    // route is realising the select has an "Any task" option. An escape hatch
    // keyed off the query text would be ABSENT in this state, which is the one
    // state that most needs it.
    expect(discoverChrome("", "automatic-speech-recognition").showsReset).toBe(true);
  });

  it("offers nothing to clear when there is nothing to clear", () => {
    // In the curated state the control would be a button that undoes nothing,
    // and a permanently visible "clear" teaches the reader that the page is
    // always filtered.
    expect(discoverChrome("", "").showsReset).toBe(false);
    expect(discoverChrome("  ", " ").showsReset).toBe(false);
  });

  it("moves with the view it is supposed to escape, in every state", () => {
    // One rule, not two: the control is on screen exactly when the results are.
    // Split into its own condition, they drift, and the drift that matters is a
    // results page with no way off it.
    for (const [q, task] of [["", ""], ["a", ""], ["", "t"], ["a", "t"], [" ", " "]]) {
      const chrome = discoverChrome(q, task);
      expect(chrome.showsReset).toBe(chrome.view === "results");
    }
  });

  it("comes back to the curated view when the reset clears BOTH inputs", () => {
    // What the button does, stated as the thing it must produce. Clearing only
    // the text is the failure this pins: the box is empty, the reader has done
    // the obvious thing, and the suggestions still are not there.
    const searching = discoverChrome("whisper", "text-to-image");
    expect(searching.view).toBe("results");
    expect(discoverChrome("", "")).toEqual({
      view: "suggested",
      heading: "Suggested models",
      showsPreamble: true,
      showsSearchNote: false,
      showsReset: false,
    });
    // …and clearing only the query is NOT enough, which is why the control
    // cannot be an ✕ wired to the input alone.
    expect(discoverChrome("", "text-to-image").view).toBe("results");
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

// What a gated result offers instead of a bare Download button. Gated repos
// are results again (D316) — a licence you accept by signing in is a step the
// user can take — so the card has to say which gate and what opens it.
describe("gateChrome", () => {
  it("is nothing at all for an ordinary repo", () => {
    // The overwhelming majority. No pill, no second action, no hedging.
    expect(gateChrome(null, false)).toBe(null);
    expect(gateChrome(null, true)).toBe(null);
  });

  it("offers the download when this machine has a token", () => {
    // The token is what turns "you cannot have this" into "you may already
    // have accepted this" — the download is the honest thing to offer, and the
    // Hub is the one that gets to refuse.
    const gate = gateChrome("auto", true)!;
    expect(gate.canDownload).toBe(true);
    expect(gate.action).toBe(null);
    expect(gate.pill).toBe("gated");
  });

  it("sends an unauthenticated reader to the licence rather than to a 403", () => {
    const gate = gateChrome("auto", false)!;
    expect(gate.canDownload).toBe(false);
    expect(gate.action).toBe("Accept terms");
  });

  it("says when a gate needs a person rather than a click", () => {
    // "manual" is the one case that takes more than signing in: the repo's
    // owner grants access by hand, and somebody told to "accept the terms"
    // would go looking for a button that is not there.
    const gate = gateChrome("manual", false)!;
    expect(gate.action).toBe("Request access");
    expect(gate.pill).toBe("gated — by approval");
    expect(gate.title).toContain("owner");
  });

  it("always explains itself, in every combination", () => {
    // The pill is two words; the sentence behind it is the whole of what the
    // reader has to do, and a card that showed the badge with no explanation
    // would be the "gated" pill D313 deleted for exactly that reason.
    for (const gated of ["auto", "manual"] as const) {
      for (const auth of [true, false]) {
        const gate = gateChrome(gated, auth)!;
        expect(gate.title.length).toBeGreaterThan(30);
        expect(gate.pill.startsWith("gated")).toBe(true);
      }
    }
  });
});

// Where a card's copy of a model lives, asked of the LIVE listing rather than
// of the search reply that first described it.
describe("localCopy", () => {
  const here = new Map([["openai/whisper-large-v3", "/Users/x/.cache/hf/models--openai--whisper-large-v3"]]);

  it("gives the path the live listing holds", () => {
    expect(localCopy("openai/whisper-large-v3", here)).toBe(
      "/Users/x/.cache/hf/models--openai--whisper-large-v3",
    );
  });

  it("answers the SAME source the ✓ downloaded badge reads", () => {
    // The bug this pins: the badge came from the live on-disk listing and
    // Explore came from `model.local` in the frozen search reply. Download a
    // model from search results and the re-walk flips the checkmark on while
    // the reply still says "none", so the one action for the copy you just
    // fetched never appeared. One map answers both questions now, so "we have
    // it" and "here is where" cannot disagree — a model absent from the ORIGINAL
    // reply but present in the listing is exactly the post-download case.
    const afterDownload = new Map([["mlx-community/Qwen3.5-4B", "/c/models--mlx-community--Qwen3.5-4B"]]);
    expect(afterDownload.has("mlx-community/Qwen3.5-4B")).toBe(true);
    expect(localCopy("mlx-community/Qwen3.5-4B", afterDownload)).toBe(
      "/c/models--mlx-community--Qwen3.5-4B",
    );
  });

  it("says nothing while the walk has not answered", () => {
    // `null` is "no idea yet", not "you don't have it" — offering Explore on a
    // guess would hand someone a path that may not exist.
    expect(localCopy("openai/whisper-large-v3", null)).toBe(null);
  });

  it("says nothing for a model that is not here", () => {
    expect(localCopy("openai/whisper-tiny", here)).toBe(null);
  });

  it("never returns an empty path as if it were a location", () => {
    // A blank path would render an Explore link to nowhere. Absent beats broken.
    expect(localCopy("a/b", new Map([["a/b", ""]]))).toBe(null);
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

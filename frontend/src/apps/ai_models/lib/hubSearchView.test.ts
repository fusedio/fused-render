// What the Local page's search face is SHOWING, driven directly. The either/or
// is the rule with teeth: the page has two faces and several pieces of chrome
// that must move together, and every way they can disagree is the page making a
// false claim about itself.
import { describe, expect, it } from "bun:test";
import {
  activeSort,
  activeTask,
  bySizeAscending,
  gateChrome,
  needsHubLogin,
  resultsSummary,
  searchChrome,
  SORTS,
  sortsOnPage,
  wireSort,
  type ResultSort,
} from "./hubSearchView";
import type { HubTask } from "@platform/lib/api";

describe("searchChrome", () => {
  it("leaves the page alone when nothing has been asked for", () => {
    // The whole of the idle contract: no results section, no host disclosure, no
    // way-back control, and no heading of its own — the capability rows and
    // "Fetched by engines" name themselves.
    expect(searchChrome("", "")).toEqual({
      face: "models",
      heading: null,
      showsSearchNote: false,
      showsReset: false,
    });
  });

  it("swaps the whole page for results as soon as there is a query", () => {
    expect(searchChrome("whisper", "")).toEqual({
      face: "results",
      heading: "Search results",
      showsSearchNote: true,
      showsReset: true,
    });
  });

  it("offers the way back for a TASK FILTER on its own", () => {
    // The complaint, exactly: pick "Text generation" from the select with an
    // empty box and this machine's models are gone, with no visible way back —
    // the only route is realising the select has an "Any task" option. An escape
    // hatch keyed off the query text would be ABSENT in this state, which is the
    // one state that most needs it (D317).
    expect(searchChrome("", "automatic-speech-recognition").face).toBe("results");
    expect(searchChrome("", "automatic-speech-recognition").showsReset).toBe(true);
  });

  it("offers nothing to clear when there is nothing to clear", () => {
    // In the idle face the control would be a button that undoes nothing, and a
    // permanently visible "clear" teaches the reader that the page is always
    // filtered.
    expect(searchChrome("", "").showsReset).toBe(false);
    expect(searchChrome("  ", " ").showsReset).toBe(false);
  });

  it("never shows both faces, and never neither, in any combination", () => {
    // The either/or, stated as the invariant rather than as two conditions. One
    // grid at a time: the carousels answer "what should I even get", which is
    // the question somebody has BEFORE they type, and two grids on screen ask
    // the reader which of them is talking to them.
    for (const [q, task] of [
      ["", ""],
      ["a", ""],
      ["", "t"],
      ["a", "t"],
      [" ", " "],
      ["  ", "text-generation"],
    ]) {
      const chrome = searchChrome(q, task);
      const searching = chrome.face === "results";
      expect(chrome.showsReset).toBe(searching);
      expect(chrome.showsSearchNote).toBe(searching);
      // Exactly one of the two faces names itself, and it is the one that
      // replaced the sections — a grid that took the page over without saying
      // its own name leaves a reader who scrolled and looked back up with no
      // idea which of two things they are in.
      expect(chrome.heading === null).toBe(!searching);
    }
  });

  it("comes back to the page's own models only when BOTH inputs are cleared", () => {
    // What the ✕ and "← Back to models" must produce, stated as the thing they
    // produce. Clearing only the text is the failure this pins: the box is
    // empty, the reader has done the obvious thing, and their models still are
    // not there — which is why neither control can be wired to the input alone.
    expect(searchChrome("whisper", "text-to-image").face).toBe("results");
    expect(searchChrome("", "text-to-image").face).toBe("results");
    expect(searchChrome("whisper", "").face).toBe("results");
    expect(searchChrome("", "")).toEqual({
      face: "models",
      heading: null,
      showsSearchNote: false,
      showsReset: false,
    });
  });

  it("is not fooled by whitespace", () => {
    // A box holding a space is a box nobody has typed a query into. Without
    // this, a stray keystroke swaps this machine's models out for "nothing
    // matches that", which reads as the app having lost its own cache.
    expect(searchChrome("   ", "").face).toBe("models");
    expect(searchChrome(" \t ", " ").face).toBe("models");
  });
});

// What a gated result offers instead of a bare Download button. Gated repos are
// results (D316) — a licence you accept by signing in is a step the user can
// take — so the card has to say which gate and what opens it.
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

// The sign-in offered beside the results — and only there.
describe("needsHubLogin", () => {
  const open = { gated: null } as const;
  const gated = { gated: "auto" } as const;

  it("offers a login exactly when a gate is standing in the way", () => {
    expect(needsHubLogin([open, gated], false)).toBe(true);
  });

  it("says nothing when every result is downloadable as it stands", () => {
    // A standing offer of an account on every search would be this page
    // recommending one to somebody who never hit a wall.
    expect(needsHubLogin([open, open], false)).toBe(false);
  });

  it("says nothing once this machine has a token", () => {
    // The gate pills stay — the licence still has to be accepted — but the
    // thing this prompt provides is already done.
    expect(needsHubLogin([gated], true)).toBe(false);
  });

  it("says nothing before there is an answer to look at", () => {
    // Null is "no results yet", and a login prompt over a grid nobody has seen
    // is an offer about nothing.
    expect(needsHubLogin(null, false)).toBe(false);
    expect(needsHubLogin([], false)).toBe(false);
  });
});

// The muted right-hand fact beside the results heading — the thing that makes it
// read as a sibling of the capability rows' byte subtotals rather than as chrome
// from somewhere else.
describe("resultsSummary", () => {
  it("says what was asked and how many came back", () => {
    expect(resultsSummary("whisper", 24, "huggingface.co", false)).toBe(
      '"whisper" · 24 on huggingface.co',
    );
  });

  it("leaves out the query when the task filter is the whole question", () => {
    // Picking "Speech to text" with an empty box is a search, and quoting an
    // empty string ('""') would be the heading reporting a query nobody typed.
    expect(resultsSummary("", 8, "huggingface.co", false)).toBe("8 on huggingface.co");
    expect(resultsSummary("  ", 8, "hf-mirror.com", false)).toBe("8 on hf-mirror.com");
  });

  it("holds off on a count nobody has yet", () => {
    // While the request is in flight there is no number. A "0 on
    // huggingface.co" beside the heading would be a wrong answer rather than a
    // missing one.
    expect(resultsSummary("whisper", null, "huggingface.co", false)).toBe('"whisper"');
    expect(resultsSummary("", null, "huggingface.co", false)).toBe(null);
  });

  it("states no count when the search FAILED", () => {
    // A soft failure answers 200 with an `error` and `models: []`, so a count
    // taken from the array length reads "0 on huggingface.co" — the heading
    // reporting that the Hub HAS none of these, beside a banner saying we never
    // heard back. Those are different facts, and D316's whole point is that a
    // missing hit is not an absent one.
    expect(resultsSummary("whisper", 0, "huggingface.co", true)).toBe('"whisper"');
    // Also for a hard rejection, where the previous rows are still on screen
    // (D255: errors are not cached, and the last good answer stays) and the
    // count would be a number from an older question entirely.
    expect(resultsSummary("whisper", 24, "huggingface.co", true)).toBe('"whisper"');
    // Nothing at all rather than a bare host, when a task filter was the whole
    // question: "on huggingface.co" alone states no fact.
    expect(resultsSummary("", 0, "huggingface.co", true)).toBe(null);
  });
});

// ---- The two menus, and the one sort the Hub cannot do ----------------------
// The rule with teeth here is a TYPE one made testable: "size" is an ordering
// the page performs and the Hub's list endpoint refuses, so the value must never
// reach the wire. Everything else is what the two triggers say they are showing,
// which is the other way this row can make a false claim about the page.

describe("wireSort", () => {
  it("asks the Hub for downloads when the page is sorting by size", () => {
    // The server's sort is an allowlist (`_SORTS` in routers/hub_models.py) with
    // no size in it — the Hub refuses to expand `usedStorage` on a list at all —
    // so sending "size" would be a 400, or worse a silent fallback to a ranking
    // nobody chose. Downloads is the candidate set: the results anybody would
    // have got by default, reordered by what they cost.
    expect(wireSort("size")).toBe("downloads");
  });

  it("passes every ordering the Hub CAN perform straight through", () => {
    // A mapper that rewrote more than the one value it exists for would be a
    // second, invisible sort control.
    expect(wireSort("downloads")).toBe("downloads");
    expect(wireSort("likes")).toBe("likes");
    expect(wireSort("updated")).toBe("updated");
    expect(wireSort("created")).toBe("created");
    // "trending" and "fit" are both real `HubSort` values the SERVER accepts —
    // "trending" is a genuine Hub field, and "fit" is the one the server
    // resolves itself over `downloads` — so neither is this mapper's business
    // to rewrite, unlike "size", which is page-only and never reaches the wire.
    expect(wireSort("trending")).toBe("trending");
    expect(wireSort("fit")).toBe("fit");
  });

  it("only ever produces a sort the server's allowlist holds", () => {
    // Stated over the whole menu rather than value by value, because the failure
    // this prevents is a sort ADDED to the menu and not to the mapper: a new
    // page-level ordering would reach the API as itself and be rejected there.
    const allowed = ["downloads", "likes", "updated", "created", "trending", "fit"];
    for (const s of SORTS) expect(allowed).toContain(wireSort(s.value));
  });

  it("knows which orderings the page has to do itself", () => {
    // What decides whether the results have to be MEASURED before they can be
    // shown in order (HubResults' size pass). Fit is server-side and needs no
    // measuring pass, unlike size.
    expect(sortsOnPage("size")).toBe(true);
    expect(sortsOnPage("downloads")).toBe(false);
    expect(sortsOnPage("created")).toBe(false);
    expect(sortsOnPage("fit")).toBe(false);
    expect(sortsOnPage("trending")).toBe(false);
  });

  it("offers size, and offers it last", () => {
    // It is the only ordering that costs a measurement, so the cheap answers
    // come first — and it exists, which is the half of D426's refinement a
    // reader of this table would come here to check.
    expect(SORTS.map((s) => s.value)).toEqual([
      "downloads",
      "likes",
      "updated",
      "created",
      "trending",
      "fit",
      "size",
    ]);
    // Every row says what its ordering MEANS: "Downloads" does not say over what
    // period and "New" does not say new to whom, and the trigger's hover is the
    // only place either gets said.
    for (const s of SORTS) {
      expect(s.label.length).toBeGreaterThan(0);
      expect(s.title.length).toBeGreaterThan(20);
    }
  });
});

describe("activeSort", () => {
  it("hands the trigger the option that is in force", () => {
    expect(activeSort("size").label).toBe("Size");
    expect(activeSort("likes").label).toBe("Likes");
    // "New", not "Created": the wire name is the Hub's field and the label is
    // the reader's word for it.
    expect(activeSort("created").label).toBe("New");
  });

  it("names something rather than nothing for a sort the menu does not offer", () => {
    // A trigger is a control with a current value; showing nothing reads as
    // broken. The state is unreachable from the menu — this is the guarantee
    // that a stale value cannot empty the control.
    expect(activeSort("nonsense" as ResultSort)).toBe(SORTS[0]);
  });

  it("agrees with the menu, row for row", () => {
    // The trigger and the marked row must be the same option — they are the
    // page's two statements about one fact, and this is the one place they can
    // disagree.
    for (const s of SORTS) expect(activeSort(s.value)).toBe(s);
  });
});

describe("activeTask", () => {
  const tasks: HubTask[] = [
    { tag: "text-generation", label: "Text generation", help: "Chat and completion models" },
    { tag: "text-to-image", label: "Image generation", help: null },
  ];

  it("says ANY TASK when nothing is filtered, and says what that means", () => {
    // Not a placeholder: it means any task THIS APP RUNS (D313, HS-0a). An
    // empty-looking control would invite the reader to think no constraint was
    // applied where in fact a whole registry is.
    const t = activeTask("", tasks);
    expect(t.label).toBe("Any task");
    expect(t.title).toContain("engine here can run");
    expect(activeTask("   ", tasks).label).toBe("Any task");
  });

  it("wears the filter's own label and its glossary sentence", () => {
    expect(activeTask("text-generation", tasks)).toEqual({
      label: "Text generation",
      title: "Chat and completion models",
    });
  });

  it("still explains a task the glossary has no sentence for", () => {
    // `help` is optional on the wire, and a trigger with no tooltip is better
    // than one claiming the wrong thing.
    expect(activeTask("text-to-image", tasks).label).toBe("Image generation");
    expect(activeTask("text-to-image", tasks).title.length).toBeGreaterThan(10);
  });

  it("never claims to be unfiltered while a filter is in force", () => {
    // The `hub/tasks` GET can fail (the menu is empty, the filter survives from
    // before) or a runner can be unregistered between the two. Falling back to
    // "Any task" there would make this the one control on the row describing a
    // different page than the one on screen — the results ARE narrowed.
    expect(activeTask("automatic-speech-recognition", []).label).toBe(
      "automatic-speech-recognition",
    );
    expect(activeTask("summarization", tasks).label).not.toBe("Any task");
  });
});

describe("bySizeAscending", () => {
  // Ids standing in for result rows; the sort is generic over the row, because
  // what it orders is HubModels and what the test needs is a size per row.
  const sizes = new Map<string, number | null | undefined>([
    ["big", 20_000_000_000],
    ["small", 500_000_000],
    ["mid", 4_000_000_000],
    ["hub-has-none", null],
    // "unasked" is deliberately absent from the map.
  ]);
  const order = (ids: string[]) => bySizeAscending(ids, (id) => sizes.get(id));

  it("puts the smallest repo first", () => {
    // ASCENDING, because the useful reading on a page where every card has a
    // Download button is "what fits" — somebody sorting multi-gigabyte models by
    // size is looking for the one they can afford.
    expect(order(["big", "small", "mid"])).toEqual(["small", "mid", "big"]);
  });

  it("puts every unmeasured repo last, both kinds together", () => {
    // `null` is the Hub having no total for that repo; `undefined` is nobody
    // having asked, or having asked and failed. Different facts about WHY there
    // is no number, neither of them a number — sorting either into the middle
    // would put an unknown between two known sizes and invite the reader to read
    // a size off its position.
    expect(order(["hub-has-none", "big", "unasked", "small"])).toEqual([
      "small",
      "big",
      "hub-has-none",
      "unasked",
    ]);
  });

  it("keeps the server's ranking as the tie-break", () => {
    // Stable, so two repos of identical size — and the twenty with no size at
    // all — stay in most-downloaded order rather than being shuffled into an
    // order nothing chose.
    const same = new Map([
      ["a", 1_000],
      ["b", 1_000],
      ["c", 1_000],
    ]);
    expect(bySizeAscending(["c", "a", "b"], (id) => same.get(id))).toEqual(["c", "a", "b"]);
    expect(order(["unasked", "hub-has-none"])).toEqual(["unasked", "hub-has-none"]);
  });

  it("leaves the caller's array alone", () => {
    // The grid renders from the server's answer while a measurement is in
    // flight; sorting that array in place would reorder it under the reader
    // before the page decided to.
    const ids = ["big", "small"];
    expect(order(ids)).toEqual(["small", "big"]);
    expect(ids).toEqual(["big", "small"]);
  });

  it("has nothing to say about an empty or single answer", () => {
    expect(order([])).toEqual([]);
    expect(order(["unasked"])).toEqual(["unasked"]);
  });
});

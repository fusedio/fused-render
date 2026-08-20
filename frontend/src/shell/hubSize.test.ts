import { beforeEach, describe, expect, it } from "bun:test";
import type { HubModel } from "@platform/lib/api";
import {
  _forgetTotalSizes,
  hubSizeLabel,
  hubSizeTitle,
  knownTotalSize,
  lookupTotalSize,
} from "./hubSize";

// The Discover tab shows two different measurements in one cell — the weights
// the Hub's dtype map describes, and (when there is no dtype map) the Hub's
// total for the whole repo. These tests are about telling them apart, and about
// the traffic the second one is allowed to cost: one request per repo, ever.

function row(extra: Partial<HubModel> = {}): HubModel {
  return {
    id: "org/m",
    task: "text generation",
    taskHelp: null,
    pipelineTag: "text-generation",
    capability: "text-generation",
    gated: null,
    library: null,
    downloads: null,
    likes: null,
    updated: null,
    params: null,
    estimatedSize: null,
    local: { state: "none" },
    url: "https://huggingface.co/org/m",
    ...extra,
  };
}

describe("hubSizeLabel", () => {
  it("shows the weights figure when the Hub published one", () => {
    expect(hubSizeLabel(row({ estimatedSize: 16_000_000_000 }), null)).toBe("≈15 GB");
  });

  it("prefers the weights figure over a total, so a repo never changes measure", () => {
    // A card with an estimate never asks for the fallback at all; if one ever
    // arrived anyway, the free and more meaningful number still wins.
    expect(hubSizeLabel(row({ estimatedSize: 16_000_000_000 }), 20_000_000_000)).toBe("≈15 GB");
  });

  it("falls back to the repo total for a repo with no dtype map", () => {
    // The mflux repo from the complaint: no safetensors, and a dash here until
    // this fallback existed. Same bytes the Hub's own page shows as 4.61 GB —
    // it counts in decimal, `formatSize` in binary, which is the app's unit
    // everywhere else and not a thing to special-case in one cell.
    expect(hubSizeLabel(row(), 4_619_599_193)).toBe("≈4.3 GB");
  });

  it("is a dash while nothing is known", () => {
    expect(hubSizeLabel(row(), null)).toBeNull();
  });
});

describe("hubSizeTitle", () => {
  it("says the weights figure is computed from parameter counts", () => {
    const title = hubSizeTitle(row({ estimatedSize: 16_000_000_000 }), null);
    expect(title).toContain("of weights");
    expect(title).toContain("parameter counts");
  });

  it("does NOT claim parameter counts for the fallback total", () => {
    // The whole reason this function takes the fallback: the total includes the
    // tokenizer, the configs and every quantised copy the author shipped, so
    // describing it as computed from parameter counts is a claim about work
    // that never happened.
    const title = hubSizeTitle(row(), 4_619_599_193);
    expect(title).toContain("total for this repo");
    expect(title).toContain("every file in it");
    expect(title).not.toContain("parameter counts");
  });

  it("does not claim a size is impossible when nobody has looked yet", () => {
    // The lookup is lazy, so this tooltip is what an unscrolled card shows. It
    // used to say the size "can't be computed here", which stopped being true
    // the moment a fallback existed.
    const title = hubSizeTitle(row(), null);
    expect(title).toContain("no safetensors metadata");
    expect(title).not.toContain("can't be computed");
  });
});

describe("lookupTotalSize", () => {
  beforeEach(_forgetTotalSizes);

  it("asks the server once and remembers the answer", async () => {
    const asked: string[] = [];
    const fetchSize = async (id: string) => {
      asked.push(id);
      return { usedStorage: 4_619_599_193 };
    };
    expect(await lookupTotalSize("org/m", fetchSize)).toBe(4_619_599_193);
    expect(await lookupTotalSize("org/m", fetchSize)).toBe(4_619_599_193);
    expect(asked).toEqual(["org/m"]);
    // …and a card mounting later can paint the number immediately.
    expect(knownTotalSize("org/m")).toBe(4_619_599_193);
  });

  it("collapses concurrent asks for the same repo into one request", async () => {
    // React 18 runs an effect twice in strict mode and an IntersectionObserver
    // can report the same card again before state settles. Either would be a
    // second call to a third party for a number already on its way.
    const asked: string[] = [];
    let release: (v: { usedStorage: number | null }) => void = () => {};
    const fetchSize = (id: string) => {
      asked.push(id);
      return new Promise<{ usedStorage: number | null }>((res) => {
        release = res;
      });
    };
    const a = lookupTotalSize("org/m", fetchSize);
    const b = lookupTotalSize("org/m", fetchSize);
    release({ usedStorage: 7 });
    expect(await a).toBe(7);
    expect(await b).toBe(7);
    expect(asked).toEqual(["org/m"]);
  });

  it("remembers 'the Hub has no number for this one' as an answer", async () => {
    // Null is a result, not a miss. Treating it as one would re-ask on every
    // scroll for exactly the repos the Hub cannot measure.
    let calls = 0;
    const fetchSize = async () => {
      calls += 1;
      return { usedStorage: null };
    };
    expect(await lookupTotalSize("org/m", fetchSize)).toBeNull();
    expect(await lookupTotalSize("org/m", fetchSize)).toBeNull();
    expect(calls).toBe(1);
    expect(knownTotalSize("org/m")).toBeNull();
  });

  it("keeps the dash rather than rejecting when the request fails", async () => {
    const fetchSize = async () => {
      throw new Error("offline");
    };
    expect(await lookupTotalSize("org/m", fetchSize)).toBeNull();
  });

  it("does not remember a Hub-side failure as 'no number'", async () => {
    // The server answers 200 with an `error` when the Hub call itself failed —
    // a rate limit, an unreachable Hub — and deliberately does NOT cache that,
    // so the next card can find out for itself. Caching it here would undo
    // exactly that: one 429 and the repo shows a dash until the tab closes.
    let calls = 0;
    const fetchSize = async () => {
      calls += 1;
      return calls === 1
        ? { usedStorage: null, error: "429 Too Many Requests" }
        : { usedStorage: 4_619_599_193 };
    };
    expect(await lookupTotalSize("org/m", fetchSize)).toBeNull();
    // Nothing was learned, so a card mounting later must not paint the failure
    // as an answer — `undefined` is what tells the two apart.
    expect(knownTotalSize("org/m")).toBeUndefined();
    expect(await lookupTotalSize("org/m", fetchSize)).toBe(4_619_599_193);
    expect(calls).toBe(2);
    expect(knownTotalSize("org/m")).toBe(4_619_599_193);
  });

  it("does not leave a rejected lookup wedged as forever-pending", async () => {
    // The in-flight map is what collapses concurrent asks. An id left in it
    // after a rejection would hand every later caller the same dead promise.
    let calls = 0;
    const fetchSize = async () => {
      calls += 1;
      if (calls === 1) throw new Error("offline");
      return { usedStorage: 7 };
    };
    expect(await lookupTotalSize("org/m", fetchSize)).toBeNull();
    expect(await lookupTotalSize("org/m", fetchSize)).toBe(7);
    expect(calls).toBe(2);
  });

  it("tells one repo from another", async () => {
    const fetchSize = async (id: string) => ({ usedStorage: id === "org/a" ? 1 : 2 });
    expect(await lookupTotalSize("org/a", fetchSize)).toBe(1);
    expect(await lookupTotalSize("org/b", fetchSize)).toBe(2);
  });

  it("has nothing to say about a repo nobody has asked about", () => {
    expect(knownTotalSize("org/never")).toBeUndefined();
  });
});

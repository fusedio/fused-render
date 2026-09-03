import { beforeEach, describe, expect, it } from "bun:test";
import type { HubModel } from "@platform/lib/api";
import {
  _forgetTotalSizes,
  hubSizeBytes,
  hubSizeLabel,
  hubSizeTitle,
  knownFit,
  knownSpeedEstimate,
  knownTotalSize,
  lookupTotalSize,
} from "./hubSize";
import { formatSize } from "@platform/lib/format";

// A Hub search result shows two different measurements in one cell — the weights
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
    fit: null,
    speedEstimate: null,
    created: null,
    baseModel: null,
    relation: null,
    quant: null,
    format: null,
    file: null,
    local: { state: "none" },
    url: "https://huggingface.co/org/m",
    matchScore: 50,
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

  it("falls back to the resolved file's own size for a GGUF row", () => {
    // A GGUF row names the ONE quantization it would download in `model.file`
    // (`formats.pick_gguf_file` on the server), and `lookupTotalSize` asked
    // about exactly that file, so `total` here IS this row's size. Same bytes
    // the Hub's own file listing shows — it counts in decimal, `formatSize`
    // in binary, which is the app's unit everywhere else and not a thing to
    // special-case in one cell.
    expect(hubSizeLabel(row({ file: "x-Q4_K_M.gguf" }), 4_619_599_193)).toBe("≈4.3 GB");
  });

  it("is a dash for a repo-wide total, even once one has resolved", () => {
    // No `model.file` means `total` is the Hub's repo-WIDE `usedStorage` —
    // every quantization the author shipped, summed — not a size this
    // particular row would download. This is the regression: a 9.1B-param
    // row used to read this number straight onto the card and show ≈154 GB,
    // about 17 bytes a parameter, because the number on screen was several
    // sibling GGUF files' worth of weights. An honest dash beats that.
    expect(hubSizeLabel(row(), 154_000_000_000)).toBeNull();
  });

  it("is a dash while nothing is known", () => {
    expect(hubSizeLabel(row(), null)).toBeNull();
  });
});

// The bytes a size SORT ranks by (D426). One rule: whatever the card is showing,
// because that number is the only evidence a reader has that the sort worked.
describe("hubSizeBytes", () => {
  it("ranks by exactly the figure the card shows", () => {
    // The same precedence as `hubSizeLabel`, asserted against it rather than
    // restated: the two drifting apart is a grid ordered by a number nobody can
    // see, sitting next to a column of numbers that does not ascend.
    for (const [est, file, total] of [
      [16_000_000_000, null, null],
      [16_000_000_000, null, 20_000_000_000],
      [null, "x-Q4_K_M.gguf", 4_619_599_193],
      [null, null, 154_000_000_000],
      [null, null, null],
    ] as const) {
      const m = row({ estimatedSize: est, file });
      const bytes = hubSizeBytes(m, total);
      expect(bytes === null || bytes === undefined).toBe(hubSizeLabel(m, total) === null);
      if (typeof bytes === "number") expect(hubSizeLabel(m, total)).toBe(`≈${formatSize(bytes)}`);
    }
  });

  it("prefers the weights estimate, which most results carry for free", () => {
    // Why a size sort is not two dozen outbound requests: the estimate rode in
    // on the search reply, and only the repos without one have to be asked
    // about — exactly the repos a card would have asked about anyway.
    expect(hubSizeBytes(row({ estimatedSize: 16_000_000_000 }), 20_000_000_000)).toBe(
      16_000_000_000,
    );
  });

  it("keeps 'nobody asked' apart from 'the Hub has no total'", () => {
    // Neither is a number and both sort last, but the caller deciding what to
    // MEASURE needs the difference — a null is answered, an undefined is not.
    expect(hubSizeBytes(row(), undefined)).toBeUndefined();
    expect(hubSizeBytes(row(), null)).toBeNull();
  });

  it("does not rank a row by a repo-wide total it has no file to justify", () => {
    // A zero estimate is not an estimate: the server reports no size rather
    // than a guessed one (HS-6), so this row falls through to `total` — but
    // with no `model.file`, `total` is the Hub's repo-wide sum, not this
    // row's own size, and ranking by it would sort the grid by a number the
    // cell does not show (`hubSizeLabel` reads a dash for the same input).
    expect(hubSizeBytes(row({ estimatedSize: 0 }), 5_000)).toBeNull();
    // The same total, scoped to this row's own resolved file, IS trustworthy.
    expect(hubSizeBytes(row({ estimatedSize: 0, file: "x-Q4_K_M.gguf" }), 5_000)).toBe(5_000);
  });
});

describe("hubSizeTitle", () => {
  it("says the weights figure is computed from parameter counts", () => {
    const title = hubSizeTitle(row({ estimatedSize: 16_000_000_000 }), null);
    expect(title).toContain("of weights");
    expect(title).toContain("parameter counts");
  });

  it("says there is no size to show for a repo-wide total with no resolved file", () => {
    // No `model.file` means `total` is the Hub's repo-WIDE `usedStorage` —
    // every file in the repo, including every quantised copy the author
    // shipped — not a size this row itself would download. The tooltip used
    // to describe that total as this row's size ("the Hub's total for this
    // repo... not just the weights a load would read"); saying so is still a
    // claim that the number belongs to this row, which it does not. There is
    // no trustworthy figure for this row, so the tooltip says that instead.
    const title = hubSizeTitle(row(), 4_619_599_193);
    expect(title).toContain("no safetensors metadata");
    expect(title).not.toContain("parameter counts");
    expect(title).not.toContain("total for this repo");
  });

  it("does not claim a size is impossible when nobody has looked yet", () => {
    // The lookup is lazy, so this tooltip is what an unscrolled card shows. It
    // used to say the size "can't be computed here", which stopped being true
    // the moment a fallback existed.
    const title = hubSizeTitle(row(), null);
    expect(title).toContain("no safetensors metadata");
    expect(title).not.toContain("can't be computed");
  });

  it("names the resolved FILE, not the whole repo, for a GGUF row", () => {
    // The bug this fixes: a GGUF repo's fallback used to be the Hub's
    // repo-wide total — every quantization the author published — even
    // though the row already knows the ONE file it would download
    // (`HubModel.file`). Once that file's own size is what `total` carries,
    // the tooltip has to say so rather than repeating the repo-total wording.
    const title = hubSizeTitle(row({ file: "x-Q4_K_M.gguf" }), 4_200_000_000);
    expect(title).toContain("x-Q4_K_M.gguf");
    expect(title).not.toContain("total for this repo");
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
    expect(await lookupTotalSize("org/m", null, fetchSize)).toBe(4_619_599_193);
    expect(await lookupTotalSize("org/m", null, fetchSize)).toBe(4_619_599_193);
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
    const a = lookupTotalSize("org/m", null, fetchSize);
    const b = lookupTotalSize("org/m", null, fetchSize);
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
    expect(await lookupTotalSize("org/m", null, fetchSize)).toBeNull();
    expect(await lookupTotalSize("org/m", null, fetchSize)).toBeNull();
    expect(calls).toBe(1);
    expect(knownTotalSize("org/m")).toBeNull();
  });

  it("keeps the dash rather than rejecting when the request fails", async () => {
    const fetchSize = async () => {
      throw new Error("offline");
    };
    expect(await lookupTotalSize("org/m", null, fetchSize)).toBeNull();
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
    expect(await lookupTotalSize("org/m", null, fetchSize)).toBeNull();
    // Nothing was learned, so a card mounting later must not paint the failure
    // as an answer — `undefined` is what tells the two apart.
    expect(knownTotalSize("org/m")).toBeUndefined();
    expect(await lookupTotalSize("org/m", null, fetchSize)).toBe(4_619_599_193);
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
    expect(await lookupTotalSize("org/m", null, fetchSize)).toBeNull();
    expect(await lookupTotalSize("org/m", null, fetchSize)).toBe(7);
    expect(calls).toBe(2);
  });

  it("tells one repo from another", async () => {
    const fetchSize = async (id: string) => ({ usedStorage: id === "org/a" ? 1 : 2 });
    expect(await lookupTotalSize("org/a", null, fetchSize)).toBe(1);
    expect(await lookupTotalSize("org/b", null, fetchSize)).toBe(2);
  });

  it("has nothing to say about a repo nobody has asked about", () => {
    expect(knownTotalSize("org/never")).toBeUndefined();
  });

  // Bug chain fix: a GGUF row's size comes from the ONE file it would
  // download, not a repo-wide total that counts every quantization the
  // author published.
  it("passes the resolved FILE through and reads fileSize, not usedStorage", async () => {
    const asked: Array<[string, string | undefined]> = [];
    const fetchSize = async (id: string, file?: string) => {
      asked.push([id, file]);
      // A generous repo-wide total that would be the WRONG answer if this
      // read `usedStorage` instead of `fileSize` — the exact bug.
      return { usedStorage: 1_400_000_000_000, fileSize: 4_200_000_000 };
    };
    expect(await lookupTotalSize("unsloth/x-GGUF", "x-Q4_K_M.gguf", fetchSize)).toBe(4_200_000_000);
    expect(asked).toEqual([["unsloth/x-GGUF", "x-Q4_K_M.gguf"]]);
  });

  it("still reads usedStorage when no file is given (every non-GGUF fallback)", async () => {
    const fetchSize = async () => ({ usedStorage: 4_619_599_193, fileSize: null });
    expect(await lookupTotalSize("org/m", null, fetchSize)).toBe(4_619_599_193);
  });
});

// Regression: a live "klein" search once put ≈154 GB on a 9.1B-parameter row
// (≈17 bytes/param — impossible even at fp32's 4) and ≈91 GB on another
// 9.1B row. Both were the Hub's repo-wide `usedStorage` for a multi-quant
// GGUF repo — the sum of every quantization the author shipped — read
// straight onto a row that names none of them (`model.file` null). A
// 12.3B row reading ≈11 GB and an 8.2B row reading ≈8.1 GB, by contrast,
// were each that row's own resolved quantization file and stayed plausible.
describe("regression: the klein search's impossible SIZE figures", () => {
  it("never shows a multi-quant repo's total on a 9.1B row with no resolved file", () => {
    const nineBillionParams = row({ params: 9_100_000_000, file: null });
    // The repo-wide total the Hub returned for the multi-quant klein repo —
    // this is what used to print as "≈154 GB".
    expect(hubSizeLabel(nineBillionParams, 154_000_000_000)).toBeNull();
    // The second 9.1B row from the same search, same shape, different repo —
    // this used to print as "≈91 GB".
    expect(hubSizeLabel(nineBillionParams, 91_000_000_000)).toBeNull();
    // A blank-params row got the same repo-wide treatment, at a smaller
    // total ("≈172 MB") — no `params` to sanity-check against does not make
    // an unscoped total any more this row's own size.
    expect(hubSizeLabel(row({ params: null, file: null }), 172_000_000)).toBeNull();
  });

  it("still shows a row's own resolved quantization file as a plausible figure", () => {
    // The 12.3B row: ≈11 GB, about 0.9 bytes/param — a real quantized size,
    // and trustworthy because `total` here is THIS row's resolved file.
    expect(
      hubSizeLabel(
        row({ params: 12_300_000_000, file: "y-Q4_K_M.gguf" }),
        11_000_000_000,
      ),
    ).toBe("≈10 GB");
    // The 8.2B row: ≈8.1 GB, about 1 byte/param — also plausible, also a
    // resolved file's own size.
    expect(
      hubSizeLabel(row({ params: 8_200_000_000, file: "z-Q8_0.gguf" }), 8_100_000_000),
    ).toBe("≈7.5 GB");
  });
});

describe("knownFit and knownSpeedEstimate", () => {
  beforeEach(_forgetTotalSizes);

  it("rides the same lookup that resolves the size, for a row with a file", async () => {
    const fit = { verdict: "easy" as const, basis: "declared" as const, footprintBytes: 1, score: 100 };
    const speedEstimate = {
      tokensPerSecond: 42, method: "bandwidth" as const, backend: "metal-mlx" as const,
      bandwidthGbS: 200, contextTokens: 8192, calibrated: false, calibrationFactor: null,
    };
    const fetchSize = async () => ({ usedStorage: null, fileSize: 4_000_000_000, fit, speedEstimate });
    // Nothing known before the lookup resolves.
    expect(knownFit("org/g", "m.gguf")).toBeUndefined();
    await lookupTotalSize("org/g", "m.gguf", fetchSize);
    expect(knownFit("org/g", "m.gguf")).toEqual(fit);
    expect(knownSpeedEstimate("org/g", "m.gguf")).toEqual(speedEstimate);
  });

  it("is null, not undefined, once a lookup resolves with nothing to judge", async () => {
    const fetchSize = async () => ({ usedStorage: 9, fileSize: null });
    await lookupTotalSize("org/m", null, fetchSize);
    expect(knownFit("org/m")).toBeNull();
    expect(knownSpeedEstimate("org/m")).toBeNull();
  });

  it("has nothing to say about a repo nobody has asked about", () => {
    expect(knownFit("org/never")).toBeUndefined();
    expect(knownSpeedEstimate("org/never")).toBeUndefined();
  });
});

// Code review F1: the cache used to be keyed by repo id ALONE, so a
// `file: null` ask (the page-level Size sort, `measureSizes`) and a
// `file`-scoped ask (a table row, `HubResultsTable`) for the SAME repo
// clobbered each other — whichever answered first "won" for both, even
// though the two questions have different answers (a repo-wide total with no
// fit, vs. one file's bytes with a fit verdict riding along).
describe("the size cache is keyed by (id, file), not id alone", () => {
  beforeEach(_forgetTotalSizes);

  it("keeps a repo-wide lookup and a file-scoped lookup for the same repo apart", async () => {
    const asked: Array<[string, string | undefined]> = [];
    const fetchSize = async (id: string, file?: string) => {
      asked.push([id, file]);
      return file
        ? { usedStorage: null, fileSize: 4_200_000_000, fit: { verdict: "easy" as const, basis: "declared" as const, footprintBytes: 1, score: 100 } }
        : { usedStorage: 15_000_000_000 };
    };
    // The page-level Size sort asks first, with no file — this used to poison
    // the id-only cache entry for every row asking about the SAME repo.
    expect(await lookupTotalSize("unsloth/x-GGUF", null, fetchSize)).toBe(15_000_000_000);
    // A table row then asks about its own resolved file, and must NOT be
    // answered from the repo-wide entry above.
    expect(await lookupTotalSize("unsloth/x-GGUF", "x-Q4_K_M.gguf", fetchSize)).toBe(4_200_000_000);
    expect(asked).toEqual([
      ["unsloth/x-GGUF", undefined],
      ["unsloth/x-GGUF", "x-Q4_K_M.gguf"],
    ]);
    // And each question keeps its own answer for a later reader.
    expect(knownTotalSize("unsloth/x-GGUF", null)).toBe(15_000_000_000);
    expect(knownTotalSize("unsloth/x-GGUF", "x-Q4_K_M.gguf")).toBe(4_200_000_000);
    // Only the file-scoped ask carries a fit verdict — the repo-wide one never
    // asked for a capability to judge against.
    expect(knownFit("unsloth/x-GGUF", null)).toBeNull();
    expect(knownFit("unsloth/x-GGUF", "x-Q4_K_M.gguf")).not.toBeNull();
  });
});

// ---- what the full search screen OFFERS, pinned against the source -------
import { beforeEach, describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { _forgetTotalSizes, lookupTotalSize } from "@apps/ai_models/lib/hubSize";
import { measureSizes } from "./HubSearchScreen";

const SRC = readFileSync(join(import.meta.dir, "HubSearchScreen.tsx"), "utf8");

describe("HubSearchScreen back link", () => {
  it("goes back to the SAME capability's plain name, never a different one", () => {
    expect(SRC).toContain("← Back to {meta.plain}");
    expect(SRC).toContain("data-adv-back=\"1\"");
  });
});

describe("HubSearchScreen one row per hit", () => {
  it("renders one row per repo, no family grouping", () => {
    expect(SRC).not.toContain("groupIntoFamilies");
    expect(SRC).not.toContain('from "./hubFamilies"');
  });

  it("never offers a Download for a repo already on this Mac", () => {
    const start = SRC.indexOf("function HitRow(");
    const body = SRC.slice(start, SRC.indexOf("\nexport function HubSearchScreen"));
    expect(body).toContain('const have = disk.state === "downloaded";');
    expect(body).toContain("have ? (");
    // Item A: the plain "✓ Downloaded" caption became a quant-aware one
    // (`downloadedVariantLabel`) so a non-default cached variant is named
    // rather than reading as the repo's default download.
    expect(body).toContain("downloadedVariantLabel({");
  });

  it("states the memory reason for a will-not-fit row rather than inventing one", () => {
    expect(SRC).toContain('model.fit?.verdict === "no"');
    expect(SRC).toContain("formatSize(model.fit.footprintBytes)");
  });
});

describe("HubSearchScreen pagination", () => {
  it("asks for 20 more without resetting the settled query", () => {
    expect(SRC).toContain("setLimit((n) => n + LOAD_MORE)");
  });
});

// Item 1 (fix round 4): the Size sort's lookup must match what the row's own
// cell would show — a GGUF row's resolved FILE, never the repo-wide total —
// and must not spend a lookup on a row `hubSizeBytes` can never use anyway.
describe("measureSizes (item 1)", () => {
  beforeEach(_forgetTotalSizes);

  it("ranks a GGUF multi-quant row by its own resolved file's size, not the repo-wide total", async () => {
    const asked: Array<[string, string | undefined]> = [];
    const fetchSize = async (id: string, file?: string) => {
      asked.push([id, file]);
      // A generous multi-quant repo total that would be the wrong sort key
      // if this read `usedStorage` (via a `file: null` ask) instead of the
      // resolved file's own `fileSize`.
      return file
        ? { usedStorage: 1_400_000_000_000, fileSize: 4_200_000_000 }
        : { usedStorage: 1_400_000_000_000 };
    };
    // Seed the (id, file)-keyed cache the same way a real card lookup would —
    // `measureSizes` itself always asks with its default (network) fetcher,
    // so pre-resolving the key here is how the test controls the answer.
    await lookupTotalSize("unsloth/x-GGUF", "x-Q4_K_M.gguf", fetchSize);

    const sizes = await measureSizes([{ id: "unsloth/x-GGUF", file: "x-Q4_K_M.gguf" }], () => true);
    expect(sizes.get("unsloth/x-GGUF")).toBe(4_200_000_000);
    expect(asked).toEqual([["unsloth/x-GGUF", "x-Q4_K_M.gguf"]]);
  });

  it("skips the lookup entirely for a row with no file — hubSizeBytes can never use it", async () => {
    const sizes = await measureSizes([{ id: "org/no-file", file: null }], () => true);
    expect(sizes.get("org/no-file")).toBeNull();
  });
});

// Item 2 (fix round 4): the debounce effect must (a) not settle when the
// text matches what is already settled — opening the screen must issue
// exactly one search, not a duplicate on mount — and (b) merge into the
// LATEST settled state when the timer fires, not a stale closure, so a
// filter/sort click inside the 350ms window survives.
describe("HubSearchScreen debounce (item 2)", () => {
  it("skips onSettle when the live text already matches settled.q (no duplicate mount search)", () => {
    expect(SRC).toContain("if (liveQuery === settledRef.current.q) return;");
  });

  it("merges the timer into a ref tracking the LATEST settled, not a stale closure", () => {
    // A plain `settled` read inside the 350ms setTimeout callback would close
    // over whatever `settled` was when the effect last ran (deps: [liveQuery]
    // only) — a ref updated every render is what makes onSettle merge into
    // whatever settled state is current when the timer actually fires.
    expect(SRC).toContain("const settledRef = useRef(settled);");
    expect(SRC).toContain("settledRef.current = settled;");
    const timeoutStart = SRC.indexOf("debounce.current = window.setTimeout(() => {");
    const timeoutBody = SRC.slice(timeoutStart, SRC.indexOf("}, 350);"));
    expect(timeoutBody).toContain("onSettle({ ...settledRef.current, q: liveQuery });");
    expect(timeoutBody).not.toMatch(/onSettle\(\{\s*\.\.\.settled,/);
  });
});

// Item 3 (fix round 4): empty-result copy must not literally quote an empty
// query — the default state reached from a pane's own "Search Hugging Face
// for more…" door opens with a task filter and no typed query.
describe("HubSearchScreen empty-result copy (item 3)", () => {
  it("says something other than `matches \"\"` when q is empty", () => {
    expect(SRC).toContain("settled.q.trim()");
    expect(SRC).not.toContain('Nothing on {host} matches "{settled.q}"');
  });
});

// Item 4 (fix round 4): the dead `display: none` summary paragraph, left
// over from the HubResults port, is gone.
describe("HubSearchScreen dead summary output (item 4)", () => {
  it("no longer computes or renders the hidden summary paragraph", () => {
    expect(SRC).not.toContain("resultsSummary");
    expect(SRC).not.toContain('style={{ display: "none" }}');
  });
});

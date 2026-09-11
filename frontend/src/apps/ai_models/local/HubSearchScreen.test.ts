// ---- what the full search screen OFFERS, pinned against the source -------
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

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
    expect(body).toContain('disk.state === "downloaded" ? (');
    expect(body).toContain("✓ Downloaded");
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

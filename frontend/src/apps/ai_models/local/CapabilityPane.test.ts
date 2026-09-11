// ---- what a capability pane OFFERS, pinned against the source ------------
// Same discipline as ModelRow.test.ts: the off/on branch and the
// have/suggest group headings are one-sentence facts a screenshot does not
// distinguish from their near neighbours.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const PANE = readFileSync(join(import.meta.dir, "CapabilityPane.tsx"), "utf8");

describe("CapabilityPane off/on split", () => {
  it("renders the registry's own reason sentence, never inventing a diagnosis", () => {
    expect(PANE).toContain("<b>This Mac has no engine that can run {capabilityLabel(capabilityKey).toLowerCase()} models</b> — it{\" \"}");
    expect(PANE).toContain("{offReason}. There is nothing to download or search for here");
  });

  it("offers no suggestions and no Hub door in the off pane — it returns before either is drawn", () => {
    const offBranchStart = PANE.indexOf("if (offReason) {");
    const offBranchEnd = PANE.indexOf("\n  }\n", offBranchStart);
    const offBranch = PANE.slice(offBranchStart, offBranchEnd);
    expect(offBranch).not.toContain("pane.suggest");
    expect(offBranch).not.toContain("pane.door");
    expect(offBranch).not.toContain("hubdoor");
  });

  it("still lists on-disk rows in the off pane, with noEngine so Try is disabled", () => {
    const offBranchStart = PANE.indexOf("if (offReason) {");
    const offBranchEnd = PANE.indexOf("\n  }\n", offBranchStart);
    const offBranch = PANE.slice(offBranchStart, offBranchEnd);
    expect(offBranch).toContain("opts={{ noEngine: true }}");
  });
});

describe("CapabilityPane head", () => {
  it("shows a one-time-download chip only when nothing is on this Mac", () => {
    expect(PANE).toContain('<span className="chip">Needs a one-time download</span>');
  });

  it("shows the on-this-Mac count and total size otherwise", () => {
    expect(PANE).toContain("{have.length} on this Mac ·");
  });
});

describe("CapabilityPane suggestions", () => {
  it("labels the group Start with one of these when nothing is downloaded yet, else Also worth having", () => {
    expect(PANE).toContain('{have.length ? "Also worth having" : "Start with one of these"}');
  });

  it("shows only two suggestions until expanded, and counts the rest correctly", () => {
    expect(PANE).toContain("const shown = expanded ? rest : rest.slice(0, 2);");
    expect(PANE).toContain("const moreCount = rest.length - 2;");
  });

  it("pulls the downloading row out of the ordinary list so it is not shown twice", () => {
    expect(PANE).toContain("recommended.filter((m) => m.id !== downloadingId)");
  });

  it("gives the first suggestion the primary treatment only on an empty-Mac capability with nothing downloading", () => {
    expect(PANE).toContain("primary: !have.length && !downloadingRow && i === 0");
  });
});

describe("EngineFilesPane", () => {
  it("offers only a delete action, no search, on every row", () => {
    const start = PANE.indexOf("export function EngineFilesPane(");
    const body = PANE.slice(start);
    expect(body).toContain('title="Delete"');
    expect(body).not.toContain("data-adv");
  });

  it("states in words that there is no search here on purpose", () => {
    expect(PANE).toContain("There is no search here on purpose");
  });
});

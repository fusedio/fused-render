// ---- what a model row OFFERS, pinned against the source ------------------
// Same discipline as CapabilityNav.test.ts: the
// four action-slot branches and the drawer's "not recorded" fallbacks are
// one-line facts a screenshot does not distinguish from their near
// neighbours (Try vs disabled Try, a note vs the cache-row sentence), so
// they are pinned by reading the source.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROW = readFileSync(join(import.meta.dir, "ModelRow.tsx"), "utf8");

describe("ModelRow action slot", () => {
  it("shows Stop while downloading, before any other branch", () => {
    const stopAt = ROW.indexOf("if (opts.downloading) {");
    const noEngineAt = ROW.indexOf("if (model.have && opts.noEngine) {");
    expect(stopAt).toBeGreaterThan(-1);
    expect(noEngineAt).toBeGreaterThan(stopAt);
  });

  it("disables Try and offers no Download when a downloaded model has no engine", () => {
    expect(ROW).toContain('title="No engine on this device can run it"');
  });

  it("uses a plain (Download-matching) Try link only for a model this Mac actually has", () => {
    expect(ROW).toContain('className="btn"');
    expect(ROW).toContain("href={tryHref}");
  });

  it("Try is a real link to the Playground carrying the encoded repo id (item I)", () => {
    expect(ROW).toContain('tabHref("playground", `?model=${encodeURIComponent(model.id)}`)');
    expect(ROW).toContain("navigateUrl(tryHref)");
    expect(ROW).toContain("handlers.onTry?.(model.id)");
  });

  it("gives Download the primary class only when opts.primary is set", () => {
    expect(ROW).toContain('`btn${opts.primary ? " btn-primary" : ""}`');
  });

  it("never offers a delete button for a row this Mac does not have", () => {
    const downloadBranchStart = ROW.lastIndexOf("return (\n    <>\n      <button");
    const downloadBranch = ROW.slice(downloadBranchStart);
    expect(downloadBranch).not.toContain("onDelete");
  });
});

describe("ModelRow chips", () => {
  it("renders Last used, Our pick and a caller-supplied warning chip", () => {
    expect(ROW).toContain('className="chip last-chip"');
    expect(ROW).toContain('className="chip rec-chip"');
    expect(ROW).toContain('className="chip warn-chip"');
  });
});

describe("ModelRow drawer", () => {
  it("falls back to a visible not-recorded marker rather than guessing", () => {
    expect(ROW).toContain('<span className="unknown">not recorded</span>');
    expect(ROW).toContain("function fact(value: string | null): ReactNode");
  });

  it("always states the format field as its own line, never inferred", () => {
    expect(ROW).toContain("<dt>Format</dt>");
    expect(ROW).toContain("{model.format}");
  });

  it("distinguishes curated rows (a why-note) from cache rows (the fallback sentence)", () => {
    expect(ROW).toContain("<b>Why we suggest it.</b>");
    expect(ROW).toContain("Not one of our suggestions");
  });

  it("only shows Last used, Path and Show in Finder for a model this Mac has", () => {
    const drawerFn = ROW.slice(ROW.indexOf("function Drawer("), ROW.indexOf("function Actions("));
    expect(drawerFn).toContain("{model.have && (");
    expect(drawerFn).toContain("Show in Finder");
  });

  it("links out to the Hub via hubModelUrl rather than a hand-built URL", () => {
    expect(ROW).toContain("hubModelUrl(model.id)");
  });
});

// The reserved row-path both row-activation call sites (Listing.tsx's
// onRowPointerUp, useListingSelection.ts's Enter case) branch on to run the
// zero-match glob-broadening offer instead of navigate().
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  isZeroMatchOfferPath,
  ZERO_MATCH_OFFER_PATH,
} from "@apps/explorer/listing/zero-match-offer";

describe("the zero-match offer sentinel", () => {
  test("carries a NUL byte, which is illegal in a POSIX or Windows path — no real row can ever collide with it", () => {
    expect(ZERO_MATCH_OFFER_PATH).toContain("\0");
  });

  test("the predicate recognizes only the sentinel itself", () => {
    expect(isZeroMatchOfferPath(ZERO_MATCH_OFFER_PATH)).toBe(true);
  });

  test("the predicate rejects real filesystem paths, including ones that echo the sentinel's own words", () => {
    expect(isZeroMatchOfferPath("/home/iamsdas/zero-match-offer")).toBe(false);
    expect(isZeroMatchOfferPath("")).toBe(false);
    expect(isZeroMatchOfferPath("/")).toBe(false);
  });
});

// Both wiring bugs below are pinned at the source: neither the sentinel
// branch of onRowPointerUp nor the offer row's own <button> can be driven
// through a headless React renderer (Listing.tsx has no full-mount test
// harness — see selection.test.ts's own header and its "the listing rows
// wire both halves of the model" precedent for this same technique).
describe("the offer row's activation wiring", () => {
  const src = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");

  test("the sentinel branch only reruns on the primary button, like every real row's press does", () => {
    const branch = src.slice(
      src.indexOf("if (isZeroMatchOfferPath(path)) {"),
      src.indexOf("const press = pressRef.current;"),
    );
    expect(branch).toMatch(/if \(e\.button !== 0\) return;/);
  });

  test("the offer button wires onClick, so a keyboard Tab+Enter/Space can activate it", () => {
    // `navActive` (useListingSelection.ts) requires focus on the search
    // input or document body/root — a focused <button> fails that check, so
    // the document-level Enter handler never reaches this offer at all
    // unless the button answers a plain `click` itself.
    const row = src.slice(
      src.indexOf("broadenOffer !== null ? ("),
      src.indexOf(") : ("),
    );
    expect(row).toContain("onClick={");
    // Guarded against double-firing for a real pointer interaction, which
    // onPointerUp above already handles — see the guard's own comment.
    expect(row).toMatch(/if \(e\.detail !== 0\) return;/);
  });
});

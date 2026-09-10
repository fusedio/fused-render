// The reserved row-path both row-activation call sites (Listing.tsx's
// onRowPointerUp, useListingSelection.ts's Enter case) branch on to run the
// zero-match glob-broadening offer instead of navigate().
import { describe, expect, test } from "bun:test";
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

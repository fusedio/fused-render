// isRasterIconUrl decides whether an icon is drawn as a clipped raster tile:
// /raw URLs name the file in `?path=`, Marketplace assets in their own path
// (with a `?v=` cache key after it).
import { describe, expect, test } from "bun:test";

import { installDomShim } from "./testDomShim";

installDomShim();

const { isRasterIconUrl } = await import("./app-icon-src");

describe("isRasterIconUrl", () => {
  test("reads /raw ?path= and plain paths", () => {
    expect(isRasterIconUrl("/raw?path=%2Fa%2Ficon.png&v=3")).toBe(true);
    expect(isRasterIconUrl("/raw?path=%2Fa%2Ficon.svg")).toBe(false);
    expect(isRasterIconUrl("/x/icon.png")).toBe(true);
  });

  test("a Marketplace asset keeps its type behind a ?v= cache key", () => {
    expect(isRasterIconUrl("/api/marketplace/asset/app/icon.png?v=abc123")).toBe(true);
    expect(isRasterIconUrl("/api/marketplace/asset/app/icon.svg?v=abc123")).toBe(false);
  });

  test("data: URLs and null are never raster", () => {
    expect(isRasterIconUrl(null)).toBe(false);
    expect(isRasterIconUrl("data:image/png;base64,AAAA")).toBe(false);
  });
});

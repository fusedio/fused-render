// The one grid's order (newest release first; a count-sort once the
// catalog carries counts) and the footer.
import { describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { sortApps, hasSortSignal, CatalogFooter } = await import("./Discover");
type AppEntry = import("@platform/lib/marketplace").AppEntry;
type Catalog = import("@platform/lib/marketplace").Catalog;

const mk = (slug: string, category: string) => ({ slug, category }) as AppEntry;
const catalog = (over: Partial<Catalog>): Catalog =>
  ({
    status: "ok",
    categories: [],
    collections: [],
    apps: [],
    ...over,
  }) as Catalog;

describe("sortApps", () => {
  const at = (
    slug: string,
    when: string | null,
    over: Partial<AppEntry> = {},
  ) => ({ slug, category: "x", last_release_at: when, ...over }) as AppEntry;
  const order = (out: AppEntry[]) => out.map((a) => a.slug);

  test("newest: latest release first, undated last, ties keep catalog order", () => {
    const apps = [
      at("old", "2026-01-01T00:00:00Z"),
      at("none", null),
      at("new", "2026-10-01T00:00:00Z"),
      at("none2", null),
    ];
    expect(order(sortApps(apps, "newest"))).toEqual([
      "new",
      "old",
      "none",
      "none2",
    ]);
  });

  test("remixed and installed sort by the count, newest breaking ties", () => {
    const apps = [
      at("a", "2026-01-01T00:00:00Z", { remix_count: 1, install_count: 5 }),
      at("b", "2026-03-01T00:00:00Z", { remix_count: 1, install_count: 0 }),
      at("c", "2026-02-01T00:00:00Z", { remix_count: 0, install_count: 9 }),
    ];
    expect(order(sortApps(apps, "remixed"))).toEqual(["b", "a", "c"]);
    expect(order(sortApps(apps, "installed"))).toEqual(["c", "a", "b"]);
  });
});

describe("hasSortSignal", () => {
  test("only a remix or an install count counts; contributors alone do not", () => {
    expect(
      hasSortSignal([
        mk("a", "x"),
        { ...mk("b", "x"), contributor_count: 3 } as AppEntry,
      ]),
    ).toBe(false);
    expect(
      hasSortSignal([{ ...mk("a", "x"), remix_count: 1 } as AppEntry]),
    ).toBe(true);
    expect(
      hasSortSignal([{ ...mk("a", "x"), install_count: 1 } as AppEntry]),
    ).toBe(true);
  });
});

describe("CatalogFooter", () => {
  test("a numeric or missing catalog_version never throws; the segment hides when empty", () => {
    const text = (v: unknown) => {
      const out = JSON.stringify(
        CatalogFooter({
          catalog: catalog({
            source: "udf-api",
            catalog_version: v as string,
            fetched_at: "",
          }),
        }),
      );
      return out;
    };
    expect(text(20261015123)).toContain(" · catalog ");
    expect(text(null)).not.toContain(" · catalog ");
    expect(text("")).not.toContain(" · catalog ");
  });
});

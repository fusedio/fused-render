// Shelves show whole rows of the grid as it is actually laid out (2, 3 or 4
// columns), never a lone orphan card on a second row.
import { describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { shelfCount, homeSections } = await import("./Discover");
type AppEntry = import("@platform/lib/marketplace").AppEntry;
type Catalog = import("@platform/lib/marketplace").Catalog;

const mk = (slug: string, category: string) => ({ slug, category }) as AppEntry;
const catalog = (over: Partial<Catalog>): Catalog =>
  ({ status: "ok", categories: [], collections: [], apps: [], ...over }) as Catalog;

describe("shelfCount", () => {
  test("fewer apps than one row are all shown", () => {
    expect(shelfCount(3, 4)).toBe(3);
    expect(shelfCount(2, 2)).toBe(2);
  });
  test("drops the orphan row", () => {
    expect(shelfCount(4, 3)).toBe(3);
    expect(shelfCount(3, 2)).toBe(2);
    expect(shelfCount(5, 4)).toBe(4);
  });
  test("caps at two whole rows", () => {
    expect(shelfCount(12, 3)).toBe(6);
    expect(shelfCount(12, 4)).toBe(8);
    expect(shelfCount(7, 2)).toBe(4);
    expect(shelfCount(9, 1)).toBe(2);
  });
});

describe("homeSections", () => {
  const apps = [mk("a", "starters"), mk("b", "local-ai"), mk("c", "local-ai")];

  test("collections drive the sections: order, titles, unknown slugs dropped", () => {
    const cat = catalog({
      categories: [{ id: "starters", label: "Starters", count: 1 }],
      collections: [
        { id: "new-and-updated", kind: "new_and_updated", title: "New and updated", slugs: ["c", "ghost", "a"] },
        { id: "local-ai", kind: "shelf", title: "Local AI", slugs: ["b", "c"] },
        { id: "empty", kind: "shelf", title: "Empty", slugs: ["ghost"] },
      ],
    });
    const out = homeSections(cat, apps);
    expect(out.map((s) => [s.id, s.title, s.category, s.apps.map((a) => a.slug)])).toEqual([
      ["new-and-updated", "New and updated", null, ["c", "a"]],
      ["local-ai", "Local AI", "local-ai", ["b", "c"]],
    ]);
  });

  test("no collections falls back to a shelf per non-empty category", () => {
    const cat = catalog({
      categories: [
        { id: "starters", label: "Starters", count: 1 },
        { id: "local-ai", label: "Local AI", count: 2 },
        { id: "none", label: "None", count: 0 },
      ],
    });
    const out = homeSections(cat, apps);
    expect(out.map((s) => [s.id, s.title, s.category, s.apps.length])).toEqual([
      ["starters", "Starters", "starters", 1],
      ["local-ai", "Local AI", "local-ai", 2],
    ]);
  });
});

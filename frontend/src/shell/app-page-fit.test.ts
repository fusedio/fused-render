// The app page's header and tab strip fold to icons when the room runs out
// (app-page-fit.ts). The verdict is `pickLevelFromNeeds`'s, pinned in
// row-fit.test.ts; what is proved here is the wiring — the words carry the
// one class the ladders hide, the stylesheet hides it by `[data-fit]` and a
// non-zero level only, and the tooltips keep the words.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "bun:test";
import { HEAD_DROPS, TABBAR_DROPS, TITLE_FLOOR, APP_PAGE_FIT_LABEL } from "./app-page-fit";

const SHELL = new URL(".", import.meta.url).pathname;
const APP = readFileSync(join(SHELL, "AppPage.tsx"), "utf8");
const CSS = readFileSync(join(SHELL, "..", "styles", "app-page.css"), "utf8");

describe("the ladders", () => {
  it("hide the words by one class, and the stylesheet names a non-zero level for every rule", () => {
    expect(APP_PAGE_FIT_LABEL).toBe("app-page-fit-lbl");
    for (const sel of [...HEAD_DROPS, ...TABBAR_DROPS]) {
      const last = sel.split(" ").pop()!;
      expect(CSS).toContain(last);
    }
    const rules = CSS.split("}")
      .map((chunk) => chunk.slice(chunk.lastIndexOf("*/") + 1).split("{")[0] ?? "")
      .filter((sel) => /app-page-fit-lbl|app-version-picker-eyebrow\b/.test(sel) && /data-fit/.test(sel));
    expect(rules.length).toBeGreaterThan(1);
    for (const sel of rules) {
      for (const one of sel.split(",")) {
        if (!one.trim()) continue;
        expect(one).toMatch(/\[data-fit\]:not\(\[data-fit="0"\]\)|\[data-fit="[1-9]"\]/);
      }
    }
  });

  it("charge the title a floor, not its rendered width — a column inside a row is not a row", () => {
    expect(TITLE_FLOOR).toBeGreaterThan(100);
    expect(readFileSync(join(SHELL, "app-page-fit.ts"), "utf8")).toContain("return TITLE_FLOOR + ");
  });
});

describe("the page", () => {
  it("puts the refs and the level on the header and the tab bar", () => {
    expect(APP).toContain('<header className="app-page-head" ref={headRef} data-fit={headFit}>');
    expect(APP).toContain('<div className="app-page-tabbar flex-none" ref={tabbarRef} data-fit={tabbarFit}>');
  });

  it("wraps every foldable word, and keeps it in a tooltip", () => {
    expect(APP).toContain("<span className={APP_PAGE_FIT_LABEL}>Share</span>");
    expect(APP).toContain("<span className={APP_PAGE_FIT_LABEL}>Open</span>");
    expect(APP).toContain("<span className={APP_PAGE_FIT_LABEL}>{label}</span>");
    expect(APP).toContain('title="Open the app in the Explorer"');
    expect(APP).toContain("title={label}");
    expect(APP).toContain("aria-label={label}");
  });
});

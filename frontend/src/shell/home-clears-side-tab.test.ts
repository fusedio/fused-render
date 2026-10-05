// A fresh page from Home opens the default companion (Claude): landing on either
// home page drops the remembered sidebar tab. Source pin — App.tsx has no render
// harness — plus the store contract it relies on.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

test("App clears the remembered tab when the Home or explorer-home page shows", () => {
  const app = readFileSync(join(import.meta.dir, "App.tsx"), "utf8");
  expect(app).toContain('import { setSideTab } from "@apps/explorer/lib/side-tab-store";');
  expect(app).toContain("if (isHome || isExplorerHome) setSideTab(null);");
  // only the tab: the open/closed flag and the width are global
  expect(app).not.toContain("setSideHidden(");
});

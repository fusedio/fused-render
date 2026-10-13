import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("./AppsWidget.tsx", import.meta.url), "utf8");

test("icon tiles open the same app context menu as cards", () => {
  const tile = src.slice(src.indexOf("function AppTile"), src.indexOf("export function AppsWidget"));
  expect(tile).toContain("onContextMenu={(e) => onContextMenu(e, app)}");
  expect(src).toContain("<AppTile key={app.path} app={app} onContextMenu={openCardMenu} />");
});

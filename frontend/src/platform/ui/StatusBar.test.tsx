// StatusBar's own composition rules (D565, code review finding #8; the
// status-bar merge narrowed this to two sections, a follow-up revision split
// Models back out into its own chip for a third): Models, Activity and
// Notifications, left to right, always rendered — replacing
// `tests/test_queue_dock.py::test_the_bar_reserves_space_inside_main_not_the_floating_column`,
// which only ever grepped the stylesheet for a literal `.status-bar:empty`
// string and could not see whether the bar actually behaves this way. The
// bare-`<DownloadManager />` fallback for an omitted `activity` prop is left
// to that pytest's own source check (`{activity ?? <DownloadManager />}`):
// the real `DownloadManager` polls `/api/jobs` on mount, which this file
// cannot exercise without mocking `@platform/lib/api` — the exact
// contamination risk DownloadManager.test.tsx's own header comment documents
// for the identical reason.
import { describe, expect, it, test } from "bun:test";
import { create, type ReactTestRendererJSON } from "react-test-renderer";

import StatusBar from "@platform/ui/StatusBar";

function classesOf(node: ReactTestRendererJSON | ReactTestRendererJSON[] | null): string[] {
  const nodes = Array.isArray(node) ? node : node ? [node] : [];
  return nodes
    .filter((n): n is ReactTestRendererJSON => typeof n !== "string")
    .map((n) => n.props?.className as string);
}

test("renders all three sections, left to right: models, activity, repoUpdates", () => {
  const tree = create(
    <StatusBar
      models={<div className="fake-models">m</div>}
      activity={<div className="fake-activity">a</div>}
      repoUpdates={<div className="fake-updates">u</div>}
    />,
  ).toJSON();
  const bar = tree as ReactTestRendererJSON;
  expect(bar.props.className).toBe("status-bar");
  expect(classesOf(bar.children as unknown as ReactTestRendererJSON[])).toEqual([
    "fake-models",
    "fake-activity",
    "fake-updates",
  ]);
});

test("omitted sections render nothing for their slot — not an empty wrapper", () => {
  const tree = create(<StatusBar activity={<div className="fake-activity">a</div>} />).toJSON();
  const bar = tree as ReactTestRendererJSON;
  // React drops `undefined` children outright — the bar has exactly the one
  // real section, no placeholder node standing in for the others.
  expect(classesOf(bar.children as unknown as ReactTestRendererJSON[])).toEqual(["fake-activity"]);
});

// ---- right-aligned chips + the panel anchor that has to move with them ---------
// D569 (user: "the items must be right aligned"). `react-test-renderer` has no
// viewport — it is exactly what let a 130px crushed panel and a clipped Cancel
// button both ship through a fully green suite last round (D568) — so this is a
// STYLESHEET-LEVEL source pin, honest about what it can and cannot see: it proves
// the two declarations exist and agree with each other, not that a browser lays
// them out correctly. The real geometry was verified against a running dev
// server, the same way D568's was.
describe("right-aligned chips (D569) and the panel anchor that has to move with them", () => {
  const { readFileSync } = require("node:fs") as typeof import("node:fs");
  const { join } = require("node:path") as typeof import("node:path");
  const CSS = readFileSync(join(import.meta.dir, "../../styles/notifications.css"), "utf8");

  function block(css: string, selector: string): string {
    const at = css.indexOf(selector + " {");
    expect(at).toBeGreaterThan(-1);
    return css.slice(at, css.indexOf("}", at));
  }

  it("packs the bar's chips against its right edge", () => {
    expect(block(CSS, ".status-bar")).toContain("justify-content: flex-end;");
  });

  it("anchors the panel to its own chip's RIGHT edge, not left — D568 finding #2's fix in reverse", () => {
    // Chips packed left (round 2) needed a left-anchored panel; chips packed
    // right (this round) need the mirror image, or a panel near the bar's
    // right edge grows off `#main`'s right edge the moment it opens — the
    // exact class of bug D568 fixed on the opposite side.
    const panel = block(CSS, ".dl-panel");
    expect(panel).toContain("right: 0;");
    expect(panel).not.toContain("left: 0;");
  });
});

// StatusBar's own composition rules (D565, code review finding #8): three
// sections, left to right by lifetime, always rendered — replacing
// `tests/test_queue_dock.py::test_the_bar_reserves_space_inside_main_not_the_floating_column`,
// which only ever grepped the stylesheet for a literal `.status-bar:empty`
// string and could not see whether the bar actually behaves this way. The
// bare-`<DownloadManager />` fallback for an omitted `activity` prop is left
// to that pytest's own source check (`{activity ?? <DownloadManager />}`):
// the real `DownloadManager` polls `/api/jobs` on mount, which this file
// cannot exercise without mocking `@platform/lib/api` — the exact
// contamination risk DownloadManager.test.tsx's own header comment documents
// for the identical reason.
import { expect, test } from "bun:test";
import { create, type ReactTestRendererJSON } from "react-test-renderer";

import StatusBar from "@platform/ui/StatusBar";

function classesOf(node: ReactTestRendererJSON | ReactTestRendererJSON[] | null): string[] {
  const nodes = Array.isArray(node) ? node : node ? [node] : [];
  return nodes
    .filter((n): n is ReactTestRendererJSON => typeof n !== "string")
    .map((n) => n.props?.className as string);
}

test("renders all three sections, left to right by lifetime: models, activity, repoUpdates", () => {
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

test("an omitted models or repoUpdates section renders nothing for that slot — not an empty wrapper", () => {
  const tree = create(<StatusBar activity={<div className="fake-activity">a</div>} />).toJSON();
  const bar = tree as ReactTestRendererJSON;
  // React drops `undefined` children outright — the bar has exactly the one
  // real section, no placeholder node standing in for the other two.
  expect(classesOf(bar.children as unknown as ReactTestRendererJSON[])).toEqual(["fake-activity"]);
});

// D569 (user: "the collapsing arrow is very ugly"): the status bar's three
// chips used to draw their own disclosure chevron as a literal text glyph
// (`⌃`), rotated by CSS. This component replaces it with an inline SVG,
// following the codebase's own convention for a rotating disclosure chevron
// (`shell/NewJobModal.tsx`'s `ICON_CHEVRON_DOWN`, `platform/ui/MenuIcons.tsx`'s
// `chevron` entry — both inline `<polyline>`s on a 24-unit grid, rotated by
// CSS rather than swapped for a second glyph) rather than inventing a new
// spelling. Unlike the round-3 geometry fixes, this one IS fully checkable
// with `react-test-renderer`: there is no viewport dependency in "is this an
// svg with a polyline, not a span with a text node".
import { expect, test } from "bun:test";
import { create, type ReactTestRendererJSON } from "react-test-renderer";

import DlChevron from "@platform/ui/DlChevron";

function root(json: ReactTestRendererJSON | ReactTestRendererJSON[] | null): ReactTestRendererJSON {
  const node = Array.isArray(json) ? json[0] : json;
  if (node === null || typeof node === "string") throw new Error("expected an element");
  return node;
}

test("renders an SVG, not a text glyph", () => {
  const tree = root(create(<DlChevron collapsed={false} />).toJSON());
  expect(tree.type).toBe("svg");
  // No `⌃` (or any other literal caret) anywhere in the tree — the whole
  // point is that this is DRAWN, not typeset.
  expect(JSON.stringify(tree)).not.toContain("⌃");
  const polylines = (tree.children ?? []).filter(
    (c): c is ReactTestRendererJSON => typeof c !== "string" && c.type === "polyline",
  );
  expect(polylines).toHaveLength(1);
});

test("carries the .dl-chevron class the stylesheet's rotation/color rules key off of", () => {
  const expanded = root(create(<DlChevron collapsed={false} />).toJSON());
  expect(expanded.props.className).toBe("dl-chevron");

  const collapsed = root(create(<DlChevron collapsed={true} />).toJSON());
  expect(collapsed.props.className).toBe("dl-chevron is-collapsed");
});

test("points UP (the direction .dl-panel actually opens, bottom: 100%)", () => {
  // lucide "chevron-up": (18,15) -> (12,9) -> (6,15) — the same polyline
  // NewJobModal's ICON_CHEVRON_DOWN mirrors vertically for the opposite mark.
  const tree = root(create(<DlChevron collapsed={false} />).toJSON());
  const polyline = (tree.children ?? []).find(
    (c): c is ReactTestRendererJSON => typeof c !== "string" && c.type === "polyline",
  );
  expect(polyline?.props.points).toBe("18 15 12 9 6 15");
});

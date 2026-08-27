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
import { describe, expect, it, test } from "bun:test";
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

// D570 (user, on the shipped round-3 chevron: "the up arrow is glaring") —
// full `--fg` at 15px with strokeWidth 2 out-weighed `.dl-summary`'s
// 500-weight text beside it. The fix has a component half (checkable here)
// and a stylesheet half (a source pin below — `react-test-renderer` cannot
// compute a rendered stroke's visual weight against neighbouring text, only
// prove the two declarations exist and agree).
test("draws a lighter stroke than round 3 shipped (2 -> 1.5)", () => {
  const tree = root(create(<DlChevron collapsed={false} />).toJSON());
  expect(tree.props.strokeWidth).toBe("1.5");
});

describe("the chevron reads quieter than the summary text beside it (D570)", () => {
  const { readFileSync } = require("node:fs") as typeof import("node:fs");
  const { join } = require("node:path") as typeof import("node:path");
  const CSS = readFileSync(join(import.meta.dir, "../../styles/notifications.css"), "utf8");

  function block(css: string, selector: string): string {
    const at = css.indexOf(selector + " {");
    expect(at).toBeGreaterThan(-1);
    return css.slice(at, css.indexOf("}", at));
  }

  it("rests muted, not full --fg", () => {
    expect(block(CSS, ".dl-chevron")).toContain("color: var(--fg-muted);");
  });

  it("only brightens to --fg on its own chip's hover or focus", () => {
    expect(CSS).toContain(".dl-toggle:hover .dl-chevron,\n.dl-toggle:focus-visible .dl-chevron {");
    const hoverBlock = CSS.slice(
      CSS.indexOf(".dl-toggle:hover .dl-chevron"),
      CSS.indexOf("}", CSS.indexOf(".dl-toggle:hover .dl-chevron")),
    );
    expect(hoverBlock).toContain("color: var(--fg);");
  });

  it("still tints --error under a failure, same as before — is-failure is untouched", () => {
    expect(block(CSS, ".dl-toggle.is-failure,\n.dl-toggle.is-failure .dl-chevron")).toContain(
      "color: var(--error);",
    );
  });
});

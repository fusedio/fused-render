// Entering split (panel) mode from wherever the action is offered.
//
// This lived inside Breadcrumb.tsx while the path `⋮` was its only caller. It
// now has two: the FILE preview's path menu (BarMenu's PathOverflow) and the
// folder listing's own header `⋮` (Listing.tsx) — the folder's splits moved out
// of the crumb bar and onto the listing it acts on. One module rather than a
// second copy, and rather than Listing importing the crumb bar for it.
//
// The Panel import is the same acyclic exception the crumb bar already takes
// (Breadcrumb -> Panel for `panelUrl`): a function referenced at call time, not
// module-evaluation time.
import { navigateUrl } from "@platform/lib/router";
import { encodePaneSegment, splitShellSearch } from "@platform/lib/layout-codec";
import { panelUrl } from "@apps/explorer/Panel";

// Split entry (LM-10): two panes side by side (`dir` "row", `,` in the codec)
// or stacked ("col", `;`), both showing the current view — entering split mode
// with a single pane looked like nothing happened. The current view's WHOLE
// query goes pane-local, inside each `_layout` segment (LM-3/D72): nothing is
// promoted to the top-level pool — global params exist only when the user
// hand-types them on the shell URL. Read via splitShellSearch, not raw
// URLSearchParams (D51): a stray `_layout=(…)` span carries literal `&` that
// would parse as junk keys; the codec read excludes the span, so it is
// dropped — the strict-read semantics.
export function enterPanel(fsPath: string, dir: "row" | "col"): void {
  const { params } = splitShellSearch(location.search);
  const paneQ = params.toString();
  const seg = encodePaneSegment(fsPath, paneQ ? "?" + paneQ : "");
  navigateUrl(panelUrl(seg + (dir === "row" ? "," : ";") + seg, null));
}

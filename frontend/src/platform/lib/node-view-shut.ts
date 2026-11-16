// WHETHER THE USER SHUT THE CANVAS NODE VIEW'S PANE (SPEC §53.3 NV-4).
//
// Opening a node file of a cloned canvas opens the node pane by itself
// (apps/explorer/lib/preview-side `nodePaneAuto`). Closing that pane is a
// choice about EVERY node file, not this one: it holds until the pane is
// opened again, on any node file. Per user, so in the server-held layout
// document (`node_view_shut`, ui-layout.ts, D1386) and never in browser
// storage, read synchronously off that module's cache, which main.tsx fills
// before the first render.
//
// In `platform/` so the explorer's preview (which decides) and anything else
// that may one day read it share one definition without crossing apps
// (scripts/check-boundaries.mjs).
import { getUiLayout, patchUiLayout } from "./ui-layout";

export function getNodeViewShut(): boolean {
  return getUiLayout().node_view_shut === true;
}

export function setNodeViewShut(next: boolean): void {
  if (getNodeViewShut() === next) return;
  // SET, never toggle: a patch replays onto whatever document the flush reads
  // (ui-layout.ts "WRITES REPLAY").
  patchUiLayout((d) => ({ ...d, node_view_shut: next }));
}

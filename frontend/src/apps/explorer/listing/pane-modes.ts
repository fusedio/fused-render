// The preview pane's mode list and its default, as pure functions — the one
// place that decides WHICH modes the listing's pane offers for a target and
// WHICH of them it lands on. Extracted from ListingPreviewPane so the ordering
// and default rules are testable without mounting the pane (the component
// still owns icons, fetching and rendering).
//
// Two pane-only sentinels live here (neither is a registry mode, so neither is
// in KNOWN_SENTINEL_MODES — the server never sees them):
//   `_app`   — a folder's lone top-level HTML app, rendered in place.
//   `_none`  — "nothing previewed": the neutral hint. It exists so a SELF
//              target (nothing selected, i.e. the folder already open on the
//              left) has a real default that is NOT one of the heavyweight
//              opt-in modes the `/` registry key carries (the chat, git,
//              versions). Before it, dropping the self row's `_listing` —
//              redundant, that listing is the left half — left `claude` as
//              modes[0], so merely opening the pane opened a chat. As a real
//              mode it also keeps the switcher populated (≥2 entries, PT-10)
//              and makes the choice reversible: the chat is one click away and
//              one click back. It is offered for a self target only — a
//              selected row has a subject, so it has a real default.
import type { TemplateEntry } from "@platform/lib/api";

export const PANE_APP_MODE = "_app";
export const PANE_NONE_MODE = "_none";

export interface PaneModeInput {
  // The target's resolved template list (already sentinel-filtered, PT-12).
  templates: TemplateEntry[];
  // Gate verdicts (CT-12); null while still resolving — an unresolved gated
  // entry reads as not offered.
  conditions: Record<string, boolean> | null;
  isDir: boolean;
  // True when the target is the listing's OWN folder (nothing selected).
  self: boolean;
  // A lone top-level HTML app was found in this folder (`_app` is offerable).
  hasApp: boolean;
}

// A gated entry counts only once its verdict says yes (CT-12, fail closed).
export function paneModeAllowed(e: TemplateEntry, conditions: Record<string, boolean> | null): boolean {
  return !e.conditional || (conditions !== null && conditions[e.mode] === true);
}

// The pane's mode list, in pane priority order (first = default).
//
// `_app` leads when a lone app exists — a folder's own app is what that folder
// IS, so it stays the self target's default too (it is a preview, not an
// opt-in tool). `_none` comes next and therefore leads for every OTHER self
// target: previewing the folder you are already looking at has no obvious
// subject, so the pane starts neutral rather than in the first template mode.
// After those, the template system's own order untouched — `_listing` stays
// exactly where the registry ranks it. `_listing` is dropped for a FILE (no
// slot for a listing of a file) and for a SELF target (that listing is the
// left half of the split).
export function paneModeList(input: PaneModeInput): string[] {
  const { templates, conditions, isDir, self, hasApp } = input;
  const modes: string[] = [];
  if (isDir && hasApp) modes.push(PANE_APP_MODE);
  if (self) modes.push(PANE_NONE_MODE);
  for (const e of templates) {
    if (e.mode === "_listing") {
      if (isDir && !self) modes.push("_listing");
      continue;
    }
    if (paneModeAllowed(e, conditions)) modes.push(e.mode);
  }
  return modes;
}

// The mode the pane shows: the user's override (from the switcher, seeded from
// the `_panelMode` URL param) while that mode is still offered, else the first
// mode in pane priority order.
export function activePaneMode(modes: string[], modeOverride: string | null): string | null {
  if (modeOverride !== null && modes.includes(modeOverride)) return modeOverride;
  return modes[0] ?? null;
}

// Does this list carry anything beyond the neutral placeholder? A self target
// with nothing else to offer needs no header at all — there is no mode to
// switch to.
export function paneHasRealMode(modes: string[]): boolean {
  return modes.some((m) => m !== PANE_NONE_MODE);
}

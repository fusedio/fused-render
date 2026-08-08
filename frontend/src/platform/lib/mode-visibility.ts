// One visibility policy for condition-gated template entries (CT-12), shared
// by every surface that renders a mode list: the title bar's ModeMenu
// (Preview), the listing's preview-pane header, the panel/tab pane bars
// (PaneModeMenu) and the Open With menu (lib/fs-actions). They used to each
// filter by hand and drifted apart, so the SAME folder could offer a mode in
// one surface and nowhere else.
//
// The policy, and why:
//
//   unconditional        -> always visible.
//   verdicts === null    -> visible, PENDING (the request is still in flight;
//                           the entry renders as a disabled spinner).
//   verdicts[mode]===false -> hidden. An explicit denial is the one signal a
//                           gate actually gives, and it is honoured.
//   no verdict for mode  -> VISIBLE. /api/fs/conditions always reports a key
//                           per gated mode, so a missing one means the call
//                           FAILED (the callers resolve a failure to `{}`) or
//                           the mode list moved on. The old posture dropped
//                           those entries ("fail closed"), which silently
//                           emptied the menu — and since a menu of one hides
//                           itself, a slow or failing gate probe made the
//                           whole mode control disappear while another
//                           surface still rendered the gated mode. Showing
//                           the entry is the least surprising failure: at
//                           worst the user picks a mode whose template then
//                           declines to render, which is visible and
//                           recoverable; a vanished control is neither.
//
// "Never strand the user in a mode the menu doesn't list" is the other half of
// the policy, but it is NOT done by pinning a denied entry into the list — an
// explicit denial hides the entry even when it is the mode currently
// requested. Pinning it looked like the kind thing to do and was worse: on a
// two-mode path it left `visible` at exactly one entry, and a menu of one
// hides itself (ModeMenu), so the view sat on a denied mode with no switcher
// and no fallback. Instead the ACTIVE mode moves: `effectiveActive` resolves a
// denied/unknown request to the default entry, the same silent fallback an
// unknown `_mode` already gets (PT-9). The denied entry then drops from the
// menu like any other, and a path whose every gated mode is denied falls
// through to the caller's own empty-list handling (Preview's FallbackPreview).
import type { TemplateEntry } from "@platform/lib/api";

export type ConditionVerdicts = Record<string, boolean> | null;

// Gated and no verdict yet — the entry shows, but is not selectable.
export function isModePending(entry: TemplateEntry, verdicts: ConditionVerdicts): boolean {
  return !!entry.conditional && verdicts === null;
}

export function isModeVisible(entry: TemplateEntry, verdicts: ConditionVerdicts): boolean {
  if (!entry.conditional) return true;
  if (verdicts === null) return true; // in flight — pending, not denied
  return verdicts[entry.mode] !== false; // absent verdict (failed call) shows
}

// Order-preserving filter — the registry's own ranking is the menu order.
export function visibleModes(
  entries: TemplateEntry[],
  verdicts: ConditionVerdicts
): TemplateEntry[] {
  return entries.filter((e) => isModeVisible(e, verdicts));
}

// The default entry among an ALREADY-VISIBLE list: the first UNCONDITIONAL one
// (CT-12 — a gated template is never the default while a normal one exists);
// only an all-conditional list falls back to its first entry.
export function defaultMode(visible: TemplateEntry[]): TemplateEntry | null {
  return visible.find((e) => !e.conditional) ?? visible[0] ?? null;
}

// The entry a surface should actually show for a requested `_mode`: the
// request when it is still on offer, otherwise the default. Unknown, stale and
// gate-denied requests all land here and all resolve the same silent way.
export function effectiveActive(
  visible: TemplateEntry[],
  requestedMode?: string | null
): TemplateEntry | null {
  return visible.find((e) => e.mode === requestedMode) ?? defaultMode(visible);
}

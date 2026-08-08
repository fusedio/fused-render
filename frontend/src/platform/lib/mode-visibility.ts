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
//   the ACTIVE mode      -> always visible, whatever the verdict says. A user
//                           can never be stranded in a mode the menu does not
//                           list (there would be no way back out of it).
import type { TemplateEntry } from "@platform/lib/api";

export type ConditionVerdicts = Record<string, boolean> | null;

// Gated and no verdict yet — the entry shows, but is not selectable.
export function isModePending(entry: TemplateEntry, verdicts: ConditionVerdicts): boolean {
  return !!entry.conditional && verdicts === null;
}

export function isModeVisible(
  entry: TemplateEntry,
  verdicts: ConditionVerdicts,
  activeMode?: string | null
): boolean {
  if (!entry.conditional) return true;
  if (activeMode && entry.mode === activeMode) return true;
  if (verdicts === null) return true;
  return verdicts[entry.mode] !== false;
}

// Order-preserving filter — the registry's own ranking is the menu order.
export function visibleModes(
  entries: TemplateEntry[],
  verdicts: ConditionVerdicts,
  activeMode?: string | null
): TemplateEntry[] {
  return entries.filter((e) => isModeVisible(e, verdicts, activeMode));
}

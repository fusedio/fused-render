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

// --- content pane vs. sidebar (Preview's `_side`) ---------------------------
// Some of the modes around a file are not another WAY OF LOOKING at it, they
// are companions TO looking at it: the agent chat, the working tree it sits in,
// and the MCP tools its app folder publishes all talk about the file while you
// are viewing it as
// something (an image, a table, its source). Putting them in the same radio list
// as the real content modes made them mutually exclusive with the view they are
// about — asking Claude about a .png meant giving up looking at the .png.
//
// So on a single-file explorer preview they move to a right-hand SIDEBAR with
// its own URL param (`_side`), and the content pane's own mode list drops them.
// The partition lives here, with the rest of the mode policy, because every
// surface has to agree on which side of the split a mode belongs to — and
// because the surfaces that DON'T split (the panel/tab panes, the app builder)
// must keep offering them as ordinary modes, which is only safe while "is this a
// sidebar mode?" has one definition.
//
// `git` AND `mcp` ARE ON THIS LIST AND ARE IN NO FILE'S TEMPLATE LIST, and both
// halves of that are deliberate. The registry binds each to the universal "/"
// DIRECTORY key alone and both gates refuse anything that is not a directory
// (templates/git/condition.py: a working tree belongs to the folder, since you
// stash a tree and not a file; templates/mcp/condition.py: the tool manifest
// belongs to the app folder it sits in), so `partitionModes` will never pull
// either entry out of a file's own modes. The file sidebar BORROWS them from the
// file's parent folder instead (apps/explorer/lib/dir-mode.ts) and inserts them
// here — which is why the ordering below is a rule of its own rather than the
// registry's.
export const SIDEBAR_MODES = ["claude", "git", "mcp"] as const;

const SIDEBAR_MODE_SET: ReadonlySet<string> = new Set(SIDEBAR_MODES);

// WHY A COMPANION IS NOT ON OFFER, in the words the switcher puts on it.
//
// The companion surfaces (the file sidebar, the folder pane) do NOT drop a
// companion they cannot show: they list it DISABLED, with one of these as its
// `title`. That is the opposite of what the content-mode policy at the top of
// this file does with a denied entry, and the difference is the surfaces, not a
// change of mind. A content mode list is OPEN — it is whatever the registry
// bound to this extension, and a user has no expectation about its length, so a
// missing entry is invisible rather than confusing. The companion list is
// CLOSED and always the same set; a user who has seen Claude / Git
// beside one file and only Claude beside the next has been told nothing about
// why, and dropping the entry would leave a one-entry menu, which hides itself
// outright, so a file outside a repository would have no switcher at all. Naming
// the reason costs one disabled row and answers the question the empty space raised.
//
// The reasons are CANNED CLIENT-SIDE and per MODE, not per verdict: /api/fs/
// conditions is bool-only by design (a gate is a condition.py returning a bool,
// not a message), and each condition is stable enough to say in a
// sentence — the working tree, the app shape, and the chat's applicability. A
// mode is equally unavailable whether its gate said no or the file never bound
// the template at all, and from the user's side those are the same fact, so one
// string covers both.
const UNAVAILABLE_REASONS: Record<string, string> = {
  claude: "Claude is not available for this file",
  git: "Not inside a git repository",
  mcp: "Not a fused app folder (needs index.html and a main())",
};

// Deliberately total: a user registry can bind a mode of its own into either
// companion list, and a disabled row with a vague tooltip still beats a row
// whose tooltip is the word "undefined".
export function unavailableReason(mode: string): string {
  return UNAVAILABLE_REASONS[mode] ?? "Not available here";
}

export function isSidebarMode(mode: string): boolean {
  return SIDEBAR_MODE_SET.has(mode);
}

// Split an ALREADY-VISIBLE list in two, order-preserving in both halves.
export function partitionModes(entries: TemplateEntry[]): {
  content: TemplateEntry[];
  sidebar: TemplateEntry[];
} {
  return {
    content: entries.filter((e) => !isSidebarMode(e.mode)),
    sidebar: entries.filter((e) => isSidebarMode(e.mode)),
  };
}

// The sidebar switcher's order: SIDEBAR_MODES', not the registry's.
//
// The registry ranks views for a FILE TYPE — which of `.png`'s viewers should
// open first — and that is a genuinely different question from how the companions
// rank against each other, which is the same answer for every file: the chat, then
// the working tree, then the app's MCP tools. It also has to be a rule here
// because the list is ASSEMBLED rather than read: `git` and `mcp` are borrowed
// from the parent folder (see above) and appended, so leaving the order to the
// input would rank them by where the assembly happened to put them rather than
// by what they are.
//
// Stable within a rank, so an unknown companion (a user registry binding one of
// its own into this half) keeps its relative position at the end rather than
// being reshuffled.
export function orderSidebarModes(entries: TemplateEntry[]): TemplateEntry[] {
  const rank = (mode: string) => {
    const i = (SIDEBAR_MODES as readonly string[]).indexOf(mode);
    return i === -1 ? SIDEBAR_MODES.length : i;
  };
  return [...entries].sort((a, b) => rank(a.mode) - rank(b.mode));
}

// Which sidebar mode a bare "open the sidebar" lands on: SIDEBAR_MODES order,
// not the registry's, because this is a preference between the companions (the
// chat first — it is the one users open the sidebar FOR) rather than a ranking of
// views for a file type. Falls through to whatever IS on offer so a file whose
// only companion is one nothing here ranks still opens something.
export function defaultSidebarMode(sidebar: TemplateEntry[]): string | null {
  for (const mode of SIDEBAR_MODES) if (sidebar.some((e) => e.mode === mode)) return mode;
  return sidebar[0]?.mode ?? null;
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

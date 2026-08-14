// The preview pane's mode list and its default, as pure functions — the one
// place that decides WHICH modes the listing's pane offers for a target and
// WHICH of them it lands on. Extracted from ListingPreviewPane so the ordering
// and default rules are testable without mounting the pane (the component
// still owns icons, fetching and rendering).
//
// Everything here describes a SELECTED row. The self target (nothing selected —
// the folder already open on the left) never reaches this module: it shows no
// mode menu at all and always renders the pane's neutral hint, so there is
// nothing to rank and nothing to default to. It used to be modelled here, with
// an elaborate no-default rule that existed only to stop a first-wins default
// from opening a chat on the folder merely because the pane was on; hiding the
// picker deletes the question instead of answering it.
import type { TemplateEntry } from "@platform/lib/api";
import { isModeVisible } from "@platform/lib/mode-visibility";

export interface PaneModeInput {
  // The target's resolved template list (already sentinel-filtered, PT-12).
  templates: TemplateEntry[];
  // Gate verdicts (CT-12); null while still resolving. Visibility is NOT this
  // module's call — it is `lib/mode-visibility`'s one policy for every mode
  // surface: an entry hides only on an explicit `false`.
  conditions: Record<string, boolean> | null;
  isDir: boolean;
}

// The pane's mode list, in pane priority order (first = default): the template
// system's own order, untouched — `_listing` stays exactly where the registry
// ranks it, and is dropped only for a FILE (no slot for a listing of a file).
//
// It used to hoist an APP entry to the lead — a folder's own app being what
// that folder IS, so it outranked the opt-in tools aimed at it — with a
// pane-only `_app` sentinel standing in wherever the registry's `app` mode was
// absent or gate-denied. Both are gone with the app concept itself (D264) and
// D269 brought neither back: a folder that IS an app never reaches this module
// at all, because the pane resolves it to its entry PAGE before asking for a
// mode list (ListingPreviewPane's retarget), and what arrives here is then an
// html FILE like any other. Every folder this module still sees previews as its
// listing, like every other folder.
//
// Every entry here is a REAL mode: the list says what the pane can show, never
// that it should show nothing. An EMPTY list means a target with nothing to
// offer at all, which the pane answers with its metadata card.
export function paneModeList(input: PaneModeInput): string[] {
  const { templates, conditions, isDir } = input;
  const visible = (e: TemplateEntry) => isModeVisible(e, conditions);
  const modes: string[] = [];
  for (const e of templates) {
    if (e.mode === "_listing") {
      if (isDir) modes.push("_listing");
      continue;
    }
    if (visible(e)) modes.push(e.mode);
  }
  return modes;
}

// Does this mode's template need `chat_only=1` — i.e. must the pane take away
// the template's OWN preview pane?
//
// One mode does: `claude`. In a column this narrow the chat template's copy of
// the target would be a second, differently run preview of the same thing beside
// the host's (see Preview's sideSrcFor, and CHAT_ONLY in
// templates/claude/template.html).
//
// **And for a FOLDER it is more than a layout question.** The chat template fills
// its own pane by resolving the folder's ENTRY PAGE and rendering it
// (templates/shared/app_entry.py). Since `claude` leads the universal directory
// key (D277), a selected folder's pane defaults to this template — so without the
// flag the folder's app page would be back on screen, nested one level deeper,
// running the same Python for the same mere selection that D277 exists to stop.
// The template reads the flag BEFORE it looks an entry page up, so the flag is
// the whole cure and not a cosmetic one.
//
// It lives here, as the one rule with two callers, because the pane has TWO
// claude surfaces — its `claude` SIDE (the companion, about the selected row) and
// now a row MODE (a folder's default view) — and a second literal in the second
// place is how the first one would have been forgotten.
export function paneChatOnly(mode: string): boolean {
  return mode === "claude";
}

// The mode the pane shows: the user's override (from the switcher, seeded from
// the `_panelMode` URL param) while that mode is still offered, else the first
// mode in pane priority order. `null` means the target offers nothing at all
// (an empty list — a file that maps to no template, or one whose every
// template is gate-denied), which the pane answers with its metadata card.
//
// Deliberately `modes[0]` and not `mode-visibility`'s `defaultMode` (first
// UNCONDITIONAL entry, PT-8/PT-9): the pane never
// reaches here with verdicts in flight — the component holds a skeleton until
// they land — so "unconditional" carries no render-now-don't-wait meaning here
// the way it does on the preview route.
export function activePaneMode(modes: string[], modeOverride: string | null): string | null {
  if (modeOverride !== null && modes.includes(modeOverride)) return modeOverride;
  return modes[0] ?? null;
}

// Where the pane's expand button goes: the previewed row, opened full-screen IN
// THE MODE THE PANE IS SHOWING. Without the mode the expand icon silently threw
// away the template the user had switched to and reopened the target in its
// default — the one thing "make this the whole view" must not do.
//
// It takes the RESOLVED active mode, never the raw `_panelMode` param: when the
// param names a mode this selection does not offer, the pane has already fallen
// back to its default, and the open has to match what is on screen.
//
// This answers for FILES; a folder never reaches it (see paneOpenAction).
//
// Two values are not `_mode` values and are dropped rather than passed on:
//
//   `_listing`  — a folder full-screen IS its listing; `_mode=_listing` would
//                 be the destination's own default written out longhand, and
//                 Preview strips it again the moment the user switches modes.
//   null        — the target offers nothing at all (the pane is showing its
//                 metadata card), so there is no mode to carry.
export interface PaneOpenTarget {
  path: string;
  isDir: boolean;
  // Absent = open the destination's own default view.
  mode?: string;
}

export function paneOpenTarget(
  row: { path: string; isDir: boolean },
  activeMode: string | null
): PaneOpenTarget {
  if (activeMode === null || activeMode === "_listing") {
    return { path: row.path, isDir: row.isDir };
  }
  return { path: row.path, isDir: row.isDir, mode: activeMode };
}

// WHICH control the pane's header offers for the previewed row — the half
// paneOpenTarget deliberately does not answer, because for a folder the honest
// answer is "none of them".
//
//   file    → `expand`. "Make this preview the whole view", in the mode the
//             pane is showing (paneOpenTarget above).
//   folder  → `none`. Expanding a folder means opening its listing — and its
//             listing is what the LEFT HALF of this very split already is. The
//             button offered to replace a two-pane view of a folder with a
//             one-pane view of the same folder, which is not an action so much
//             as a step backwards. Its one honest use, "get into this folder",
//             is what double-click and Enter on the row already do.
//
// A folder holding a lone page used to get a second, LABELLED button here —
// "Open as app" — which is gone with the app concept (D264).
export type PaneOpenAction = { kind: "expand"; target: PaneOpenTarget } | { kind: "none" };

export function paneOpenAction(
  row: { path: string; isDir: boolean },
  activeMode: string | null
): PaneOpenAction {
  if (!row.isDir) return { kind: "expand", target: paneOpenTarget(row, activeMode) };
  return { kind: "none" };
}

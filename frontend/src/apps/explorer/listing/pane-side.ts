// The listing preview pane's THREE modes, and the `_side` param that records
// which one is showing — the folder half of the same split the file preview grew
// (Preview.tsx / PreviewSidebar.tsx). Pure and DOM-free, like the pane's other
// decision modules (pane-math, pane-modes), so the semantics below can be pinned
// by a test with no router and no React.
//
// THE THREE, in this order, and it is a closed list rather than the registry's:
//
//   preview  the SELECTED ROW's own default view — the pane exactly as it has
//            always been (pane-modes.ts still decides what that resolves to: a
//            folder's embedded listing, a lone app, a file's first template).
//   claude   the chat, `chat_only=1`, about the selected row.
//   git      the OPEN FOLDER's working tree — not the row's. See dir-mode.ts:
//            a working tree belongs to the folder, so `git` is bound to the
//            universal "/" key alone and the pane borrows the folder's entry.
//
// Deliberately NO `history`. The file sidebar offers it because "what happened to
// this file" is a question about the thing you are looking at; over a folder the
// thing you are looking at is a LIST, and a per-row history in a column beside it
// is a fourth pill for a view nobody browses into. It stays one click away — open
// the row, and the file sidebar has it.
//
// This also RETIRES the pane's old per-template switcher (`_panelMode`), which
// offered every mode the selected row resolved — image/photos/pano for a .png,
// and `claude`/`history` among them. Two mode controls over one row is the thing
// the bars' grammar has been converging away from (see Preview's headerActions on
// why a folder has no top-bar switcher), and the companions are not row views at
// all, which is the whole premise of the split. What is genuinely lost is picking
// a NON-DEFAULT content template for a previewed row inside the pane; the
// expand button opens the row full-screen, where the content switcher lives.
import type { TemplateEntry } from "@platform/lib/api";

export const PANE_SIDE_MODES = ["preview", "claude", "git"] as const;

export type PaneSide = (typeof PANE_SIDE_MODES)[number];

export const DEFAULT_PANE_SIDE: PaneSide = "preview";

// The value `_side` takes when the user has SHUT the pane. Not a mode, and
// spelled as a word rather than as an absent param for the reason in
// `parsePaneSide` below.
export const PANE_SIDE_OFF = "off";

export interface PaneSideState {
  open: boolean;
  mode: PaneSide;
}

const OPEN_DEFAULT: PaneSideState = { open: true, mode: DEFAULT_PANE_SIDE };

function isPaneSide(v: string): v is PaneSide {
  return (PANE_SIDE_MODES as readonly string[]).includes(v);
}

// `_side` ON A FOLDER URL, and what an ABSENT one means. This is the one place
// the folder's reading of the param differs from the file preview's, so it is
// worth being explicit about both:
//
//   file    absent = CLOSED. The sidebar is an extra beside a content view that
//           is already complete on its own, so nothing is the same as nothing.
//   folder  absent = OPEN, at `preview`. The pane is not an extra — it IS the
//           folder view's other half, on since long before this param existed,
//           and it appears purely from the width of the split (pane.ts). Reading
//           absence as "closed" would turn every existing folder URL, bookmark
//           and recent into a one-column listing.
//
// Which is why closing is `_side=off` and not a deleted param: the two states an
// absent param could mean are not the same here, so the SHUT one has to say so.
// The open-at-default state is what gets the clean URL, per the `_mode` rule
// (PT-9 — selecting the default deletes the param).
//
// An unknown value reads as the default rather than as an error: a hand-typed
// `_side=graph`, or a `_side=history` carried in from a file view (router's
// navigate keeps the param across a DIRECTORY hop), lands on `preview` with the
// pane open. Same silent fallback an unknown `_mode` gets.
export function parsePaneSide(raw: string | null): PaneSideState {
  if (raw === null) return OPEN_DEFAULT;
  if (raw === PANE_SIDE_OFF) return { open: false, mode: DEFAULT_PANE_SIDE };
  return raw !== "" && isPaneSide(raw) ? { open: true, mode: raw } : OPEN_DEFAULT;
}

// What to write back — null means DELETE the param.
//
// A shut pane records only that it is shut, not what it last showed: the mode it
// would reopen to is remembered in component state for the session (the same
// place the file sidebar keeps its `lastSide`), so a reload of a closed pane
// reopens at Preview. Encoding the pair would put a mode nobody can see into
// every shared link of a one-column listing.
export function paneSideParam(state: PaneSideState): string | null {
  if (!state.open) return PANE_SIDE_OFF;
  return state.mode === DEFAULT_PANE_SIDE ? null : state.mode;
}

// Which of the three a folder actually offers. `preview` ALWAYS — it is the
// pane's identity, and even a row with no template at all has a preview state
// (the metadata card). The other two exist only while the folder's own entry for
// them does (dir-mode.ts), so a folder outside a repository has no Git pill and a
// mount-backed folder — where both gates refuse — has only Preview, at which
// point the shared ModeMenu hides itself ("one mode is not a choice", BarMenu)
// and the header is a chevron over the pane, exactly as it was before the split.
//
// Order is by construction, not by sorting: this list IS the switcher's order.
export interface PaneSideEntries {
  // The OPEN FOLDER's `claude` / `git` template entries, or null when the folder
  // does not offer the mode. `preview` needs no entry — it is the selected row's
  // own default template, resolved from the row's stat by pane-modes.ts.
  claude: TemplateEntry | null;
  git: TemplateEntry | null;
}

export function paneSideList(entries: PaneSideEntries): PaneSide[] {
  const list: PaneSide[] = ["preview"];
  if (entries.claude) list.push("claude");
  if (entries.git) list.push("git");
  return list;
}

// The mode the pane SHOWS for a requested one: the request while it is still on
// offer, else the default. A `_side` naming a mode this folder hasn't got — a
// `git` carried in from a repository to a folder outside one — falls back here
// and the param is deliberately LEFT ALONE, which is the posture `_panelMode` had
// before it: the next folder that does offer the mode picks it up again, so a hop
// out of a repo and back in does not silently reset the pane.
export function activePaneSide(offered: PaneSide[], want: PaneSide): PaneSide {
  return offered.includes(want) ? want : DEFAULT_PANE_SIDE;
}

// WHAT THE PANE IS ABOUT, as a React key — and it is not always the selected row,
// which is the whole reason this is a function and not `row.path`.
//
// In `git` the subject is the FOLDER. The pane is keyed so that switching rows
// remounts it (a stale iframe must not linger while the new row resolves), and
// with the row in the key, arrow-keying down a listing would tear down and reload
// the git view — a `git status` and a `git log` fork — on every keystroke, for a
// view whose content did not change. So the selection is left out of it.
//
// `claude` and `preview` are both about the ROW, and drop to the folder itself
// when the selection names no single one: the folder-scoped chat for `claude`, the
// pane's self/placeholder states for `preview`.
export function paneKey(
  side: PaneSide,
  folder: string,
  // The lead row's path when EXACTLY ONE row is selected, else null.
  rowPath: string | null,
  selCount: number
): string {
  if (side === "git") return "git:" + folder;
  if (side === "claude") return "claude:" + (rowPath ?? folder);
  if (rowPath) return "preview:" + rowPath;
  // Nothing selected previews the folder itself (the self target); a
  // multi-selection previews nothing and needs no per-path identity.
  return selCount === 0 ? "preview:self:" + folder : "preview:none";
}

// The path a mode's iframe is aimed at (`_file`). Three modes, two subjects: the
// folder for `git`, the selected row for the other two — falling back to the
// folder when there is no single row, so the chat has something to be about.
export function paneSideTarget(
  side: PaneSide,
  folder: string,
  rowPath: string | null
): string {
  return side === "git" ? folder : (rowPath ?? folder);
}

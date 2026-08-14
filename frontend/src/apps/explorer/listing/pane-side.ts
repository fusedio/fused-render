// The listing preview pane's THREE modes, and the `_side` param that records
// which one is showing — the folder half of the same split the file preview grew
// (Preview.tsx / PreviewSidebar.tsx). Pure and DOM-free, like the pane's other
// decision modules (pane-math, pane-modes), so the semantics below can be pinned
// by a test with no router and no React.
//
// THE THREE, in this order, and it is a closed list rather than the registry's:
//
//   preview  the SELECTED ROW's own default view — pane-modes.ts decides what
//            that resolves to (a file's first template; for a folder, only the
//            `_listing` peek it falls back to). **Offered for a FILE row and for a
//            multi-selection; for a FOLDER SUBJECT — a selected directory, or
//            nothing selected at all — only when neither companion is**
//            (D281/D284: a folder is not a thing this pane previews; see
//            paneSideList). It used to resolve a folder to the PAGE it holds and
//            render it, which is what D280 deleted; and with nothing selected it
//            used to render the "Select a file to preview." hint, which D284
//            demoted to that same neither-companion fallback.
//   claude   the chat, `chat_only=1`, about the selected row — about the OPEN
//            FOLDER when the selection names no single row (paneSideTarget), which
//            is what the no-selection state now lands on.
//   git      the OPEN FOLDER's working tree — not the row's. See dir-mode.ts:
//            a working tree belongs to the folder, so `git` is bound to the
//            universal "/" key alone and the pane borrows the folder's entry.
//
// Closed at three, and a per-ROW companion is deliberately not a fourth: over a
// folder the thing you are looking at is a LIST, so a column that talks about one
// row is a pill for a view nobody browses into. Anything of that shape stays one
// click away — open the row, and the file sidebar has it.
//
// This also RETIRES the pane's old per-template switcher (`_panelMode`), which
// offered every mode the selected row resolved — image/photos/pano for a .png,
// and `claude` among them. Two mode controls over one row is the thing
// the bars' grammar has been converging away from (see Preview's headerActions on
// why a folder has no top-bar switcher), and the companions are not row views at
// all, which is the whole premise of the split. What is genuinely lost is picking
// a NON-DEFAULT content template for a previewed row inside the pane; the
// expand button opens the row full-screen, where the content switcher lives.
import type { TemplateEntry } from "@platform/lib/api";
import { unavailableReason } from "@platform/lib/mode-visibility";

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
// `_side=graph`, or a stale `_side` carried in from a file view (router's
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

// Which of the three this target actually offers — and it is TWO questions now,
// not one: what the FOLDER can back, and what the previewed ROW can be.
//
// `preview` for a FILE row always: it is the pane's identity there, and even a row
// with no template at all has a preview state (the metadata card). **For a FOLDER
// SUBJECT — a selected directory, or nothing selected — it is offered only when
// neither companion is** (D281/D284, `subjectIsDir` below) — see the block on the
// function for why a folder has no preview and what the exception is for. The companions exist only while the folder's own entry for them
// does (dir-mode.ts), so a folder outside a repository can be SHOWN no Git and a
// mount-backed folder — where both gates refuse — can be shown neither.
//
// What the switcher DRAWS is a different question and `paneSideMenu` below
// answers it: an unofferable mode is listed as a disabled row rather than
// dropped, so this list is what the pane may be ON, never what the header
// contains.
//
// Order is by construction, not by sorting: this list IS the switcher's order.
export interface PaneSideEntries {
  // The OPEN FOLDER's `claude` / `git` template entries, or null when the folder
  // does not offer the mode. `preview` needs no entry — it is the selected row's
  // own default template, resolved from the row's stat by pane-modes.ts.
  claude: TemplateEntry | null;
  git: TemplateEntry | null;
  // The dir-mode probe behind that null has not answered yet (Listing passes the
  // flag alongside; the entry stays null because a placeholder has no template
  // path to frame). Only the MENU reads these — an undecided mode is neither
  // offered nor denied, so it is drawn as CT-12's spinner row rather than as a
  // disabled one asserting a reason nobody has established.
  claudePending?: boolean;
  gitPending?: boolean;
  // The folder's entry for a mode it will not SHOW: the binding as the stat
  // reported it, gate verdict or not (lib/dir-mode's `bound`). Read for one thing
  // only — see `paneSideIconEntry` — and never as an offer: a mode is on offer
  // when `claude`/`git` above is non-null, and nowhere else.
  claudeBound?: TemplateEntry | null;
  gitBound?: TemplateEntry | null;
}

// `subjectIsDir` — **the pane's SUBJECT is a folder**, which is two states and not
// one: the previewed row is a directory, OR nothing is selected at all and the
// subject is the OPEN FOLDER itself (the `self` target). **A folder has no
// `preview`** (D281, extended to the no-selection state by D284): a folder is not a
// thing this pane can preview. Rendering the page it holds is what D280 stopped, and
// the other candidate — the embedded listing peek — is the very listing on the other
// side of the divider.
//
// Leaving `preview` on the list was the whole of the bug the owner reported, twice.
// First for a selected folder row: the pill read "Preview" while a chat rendered in
// it, because the folder's default MODE had become `claude` (registry, D280) while
// the pane's own default SIDE was still `preview`. Then for the NO-SELECTION state,
// which D281 left behind and which FS-16/D278 had meanwhile made the state **every
// folder opens into** — so the wrong pill and a "Select a file to preview." hint
// over a folder were what a user saw on every single folder open. Same argument,
// same fix, and the second one is the more visible of the two.
//
// A MULTI-selection is deliberately NOT a folder: several rows are not a subject
// this rule is about, so `selCount > 1` keeps its `preview` side and its "N items
// selected" placeholder untouched.
//
// So a folder subject offers the COMPANIONS, and since `_side` absent parses as
// `preview`, `activePaneSide` lands it on the first of them — the chat about that
// folder (`paneSideTarget` already falls a null row back to the folder, so the
// no-selection case needed no new plumbing). **First OFFERED,
// so a denied `claude` lands on `git`** where the folder is in a work tree; the
// built-in gates make that shape unreachable in practice (claude refuses only a
// mount-backed or nonexistent path, and git refuses those too, on top of needing a
// work tree), but the rule is "the first one on offer" and not "claude or the
// peek".
//
// **Unless neither companion is offered** (a mount-backed folder: both gates
// refuse), when `preview` comes back as the fallback. The pane must show
// something, and there the row's own default mode is the unconditional `_listing`
// sentinel — a peek that renders no template and runs no Python.
//
// **A PROBE STILL OUT IS NOT A DENIAL, and for a folder subject the answer is then
// NEITHER — an EMPTY LIST, meaning UNDECIDED.** `Listing` nulls both entries while
// `lib/dir-mode` resolves them, so "no companion offered" and "not answered yet"
// arrive in the same shape; taking the `preview` fallback there reproduced the bug
// this whole rule exists to fix, for the window after a folder opens — and with
// D284 that window is on the path EVERY folder-open takes, since the no-selection
// state is now a folder subject too. The pill read
// "Preview" while the row's own default mode — `claude` since D280 — rendered a chat
// inside it, and when the probe landed the side flipped to `claude`, the key changed
// and `agent.py` was spawned a SECOND time. So the caller holds a SKELETON on an
// empty list: no mode resolved, nothing mounted, one spawn when the answer arrives.
// (`paneSideMenu` below reads this same list, so it draws no `preview` row either —
// the companions are already CT-12 spinners while pending.)
//
// A FILE row, and a multi-selection, are untouched by a pending probe: their
// `preview` is the row's own template list or a placeholder, neither of which the
// folder's companion gates say anything about, so waiting there would be a skeleton
// over an answer already in hand.
export function paneSideList(entries: PaneSideEntries, subjectIsDir = false): PaneSide[] {
  const companions: PaneSide[] = [];
  if (entries.claude) companions.push("claude");
  if (entries.git) companions.push("git");
  if (subjectIsDir && companions.length > 0) return companions;
  if (subjectIsDir && (entries.claudePending || entries.gitPending)) return [];
  return ["preview", ...companions];
}

// One row per mode the pane MAY BE ON — all three for a file row or a
// multi-selection, and the two companions for a folder SUBJECT, selected row or open
// folder alike (D281/D284: no Preview for a folder, `subjectIsDir` below).
//
// Within that list, an unofferable COMPANION is still drawn, which is the folder
// half of the rule the file sidebar states at length (lib/preview-side,
// mode-visibility's `unavailableReason`): a closed pair the user is entitled to see
// both of, with the unavailable one disabled and saying why, rather than a header
// that quietly shrank — and, at one row, hid its switcher altogether, leaving a
// mount-backed folder's pane with a chevron and nothing else. That rule is about
// the companions and always was; `preview` is not one of them, and the difference
// is that a companion is unavailable for a REASON the user is owed, while a folder
// simply has no preview to offer.
//
// `preview` never carries a reason: it is the pane's identity and cannot be
// unavailable. The other two are disabled when the folder has no entry for them,
// or spinning while the probe is still out.
export interface PaneSideMenuEntry {
  mode: PaneSide;
  // Gate/stat still in flight — disabled spinner, no claim made.
  pending?: boolean;
  // Not on offer here, and this is why — disabled, with the reason as its title.
  disabledReason?: string;
}

// The `preview` ROW follows the LIST (D281/D284): a folder subject that offers the
// companions draws no Preview row, because the pane cannot be on it there. That
// is not a hole in the "list every mode, disable the unavailable ones" rule — that
// rule is about the two COMPANIONS, which are unavailable for a REASON the user is
// owed. `preview` carries no reason (it is the pane's identity), so a disabled
// Preview row would be a dead control with nothing to say.
export function paneSideMenu(
  entries: PaneSideEntries,
  subjectIsDir = false,
): PaneSideMenuEntry[] {
  const companion = (mode: "claude" | "git"): PaneSideMenuEntry => {
    if (entries[mode]) return { mode };
    const pending = mode === "claude" ? entries.claudePending : entries.gitPending;
    return pending ? { mode, pending: true } : { mode, disabledReason: unavailableReason(mode) };
  };
  const rows = [companion("claude"), companion("git")];
  // Exactly the list's own condition, asked of the same inputs, so the menu can
  // never draw a row the pane may not be on — or drop the one it is on.
  const offersPreview = paneSideList(entries, subjectIsDir).includes("preview");
  return offersPreview ? [{ mode: "preview" }, ...rows] : rows;
}

// WHICH TEMPLATE A ROW TAKES ITS ICON FROM — the offered entry, and failing that
// the binding behind a disabled row. Null means the mode is bound nowhere and the
// caller has to draw something of its own.
//
// A decision rather than a line inside the icon function because it is the same
// rule the file sidebar's menu applies (lib/preview-side's `bound`) and the same
// bug if it is got wrong: a disabled row is the mode with the click taken away,
// so it keeps the mode's glyph. Falling back to a generic one made the Git row
// look like a different mode outside a repository than inside it — and falling
// back to the PREVIEW glyph, which this once did, put two identical icons in a
// three-row menu.
//
// `preview` is not here: it is not a template at all (the row's own default view)
// and its glyph is baked into the shell.
export function paneSideIconEntry(
  side: Exclude<PaneSide, "preview">,
  entries: PaneSideEntries
): TemplateEntry | null {
  return side === "claude"
    ? (entries.claude ?? entries.claudeBound ?? null)
    : (entries.git ?? entries.gitBound ?? null);
}

// The mode the pane SHOWS for a requested one: the request while it is still on
// offer, else THE FIRST MODE ON OFFER. A `_side` naming a mode this target hasn't
// got — a `git` carried in from a repository to a folder outside one — falls back
// here and the param is deliberately LEFT ALONE, which is the posture
// `_panelMode` had before it: the next folder that does offer the mode picks it up
// again, so a hop out of a repo and back in does not silently reset the pane.
//
// "First on offer" rather than the pane's own `preview` default (D281/D284), because
// `preview` is no longer always on offer: no FOLDER SUBJECT has one — neither a
// selected directory nor the no-selection state — and an absent `_side` parses as
// exactly that request, so falling back to the constant would resolve a folder to a
// side the list refuses, which is the pill naming one thing while the body renders
// another. **The no-selection case makes that the default path**: every folder opens
// with nothing selected (FS-16), so this fallback is what puts the pane on the chat
// about the open folder rather than on a Preview of it.
//
// **AN EMPTY LIST IS "UNDECIDED", NOT "the default"** (paneSideList: a folder
// subject whose companion probes are still out). The constant this returns there is a
// placeholder for the pill's label and the pane's key, NOT permission to render:
// the caller must hold its skeleton on an empty list. Answering `preview` and
// rendering it is precisely the bug — a chat under a pill reading "Preview" — so
// this function cannot be the only guard, and the caller's check is what makes it
// safe (see Listing's `paneUndecided`).
export function activePaneSide(offered: PaneSide[], want: PaneSide): PaneSide {
  if (offered.includes(want)) return want;
  return offered[0] ?? DEFAULT_PANE_SIDE;
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
  // `preview` with nothing selected is now REACHED ONLY as the neither-companion
  // fallback (D284): the no-selection state is a folder subject, so it lands on a
  // companion wherever one is offered, and the `self` key below identifies the state
  // that renders the "Select a file to preview." hint — a mount-backed folder, where
  // the hint is the pane's only content. A multi-selection previews nothing and needs
  // no per-path identity.
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

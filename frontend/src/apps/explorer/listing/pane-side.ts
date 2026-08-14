// The listing preview pane's TWO COMPANIONS and the `_side` param that records
// which one is showing — plus a `preview` FALLBACK that is not on offer. Pure and
// DOM-free, like the pane's other decision modules (pane-math, pane-modes), so the
// semantics below can be pinned by a test with no router and no React.
//
// **THE PANE IS A COMPANION COLUMN, NOT A PREVIEW SPLIT** (D285). That is the frame
// for everything here, and it took four decisions to arrive at because it was
// discovered rather than designed: D280 took away the folder's rendered PAGE, D281 a
// selected folder's preview, D284 the no-selection state's, and D285 the last one —
// a FILE row's. Each was reported as its own bug ("we don't want rendering", "the
// preview template in the dropdown when nothing is selected is wrong", "we do not
// want preview template in the sidebar for the file previews either"), and the
// through-line only reads clearly from the end: the column beside a listing is for
// things that TALK ABOUT what you are looking at, not for a second copy of it.
//
// **This makes the pane agree with the full-screen file sidebar, which was
// companions-only from the start** (`lib/preview-side.ts` over
// `mode-visibility.SIDEBAR_MODES` = `["claude", "git"]` — no `preview` side, ever).
// The listing pane was the odd one out, and the four decisions above are it
// converging on the shape the other half of the app already had.
//
//   claude   the chat, `chat_only=1`, about the selected row — about the OPEN
//            FOLDER when the selection names no single row (paneSideTarget).
//   git      the OPEN FOLDER's working tree — not the row's. See dir-mode.ts:
//            a working tree belongs to the folder, so `git` is bound to the
//            universal "/" key alone and the pane borrows the folder's entry.
//
//   preview  **NOT SELECTABLE, and not in the switcher.** It survives as the
//            pane's internal FALLBACK for one state: neither companion offered (a
//            mount-backed folder, where both gates refuse), where the pane must
//            still render something. There it shows what pane-modes.ts resolves —
//            the "Select a file to preview." hint for the folder itself, a file
//            row's own default template, or the metadata card. It is a state the
//            pane falls into, never a mode a user picks, which is why
//            `PANE_SIDE_MODES` still carries it (the fallback needs the type)
//            while `paneSideMenu` never draws a row for it and `paneSideList`
//            yields it only as that last resort.
//
// Closed at two, and a per-ROW companion is deliberately not a third: over a
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

// All three states the pane can BE IN. `preview` is here because the fallback needs
// the type, not because anything offers it (see the header).
export const PANE_SIDE_MODES = ["preview", "claude", "git"] as const;

export type PaneSide = (typeof PANE_SIDE_MODES)[number];

// What a USER can choose and what the URL can carry: the companions, and nothing
// else. Splitting this out of `PaneSide` is what makes "the pane fell back to
// preview" un-writable and un-requestable at the type level rather than by
// convention — a `_side=preview` cannot round-trip because it cannot be held.
export const PANE_SIDE_COMPANIONS = ["claude", "git"] as const;

export type PaneSideChoice = (typeof PANE_SIDE_COMPANIONS)[number];

// The state the pane falls into when neither companion is offered. **Not a
// default** — nothing lands here by preference, only by exhaustion. It was
// `DEFAULT_PANE_SIDE` until D285, and the rename is the point: as a "default" it
// was what an absent `_side` meant, which is exactly how a non-previewable subject
// ended up on a Preview pill.
export const PANE_SIDE_FALLBACK: PaneSide = "preview";

// The value `_side` takes when the user has SHUT the pane. Not a mode, and
// spelled as a word rather than as an absent param for the reason in
// `parsePaneSide` below.
export const PANE_SIDE_OFF = "off";

export interface PaneSideState {
  open: boolean;
  // The companion the user CHOSE, or **null for "no choice yet"** — which is what
  // an absent `_side` means and what `activePaneSide` resolves against the offered
  // list. Null rather than a named default, because the default is now dynamic
  // (whichever companion the folder offers first) and a constant cannot express it.
  mode: PaneSideChoice | null;
}

// No choice, pane open — an absent `_side`, an unknown value, and `_side=preview`
// all land here.
const OPEN_UNCHOSEN: PaneSideState = { open: true, mode: null };

function isPaneSideChoice(v: string): v is PaneSideChoice {
  return (PANE_SIDE_COMPANIONS as readonly string[]).includes(v);
}

// `_side` ON A FOLDER URL, and what an ABSENT one means. This is the one place
// the folder's reading of the param differs from the file preview's, so it is
// worth being explicit about both:
//
//   file    absent = CLOSED. The sidebar is an extra beside a content view that
//           is already complete on its own, so nothing is the same as nothing.
//   folder  absent = OPEN, **at whichever companion is offered first** (D285). The
//           pane is not an extra — it IS the folder view's other half, on since
//           long before this param existed, and it appears purely from the width of
//           the split (pane.ts). Reading absence as "closed" would turn every
//           existing folder URL, bookmark and recent into a one-column listing.
//
// **Absence is `mode: null`, not a named mode**, and that is the change D285 forced.
// It used to mean `preview`, which was safe only while `preview` was universally
// offered; once no subject offers it, an absent param resolving to it meant the pill
// naming a mode the pane would then fall back OUT of. Null says "no choice yet" and
// lets `activePaneSide` answer against what this folder actually offers — so a clean
// URL lands on Claude, and on Git in a folder whose chat is refused.
//
// Which is why closing is `_side=off` and not a deleted param: the two states an
// absent param could mean are not the same here, so the SHUT one has to say so.
// **The mode that is on offer first gets the CLEAN URL** (PT-9's rule — selecting
// the default deletes the param — now reading "Claude gets the clean URL"): the ONE
// writer normalises a pick of the leading companion to `null` (Listing's
// `selectSide`), so choosing the mode you are already on cannot grow a param, and
// only a deliberate second choice (`_side=git`) is written down.
//
// An unknown value reads as no-choice rather than as an error: a hand-typed
// `_side=graph`, or a stale `_side` carried in from a file view (router's navigate
// keeps the param across a DIRECTORY hop), lands open on the leading companion. Same
// silent fallback an unknown `_mode` gets. **`_side=preview` is one of those unknown
// values now** — it names a state the pane can only fall into, so a hand-typed or
// carried-in one is refused here rather than resolving to the fallback and pinning
// the pane to a hint.
export function parsePaneSide(raw: string | null): PaneSideState {
  if (raw === null) return OPEN_UNCHOSEN;
  if (raw === PANE_SIDE_OFF) return { open: false, mode: null };
  return raw !== "" && isPaneSideChoice(raw) ? { open: true, mode: raw } : OPEN_UNCHOSEN;
}

// What to write back — null means DELETE the param.
//
// A shut pane records only that it is shut, not what it last showed: the mode it
// would reopen to is remembered in component state for the session (the same
// place the file sidebar keeps its `lastSide`), so a reload of a closed pane
// **reopens at the leading companion** — Claude, where the folder offers it.
// Encoding the pair would put a mode nobody can see into every shared link of a
// one-column listing.
export function paneSideParam(state: PaneSideState): string | null {
  if (!state.open) return PANE_SIDE_OFF;
  return state.mode;
}

// Which sides this FOLDER actually offers. The subject no longer enters into it at
// all — D285 deleted the `subjectIsDir` parameter along with the last reason to ask,
// since `preview` is not on offer for a file row either. **What is offered is what
// the FOLDER can back**: the companions exist only while its own entry for them does
// (dir-mode.ts), so a folder outside a repository can be shown no Git and a
// mount-backed folder — where both gates refuse — can be shown neither.
//
// What the switcher DRAWS is a different question and `paneSideMenu` below
// answers it: an unofferable COMPANION is listed as a disabled row rather than
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

// **`preview` IS NOT ON OFFER — it is the last resort** (D285, the end of the arc
// D281 and D284 walked). A folder subject lost its preview because a folder is not a
// thing this pane previews (rendering the page it holds was D280's bug; the embedded
// listing peek is the listing already on the left; the "Select a file to preview."
// hint was a non-answer over a non-previewable subject). A FILE row lost it because
// the pane is a companion column, full stop — the same shape the full-screen file
// sidebar has always had. So there is nothing left for the subject to change, and
// the parameter that asked went with the question.
//
// **Unless neither companion is offered** (a mount-backed folder: both gates
// refuse), when `preview` comes back as the fallback. The pane must show something,
// and it is what pane-modes.ts resolves there: the hint for the folder itself, a
// file row's own default template, or the metadata card.
//
// **A PROBE STILL OUT IS NOT A DENIAL, and the answer is then NEITHER — an EMPTY
// LIST, meaning UNDECIDED.** `Listing` nulls both entries while `lib/dir-mode`
// resolves them, so "no companion offered" and "not answered yet" arrive in the same
// shape; taking the `preview` fallback there was the bug 7c37acf4 fixed — the pill
// reading "Preview" while a chat rendered under it, then a remount and a SECOND
// `agent.py` spawn when the verdict landed. So the caller holds a SKELETON on an
// empty list: no mode resolved, nothing mounted, one spawn when the answer arrives.
// (`paneSideMenu` reads this same list, so it draws its companions as CT-12 spinners
// meanwhile.)
//
// **A FILE ROW IS NO LONGER EXEMPT FROM THAT WAIT, and this is a deliberate, stated
// cost.** It used to be: its `preview` was its own template list, which the folder's
// companion gates say nothing about, so waiting would have been a skeleton over an
// answer already in hand. That exemption's premise is gone with the offer — there is
// no side to resolve for a file row until the probe answers either. The cost is
// bounded by the probe being **per FOLDER, not per row** (`useDirMode` is keyed on
// the folder and caches per directory): the window opens once per folder open, so
// arrow-keying between file rows afterwards never re-enters it. The one visible case
// is a `?sel=` deep link straight to a file row, which now shows the skeleton for
// that one window instead of the file's preview.
export function paneSideList(entries: PaneSideEntries): PaneSide[] {
  const companions: PaneSide[] = [];
  if (entries.claude) companions.push("claude");
  if (entries.git) companions.push("git");
  if (companions.length > 0) return companions;
  if (entries.claudePending || entries.gitPending) return [];
  return [PANE_SIDE_FALLBACK];
}

// One row per mode the pane MAY BE ON — all three for a file row or a
// multi-selection, and the two companions for a folder SUBJECT, selected row or open
// (D285: `preview` is not a row here at all — see the block on paneSideMenu).
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
export function paneSideMenu(entries: PaneSideEntries): PaneSideMenuEntry[] {
  const companion = (mode: "claude" | "git"): PaneSideMenuEntry => {
    if (entries[mode]) return { mode };
    const pending = mode === "claude" ? entries.claudePending : entries.gitPending;
    return pending ? { mode, pending: true } : { mode, disabledReason: unavailableReason(mode) };
  };
  // The COMPANIONS, always both, and NEVER a `preview` row: it is not a mode a user
  // can pick (D285), so drawing it would be a control that cannot be honoured. In the
  // fallback state the pane IS on `preview` and this menu shows two disabled
  // companions with their reasons — which is the honest account of why the pane is
  // showing what it is showing.
  return [companion("claude"), companion("git")];
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
export function activePaneSide(offered: PaneSide[], want: PaneSideChoice | null): PaneSide {
  if (want !== null && offered.includes(want)) return want;
  return offered[0] ?? PANE_SIDE_FALLBACK;
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

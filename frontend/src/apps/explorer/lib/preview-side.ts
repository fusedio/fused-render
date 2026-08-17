// The file preview's `_side` split, as decisions rather than as JSX — the file
// half of what pane-side.ts is for the folder listing's pane, and DOM-free for
// the same reason: the rules below are all about a list that is ASSEMBLED from
// two sources with different timings, which is exactly the thing a test should
// pin and a React component should not be the only statement of.
//
// **AN ABSENT `_side` MEANS OPEN, AT THIS FILE'S DEFAULT COMPANION** (D323), and
// shut is the word `off` — the same reading the folder pane has always had, and
// the reversal of the asymmetry both modules used to state at length ("file:
// absent = CLOSED; folder: absent = OPEN"). The old rule was defensible on its
// own terms — a sidebar is an extra beside a content view that is complete
// without it, so nothing meant nothing — but it made the sidebar's presence a
// PREFERENCE, and a preference has to be stored somewhere. It was, twice over:
// in the URL, and from there into the file's session sidecar (lib/session), which
// replayed it on every later bare open. So one file remembered a sidebar for
// months while its neighbour never had one, and the app had no way back to a
// uniform starting state. The owner's words were "we don't want any persisted
// preference. similar to folder sidebars, they should always open at a fixed 30%
// size on page refresh. any other changes being made (open/width) must be
// persisted only for the session."
//
// So the whole of `_side` on a file is now a REQUEST FOR THIS DOCUMENT: read off
// the URL at mount (`parseSide`), resolved against what the file offers on every
// render (`resolveSide`), carried across the shell's pushState navigation because
// the URL carries it, and gone on a refresh because a bare URL opens at the
// default again. The width follows the same policy in `lib/side-store.ts`, and
// `_side` is stripped from the session sidecar at both ends (lib/session,
// server/session.py) so no old sidecar can put the old behaviour back.
//
// THE TWO SOURCES, and the whole reason this module exists:
//
//   OWN         the companions in the file's own stat.templates — `claude`, and
//               a `git` only if a user registry bound one to the
//               extension. These EXIST as of the stat; a conditional one's GATE
//               may still be in flight (CT-12), but the entry is real.
//   BORROWED    `git`, from the file's PARENT FOLDER (lib/dir-mode). Until that
//               probe answers there is only a PLACEHOLDER — an entry whose path
//               and icon are null and which may turn out not to exist at all,
//               because the parent may be outside a repository.
//
// A placeholder is listed anyway, because `_side` is read from the URL at MOUNT:
// resolve `?_side=git` against a list that does not mention git yet and the
// reconcile below rewrites the param away before the answer lands. But listing it
// is ALL it may do. A pending placeholder must not decide anything the user can
// see, and that is the difference between `offered` and `on` below:
//
//   offered   there is SOMETHING that may end up in the sidebar, so a `_side`
//             naming it is honoured instead of stripped. Pending included.
//   on        the split is really on: a companion is KNOWN to exist, so the
//             sidebar column and its toggle may render and the content pane's
//             mode list drops the companions.
//
// Conflating the two is what made a companion-less file (a .pdf, a video) open
// with a Git toggle in its bar for as long as the probe took and then lose it
// again — a control flashing in and out on every open, for a folder that has no
// working tree at all.
//
// Note the asymmetry with an OWN conditional companion, which DOES count as
// settled while its gate resolves. Its entry is in this file's template list
// either way, so the only question open is whether it is allowed; treating it as
// unsettled would move it between the content menu and the sidebar as the verdict
// landed, which is a worse flash than the one this module removes.
//
// **THAT ASYMMETRY DOES NOT REACH `defaultSide`** — the one thing an absent `_side`
// consults (D323). "Settled enough to be listed and toggled to" is not "settled
// enough to OPEN BY ITSELF": `claude` ships a condition.py, so on every file it is
// pending for as long as /api/fs/conditions takes, and auto-opening it would put an
// empty column on screen that vanishes when a mount-backed file's verdict comes
// back false. Both kinds of pending are excluded there and only there; see the
// field.
//
// THE THIRD LIST, `menu`, and why it is not either of the two above: what the
// SWITCHER shows is now EVERY COMPANION, ALWAYS — the ones this file has not
// got listed as disabled rows carrying the reason why (mode-visibility's
// `unavailableReason`, where the argument for saying it out loud is written down).
// So the sidebar's header reads the same over every file, and a file outside a
// repository gets a Git row that explains itself instead of a switcher that
// quietly shrank — or, at one entry, disappeared.
//
// A disabled row is a LABEL, not a mode, and none of the rules above may see it:
// it does not turn the split `on`, it is not `settled`, it never becomes the
// toggle's target, and `initialSide`/`reconcileSideSearch` resolve a `?_side`
// naming it exactly as they resolve one naming a mode nobody has ever heard of —
// away. Hence three lists rather than a flag on one: `menu` is for drawing,
// `all` is what a `_side` may name, `settled` is what actually exists. Feeding
// the drawing list to any of the decisions would put a file back on a Git view
// its folder cannot produce, which is the failure `on` vs `offered` exists to
// prevent, arrived at from the other direction.
import type { TemplateEntry } from "@platform/lib/api";
import {
  SIDEBAR_MODES,
  defaultSidebarMode,
  isSidebarMode,
  orderSidebarModes,
  unavailableReason,
} from "@platform/lib/mode-visibility";

export interface SideSplitInput {
  // Whether this surface splits at all (a single file on the explorer route, in
  // its own window — see Preview's `splitCapable`).
  splitCapable: boolean;
  // The file's own modes, already partitioned (lib/mode-visibility).
  content: TemplateEntry[];
  own: TemplateEntry[];
  // The parent folder's `git` entry — a placeholder while `borrowedPending`,
  // null when the parent does not offer one (or there is nothing to borrow
  // because the file has a `git` of its own).
  borrowed: TemplateEntry | null;
  borrowedPending: boolean;
  // THIS FILE's condition.py verdicts are still in flight (`conditions === null`,
  // lib/mode-visibility's `isModePending`). Read for ONE thing — `defaultSide`
  // below — because an OWN gated companion is `settled` for every other purpose
  // and deliberately so (see the header's asymmetry note), but must not be what
  // an absent `_side` opens: there is nothing to put in the column until the
  // gate answers, and the gate may say no.
  //
  // Separate from `borrowedPending` rather than folded into it because the two
  // are different probes with different timings — this file's `/api/fs/conditions`
  // and the PARENT's stat + gate — which is the same split Preview's
  // `isSidePending` makes for exactly this reason.
  conditionsPending?: boolean;
  // Every companion that EXISTS AS A BINDING, whether or not it may be shown: the
  // file's own sidebar templates BEFORE the visibility filter, plus the parent's
  // `git` however its gate voted (lib/dir-mode's `bound`). Order and duplicates
  // are irrelevant — exactly one field is ever read off these.
  //
  // That field is the ICON, and it is the whole reason the input exists. A
  // disabled row is the same mode with the click taken away — same glyph, same
  // name, dimmed — and building it from nothing gave it templateModeIcon's
  // last-resort letter box instead: Git was the Git logo inside a repository and a
  // boxed "G" one folder outside it. Two glyphs for one mode reads as two
  // different modes, which is the one thing a disabled row must not do.
  //
  // Optional because a surface that never draws the menu (anything with
  // `splitCapable` false) has no icons to look up.
  bound?: TemplateEntry[];
}

// A switcher row. A real companion is its TemplateEntry unchanged; a companion
// this file cannot show is a placeholder carrying the reason it is disabled, and
// its icon if the mode is bound anywhere. The two are told apart by
// `disabledReason` alone, so a consumer that only draws rows needs to know
// nothing else — and `path` stays null on a placeholder, so nothing can build a
// render URL out of one by mistake.
export interface SideEntry extends TemplateEntry {
  disabledReason?: string;
}

export interface SideSplit {
  // What the switcher DRAWS: every companion in SIDEBAR_MODES, always, with the
  // unavailable ones disabled and explained. Nothing decides anything from this
  // list — see the header.
  menu: SideEntry[];
  // The companions a `_side` may NAME: the real entries plus a still-pending
  // borrowed placeholder, in SIDEBAR_MODES order. Not what the switcher shows.
  all: TemplateEntry[];
  // Those known to exist — `all` minus a still-pending borrowed entry.
  settled: TemplateEntry[];
  // A companion is known to exist AND there is a content pane to put it beside.
  on: boolean;
  // Something may yet land in the sidebar, so a `_side` naming it is tolerated.
  offered: boolean;
  // WHAT AN ABSENT `_side` OPENS (D323), and null when nothing does. Over the
  // companions KNOWN TO BE SHOWABLE — which is a shorter list than `settled`, and
  // shorter for two different reasons:
  //
  //   the borrowed placeholder   `settled` already excludes it. A file whose only
  //                              candidate is a `git` that may turn out not to
  //                              exist must not flash a column open for the length
  //                              of the parent probe.
  //   an OWN gate still out      `settled` includes it, and that asymmetry is
  //                              deliberate for the split's EXISTENCE (see the
  //                              header) — but not for this. `claude` ships a
  //                              condition.py, so on every file it is pending until
  //                              /api/fs/conditions answers; auto-opening it means
  //                              an empty column (`src` null) that vanishes when a
  //                              mount-backed file's verdict comes back false,
  //                              seconds later on a cold mount, jumping the content
  //                              pane's width twice for a sidebar the file never
  //                              gets. And a gated entry being "never the default"
  //                              is the rule the content pane already follows
  //                              (`defaultMode`, CT-12, and the server's own
  //                              `_mark_conditions` docstring).
  //
  // Null means "not yet, ask again when the verdict lands" — the same posture `on`
  // and `sideToggleTarget` take, and what makes the reconcile leave `_side` alone
  // meanwhile instead of writing `off`. A DEEP LINK to either kind of pending
  // companion is still honoured: `resolveSide` resolves a NAMED mode against
  // `all`, and only the default is withheld here.
  //
  // It is also the value that decides the URL's SPELLING (`sideParam`): the
  // default gets the clean URL, so only a deliberate second choice is written
  // down. That is PT-9's rule, and the folder pane's `selectSide` normalisation.
  defaultSide: string | null;
}

// The switcher's rows: the closed list of companions, each one either the real
// entry or a disabled placeholder. A PENDING borrowed entry is a real entry here
// and NOT a disabled one — its probe has not answered, so "not inside a git
// repository" would be a claim rather than a report; the caller renders it as the
// spinner row CT-12 already defines and it either becomes selectable or becomes
// disabled when the verdict lands.
//
// A disabled row wears the mode's OWN icon wherever the mode is bound at all —
// `bound` above says where those come from and why it matters. The letter box
// templateModeIcon falls back to is reached only by a mode NOTHING binds (a
// companion no registry anywhere registers a template for), where there is no
// real glyph in existence to use.
//
// The trailing loop is for a companion that is NOT one of SIDEBAR_MODES — a user
// registry binding one of its own into this half. It has no canned reason and no
// fixed rank, so it keeps its place at the end, which is where orderSidebarModes
// puts it too.
function sidebarMenu(all: TemplateEntry[], bound: TemplateEntry[]): SideEntry[] {
  const menu: SideEntry[] = (SIDEBAR_MODES as readonly string[]).map(
    (mode) =>
      all.find((e) => e.mode === mode) ?? {
        mode,
        // A placeholder is a row, never a template: no path, so no URL can be
        // built from it even by accident.
        path: null,
        icon: bound.find((e) => e.mode === mode)?.icon ?? null,
        disabledReason: unavailableReason(mode),
      }
  );
  for (const e of all) if (!isSidebarMode(e.mode)) menu.push(e);
  return menu;
}

export function sideSplit(i: SideSplitInput): SideSplit {
  const all = orderSidebarModes(i.borrowed ? [...i.own, i.borrowed] : i.own);
  const settled = i.borrowedPending ? all.filter((e) => e !== i.borrowed) : all;
  // ...and only while there is something to put on BOTH sides. A file whose only
  // companion is `claude` has no content pane to sit a sidebar next to, so it
  // renders as it did before the split existed: chat, full width, content mode.
  const splittable = i.splitCapable && i.content.length > 0;
  // Every reason a companion is not yet SHOWABLE, in one predicate — the borrowed
  // entry answers to its own probe, everything else to this file's gates. Only
  // `defaultSide` reads it; see the field's comment for why nothing else does.
  const unresolved = (e: TemplateEntry) =>
    e === i.borrowed ? i.borrowedPending : !!e.conditional && !!i.conditionsPending;
  return {
    menu: sidebarMenu(all, i.bound ?? []),
    all,
    settled,
    on: splittable && settled.length > 0,
    offered: splittable && all.length > 0,
    defaultSide: splittable ? defaultSidebarMode(all.filter((e) => !unresolved(e))) : null,
  };
}

// THE VALUE `_side` TAKES WHEN THE USER HAS SHUT THE SIDEBAR — a word, not an
// absent param, because absence means the opposite (see the header). Deliberately
// the SAME word the folder pane uses (`listing/pane-side.ts`'s `PANE_SIDE_OFF`):
// one param name across two surfaces already, so a second spelling for the same
// state would make a `_side` carried between them mean different things
// depending on which way the user walked. The two constants are asserted equal in
// the test rather than one importing the other — a file module has no business
// depending on the listing's, and the assertion catches a rename either way.
export const SIDE_OFF = "off";

// What the URL ASKED FOR, before anything about this file is consulted. Split out
// of the resolution below because the two answer different questions and only one
// of them can be held as state: the request is what the user chose and survives a
// verdict landing, while the resolution is what this paint can honour.
//
//   open    false ONLY for an explicit `_side=off`. Absence, an empty value and
//           an unknown mode are all OPEN — see the header, and note that "unknown
//           reads as no-choice rather than as an error" is the folder pane's rule
//           too (a hand-typed `_side=graph`, or a `_side=preview` carried in from
//           a listing, must not leave the sidebar in a state nothing can render).
//   mode    the companion NAMED, or null for "no choice yet". Not validated here:
//           `resolveSide` owns the lists, so a mode this file cannot show is
//           dropped there and lands on the default rather than on nothing.
export interface SideRequest {
  open: boolean;
  mode: string | null;
}

// The state a bare URL asks for: open, at whatever this file offers first.
const OPEN_UNCHOSEN: SideRequest = { open: true, mode: null };

// LEGACY DEEP LINKS are the second branch. `?_mode=claude` is what every
// bookmark, recent, saved session and shared URL from before the split says, and
// it is still a perfectly clear request — "open this file's chat". It now means
// the SIDEBAR: the content pane falls back to its default mode (effectiveActive
// does that for free, since `claude` is no longer in its list) and
// `reconcileSideSearch` drops the param once.
//
// It is read only where `_side` is silent, and an explicit `_side=off` therefore
// beats it: a URL that says both "shut" and "open the chat" was assembled from a
// close click on top of an old link, and the click is the newer of the two.
export function parseSide(search: string): SideRequest {
  const params = new URLSearchParams(search);
  const raw = params.get("_side");
  if (raw === SIDE_OFF) return { open: false, mode: null };
  if (raw !== null && raw !== "") return { open: true, mode: raw };
  const legacy = params.get("_mode");
  if (legacy !== null && isSidebarMode(legacy)) return { open: true, mode: legacy };
  return OPEN_UNCHOSEN;
}

// WHICH COMPANION IS ACTUALLY SHOWING, recomputed on every render — not stored,
// so a verdict that lands cannot leave the screen disagreeing with the lists.
//
// The named request is resolved against `all` (see the header) so a `?_side=git`
// deep link survives until the borrowed probe answers — and, for the same reason
// stated the other way round, a `?_side=git` on a file whose Git row is the
// DISABLED placeholder does not resolve. The row is in `menu`, not in `all`; a
// deep link to it is a deep link to an explanation.
//
// What is NEW is where an unhonourable request lands: the DEFAULT, not nothing.
// A denial says "this companion is not the one", never "shut the sidebar", and
// the only thing that shuts it is the user (`_side=off`) or a file with nothing
// to put in it. That is also what makes the switcher's disabled rows safe to
// deep-link to: the URL resolves onward to something real instead of leaving a
// closed column beside a param the user cannot act on.
export function resolveSide(req: SideRequest, split: SideSplit): string | null {
  if (!split.offered || !req.open) return null;
  if (req.mode && split.all.some((e) => e.mode === req.mode)) return req.mode;
  return split.defaultSide;
}

// SET OR DELETE ONE PARAM, TEXTUALLY, LEAVING EVERY OTHER BYTE ALONE. `null`
// deletes; a value is appended (a replaced key therefore moves to the end, which
// is the only difference from URLSearchParams.set and is not something any reader
// can see).
//
// It exists because URLSearchParams was the writer and a round trip through it
// RE-ENCODES what it merely passes through: `stretch=2,1471` came back as
// `stretch=2%2C1471`, `sel=a b` as `sel=a+b`. Whatever this module returns is what
// goes in the address bar AND what the session sidecar records, and LSN-2 says
// that string is the shell's query verbatim — the same reason the session strip
// itself is textual (platform/lib/session-params, server/session.py's
// `_strip_side`). It used to be nearly unreachable, since the reconcile only fired
// when `_side` disagreed; normalising the default away (`sideParam`) makes it fire
// on ordinary links that were previously left alone — every `?_side=claude` inbox
// handoff (shell/schedule-lib), every old bookmark — and on the first close of
// every auto-opened sidebar, so the exposure is now most of the traffic.
//
// Both writers go through it: this module's reconcile and Preview's `setSide`.
export function writeQueryParam(
  search: string,
  key: string,
  value: string | null
): string {
  const kept = search
    .split("&")
    .filter((p) => p !== "" && p.split("=", 1)[0] !== key);
  if (value !== null) kept.push(key + "=" + value);
  return kept.join("&");
}

// The URL's SPELLING for a state, and the one writer's rule (used by Preview's
// `setSide` and by the reconcile below, so the two cannot drift): the default
// companion gets the CLEAN URL, a shut sidebar says `off`, and only a deliberate
// second choice is written down. Null means DELETE the param.
//
// Normalising the default away is not cosmetic. It is what keeps a plain file
// open from growing a param at all — and a param is exactly what got recorded and
// replayed before (see the header), so the shortest URL that means "the usual
// thing" is the one worth having.
export function sideParam(
  activeSide: string | null,
  defaultSide: string | null
): string | null {
  if (activeSide === null) return SIDE_OFF;
  return activeSide === defaultSide ? null : activeSide;
}

// What the toggle button acts on, and so what it looks like: whatever is open,
// else the last thing that was, else this file's default companion (the chat, if
// it has one — lib/mode-visibility). Null means the button does not render, which
// is the answer while the only candidate is a pending placeholder: a control that
// may be about to prove it has nothing to open is worse than no control.
//
// `targets` is the SETTLED list (empty when the split is off), never `all`.
export function sideToggleTarget(
  targets: TemplateEntry[],
  activeSide: string | null,
  lastSide: string | null
): string | null {
  if (activeSide) return activeSide;
  if (lastSide && targets.some((e) => e.mode === lastSide)) return lastSide;
  return defaultSidebarMode(targets);
}

// Keeping the URL honest about what is actually open, for the cases the user's
// own clicks don't cover. Returns the query string to REPLACE the current one
// with (no leading "?"), or null when it already agrees and must be left alone.
//
// Three jobs:
//
//   1. the legacy `?_mode=claude` migration above — drop the param the split
//      re-homed, once. Only where the split is on offer, since everywhere else
//      `_mode=claude` still names a real content mode. Note that the migration is
//      now usually a pure DELETION: the sidebar opens at the chat from a bare URL
//      anyway, so there is nothing to write in `_mode`'s place unless the chat is
//      not what this file would open by itself.
//   2. a `_side` this file cannot honour: a param carried in from another view
//      (the folder pane writes `_side` too), or one whose gate has just DENIED
//      the mode. It goes — but "goes" now means "back to absence, which means the
//      default", not "the sidebar closes". The denied mode is still LISTED, as the
//      disabled row that says why (see the header), and that changes nothing here:
//      a row the user cannot select is not a state the URL is allowed to hold.
//   3. write `_side` when state moved without the user's click, and equally DROP
//      one that says no more than absence already says (`_side=claude` where
//      claude is the default) — the normalisation `sideParam` states.
//
// **A STILL-PENDING BORROWED ENTRY SUSPENDS ALL OF IT**, and there are two shapes
// of that now. The old one: `activeSide` is the pending mode, `_side` already says
// so, nothing to do. The new one: the request is OPEN, nothing is settled, so
// there is no default to open at and `activeSide` is null — and `off` must NOT be
// written there, because that would shut the sidebar for good on a file whose only
// companion is about to prove it exists. Both leave `_side` exactly as it is; the
// verdict is what settles it, and a denial that empties the list brings us back to
// job 2 through `offered`.
//
// Only ever on a splitting surface: a panel pane renders `claude` as its content
// mode and a folder's `_side` is the listing pane's (listing/pane-side.ts), so
// nothing here may touch either.
export function reconcileSideSearch(
  search: string,
  o: {
    splitCapable: boolean;
    offered: boolean;
    // The REQUEST's open bit, which `activeSide` alone cannot carry: "the user
    // shut it" and "nothing has resolved yet" are both a null active side and
    // want opposite things done to the URL (see the pending block above).
    open: boolean;
    activeSide: string | null;
    defaultSide: string | null;
  }
): string | null {
  if (!o.splitCapable) return null;
  // READ through URLSearchParams (decoding is what a reader wants), WRITE through
  // writeQueryParam (byte-preserving — see there).
  const params = new URLSearchParams(search);
  const legacy = params.get("_mode");
  const stale = o.offered && legacy !== null && isSidebarMode(legacy);
  // What the URL should say — `undefined` for "no verdict yet, leave `_side`
  // alone" (the pending block above). A file with nothing on offer holds no
  // `_side` at all, whatever was carried in.
  const want =
    !o.offered
      ? null
      : o.activeSide === null && o.open
        ? undefined
        : sideParam(o.activeSide, o.defaultSide);
  const agrees = want === undefined || (params.get("_side") ?? null) === want;
  if (agrees && !stale) return null;
  let out = search.replace(/^\?/, "");
  if (want !== undefined) out = writeQueryParam(out, "_side", want);
  if (stale) out = writeQueryParam(out, "_mode", null);
  return out;
}

// The file preview's `_side` split, as decisions rather than as JSX — the file
// half of what pane-side.ts is for the folder listing's pane, and DOM-free for
// the same reason: the rules below are all about a list that is ASSEMBLED from
// two sources with different timings, which is exactly the thing a test should
// pin and a React component should not be the only statement of.
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
  return {
    menu: sidebarMenu(all, i.bound ?? []),
    all,
    settled,
    on: splittable && settled.length > 0,
    offered: splittable && all.length > 0,
  };
}

// `_side` as read at MOUNT, exactly like `_mode`, so a bookmark or a shared link
// restores the split. Resolved against `all` (see the header) so a `?_side=git`
// deep link survives until the borrowed probe answers — and, for the same reason
// stated the other way round, so a `?_side=git` on a file whose Git row is the
// DISABLED placeholder resolves to null. The row is in `menu`, not in `all`; a
// deep link to it is a deep link to an explanation, and is dropped exactly like
// a `_side` naming a mode that does not exist.
//
// LEGACY DEEP LINKS are the second branch. `?_mode=claude` is what every
// bookmark, recent, saved session and shared URL from before the split says, and
// it is still a perfectly clear request — "open this file's chat". It now means
// the SIDEBAR: the content pane falls back to its default mode (effectiveActive
// does that for free, since `claude` is no longer in its list) and
// `reconcileSideSearch` rewrites the URL once.
export function initialSide(search: string, split: SideSplit): string | null {
  if (!split.offered) return null;
  const params = new URLSearchParams(search);
  const has = (m: string | null) => !!m && split.all.some((e) => e.mode === m);
  const want = params.get("_side");
  if (has(want)) return want;
  const legacy = params.get("_mode");
  return has(legacy) ? legacy : null;
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
//      `_mode=claude` still names a real content mode.
//   2. a `_side` this file cannot honour: a param carried in from another view
//      (the folder pane writes `_side` too), or one whose gate has just DENIED
//      the mode. It goes, rather than sitting in the URL for a session sidecar to
//      record and replay on the next bare open (lib/session). The denied mode is
//      still LISTED — as the disabled row that says why (see the header) — and
//      that changes nothing here: a row the user cannot select is not a state the
//      URL is allowed to hold.
//   3. write `_side` when state moved without the user's click.
//
// A still-PENDING borrowed entry is neither honoured nor stripped: `activeSide`
// is the pending mode, `_side` already says so, and this returns null. The verdict
// is what settles it — allowed keeps the param, denied brings us back to job 2.
//
// Only ever on a splitting surface: a panel pane renders `claude` as its content
// mode and a folder's `_side` is the listing pane's (listing/pane-side.ts), so
// nothing here may touch either.
export function reconcileSideSearch(
  search: string,
  o: { splitCapable: boolean; offered: boolean; activeSide: string | null }
): string | null {
  if (!o.splitCapable) return null;
  const params = new URLSearchParams(search);
  const legacy = params.get("_mode");
  const stale = o.offered && legacy !== null && isSidebarMode(legacy);
  if (params.get("_side") === o.activeSide && !stale) return null;
  if (o.activeSide) params.set("_side", o.activeSide);
  else params.delete("_side");
  if (stale) params.delete("_mode");
  return params.toString();
}

// The file preview's `_side` split, as decisions rather than as JSX — the file
// half of what pane-side.ts is for the folder listing's pane, and DOM-free for
// the same reason: the rules below are all about a list that is ASSEMBLED from
// two sources with different timings, which is exactly the thing a test should
// pin and a React component should not be the only statement of.
//
// THE TWO SOURCES, and the whole reason this module exists:
//
//   OWN         the companions in the file's own stat.templates — `claude`,
//               `history`, and a `git` only if a user registry bound one to the
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
import type { TemplateEntry } from "@platform/lib/api";
import { defaultSidebarMode, isSidebarMode, orderSidebarModes } from "@platform/lib/mode-visibility";

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
}

export interface SideSplit {
  // The sidebar switcher's list, in SIDEBAR_MODES order, pending placeholder
  // included.
  all: TemplateEntry[];
  // Those known to exist — `all` minus a still-pending borrowed entry.
  settled: TemplateEntry[];
  // A companion is known to exist AND there is a content pane to put it beside.
  on: boolean;
  // Something may yet land in the sidebar, so a `_side` naming it is tolerated.
  offered: boolean;
}

export function sideSplit(i: SideSplitInput): SideSplit {
  const all = orderSidebarModes(i.borrowed ? [...i.own, i.borrowed] : i.own);
  const settled = i.borrowedPending ? all.filter((e) => e !== i.borrowed) : all;
  // ...and only while there is something to put on BOTH sides. A file whose only
  // companion is `claude` has no content pane to sit a sidebar next to, so it
  // renders as it did before the split existed: chat, full width, content mode.
  const splittable = i.splitCapable && i.content.length > 0;
  return {
    all,
    settled,
    on: splittable && settled.length > 0,
    offered: splittable && all.length > 0,
  };
}

// `_side` as read at MOUNT, exactly like `_mode`, so a bookmark or a shared link
// restores the split. Resolved against `all` (see the header) so a `?_side=git`
// deep link survives until the borrowed probe answers.
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
//      record and replay on the next bare open (lib/session).
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

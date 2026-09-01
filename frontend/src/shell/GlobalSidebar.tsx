// THE sidebar — one for the whole app, on every route. Replaces the old pair
// (ShellSidebar app-switcher on shell routes, ExplorerSidebar on fs routes):
// primary nav on top (Home / Tasks / AI Models, plus Canvases once the feature
// is turned on in Preferences AND this machine is signed in to Fused), the
// explorer's Bookmarks below it, and a
// single Settings trigger pinned to the bottom that opens a menu holding
// everything else (Config for now, plus Templates / Mounts /
// Preferences).
//
// Lives in the shell layer on purpose: it composes both platform chrome
// (SidebarFrame) and explorer-owned sections (Bookmarks), which only
// the shell is allowed to import together (scripts/check-boundaries.mjs).
import { useEffect, useState } from "react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "@platform/shadcn/ui/dropdown-menu";
import { Badge } from "@platform/shadcn/ui/badge";
import { ListTodo } from "lucide-react";
import { SidebarFrame, NavItem } from "@platform/ui/sidebar/SidebarFrame";
import UpdateBadge from "@platform/ui/UpdateBadge";
import type { SidebarRailItem } from "@platform/ui/sidebar/SidebarFrame";
import type { Config } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { isBrowserHandledClick } from "@platform/lib/appEntry";
import { TOURS, startTour } from "@platform/lib/tours";
import { useUrlVersion } from "@platform/lib/hooks";
import { useClaudeConfigAvailable } from "@apps/claude_config/available";
import { useCanvasesLoggedIn } from "@apps/canvases/logged-in";
import { useCanvasesFeature } from "@apps/canvases/feature-flag";
import { useAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { isAiModelsPath, tabHref } from "@apps/ai_models/routes";
import { markTasksSeen, useTasksPulse } from "@shell/tasksPulse";
import { pulseTitle, runningLabel } from "@shell/tasks-lib";
import { formatSize } from "@platform/lib/format";
import BookmarksSection from "@apps/explorer/sidebar/BookmarksSection";
import CurrentAppsSection from "@shell/CurrentAppsSection";
import { useSidebarArrowNav } from "@shell/sidebarArrowNav";

// House — the Home page (/home): search hero + the three recency strips.
const HOME_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M3 10.5 12 3l9 7.5" />
    <path d="M5 9.5V21h14V9.5" />
    <path d="M10 21v-6h4v6" />
  </svg>
);

// Sliders — the Claude Config app is a settings panel over ~/.claude.
const CLAUDE_CONFIG_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <line x1="4" y1="21" x2="4" y2="14" />
    <line x1="4" y1="10" x2="4" y2="3" />
    <line x1="12" y1="21" x2="12" y2="12" />
    <line x1="12" y1="8" x2="12" y2="3" />
    <line x1="20" y1="21" x2="20" y2="16" />
    <line x1="20" y1="12" x2="20" y2="3" />
    <line x1="1" y1="14" x2="7" y2="14" />
    <line x1="9" y1="8" x2="15" y2="8" />
    <line x1="17" y1="16" x2="23" y2="16" />
  </svg>
);

// The Inbox tray icon lived here. Inbox is GONE, not merely unadvertised
// (Akshil, 2026-08-18: "why do we have sessions route? i thought we remove
// sessions.. let's remove this we don't need this") — Tasks supersedes it, and
// a route nothing links to is a page nobody maintains.

// A processor die with its pins (ionicons' hardware-chip-outline): the AI
// Models entry is about what this machine can RUN, not about the bytes the
// Hugging Face cache is parking on disk — the stacked-disks storage icon it
// replaces read as the latter. Three pins a side, not ionicons' four: at 16px
// the fourth pair closes the gap into a smudge.
const AI_MODELS_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="5" y="5" width="14" height="14" rx="3" />
    <rect x="9" y="9" width="6" height="6" rx="1" />
    <path d="M8.5 5V2.5M12 5V2.5M15.5 5V2.5M8.5 19v2.5M12 19v2.5M15.5 19v2.5M5 8.5H2.5M5 12H2.5M5 15.5H2.5M19 8.5h2.5M19 12h2.5M19 15.5h2.5" />
  </svg>
);

const TEMPLATES_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="3" y="3" width="7" height="7" rx="1" />
    <rect x="14" y="3" width="7" height="7" rx="1" />
    <rect x="3" y="14" width="7" height="7" rx="1" />
    <rect x="14" y="14" width="7" height="7" rx="1" />
  </svg>
);

const MOUNTS_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M17.5 19a4.5 4.5 0 1 0-.9-8.9 6 6 0 1 0-11.4 2.4A3.5 3.5 0 0 0 6.5 19h11z" />
  </svg>
);

// A to-do list (lucide ListTodo, sized like the hand-drawn icons around it):
// Tasks is the page about work, and the app page's Tasks tab wears the same
// glyph (shell/AppPage.tsx) so the two read as one thing. Was a clock while the
// page was "Scheduled".
const SCHEDULED_ICON = <ListTodo size={16} strokeWidth={2} aria-hidden="true" />;

// Connected nodes: a canvas is a graph of UDFs.
const CANVASES_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="6" cy="6" r="3" />
    <circle cx="18" cy="10" r="3" />
    <circle cx="9" cy="18" r="3" />
    <path d="M8.8 7.1 15.2 9M7.9 15.4 6.8 8.9M11.6 16.6l4.2-4.4" />
  </svg>
);

const PREFERENCES_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </svg>
);

// A circled question mark — the app's one help affordance, and what a reader
// looks for when they want the walkthrough back.
const TOURS_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="9" />
    <path d="M9.4 9.2a2.7 2.7 0 1 1 3.4 2.6v1.6" />
    <circle cx="12.8" cy="16.8" r="0.6" fill="currentColor" stroke="none" />
  </svg>
);

interface PrefsMenuEntry {
  /** Where the entry goes — or, for an `onPick` entry, a stable key that also
      says where its subject lives (the tours have no page of their own). */
  href: string;
  label: string;
  /** Optional, because a flyout of plain titles (the tours) has none: the icon
      column is reserved per group, so those rows sit flush instead of behind an
      empty gutter — the same rule ContextMenu's `showIcon` applies. */
  icon?: React.ReactNode;
  extra?: React.ReactNode;
  /** Run this instead of navigating to `href` — the tour entries replay a
      walkthrough in place rather than going anywhere. */
  onPick?: () => void;
  /** A one-level flyout hung off this row (Tours). Its own entries never carry
      a `submenu` of their own — one level, like ContextMenu's. */
  submenu?: PrefsMenuEntry[];
}

// The bottom Settings trigger (a NavItem-shaped row in the expanded sidebar,
// an icon on the collapsed rail) and its pop-up menu. Split in two because the
// two triggers live in different, mutually-exclusive subtrees — SidebarFrame
// renders either its rail or its `children`, never both — while the popover
// itself must stay mounted regardless of collapse state, so its open/closed
// position lives in GlobalSidebar and the popover renders as a sibling of
// SidebarFrame rather than nested inside one trigger's markup.

// The expanded-sidebar trigger row.
function PreferencesTrigger({
  open,
  dot,
  active,
  trailing,
  onToggle,
}: {
  open: boolean;
  dot?: React.ReactNode;
  /** The current route is one of the menu's destinations — the trigger is
      the only sidebar chrome that can show it. */
  active: boolean;
  /** Trailing-edge content, same slot NavItem gives Tasks its count — this
      row's is the version chip. */
  trailing?: React.ReactNode;
  onToggle: (el: HTMLElement) => void;
}) {
  return (
    <button
      type="button"
      className={"sidebar-item sidebar-prefs-trigger" + (active ? " active" : "")}
      aria-haspopup="menu"
      aria-expanded={open}
      onClick={(e) => onToggle(e.currentTarget)}
    >
      <span className="icon">
        {PREFERENCES_ICON}
        {dot}
      </span>{" "}
      Settings
      {trailing && <span className="sidebar-item-trail">{trailing}</span>}
    </button>
  );
}

// Whether a group of entries reserves the fixed icon column: true if any real
// entry in it carries an icon. Same rule (and same reason) as ContextMenu's
// `showIcon` — a flyout of pure-text rows sits flush rather than behind a 20px
// gutter nothing is ever drawn in.
function groupHasIcon(entries: (PrefsMenuEntry | "separator")[]): boolean {
  return entries.some((e) => e !== "separator" && e.icon != null);
}

// Where the sidebar row points: the page's DEFAULT tab by name, not the bare
// prefix. Both work — App.tsx redirects the bare one — but a nav link that is
// rewritten the moment it lands puts a URL in the address bar that the user
// never clicked, and leaves the row's href disagreeing with where it went. An
// empty search, deliberately: this is an entry point, not a tab switch, so
// there is nothing to carry (see `tabHref`).
const AI_MODELS_HOME = tabHref("playground", "");

export default function GlobalSidebar({ config }: { config: Config }) {
  // Re-render on any nav/url change (active-item highlight).
  useUrlVersion();
  // Up/Down step through the Projects + Bookmarks rows (sidebarArrowNav.ts).
  useSidebarArrowNav();

  // No builtin-mount gate any more: the entries they guarded (Inbox, App
  // Basics) are gone from the sidebar — the learn content ships as a community
  // app now. The sessions route and its mount are untouched.
  const claudeConfigAvailable = useClaudeConfigAvailable();

  // A model resident in memory is the one piece of app state that costs
  // something while you are not looking at it — surfaced as a dot on the
  // AI Models row itself now that it is primary nav.
  const aiRuntime = useAiRuntime();
  const residentModels = aiRuntime.loaded.filter((m) => m.state === "ready");
  // `.sidebar-rail-dot`, the SAME dot the Tasks row wears, since 2026-08-24
  // (Akshil: "the dots in left sidebar are not consistent, make dot on ai models
  // page similar to one we have in tasks page"). It wore
  // `.account-signedin-dot` before — a class from account.css that happens to be
  // 7px too, and that is where the resemblance stopped. Two differences, both
  // visible on the collapsed rail: that class positions at `top: -2px; right:
  // -3px`, which against the 28px rail BUTTON lands outside its corner instead of
  // on the glyph, so the mark floated off to the right while the Tasks dot hugged
  // its icon; and it is border-box against the other's `content-box`, so its 1px
  // ring ate the dot down to 5px of fill beside a 7px neighbour. One dot
  // vocabulary in this sidebar, one class that draws it.
  const residentDot = residentModels.length ? (
    <span
      className="sidebar-rail-dot is-resident"
      title={
        `In memory: ${residentModels.map((m) => m.model).join(", ")}` +
        (aiRuntime.totalResidentBytes ? ` — ${formatSize(aiRuntime.totalResidentBytes)}` : "")
      }
    />
  ) : undefined;

  // Only the Home page itself lights the row. Viewing a file
  // (/explorer/view|embed/...) is "being somewhere", not "being on Home" —
  // highlighting both the row and the thing you opened read as two selections.
  const pathname = location.pathname;
  const homeActive = pathname === "/home";
  const tasksActive = pathname === "/tasks";
  // Exact, for the reason Home is: /canvases/<name> is a workspace you opened,
  // not the list page, and lighting the row while you are inside a canvas reads
  // as two selections.
  const canvasesActive = pathname === "/canvases";
  // PREFIX, unlike Canvases above: /ai-models/<tab> is the same page seen
  // through a different tab, not a second destination, so every one of the five
  // lights the row. (The bare prefix is redirected to the default tab before
  // this runs, so it is matched for completeness rather than in practice.)
  const aiModelsActive = isAiModelsPath(pathname);
  // PRIMARY NAV ONLY ONCE THERE IS AN ACCOUNT BEHIND IT. Signed out, the row
  // would lead to a sign-in wall — the menu entry is the right weight for
  // "there is a thing here you could set up"; a top-of-sidebar row is for a
  // place you already work. Signed in it is one of the two or three things this
  // machine is FOR, so it sits with Home and Tasks (see @apps/canvases/logged-in
  // for why this is a shared store and not a one-shot probe).
  const canvasesLoggedIn = useCanvasesLoggedIn();
  // AND THE FEATURE HAS TO BE ON AT ALL (D427, default off). Two conditions,
  // deliberately not one store: this one is "does this machine offer Canvases",
  // the one above is "is there an account behind it". The MENU entry needs only
  // this — it has always been ungated on login, since the page explains a
  // signed-out state itself, and gating it on the account would delete the only
  // affordance for reaching a feature you have not set up yet. The primary row
  // needs both.
  const canvasesEnabled = useCanvasesFeature();
  const canvasesInNav = canvasesEnabled && canvasesLoggedIn;

  // WHAT THE TASKS ENTRY KNOWS: what is running, and what finished with
  // something unread (shell/tasksPulse — one poll shared with the page, which
  // publishes into it). Two facts rather than a badge count, because they are two
  // different sentences: yellow is "the machine is working for you", green is
  // "go and look". Yellow WINS whenever both are true — a reader being told work
  // is still in flight does not also need sending to the page mid-run, and the
  // trip is still waiting when it settles.
  const pulse = useTasksPulse();
  // LANDING ON THE PAGE IS THE DISMISSAL — OF THE DOT, and only the dot. Not a
  // button and not a per-row read: "an in-progress task completed" is news
  // exactly until the reader has been where it is shown. Repeated on every pulse
  // while the entry is active so the mark stays gone while the page is open; it
  // comes back only for a completion stamped after the visit
  // (tasks-lib.seenAfterVisit), which is why the dismissal is a stamp per task
  // and not a flag.
  //
  // It fires on the FIRST render here too, before any answer has landed, and the
  // store is what makes that harmless: markTasksSeen is a no-op until a real
  // fetch has come back, because stamping "everything on screen" over an empty
  // store would write an empty map and throw away every dismissal the reader
  // had (bugbot, 2026-08-18).
  useEffect(() => {
    if (tasksActive) markTasksSeen();
  }, [tasksActive, pulse]);
  // THE DOT AND THE CHIP COUNT DIFFERENT THINGS, on purpose (Akshil,
  // 2026-08-18). `unseen` is dismissal-gated and is the dot: an interruption
  // that has done its job once the reader has been where it points — and it is
  // suppressed outright while that page IS the one on screen, since the stamp
  // needs a poll to land and a dot flashing beside the open page is noise. The
  // CHIP reads `doneUnread`, the raw state, and no visit touches it: "three
  // finished things are waiting" stays true until they are read, and a number
  // that cleared itself for being glanced at would be a number nobody could
  // trust. See tasks-lib.isDoneUnread / isUnseenCompletion.
  const unseen = tasksActive ? 0 : pulse.unseen;
  const tasksTip = pulseTitle(pulse);

  // ONE DOT ON THE ICON, IN BOTH MODES (Akshil, 2026-08-18): yellow while
  // anything runs, green for completions not yet shown, nothing at all
  // otherwise. It began as the collapsed rail's whole signal — no label there to
  // hang a word on — and the expanded row deliberately went without it. That was
  // wrong in use: the icon is where the eye lands whatever the sidebar's width,
  // so a mark that shows collapsed and vanishes on expand reads as the STATE
  // going away rather than the sidebar changing shape. The dot is now the
  // constant, and expanding ADDS words beside it ("N running" + the count chip)
  // instead of trading the dot for them.
  //
  // Still ONE dot: yellow outranks green — a reader told work is in flight does
  // not also need sending to the page mid-run. The hues are the status ring's
  // own (--status-progress / --status-done, schedule.css) — one status, one
  // colour, on every surface that names it (design-principles §1).
  const tasksDot =
    pulse.running > 0 ? (
      <span className="sidebar-rail-dot is-running" title={tasksTip} />
    ) : unseen > 0 ? (
      <span className="sidebar-rail-dot is-unread" title={tasksTip} />
    ) : undefined;

  // Expanded, the same two facts ALSO get words, beside the dot rather than
  // instead of it: a shimmering "N running" (the ink moves while the work does,
  // and prefers-reduced-motion pins it), and the count chip the bookmark folders
  // wear (`.sidebar-count-chip`, sidebar.css) — the same element for the same
  // kind of fact, not a lookalike. The dot says THAT there is something; the
  // words say how much, and the chip's number is the ungated one (see above).
  const tasksTrailing =
    pulse.running > 0 || pulse.doneUnread > 0 ? (
      <>
        {pulse.running > 0 && (
          <span className="sidebar-running" title={tasksTip}>
            {runningLabel(pulse.running)}
          </span>
        )}
        {pulse.doneUnread > 0 && (
          <Badge variant="secondary" className="sidebar-count-chip" title={tasksTip}>
            {pulse.doneUnread}
          </Badge>
        )}
      </>
    ) : undefined;

  // Everything that is not primary nav lives in the bottom menu for now:
  // the former sidebar entries (Config), then the settings
  // pages. Same gates as before — an entry a machine can't use stays hidden.
  const menuEntries: (PrefsMenuEntry | "separator")[] = [
    ...(claudeConfigAvailable
      ? [{ href: "/claude-config", label: "Claude Config", icon: CLAUDE_CONFIG_ICON }]
      : []),
  ];
  if (menuEntries.length > 0) menuEntries.push("separator");
  menuEntries.push(
    { href: "/templates", label: "Templates", icon: TEMPLATES_ICON },
    { href: "/mounts", label: "Mounts", icon: MOUNTS_ICON },
    // No /tasks entry here on purpose: Tasks is primary nav now (see the
    // rail below). Listing the same route in the menu too would light the Tasks
    // row and the Preferences trigger at once, since `prefsActive` treats every
    // menu href as "you are on one of my pages" — the same double-selection the
    // Home comment above rejects.
    // Gated on the FEATURE only (D427), not on the account: the page explains a
    // CLI-missing / signed-out state itself, so the entry is what "there is a
    // thing here you could set up" looks like — and the whole point of the
    // preference is that a machine which has not turned Canvases on is not
    // shown it anywhere. It stays here even while the primary row above is
    // showing — the menu is where someone looks for a named destination — and
    // `prefsActive` below drops it instead, so the two never light at once.
    ...(canvasesEnabled
      ? [{ href: "/canvases", label: "Canvases", icon: CANVASES_ICON }]
      : []),
    // No /ai-models entry either, and unlike Canvases it is dropped outright:
    // its primary row is ungated, so a menu copy would only ever be the
    // double-selection the Tasks note rejects.
    { href: "/preferences", label: "Preferences", icon: PREFERENCES_ICON }
  );
  // The tour replays, at the menu's tail, behind ONE row: four sibling entries
  // each prefixed "Tour: " repeated the category name in every line and made a
  // short settings menu twice as long as its actual destinations. Nested, the
  // menu reads as its pages plus one help affordance, and the four titles are
  // just titles.
  //
  // Picking one runs it against the DOM on screen NOW — steps whose targets
  // aren't there drop out (presentSteps), seen keys are ignored: this is the
  // deliberate ask. From a route the tour is not about, startTour goes to its
  // `startPath` first and waits for the page's chrome.
  menuEntries.push("separator");
  menuEntries.push({
    href: "/preferences#tours",
    // "Help", not "Tours": what a stuck reader scans a settings menu for is
    // help — the walkthroughs are what the row holds, not what it is named.
    label: "Help",
    icon: TOURS_ICON,
    submenu: TOURS.map((tour) => ({
      href: `/preferences#tour-${tour.id}`,
      label: tour.title,
      // Next frame, not now: driver.js measures its highlight the moment it is
      // told to drive, and closing the menu only queues the unmount — it is
      // still over the sidebar rows some tours point at until React has painted
      // without it.
      onPick: () => requestAnimationFrame(() => startTour(tour)),
    })),
  });

  // The trigger (and its rail icon) is the only sidebar chrome that can show
  // "you are on one of the menu's pages" — highlight it on any of them.
  // ...except a page primary nav is ALSO showing: the Tasks note above rejects
  // lighting a row and the Preferences trigger over one destination, and
  // Canvases is listed in both places whenever it is in the primary nav. With
  // the feature off it is in NEITHER, so a deep link to /canvases lights
  // nothing here — the entry is not in the list to be matched.
  const prefsActive = menuEntries.some(
    (e) => e !== "separator" && e.href === pathname && !(canvasesInNav && e.href === "/canvases")
  );

  // Owned here, not inside either trigger, because the menu must stay mounted
  // whichever trigger (expanded row vs. collapsed rail icon) opened it — the
  // two live in subtrees SidebarFrame never renders together. The menu is a
  // base-ui DropdownMenu anchored to that element; base-ui owns dismissal
  // (outside press, Escape, focus loss).
  const [prefsAnchor, setPrefsAnchor] = useState<HTMLElement | null>(null);
  const togglePrefsMenu = (el: HTMLElement) => {
    setPrefsAnchor((cur) => (cur ? null : el));
  };
  const closePrefs = () => setPrefsAnchor(null);
  // Picking anything — top level or inside the flyout — closes the WHOLE menu.
  const pick = (entry: PrefsMenuEntry) => {
    closePrefs();
    if (entry.onPick) entry.onPick();
    else navigateUrl(entry.href);
  };
  const showPrefsIcon = groupHasIcon(menuEntries);

  const rail: SidebarRailItem[] = [
    { key: "home", label: "Home", icon: HOME_ICON, href: "/home", active: homeActive },
    {
      key: "tasks",
      label: tasksTip ? `Tasks — ${tasksTip}` : "Tasks",
      icon: SCHEDULED_ICON,
      href: "/tasks",
      active: tasksActive,
      badge: tasksDot,
    },
    // Same gate and same order as the expanded row below — a row that exists
    // only until you collapse the sidebar is a destination people lose.
    ...(canvasesInNav
      ? [
          {
            key: "canvases",
            label: "Canvases",
            icon: CANVASES_ICON,
            href: "/canvases",
            active: canvasesActive,
          },
        ]
      : []),
    {
      key: "ai-models",
      label: "AI Models",
      icon: AI_MODELS_ICON,
      href: AI_MODELS_HOME,
      active: aiModelsActive,
      badge: residentDot,
    },
    {
      key: "preferences",
      label: "Preferences",
      icon: PREFERENCES_ICON,
      href: "/preferences",
      pinBottom: true,
      active: prefsActive,
      // Same Settings popover as the expanded row, not a straight nav — the
      // collapsed rail otherwise has no way to reach Templates/Mounts/etc.
      onClick: (e) => togglePrefsMenu(e.currentTarget),
    },
  ];

  return (
    <>
      <SidebarFrame title="Render" homeHref="/home" rail={rail}>
        <div className="sidebar-section sidebar-group">
          <NavItem
            href="/home"
            id="home-link"
            label="Home"
            icon={HOME_ICON}
            active={homeActive}
          />
          {/* Tasks took Inbox's place as well as its job: the two pages showed
              the same pile of work from two ends, and the one that survives is
              the one that can say when the work runs. Inbox is deleted. */}
          <NavItem
            href="/tasks"
            id="tasks-link"
            label="Tasks"
            icon={SCHEDULED_ICON}
            active={tasksActive}
            extra={tasksDot}
            trailing={tasksTrailing}
          />
          {canvasesInNav && (
            <NavItem
              href="/canvases"
              id="canvases-link"
              label="Canvases"
              icon={CANVASES_ICON}
              active={canvasesActive}
            />
          )}
          <NavItem
            href={AI_MODELS_HOME}
            id="ai-models-link"
            label="AI Models"
            icon={AI_MODELS_ICON}
            active={aiModelsActive}
            extra={residentDot}
            trailing={
              // Beta while the surface (playground foremost) is still settling —
              // the chip skin is the shared one, the modifier only recolours it.
              <Badge variant="secondary" className="sidebar-beta-chip">Beta</Badge>
            }
          />
        </div>
        {/* Projects (D487, "Current apps" until 2026-08-26): the apps on the
            desk, above the permanent Bookmarks tree. Collapsible; always ends
            in a "+ New app" row. */}
        <CurrentAppsSection />
        <BookmarksSection />
        <div className="sidebar-section sidebar-settings">
          <UpdateBadge />
          {/* The version rides the Settings row's trailing edge rather than the
              brand row it used to sit in. Two reasons it moved: the brand row is
              one click target for Home, so a version glued to the title read as
              part of the app's NAME; and it competed for the line the title
              ellipsises on when the sidebar is dragged narrow. Down here it is
              what it is — a footnote about this install, in the same trailing
              slot the Tasks row states its count in. */}
          <PreferencesTrigger
            open={prefsAnchor !== null}
            active={prefsActive}
            trailing={
              config.version ? (
                <Badge variant="outline" className="font-mono text-[10px]" title={`Fused Render v${config.version}`}>
                  v{config.version}
                </Badge>
              ) : undefined
            }
            onToggle={togglePrefsMenu}
          />
        </div>
      </SidebarFrame>
      <DropdownMenu
        open={prefsAnchor !== null}
        onOpenChange={(open) => {
          if (!open) closePrefs();
        }}
      >
        <DropdownMenuContent anchor={prefsAnchor} side="top" align="start" className="w-auto min-w-52">
          <DropdownMenuGroup>
            {menuEntries.map((entry, i) =>
              entry === "separator" ? (
                <DropdownMenuSeparator key={"sep" + i} />
              ) : entry.submenu ? (
                <DropdownMenuSub key={entry.href}>
                  <DropdownMenuSubTrigger>
                    {showPrefsIcon && (
                      <span className="flex size-4 items-center justify-center" aria-hidden="true">
                        {entry.icon}
                      </span>
                    )}
                    {entry.label}
                    {entry.extra}
                  </DropdownMenuSubTrigger>
                  <DropdownMenuSubContent>
                    {entry.submenu.map((sub) => (
                      <DropdownMenuItem key={sub.href} onClick={() => pick(sub)}>
                        {sub.label}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuSubContent>
                </DropdownMenuSub>
              ) : (
                <DropdownMenuItem
                  key={entry.href}
                  // The row is the choice in force when its page is showing.
                  className={location.pathname === entry.href ? "bg-primary/15" : undefined}
                  // A real destination is a real <a href>: middle-click and
                  // modifier-clicks stay with the browser (open in new tab);
                  // a plain click routes through the SPA (pick → navigateUrl).
                  render={
                    entry.onPick ? undefined : (
                      <a
                        href={entry.href}
                        onClick={(e) => {
                          if (isBrowserHandledClick(e)) {
                            closePrefs();
                            return;
                          }
                          e.preventDefault();
                        }}
                      />
                    )
                  }
                  onClick={() => pick(entry)}
                >
                  {showPrefsIcon && (
                    <span className="flex size-4 items-center justify-center" aria-hidden="true">
                      {entry.icon}
                    </span>
                  )}
                  {entry.label}
                  {entry.extra}
                </DropdownMenuItem>
              ),
            )}
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}

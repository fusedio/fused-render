// THE sidebar — one for the whole app, on every route. Replaces the old pair
// (ShellSidebar app-switcher on shell routes, ExplorerSidebar on fs routes):
// primary nav on top (Home / Tasks / AI Models, plus Canvases once the feature
// is turned on in Preferences AND this machine is signed in to Fused), the
// explorer's Bookmarks below it, and a
// single Settings trigger pinned to the bottom that opens a menu holding
// everything else (Config for now, plus Templates /
// Preferences).
//
// Lives in the shell layer on purpose: it composes both platform chrome
// (SidebarFrame) and explorer-owned sections (Bookmarks), which only
// the shell is allowed to import together (scripts/check-boundaries.mjs).
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Plug, Sparkles, Store } from "lucide-react";
import { ClaudeMark } from "@platform/ui/ClaudeMark";
import { CanvasIcon } from "@platform/ui/NodeTypeIcons";
import { SidebarFrame, NavItem } from "@platform/ui/sidebar/SidebarFrame";
import { setSidebarSectionOpen } from "@platform/ui/sidebar/sidebar-accordion";
import type { SidebarRailItem } from "@platform/ui/sidebar/SidebarFrame";
import { SidebarDoorContext, type SidebarDoorLookup } from "@platform/ui/sidebar/sidebar-doors";
import { botsAvailable, rawUrl, type Config } from "@platform/lib/api";
import { embedUrlForFsPath, navigateUrl, viewUrlForFsPath } from "@platform/lib/router";
import { isBrowserHandledClick } from "@platform/lib/appEntry";
import { TOURS, startTour } from "@platform/lib/tours";
import { ONBOARDING_PATH } from "@shell/onboarding/state";
import { SetupProgressRing, SetupProgressRow, useSetupMeter } from "@shell/onboarding/SetupProgress";
import {
  useBookmarksVersion,
  useSidebarState,
  useUrlVersion,
} from "@platform/lib/hooks";
import {
  armBookmark,
  allBookmarks,
  loadBookmarks,
  pinnedBookmarks,
} from "@platform/lib/bookmarks";
import { AppStar } from "@platform/ui/AppStar";
import { isRasterIconUrl, useThemedIconSrc } from "@platform/lib/app-icon-src";
import { appPageUrl, appPathFromPath, type CurrentApp } from "@shell/current-apps-lib";
import { railExtras } from "@shell/sidebar-rail-lib";
import { displayName } from "@platform/lib/flavor";
import { useTaskPeekEnabled } from "@shell/task-peek-flag";
import { useCanvasesFeature } from "@apps/canvases/feature-flag";
import { useAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { isAiModelsPath, tabHref } from "@apps/ai_models/routes";
import { markTasksSeen, useTasksPulse } from "@shell/tasksPulse";
import { useBotsPulse } from "@platform/lib/botsPulse";
import { marketplacePulseTitle, useMarketplacePulse } from "@platform/lib/marketplacePulse";
import { formatSize } from "@platform/lib/format";
import BookmarksSection from "@apps/explorer/sidebar/BookmarksSection";
import CurrentAppsSection, {
  NewAppModal,
  canvasRowHref,
  useDeskAppsOrdered,
} from "@shell/CurrentAppsSection";
import { canvasNameFromPath } from "@apps/canvases/api";
import { CanvasGlyph } from "@apps/canvases/glyph";
import UpdateCard, { updateCardActive } from "@shell/UpdateCard";
import { useRestartFlow } from "@platform/lib/restart-store";
import { useUpdateStatus } from "@platform/lib/update-status";
import { useSidebarArrowNav } from "@shell/sidebarArrowNav";
import { useLeftRailPeek } from "@shell/left-rail/left-rail-peek-flag";
import {
  leftDoorFor,
  publishLeftItems,
  setLeftPeekBlocked,
  setLeftRail,
  useLeftPeek,
  type LeftPeekItem,
} from "@shell/left-rail/left-peek-store";
import { appShow, bookmarkFrameSrc, canvasShow, withQueryEmbed } from "@shell/left-rail/left-peek-lib";

// House — the Home page (/home): search hero + the three recency strips.
const HOME_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M3 10.5 12 3l9 7.5" />
    <path d="M5 9.5V21h14V9.5" />
    <path d="M10 21v-6h4v6" />
  </svg>
);

// The Bots and Tasks glyphs live in shell/nav-icons.tsx (the Claude rail
// wears them too).

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

// The Claude mark, as Preferences' Claude Code group draws it (scale 0.85: the
// mark fills its 512 box edge to edge, lucide's icons carry ~2px of padding):
// the Claude Code page, where plugins, MCP servers and memory are edited.
const CLAUDE_CODE_ICON = (
  <ClaudeMark className="scale-[0.85]" style={{ width: 16, height: 16 }} />
);

// Skills have their own page: what the coding CLIs on this machine read.
const SKILLS_ICON = <Sparkles size={16} strokeWidth={2} aria-hidden="true" />;

// A shop front (lucide Store) for the Marketplace: where community apps are
// browsed and installed, sized like the lucide Tasks glyph above.
// The plug Preferences draws beside its Integrations page.
const INTEGRATIONS_ICON = <Plug size={16} strokeWidth={2} aria-hidden="true" />;
const MARKETPLACE_ICON = <Store size={16} strokeWidth={1.9} aria-hidden="true" />;

// Connected nodes: a canvas is a graph of UDFs.
const CANVASES_ICON = <CanvasIcon size={16} />;

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
  /** Drawn but inert: an entry that has nothing to do right now takes no
      press (no current caller sets it; the update row that did is gone). */
  disabled?: boolean;
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

// Close-on-outside-pointerdown / Escape / window blur, shared by the two
// bottom-edge popovers. `triggerRef`'s element is NOT outside: a click there is
// the trigger's own toggle-closed and must be left to that handler, or
// pointerdown closes the menu here first and the paired click — which re-checks
// "is it open" after React 18 has batched the setState — reopens it right back.
function useDismiss(
  rootRef: React.RefObject<HTMLElement | null>,
  triggerRef: React.RefObject<HTMLElement | null>,
  onClose: () => void,
) {
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      const target = e.target as Node;
      if (rootRef.current?.contains(target)) return;
      if (triggerRef.current?.contains(target)) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("blur", onClose);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", onClose);
    };
  }, [onClose]);
}

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

// One menu row, used for both the popover's own entries and a flyout's. The
// markup is deliberately the shared .context-menu-item / -icon / -label / -arrow
// vocabulary the explorer's right-click menu uses (ContextMenu.tsx), so the two
// surfaces stay one thing to restyle rather than two.
function PrefsRow({
  entry,
  open,
  showIcon,
  onActivate,
  onClose,
}: {
  entry: PrefsMenuEntry;
  /** This row's flyout is showing — the same `open` tint a submenu parent gets
      in the context menu. */
  open: boolean;
  showIcon: boolean;
  onActivate: () => void;
  /** Closes the whole popover — passed straight through so a real link (below)
      can close it even on a gesture the browser owns, one that never reaches
      `onActivate` because navigation itself is left to the anchor. */
  onClose: () => void;
}) {
  const hasSub = !!entry.submenu;
  // A row with neither a flyout nor an in-place action actually GOES
  // somewhere — that's the only shape a real `<a href>` makes sense for.
  // A flyout parent has no page, and a tour's onPick replays in place, so
  // both stay plain <div>s with nothing for the browser to open elsewhere.
  const isLink = !hasSub && !entry.onPick;
  const className =
    "context-menu-item" +
    (hasSub ? " has-submenu" : "") +
    (open ? " open" : "") +
    (entry.disabled ? " disabled" : "") +
    // A flyout parent is never "the page you are on": it has no page.
    (!hasSub && !entry.onPick && location.pathname === entry.href ? " active" : "");
  const content = (
    <>
      {showIcon && (
        <span className="context-menu-icon" aria-hidden="true">
          {entry.icon}
        </span>
      )}
      <span className="context-menu-label">{entry.label}</span>
      {entry.extra}
      {hasSub && <span className="context-menu-arrow">›</span>}
    </>
  );
  if (isLink) {
    return (
      <a
        href={entry.href}
        role="menuitem"
        className={className}
        onClick={(e) => {
          // Picking a real destination closes the popover either way. A
          // plain left click also hijacks the navigation into the SPA's own
          // route via onActivate (pick → navigateUrl); anything the browser
          // already owns (middle-click, ctrl/cmd/shift/alt-click) is left
          // alone so "open in new tab" and friends work on the real href
          // (see appEntry.isBrowserHandledClick).
          onClose();
          if (isBrowserHandledClick(e)) return;
          e.preventDefault();
          onActivate();
        }}
        // Middle-click never fires `click` in modern browsers — it fires
        // `auxclick` instead — so the close above would otherwise never run
        // for that gesture. `auxclick` also covers right-click, though, which
        // must reach the native context menu (copy link, open in new tab)
        // undisturbed — button 1 singles out the middle button.
        onAuxClick={(e) => {
          if (e.button === 1) onClose();
        }}
      >
        {content}
      </a>
    );
  }
  return (
    <div
      role="menuitem"
      aria-haspopup={hasSub ? "menu" : undefined}
      aria-expanded={hasSub ? open : undefined}
      aria-disabled={entry.disabled || undefined}
      className={className}
      onClick={entry.disabled ? undefined : onActivate}
    >
      {content}
    </div>
  );
}

// The floating menu itself — a fixed-position panel growing UP from whichever
// trigger opened it (that row/icon sits on the sidebar's bottom edge). Closes
// on outside pointerdown / Escape / blur, or on picking an entry.
function PreferencesPopover({
  pos,
  entries,
  onClose,
  triggerRef,
}: {
  pos: { left: number; bottom: number };
  entries: (PrefsMenuEntry | "separator")[];
  onClose: () => void;
  /** The element that opened this popover — see useDismiss. */
  triggerRef: React.RefObject<HTMLElement | null>;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  useDismiss(rootRef, triggerRef, onClose);
  // Which row's flyout is open, keyed by href rather than index so a menu whose
  // gated entries change under it can't point the flyout at another row. One at
  // a time; null is none.
  const [openSub, setOpenSub] = useState<string | null>(null);

  // Picking anything — top level or inside the flyout — closes the WHOLE menu.
  const pick = (entry: PrefsMenuEntry) => {
    onClose();
    if (entry.onPick) entry.onPick();
    else navigateUrl(entry.href);
  };

  const showIcon = groupHasIcon(entries);

  return (
    <div
      ref={rootRef}
      className="context-menu placed sidebar-prefs-menu"
      role="menu"
      style={{ left: pos.left, bottom: pos.bottom }}
      /* The pointer leaving the menu takes the flyout with it; while it is
         inside, entering any row is what closes the previous one (below). */
      onMouseLeave={() => setOpenSub(null)}
    >
      {entries.map((entry, i) =>
        entry === "separator" ? (
          <div key={"sep" + i} className="context-menu-sep" role="separator" />
        ) : (
          <div
            key={entry.href}
            className="context-menu-row"
            /* Hover opens the flyout and hovering any other row closes it —
               click also toggles it (PrefsRow's onActivate), which is the
               keyboard/touch path onto the same state. */
            onMouseEnter={() => setOpenSub(entry.submenu ? entry.href : null)}
          >
            <PrefsRow
              entry={entry}
              open={openSub === entry.href}
              showIcon={showIcon}
              onActivate={() =>
                entry.submenu
                  ? setOpenSub(openSub === entry.href ? null : entry.href)
                  : pick(entry)
              }
              onClose={onClose}
            />
            {entry.submenu && openSub === entry.href && (
              <div className="context-menu context-submenu placed" role="menu">
                {entry.submenu.map((sub) => (
                  <PrefsRow
                    key={sub.href}
                    entry={sub}
                    open={false}
                    showIcon={groupHasIcon(entry.submenu ?? [])}
                    onActivate={() => pick(sub)}
                    onClose={onClose}
                  />
                ))}
              </div>
            )}
          </div>
        )
      )}
    </div>
  );
}

// A desk app on the collapsed rail: its own icon (CurrentAppRow's rule — the
// author's svg as is, a raster clipped round) or the generic AppStar.
function RailAppIcon({ app }: { app: CurrentApp }) {
  const src = useThemedIconSrc(app.iconUrl);
  return src ? (
    <img
      className={"sidebar-rail-app-icon" + (isRasterIconUrl(app.iconUrl) ? " is-raster" : "")}
      src={src}
      alt=""
      draggable={false}
    />
  ) : (
    <AppStar width={16} height={16} />
  );
}

// Where the sidebar row points: the page's DEFAULT tab by name, not the bare
// prefix. Both work — App.tsx redirects the bare one — but a nav link that is
// rewritten the moment it lands puts a URL in the address bar that the user
// never clicked, and leaves the row's href disagreeing with where it went. An
// empty search, deliberately: this is an entry point, not a tab switch, so
// there is nothing to carry (see `tabHref`).
const AI_MODELS_HOME = tabHref("playground", "");

// The "+" glyph of the New app and Canvas entries, in a card's head.
const NEW_GLYPH = (
  <span className="sidebar-rail-new-glyph" aria-hidden="true">
    +
  </span>
);

/** Publish the left cards' facts after each render (left-peek-store
 *  `publishLeftItems`): with the switch on, collapsed or expanded; with the
 *  switch off, nothing — once — so every card goes. */
function useLeftRailPublish(items: LeftPeekItem[], on: boolean): void {
  const wasOn = useRef(false);
  useLayoutEffect(() => {
    if (on) publishLeftItems(items);
    else if (wasOn.current) publishLeftItems([]);
    wasOn.current = on;
  });
}

/** The expanded sidebar's rows' hooks (platform sidebar-doors): a row's key →
 *  its card's door, as a row (left-peek-store `leftDoorFor`). A new function
 *  each render, so the rows re-read the cards' state with the sidebar. */
const rowDoorLookup = (): SidebarDoorLookup => (key) => leftDoorFor(key, "row");

export default function GlobalSidebar({ config }: { config: Config }) {
  // See `tuckOnCollapse` below.
  const taskPeekOn = useTaskPeekEnabled();
  // SIDEBAR PREVIEWS (shell/left-rail): read up here, the desk read below
  // follows it.
  const leftPeekOn = useLeftRailPeek();
  // Re-render on any nav/url change (active-item highlight).
  useUrlVersion();
  // Up/Down step through the Projects + Bookmarks rows (sidebarArrowNav.ts).
  useSidebarArrowNav();

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
  // The collapsed rail's stand-in for the expanded footer's UpdateCard.
  const updateStatus = useUpdateStatus();
  const restartFlow = useRestartFlow();
  const updateDot = updateCardActive(updateStatus, restartFlow.stage) ? (
    <span className="sidebar-rail-dot is-update" title="Update available. Open the sidebar to act on it" />
  ) : undefined;

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
  // PREFIX, unlike Canvases above: /ai-models/<tab> is the same page seen
  // through a different tab, not a second destination, so every one of the five
  // lights the row. (The bare prefix is redirected to the default tab before
  // this runs, so it is matched for completeness rather than in practice.)
  const aiModelsActive = isAiModelsPath(pathname);
  // THE FEATURE HAS TO BE ON (D427, default off): "does this machine offer
  // Canvases". That is the WHOLE gate now (owner, 2026-10-22). It used to be a
  // conjunction with "is there an account behind it" (D358), which on a fresh
  // machine meant flipping the preference showed nothing at all until a sign-in
  // had happened somewhere — the section the reader had just enabled was
  // invisible, and the only door to setting it up was the settings menu. Now
  // the Canvases section appears with the switch; its "+ Canvas" row opens the
  // catalog, which explains a CLI-missing / signed-out state itself and is
  // where sign-in happens. The menu entry below takes the same single gate.
  const canvasesEnabled = useCanvasesFeature();
  const canvasesInNav = canvasesEnabled;
  // The Community (apps/marketplace): a row after AI Models. Always on since
  // 2026-10-29 (it was behind the
  // `marketplace_enabled` switch while in preview; Beta tag dropped 2026-11-08).
  // PREFIX match: /marketplace/app/<slug> and /marketplace/installed are the
  // same store.
  const marketplaceActive = pathname === "/marketplace" || pathname.startsWith("/marketplace/");
  // THE COMMUNITY ROW SAYS WHAT THE INSTALLED PAGE'S "UPDATES" SECTION SAYS
  // (owner, 2026-11-03: "notification dot in the community similar to bots
  // and tasks"): a dot while an installed app has a reviewed release waiting,
  // and a count chip when expanded. Read from
  // platform/lib/marketplacePulse — the store's own catalog reads feed it, so
  // an Update that lands clears it on the same read. Accent, the hue the
  // Settings row's "update available" dot already wears: one state, one
  // colour. Nothing is dismissed by a visit (see the store): the dot holds
  // until the update is applied. WITH A DOT, THE ROW LANDS ON
  // /marketplace/installed — the page the dot is about — instead of Discover.
  const marketplacePulse = useMarketplacePulse();
  const marketplaceTip = marketplacePulseTitle(marketplacePulse);
  const marketplaceHref = marketplacePulse.updates > 0 ? "/marketplace/installed" : "/marketplace";
  const marketplaceDot =
    marketplacePulse.updates > 0 ? <span className="sidebar-rail-dot is-update" title={marketplaceTip} /> : undefined;
  // THE FRONT DOOR: Home. Same answer as App.tsx's "/" redirect, so clicking
  // the title lands where "/" lands. Bots is a row of its own, right under
  // Home (owner's call 2026-10-13: Home → Bots → Tasks → AI Models).
  const frontDoor = "/home";

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
  // THE TASKS AND BOTS DOORS ARE THE CLAUDE RAIL'S (owner, 2026-11-07): its
  // Tasks door and + at the top, its Bots door at the foot (ClaudeRail). This
  // column has no Tasks or Bots row at either width. The pulses still run
  // (the Tasks visit stamp above; the rail's dots read the same stores).
  // No poll where the server runs no bots (Windows).
  useBotsPulse(botsAvailable(config));

  // Everything that is not primary nav lives in the bottom menu for now: the
  // settings pages. Same gates as before — an entry a machine can't use stays
  // hidden.
  const menuEntries: (PrefsMenuEntry | "separator")[] = [];
  menuEntries.push({ href: "/templates", label: "Templates", icon: TEMPLATES_ICON });
  menuEntries.push(
    // No /tasks entry here on purpose: Tasks is primary nav now (see the
    // rail below). Listing the same route in the menu too would light the Tasks
    // row and the Preferences trigger at once, since `prefsActive` treats every
    // menu href as "you are on one of my pages" — the same double-selection the
    // Home comment above rejects.
    // Gated on the FEATURE only (D427), not on the account: the page explains a
    // CLI-missing / signed-out state itself, so the entry is what "there is a
    // thing here you could set up" looks like — and the whole point of the
    // preference is that a machine which has not turned Canvases on is not
    // shown it anywhere. The Canvases SECTION below takes the same gate since
    // 2026-10-22; this entry stays because the menu is where someone looks
    // for a named destination.
    ...(canvasesEnabled
      ? [{ href: "/canvases", label: "Canvases", icon: CANVASES_ICON }]
      : []),
    // No /ai-models entry either, and unlike Canvases it is dropped outright:
    // its primary row is ungated, so a menu copy would only ever be the
    // double-selection the Tasks note rejects.
    // Skills and Claude Code are settings-weight pages, so they live here and
    // not in the primary nav; listing them in both would light the row and
    // the trigger at once, as the Tasks note above says.
    { href: "/skills", label: "Skills", icon: SKILLS_ICON },
    { href: "/claude-config", label: "Claude Code", icon: CLAUDE_CODE_ICON },
    // A quick way to one Preferences page (owner, 2026-11-27): Fused
    // Workbench and the connectors are what people open Preferences for most.
    // Its href carries a query, so it never matches `pathname` below: on that
    // page the trigger is lit by the Preferences entry.
    { href: "/preferences?tab=integrations", label: "Integrations", icon: INTEGRATIONS_ICON },
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
    submenu: [
      // The first-run wizard, on demand — its own page (shell/onboarding).
      // Reopening touches neither flag until the user finishes or closes it.
      { href: ONBOARDING_PATH, label: "Setup wizard" },
      ...TOURS.map((tour) => ({
        href: `/preferences#tour-${tour.id}`,
        label: tour.title,
        // Next frame, not now: driver.js measures its highlight the moment it is
        // told to drive, and closing the menu only queues the unmount — it is
        // still over the sidebar rows some tours point at until React has painted
        // without it.
        onPick: () => requestAnimationFrame(() => startTour(tour)),
      })),
    ],
  });

  // The trigger (and its rail icon) is the only sidebar chrome that can show
  // "you are on one of the menu's pages" — highlight it on any of them.
  // ...except a page primary nav is ALSO showing: the Tasks note above rejects
  // lighting a row and the Preferences trigger over one destination, and
  // Canvases is listed in both places whenever it is in the primary nav. With
  // the feature off it is in NEITHER, so a deep link to /canvases lights
  // nothing here — the entry is not in the list to be matched.
  const prefsActive = menuEntries.some(
    (e) => e !== "separator" && e.href === pathname
  );

  // Owned here, not inside either trigger, because the popover must stay
  // mounted whichever trigger (expanded row vs. collapsed rail icon) opened
  // it — the two live in subtrees SidebarFrame never renders together.
  const [prefsPos, setPrefsPos] = useState<{ left: number; bottom: number } | null>(null);
  // Which trigger opened it — the popover's outside-click check must not
  // treat a re-click on this element as "outside" (see PreferencesPopover).
  const prefsTriggerRef = useRef<HTMLElement | null>(null);
  const togglePrefsMenu = (el: HTMLElement) => {
    if (prefsPos) {
      setPrefsPos(null);
      return;
    }
    prefsTriggerRef.current = el;
    const r = el.getBoundingClientRect();
    // Grows upward: pinned by its bottom edge just above the trigger.
    setPrefsPos({ left: r.left, bottom: window.innerHeight - r.top + 4 });
  };

  // THE SETUP METER (shell/onboarding/progress): how far first-run setup has
  // got, as a row above Settings and a ring on the rail, both leading back into
  // the wizard. Null = nothing to show (never started, or finished).
  const setupMeter = useSetupMeter(config);

  // THE RAIL'S DOORS INTO THE SECTIONS: the desk's apps, its canvases and
  // the pinned bookmarks, in their sections' order, capped (sidebar-rail-lib).
  // The desk fetch runs only while the rail is on screen — expanded, the
  // sections own it. The canvases group wears the Canvases section's gate
  // (the feature on, nothing more).
  const { collapsed: sidebarCollapsed } = useSidebarState();
  useBookmarksVersion();
  // …and while Left sidebar preview is on, in both layouts: every project and
  // canvas has its card, collapsed or expanded, so a held one survives the
  // switch.
  const desk = useDeskAppsOrdered(sidebarCollapsed || leftPeekOn);
  const extras = railExtras({
    desk,
    pins: pinnedBookmarks(loadBookmarks()),
    canvases: canvasesInNav,
  });
  const onAppPath = appPathFromPath(pathname);
  const here = pathname + location.search;
  // A rail door opens ITS section for the next expand (owner, 2026-11-09:
  // "my last clicked item in the collapsed sidebar should be remembered and
  // open that section"): the accordion's own write (sidebar-accordion), so
  // expanding shows the section the reader last went into from the rail.
  const railGo = (section: "projects" | "canvases" | "favourites", href: string): Promise<boolean> => {
    setSidebarSectionOpen(section, true);
    return navigateUrl(href);
  };
  // The Projects section's "+ New app" row, on the rail: same composer, same
  // place (the head of the apps group).
  const [railComposing, setRailComposing] = useState(false);
  const railNewApp: SidebarRailItem[] = [
        {
          key: "new-app",
          label: "New app",
          icon: (
            <span className="sidebar-rail-new-glyph" aria-hidden="true">
              +
            </span>
          ),
          href: "/apps",
          active: false,
          dividerBefore: true,
          scrolls: true,
          onClick: () => {
            setSidebarSectionOpen("projects", true);
            setRailComposing(true);
          },
        },
      ];
  const railApps: SidebarRailItem[] = extras.apps.map((app) => ({
    key: "app:" + app.path,
    label: app.name,
    icon: <RailAppIcon app={app} />,
    href: appPageUrl(app.path),
    active: onAppPath === app.path,
    scrolls: true,
    onClick: () => void railGo("projects", appPageUrl(app.path)),
  }));
  // A canvas door lights on its editor, the section's own rule; the href is
  // the row's (canvasRowHref), with the row's fallback for a clone of an
  // unknown environment.
  const onCanvas = canvasNameFromPath(pathname);
  // The Canvases section's "+ Canvas" row, on the rail: the catalog
  // (/canvases), at the head of the canvases group, behind the section's gate
  // — the one door in, since the primary nav has no Canvases row. Plain
  // navigation, not a composer: create and clone live on the catalog page.
  const railNewCanvas: SidebarRailItem[] = canvasesInNav
    ? [
        {
          key: "new-canvas",
          label: "Canvas — open or create",
          icon: (
            <span className="sidebar-rail-new-glyph" aria-hidden="true">
              +
            </span>
          ),
          href: "/canvases",
          active: pathname === "/canvases",
          dividerBefore: true,
          scrolls: true,
          onClick: () => void railGo("canvases", "/canvases"),
        },
      ]
    : [];
  const railCanvases: SidebarRailItem[] = extras.canvases.map((app) => ({
    key: "canvas:" + app.path,
    label: app.name,
    icon: (
      <span className="sidebar-rail-canvas-glyph" aria-hidden="true">
        <CanvasGlyph size={16} />
      </span>
    ),
    href: canvasRowHref(app) ?? appPageUrl(app.path),
    onClick: () => void railGo("canvases", canvasRowHref(app) ?? appPageUrl(app.path)),
    active:
      !!app.canvas &&
      !!onCanvas &&
      onCanvas.env === app.canvas.env &&
      onCanvas.name === app.canvas.name,
    scrolls: true,
  }));
  const railPins: SidebarRailItem[] = extras.pins.map((b, i) => ({
    key: "pin:" + b.id,
    label: b.name,
    icon: (
      <span className={"sidebar-rail-bm-glyph" + (b.icon ? " custom-icon" : "")}>
        {b.icon ?? "★"}
      </span>
    ),
    href: b.url,
    active: b.url === here,
    dividerBefore: i === 0,
    // Collapsed rail only: bottom-aligned just above Settings (owner,
    // 2026-11-09); the expanded Favourites stays under Canvases.
    pinBottom: i === 0,
    scrolls: true,
    // Arm it, as the section's own click does (Update-bookmark tracking).
    onClick: () => {
      armBookmark(b.id, b.url);
      void railGo("favourites", b.url);
    },
  }));

  const railItems: SidebarRailItem[] = [
    // Same gate and same order as the expanded rows below.
    { key: "home", label: "Home", icon: HOME_ICON, href: "/home", active: homeActive },
    // No Canvases entry (owner, 2026-10-20): the Canvases SECTION below lists
    // the canvases, and its "+ Canvas" row is the door to the catalog.
    {
      key: "ai-models",
      label: "AI Models",
      icon: AI_MODELS_ICON,
      href: AI_MODELS_HOME,
      active: aiModelsActive,
      badge: residentDot,
    },
    // Same order as the expanded row below.
    {
      key: "marketplace",
      label: marketplaceTip ? `Community — ${marketplaceTip}` : "Community",
      icon: MARKETPLACE_ICON,
      href: marketplaceHref,
      active: marketplaceActive,
      badge: marketplaceDot,
    },
    ...railNewApp,
    ...railApps,
    ...railNewCanvas,
    ...railCanvases,
    ...railPins,
    // Same gate and same place as the expanded row: the rail is the whole
    // sidebar when collapsed, and a meter that vanished on collapse would read
    // as setup being done.
    ...(setupMeter
      ? [
          {
            key: "setup",
            label: setupMeter.title,
            icon: <SetupProgressRing percent={setupMeter.percent} />,
            href: ONBOARDING_PATH,
            // The favourites already pushed the bottom cluster down; under
            // them the expanded footer's hairline stands in for the push.
            pinBottom: !railPins.length,
            dividerBefore: railPins.length > 0,
          },
        ]
      : []),
    {
      key: "preferences",
      label: "Preferences",
      icon: PREFERENCES_ICON,
      href: "/preferences",
      pinBottom: !setupMeter && !railPins.length,
      dividerBefore: !setupMeter && railPins.length > 0,
      active: prefsActive,
      // Same Settings popover as the expanded row, not a straight nav — the
      // collapsed rail otherwise has no way to reach Templates/etc.
      onClick: (e) => togglePrefsMenu(e.currentTarget),
      // The expanded footer carries the UpdateCard; the collapsed rail has no
      // room for it, so the same moments show as a dot.
      badge: updateDot,
    },
  ];

  // SIDEBAR PREVIEWS (shell/left-rail; `left_rail_peek_enabled`, default
  // off): every entry below gets a hover card — the collapsed rail's icons
  // and the expanded sidebar's rows alike, one card per key — what it shows,
  // and its own click for the card's Open. Off, nothing is published and
  // every entry is exactly what it was. The setup ring and Settings have none
  // (their tooltip stays).
  const leftItems: LeftPeekItem[] = [];
  if (leftPeekOn) {
    const goTo = (href: string) => () => navigateUrl(href);
    // THE HOME CARD (owner, 2026-11-27): the home folder's listing, a file
    // search in its head (typing swaps the listing for the results), and its
    // Open is the file explorer at ~ — the row's own click stays /home.
    leftItems.push({
      key: "home",
      title: "Home",
      icon: HOME_ICON,
      show: { kind: "frame", src: embedUrlForFsPath(config.home, "?_side=off"), searchHome: config.home },
      onOpen: goTo(viewUrlForFsPath(config.home)),
    });
    leftItems.push({ key: "ai-models", title: "AI Models", icon: AI_MODELS_ICON, show: { kind: "frame", src: withQueryEmbed(AI_MODELS_HOME) }, onOpen: goTo(AI_MODELS_HOME) });
    leftItems.push({ key: "marketplace", title: "Community", icon: MARKETPLACE_ICON, show: { kind: "frame", src: withQueryEmbed(marketplaceHref) }, onOpen: goTo(marketplaceHref) });
    // The "+" rows: the page each one leads to — their own click is unchanged.
    leftItems.push({
      key: "new-app",
      title: "New app",
      icon: NEW_GLYPH,
      show: { kind: "compose" },
      onOpen: () => {
        setSidebarSectionOpen("projects", true);
        setRailComposing(true);
      },
    });
    if (canvasesInNav)
      leftItems.push({
        key: "new-canvas",
        title: "Canvases",
        icon: NEW_GLYPH,
        show: { kind: "frame", src: withQueryEmbed("/canvases") },
        onOpen: () => railGo("canvases", "/canvases"),
      });
    // Every project and canvas on the desk (the rail draws the first seven;
    // the expanded sections every one), and every favourite — pinned or not.
    for (const app of desk.filter((a) => !a.canvas))
      leftItems.push({
        key: "app:" + app.path,
        title: app.name,
        icon: <RailAppIcon app={app} />,
        show: appShow({ path: app.path, entry: app.entry, rawUrl, embedUrl: embedUrlForFsPath }),
        onOpen: () => railGo("projects", appPageUrl(app.path)),
      });
    for (const app of canvasesInNav ? desk.filter((a) => a.canvas) : []) {
      const href = canvasRowHref(app) ?? appPageUrl(app.path);
      leftItems.push({
        key: "canvas:" + app.path,
        title: app.name,
        icon: <CanvasGlyph size={16} />,
        show: canvasShow({ path: app.path, embedUrl: embedUrlForFsPath }),
        onOpen: () => railGo("canvases", href),
      });
    }
    for (const b of allBookmarks()) {
      const src = bookmarkFrameSrc(b.url);
      if (src)
        leftItems.push({
          key: "pin:" + b.id,
          title: b.name,
          icon: <span className={"sidebar-rail-bm-glyph" + (b.icon ? " custom-icon" : "")}>{b.icon ?? "★"}</span>,
          show: { kind: "frame", src },
          active: b.url === here,
          onOpen: () => {
            armBookmark(b.id, b.url);
            return railGo("favourites", b.url);
          },
        });
    }
  }
  // THE PAGE YOU ARE ON HAS NO CARD (owner, 2026-11-27: "if the page is
  // active it shouldn't popup"): an entry lit as the current page opens
  // nothing on hover — it keeps its active look and its tooltip.
  const railActive = new Map(railItems.map((r) => [r.key, r.active === true] as const));
  for (const it of leftItems) if (railActive.get(it.key)) it.active = true;
  // Published with the switch on; off, the empty list puts every card down.
  useLeftRailPublish(leftItems, leftPeekOn);
  // The Settings menu open: no card over it, and a preview goes.
  useEffect(() => {
    if (leftPeekOn) setLeftPeekBlocked(prefsPos !== null);
  }, [leftPeekOn, prefsPos]);
  // Re-read the cards' state (ready, up) for the icons' attributes.
  useLeftPeek();
  const rail: SidebarRailItem[] = leftPeekOn
    ? railItems.map((item) => {
        const door = leftDoorFor(item.key, "rail");
        return door ? { ...item, door } : item;
      })
    : railItems;

  return (
    <>
      {railComposing && <NewAppModal onClose={() => setRailComposing(false)} />}
      {/* `tuckOnCollapse` is the task side peek's motion (it collapses this
          panel on the reader's behalf), so it rides that feature's flag —
          shell/task-peek-flag.ts. Off, the collapse is the one this sidebar has
          always had. */}
      <SidebarDoorContext.Provider value={leftPeekOn ? rowDoorLookup() : null}>
      <SidebarFrame
        title="Render"
        homeHref={frontDoor}
        rail={rail}
        tuckOnCollapse={taskPeekOn}
        onRailEl={leftPeekOn ? setLeftRail : undefined}
      >
        <div className="sidebar-section sidebar-group">
          <NavItem href="/home" id="home-link" label="Home" icon={HOME_ICON} active={homeActive} doorKey="home" />
          {/* No Canvases row (owner, 2026-10-20): the Canvases section below
              is the listing, its "+ Canvas" row the way to the catalog. */}
          <NavItem
            href={AI_MODELS_HOME}
            id="ai-models-link"
            label="AI Models"
            icon={AI_MODELS_ICON}
            active={aiModelsActive}
            extra={residentDot}
            doorKey="ai-models"
          />
          <NavItem
            href={marketplaceHref}
            id="marketplace-link"
            label="Community"
            icon={MARKETPLACE_ICON}
            active={marketplaceActive}
            extra={marketplaceDot}
            title={marketplaceTip || undefined}
            doorKey="marketplace"
            trailing={
              // The updates count, in the chip the Tasks and Bots rows wear
              // for theirs.
              marketplacePulse.updates > 0 ? (
                <span className="sidebar-count-chip" title={marketplaceTip}>
                  {marketplacePulse.updates}
                </span>
              ) : undefined
            }
          />
        </div>
        {/* Projects (D487, "Current apps" until 2026-08-26): the apps on the
            desk, above the permanent Bookmarks tree. Collapsible; always ends
            in a "+ New app" row. */}
        <CurrentAppsSection />
        {/* Canvases (2026-10-20): the clones on this machine, under Projects,
            behind the feature flag alone (owner, 2026-10-22) — signed out it
            is the "+ Canvas" door into the catalog, which does the sign-in. */}
        {canvasesInNav && <CurrentAppsSection section="canvases" />}
        <BookmarksSection />
        <div className="sidebar-section sidebar-settings">
          <UpdateCard />
          {/* Setup progress, above Settings: "Setup · 60%", back into the wizard. */}
          {setupMeter && <SetupProgressRow meter={setupMeter} />}
          {/* The version rides the Settings row's trailing edge rather than the
              brand row it used to sit in. Two reasons it moved: the brand row is
              one click target for Home, so a version glued to the title read as
              part of the app's NAME; and it competed for the line the title
              ellipsises on when the sidebar is dragged narrow. Down here it is
              what it is — a footnote about this install, in the same trailing
              slot the Tasks row states its count in. */}
          <PreferencesTrigger
            open={prefsPos !== null}
            active={prefsActive}
            trailing={
              config.version ? (
                <span className="version-chip" title={`${displayName()} v${config.version}`}>
                  v{config.version}
                </span>
              ) : undefined
            }
            onToggle={togglePrefsMenu}
          />
        </div>
      </SidebarFrame>
      </SidebarDoorContext.Provider>
      {prefsPos && (
        <PreferencesPopover
          pos={prefsPos}
          entries={menuEntries}
          onClose={() => setPrefsPos(null)}
          triggerRef={prefsTriggerRef}
        />
      )}
    </>
  );
}

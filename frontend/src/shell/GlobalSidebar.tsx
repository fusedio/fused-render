// THE sidebar — one for the whole app, on every route: primary nav on top
// (Home / Tasks / AI Models, plus Canvases once the feature is on in
// Preferences AND this machine is signed in to Fused), the Projects desk, the
// explorer's Bookmarks below it, and a single Settings trigger pinned to the
// bottom that opens a menu holding everything else (Claude Config, Templates,
// Mounts, Canvases, Preferences, Help).
//
// Lives in the shell layer on purpose: it composes both platform chrome
// (SidebarFrame) and explorer-owned sections (Bookmarks), which only the shell
// is allowed to import together (scripts/check-boundaries.mjs).
import { useEffect } from "react";
import {
  ChevronRight,
  CircleQuestionMark,
  Cloud,
  Cpu,
  Home,
  LayoutGrid,
  ListTodo,
  Settings,
  SlidersHorizontal,
  Waypoints,
} from "lucide-react";
import { NAV_ITEM_CLASS, NavItem, SidebarFrame } from "@platform/ui/sidebar/SidebarFrame";
import type { SidebarRailItem } from "@platform/ui/sidebar/SidebarFrame";
import UpdateBadge from "@platform/ui/UpdateBadge";
import { StatusDot } from "@platform/ui/flow/StatusIcon";
import { Badge } from "@platform/shadcn/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@platform/shadcn/ui/dropdown-menu";
import type { Config } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { isBrowserHandledClick } from "@platform/lib/appEntry";
import { TOURS, startTour } from "@platform/lib/tours";
import { useUrlVersion } from "@platform/lib/hooks";
import { cn } from "@platform/lib/utils";
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

// 16px lucide glyphs, the nav norm. Each names what the page IS: a house for
// Home, a to-do list for Tasks (the app page's Tasks tab wears the same one), a
// processor die for AI Models (what this machine can RUN), connected nodes for
// Canvases (a graph of UDFs), sliders for Claude Config, a gear for Preferences.
const ICON = { size: 16, strokeWidth: 2, "aria-hidden": true } as const;
const HOME_ICON = <Home {...ICON} />;
const CLAUDE_CONFIG_ICON = <SlidersHorizontal {...ICON} />;
const AI_MODELS_ICON = <Cpu {...ICON} />;
const TEMPLATES_ICON = <LayoutGrid {...ICON} />;
const MOUNTS_ICON = <Cloud {...ICON} />;
const SCHEDULED_ICON = <ListTodo {...ICON} />;
const CANVASES_ICON = <Waypoints {...ICON} />;
const PREFERENCES_ICON = <Settings {...ICON} />;
const TOURS_ICON = <CircleQuestionMark {...ICON} />;

interface PrefsMenuEntry {
  /** Where the entry goes — or, for an `onPick` entry, a stable key. */
  href: string;
  label: string;
  icon?: React.ReactNode;
  /** Run this instead of navigating to `href` — the tour entries replay a
      walkthrough in place rather than going anywhere. */
  onPick?: () => void;
  /** A one-level flyout hung off this row (Help → the tours). */
  submenu?: PrefsMenuEntry[];
}

// A NAV DOT: one mark in the icon's top-right corner, worn in BOTH sidebar
// modes — a dot on a nav icon means "there is something behind this", and the
// icon is where the eye lands whatever the sidebar's width. Colour is the
// status map's (status-colors.ts): yellow = in flight, green = done and
// unread / resident. A ring in the sidebar's own colour lifts it off the glyph.
function NavDot({ bucket, title }: { bucket: "yellow" | "green"; title: string }) {
  return (
    <StatusDot
      bucket={bucket}
      label={title}
      className="absolute -top-0.5 -right-1 ring-2 ring-sidebar"
    />
  );
}

// One menu row, used for the popover's own entries and a flyout's. A row with
// neither a flyout nor an in-place action actually GOES somewhere — the only
// shape a real `<a href>` makes sense for (middle-click, copy link).
function PrefsRow({ entry, onPick }: { entry: PrefsMenuEntry; onPick: (e: PrefsMenuEntry) => void }) {
  const active = !entry.submenu && !entry.onPick && location.pathname === entry.href;
  if (entry.submenu) {
    return (
      <DropdownMenuSub>
        <DropdownMenuSubTrigger>
          {entry.icon}
          {entry.label}
          <ChevronRight className="ml-auto size-3.5 text-muted-foreground" />
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent>
          {entry.submenu.map((sub) => (
            <PrefsRow key={sub.href} entry={sub} onPick={onPick} />
          ))}
        </DropdownMenuSubContent>
      </DropdownMenuSub>
    );
  }
  if (entry.onPick) {
    return (
      <DropdownMenuItem onClick={() => onPick(entry)}>
        {entry.icon}
        {entry.label}
      </DropdownMenuItem>
    );
  }
  return (
    <DropdownMenuItem
      render={<a href={entry.href} />}
      className={cn(active && "bg-accent/50")}
      onClick={(e) => {
        // A plain left click hijacks the navigation into the SPA's own route;
        // anything the browser already owns (middle-click, modifier-click) is
        // left alone so "open in new tab" works on the real href.
        if (isBrowserHandledClick(e)) return;
        e.preventDefault();
        onPick(entry);
      }}
    >
      {entry.icon}
      {entry.label}
    </DropdownMenuItem>
  );
}

// Where the sidebar row points: the page's DEFAULT tab by name, not the bare
// prefix, so the row's href agrees with where it went.
const AI_MODELS_HOME = tabHref("playground", "");

export default function GlobalSidebar({ config }: { config: Config }) {
  // Re-render on any nav/url change (active-item highlight).
  useUrlVersion();
  // Up/Down step through the Projects + Bookmarks rows (sidebarArrowNav.ts).
  useSidebarArrowNav();

  const claudeConfigAvailable = useClaudeConfigAvailable();

  // A model resident in memory is the one piece of app state that costs
  // something while you are not looking at it — a dot on the AI Models row.
  const aiRuntime = useAiRuntime();
  const residentModels = aiRuntime.loaded.filter((m) => m.state === "ready");
  const residentDot = residentModels.length ? (
    <NavDot
      bucket="green"
      title={
        `In memory: ${residentModels.map((m) => m.model).join(", ")}` +
        (aiRuntime.totalResidentBytes ? ` — ${formatSize(aiRuntime.totalResidentBytes)}` : "")
      }
    />
  ) : undefined;

  // Only the Home page itself lights the row: viewing a file is "being
  // somewhere", not "being on Home". Canvases is exact for the same reason;
  // AI Models is a PREFIX — every tab is the same page.
  const pathname = location.pathname;
  const homeActive = pathname === "/home";
  const tasksActive = pathname === "/tasks";
  const canvasesActive = pathname === "/canvases";
  const aiModelsActive = isAiModelsPath(pathname);
  // PRIMARY NAV ONLY ONCE THERE IS AN ACCOUNT BEHIND IT AND THE FEATURE IS ON
  // (D427). Two stores, deliberately: "does this machine offer Canvases" and
  // "is there an account behind it". The MENU entry needs only the first.
  const canvasesLoggedIn = useCanvasesLoggedIn();
  const canvasesEnabled = useCanvasesFeature();
  const canvasesInNav = canvasesEnabled && canvasesLoggedIn;

  // WHAT THE TASKS ENTRY KNOWS (shell/tasksPulse — one poll shared with the
  // page): what is running, and what finished unread. Yellow WINS when both.
  const pulse = useTasksPulse();
  // LANDING ON THE PAGE IS THE DISMISSAL — of the dot, and only the dot.
  // Repeated on every pulse while the entry is active; a no-op until a real
  // fetch has landed (markTasksSeen), so an empty store never wipes dismissals.
  useEffect(() => {
    if (tasksActive) markTasksSeen();
  }, [tasksActive, pulse]);
  // THE DOT AND THE CHIP COUNT DIFFERENT THINGS: `unseen` is dismissal-gated
  // (the dot), suppressed while the page is on screen; the CHIP reads
  // `doneUnread`, the raw state, and no visit touches it.
  const unseen = tasksActive ? 0 : pulse.unseen;
  const tasksTip = pulseTitle(pulse);

  const tasksDot =
    pulse.running > 0 ? (
      <NavDot bucket="yellow" title={tasksTip} />
    ) : unseen > 0 ? (
      <NavDot bucket="green" title={tasksTip} />
    ) : undefined;

  // Expanded, the same two facts ALSO get words beside the dot: a shimmering
  // "N running" (the house busy indicator, guarded under reduced motion in the
  // token sheet) and the unread count as a chip.
  const tasksTrailing =
    pulse.running > 0 || pulse.doneUnread > 0 ? (
      <>
        {pulse.running > 0 && (
          <span className="shimmer-text text-xs text-muted-foreground" title={tasksTip}>
            {runningLabel(pulse.running)}
          </span>
        )}
        {pulse.doneUnread > 0 && (
          <Badge variant="secondary" className="h-4 px-1.5 text-xs tabular-nums" title={tasksTip}>
            {pulse.doneUnread}
          </Badge>
        )}
      </>
    ) : undefined;

  // Everything that is not primary nav lives in the bottom menu. Same gates as
  // before — an entry a machine can't use stays hidden. No /tasks or /ai-models
  // entry: those are primary nav, and a menu copy would light two rows at once.
  const menuEntries: (PrefsMenuEntry | "separator")[] = [
    ...(claudeConfigAvailable
      ? [{ href: "/claude-config", label: "Claude Config", icon: CLAUDE_CONFIG_ICON }]
      : []),
  ];
  if (menuEntries.length > 0) menuEntries.push("separator");
  menuEntries.push(
    { href: "/templates", label: "Templates", icon: TEMPLATES_ICON },
    { href: "/mounts", label: "Mounts", icon: MOUNTS_ICON },
    // Gated on the FEATURE only (D427), not on the account: the page explains a
    // signed-out state itself. Stays here while the primary row shows too;
    // `prefsActive` below drops it so the two never light at once.
    ...(canvasesEnabled
      ? [{ href: "/canvases", label: "Canvases", icon: CANVASES_ICON }]
      : []),
    { href: "/preferences", label: "Preferences", icon: PREFERENCES_ICON }
  );
  // The tour replays behind ONE "Help" row: what a stuck reader scans a
  // settings menu for is help. Picking one runs it against the DOM on screen
  // NOW; next frame, so the menu has unmounted before driver.js measures.
  menuEntries.push("separator");
  menuEntries.push({
    href: "/preferences#tours",
    label: "Help",
    icon: TOURS_ICON,
    submenu: TOURS.map((tour) => ({
      href: `/preferences#tour-${tour.id}`,
      label: tour.title,
      onPick: () => requestAnimationFrame(() => startTour(tour)),
    })),
  });

  // The trigger (and its rail icon) is the only sidebar chrome that can show
  // "you are on one of the menu's pages" — except one primary nav is ALSO
  // showing for (Canvases in nav).
  const prefsActive = menuEntries.some(
    (e) => e !== "separator" && e.href === pathname && !(canvasesInNav && e.href === "/canvases")
  );

  const pick = (entry: PrefsMenuEntry) => {
    if (entry.onPick) entry.onPick();
    else navigateUrl(entry.href);
  };

  const menu = (
    <DropdownMenuContent side="top" align="start" sideOffset={4} className="min-w-44">
      {menuEntries.map((entry, i) =>
        entry === "separator" ? (
          <DropdownMenuSeparator key={"sep" + i} />
        ) : (
          <PrefsRow key={entry.href} entry={entry} onPick={pick} />
        )
      )}
    </DropdownMenuContent>
  );

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
      label: "Settings",
      icon: PREFERENCES_ICON,
      href: "/preferences",
      pinBottom: true,
      active: prefsActive,
      // The same Settings menu as the expanded row, not a straight nav — the
      // collapsed rail otherwise has no way to reach Templates/Mounts/etc.
      onClick: () => {},
      render: (anchor) => (
        <DropdownMenu>
          <DropdownMenuTrigger render={anchor} nativeButton={false} />
          {menu}
        </DropdownMenu>
      ),
    },
  ];

  return (
    <SidebarFrame title="Render" homeHref="/home" rail={rail}>
      <div className="flex flex-col gap-px p-2">
        <NavItem href="/home" id="home-link" label="Home" icon={HOME_ICON} active={homeActive} />
        {/* Tasks took Inbox's place as well as its job; Inbox is deleted. */}
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
            // Beta while the surface (playground foremost) is still settling.
            <Badge variant="outline" className="h-4 px-1.5 text-xs uppercase tracking-wide text-muted-foreground">
              Beta
            </Badge>
          }
        />
      </div>
      {/* Projects (D487): the apps on the desk, above the permanent Bookmarks
          tree. Collapsible; always ends in a "+ New app" row. */}
      <CurrentAppsSection />
      <BookmarksSection />
      <div className="flex flex-col p-2">
        <UpdateBadge />
        {/* The version rides the Settings row's trailing edge: a footnote about
            this install, in the same trailing slot the Tasks row states its
            count in — not glued to the brand, where it read as the app's name. */}
        <DropdownMenu>
          <DropdownMenuTrigger
            className={cn(NAV_ITEM_CLASS, "text-left")}
            data-active={prefsActive ? "" : undefined}
          >
            <span className="relative flex size-4 shrink-0 items-center justify-center opacity-85">
              {PREFERENCES_ICON}
            </span>
            <span className="min-w-0 flex-1 truncate">Settings</span>
            {config.version && (
              <Badge
                variant="outline"
                className="ml-auto h-4 px-1.5 font-mono text-xs tabular-nums text-muted-foreground"
                title={`Fused Render v${config.version}`}
              >
                v{config.version}
              </Badge>
            )}
          </DropdownMenuTrigger>
          {menu}
        </DropdownMenu>
      </div>
    </SidebarFrame>
  );
}

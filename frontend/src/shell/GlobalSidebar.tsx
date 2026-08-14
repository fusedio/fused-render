// THE sidebar — one for the whole app, on every route. Replaces the old pair
// (ShellSidebar app-switcher on shell routes, ExplorerSidebar on fs routes):
// primary nav on top (Home / Inbox), the explorer's Bookmarks below it,
// and a single Settings trigger pinned to the
// bottom that opens a menu holding everything else (Config / App Basics for
// now, plus Templates / Mounts / AI Models / Preferences).
//
// Lives in the shell layer on purpose: it composes both platform chrome
// (SidebarFrame) and explorer-owned sections (Bookmarks), which only
// the shell is allowed to import together (scripts/check-boundaries.mjs).
import { useEffect, useRef, useState } from "react";
import { SidebarFrame, NavItem } from "@platform/ui/sidebar/SidebarFrame";
import type { SidebarRailItem } from "@platform/ui/sidebar/SidebarFrame";
import { LearnIcon } from "@platform/ui/FileIcons";
import type { Config } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import {
  useUrlVersion,
  useLearnMountReady,
  useSessionsMountReady,
} from "@platform/lib/hooks";
import { useAccountLoggedIn } from "@platform/lib/account";
import { useDeployEnabled } from "@platform/lib/prefs";
import { useClaudeConfigAvailable } from "@apps/claude_config/available";
import { useAiRuntime } from "@shell/aiRuntime";
import { formatSize } from "@platform/lib/format";
import BookmarksSection from "@apps/explorer/sidebar/BookmarksSection";

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

// Inbox tray — the Sessions app is a triage inbox for Claude Code sessions.
const SESSIONS_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M22 12h-6l-2 3h-4l-2-3H2" />
    <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
  </svg>
);

// Stacked disks — the AI Models entry is an inventory of what the Hugging
// Face cache is storing on this machine, so it reads as storage, not as a chip.
const AI_MODELS_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <ellipse cx="12" cy="5" rx="8" ry="3" />
    <path d="M4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5" />
    <path d="M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6" />
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

const PREFERENCES_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </svg>
);

interface PrefsMenuEntry {
  href: string;
  label: string;
  icon: React.ReactNode;
  extra?: React.ReactNode;
}

// The bottom Settings trigger + its pop-up menu. A NavItem-shaped row that
// opens a fixed-position menu growing UP from the row (the row sits on the
// sidebar's bottom edge). Closes on outside pointerdown / Escape / navigation.
function PreferencesMenu({
  entries,
  dot,
  active,
}: {
  entries: (PrefsMenuEntry | "separator")[];
  dot?: React.ReactNode;
  /** The current route is one of the menu's destinations — the trigger is
      the only sidebar chrome that can show it. */
  active: boolean;
}) {
  const [pos, setPos] = useState<{ left: number; bottom: number } | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!pos) return;
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setPos(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPos(null);
    };
    const onBlur = () => setPos(null);
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("blur", onBlur);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", onBlur);
    };
  }, [pos]);

  return (
    <div ref={rootRef}>
      <button
        type="button"
        className={"sidebar-item sidebar-prefs-trigger" + (active ? " active" : "")}
        aria-haspopup="menu"
        aria-expanded={pos !== null}
        onClick={(e) => {
          if (pos) {
            setPos(null);
            return;
          }
          const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
          // Grows upward: pinned by its bottom edge just above the trigger.
          setPos({ left: r.left, bottom: window.innerHeight - r.top + 4 });
        }}
      >
        <span className="icon">
          {PREFERENCES_ICON}
          {dot}
        </span>{" "}
        Settings
      </button>
      {pos && (
        <div
          className="context-menu placed sidebar-prefs-menu"
          role="menu"
          style={{ left: pos.left, bottom: pos.bottom }}
        >
          {entries.map((entry, i) =>
            entry === "separator" ? (
              <div key={"sep" + i} className="context-menu-sep" role="separator" />
            ) : (
              <div
                key={entry.href}
                role="menuitem"
                className={
                  "context-menu-item" + (location.pathname === entry.href ? " active" : "")
                }
                onClick={() => {
                  setPos(null);
                  navigateUrl(entry.href);
                }}
              >
                <span className="context-menu-icon" aria-hidden="true">
                  {entry.icon}
                </span>
                <span className="context-menu-label">{entry.label}</span>
                {entry.extra}
              </div>
            )
          )}
        </div>
      )}
    </div>
  );
}

export default function GlobalSidebar({ config }: { config: Config }) {
  // Re-render on any nav/url change (active-item highlight).
  useUrlVersion();
  const accountLoggedIn = useAccountLoggedIn();
  const deployEnabled = useDeployEnabled();

  const learnMountReady = useLearnMountReady(config.learn_mount_ready);
  const sessionsMountReady = useSessionsMountReady(config.sessions_mount_ready);
  const claudeConfigAvailable = useClaudeConfigAvailable();

  // A model resident in memory is the one piece of app state that costs
  // something while you are not looking at it — surfaced as a dot on the
  // bottom trigger now that AI Models lives inside the menu.
  const aiRuntime = useAiRuntime();
  const residentModels = aiRuntime.loaded.filter((m) => m.state === "ready");
  const residentDot = residentModels.length ? (
    <span
      className="account-signedin-dot"
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

  // Everything that is not primary nav lives in the bottom menu for now:
  // the former sidebar entries (Config / App Basics), then the settings
  // pages. Same gates as before — an entry a machine can't use stays hidden.
  const menuEntries: (PrefsMenuEntry | "separator")[] = [
    ...(claudeConfigAvailable
      ? [{ href: "/claude-config", label: "Claude Config", icon: CLAUDE_CONFIG_ICON }]
      : []),
    ...(learnMountReady ? [{ href: "/learn", label: "App Basics", icon: <LearnIcon /> }] : []),
  ];
  if (menuEntries.length > 0) menuEntries.push("separator");
  menuEntries.push(
    { href: "/templates", label: "Templates", icon: TEMPLATES_ICON },
    { href: "/mounts", label: "Mounts", icon: MOUNTS_ICON },
    { href: "/ai-models", label: "AI Models", icon: AI_MODELS_ICON, extra: residentDot },
    {
      href: "/preferences",
      label: "Preferences",
      icon: PREFERENCES_ICON,
      // Fused-account signed-in signal (SPEC AC-1). Gated on Deploy being
      // enabled — that's the only reason a Fused account matters here.
      extra:
        deployEnabled && accountLoggedIn ? <span className="account-signedin-dot" /> : undefined,
    }
  );

  // The trigger (and its rail icon) is the only sidebar chrome that can show
  // "you are on one of the menu's pages" — highlight it on any of them.
  const prefsActive = menuEntries.some((e) => e !== "separator" && e.href === pathname);

  const rail: SidebarRailItem[] = [
    { key: "home", label: "Home", icon: HOME_ICON, href: "/home", active: homeActive },
    ...(sessionsMountReady
      ? [{ key: "sessions", label: "Inbox", icon: SESSIONS_ICON, href: "/sessions" }]
      : []),
    { key: "preferences", label: "Preferences", icon: PREFERENCES_ICON, href: "/preferences", pinBottom: true, active: prefsActive },
  ];

  // The trigger's own dot mirrors the strongest signal inside the menu, so
  // neither is silently hidden while the menu is closed.
  const triggerDot =
    residentDot ??
    (deployEnabled && accountLoggedIn ? <span className="account-signedin-dot" /> : undefined);

  return (
    <SidebarFrame title="Render" version={config.version} homeHref="/home" rail={rail}>
      <div className="sidebar-section sidebar-group">
        <NavItem
          href="/home"
          id="home-link"
          label="Home"
          icon={HOME_ICON}
          active={homeActive}
        />
        {sessionsMountReady && (
          <NavItem href="/sessions" id="sessions-link" label="Inbox" icon={SESSIONS_ICON} />
        )}
      </div>
      <BookmarksSection />
      <div className="sidebar-section sidebar-settings">
        <PreferencesMenu entries={menuEntries} dot={triggerDot} active={prefsActive} />
      </div>
    </SidebarFrame>
  );
}

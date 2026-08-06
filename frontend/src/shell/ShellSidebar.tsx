// The SHELL's own sidebar — the app-switcher. Sub-app list (Apps / File
// Explorer / Learn) on top, settings list (Templates / Mounts / Preferences)
// pinned to the bottom. Composes the shared SidebarFrame chassis like every
// sub-app sidebar does (apps/*/sidebar/*Sidebar.tsx); the shell knows nothing
// about bookmarks or recents — those live with their owners.
import { SidebarFrame, NavItem } from "@platform/ui/sidebar/SidebarFrame";
import { FolderIcon, LearnIcon } from "@platform/ui/FileIcons";
import type { Config } from "@platform/lib/api";
import { useUrlVersion, useLearnMountReady, useSessionsMountReady } from "@platform/lib/hooks";
import { useAccountLoggedIn } from "@platform/lib/account";
import { useDeployEnabled } from "@platform/lib/prefs";

const APPS_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="3" y="3" width="8" height="8" rx="2" />
    <rect x="13" y="3" width="8" height="8" rx="2" />
    <rect x="3" y="13" width="8" height="8" rx="2" />
    <circle cx="17" cy="17" r="4" />
  </svg>
);

// Inbox tray — the Sessions app is a triage inbox for Claude Code sessions.
const SESSIONS_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M22 12h-6l-2 3h-4l-2-3H2" />
    <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
  </svg>
);

export default function ShellSidebar({ config }: { config: Config }) {
  // Re-render on any nav/url change (active-item highlight).
  useUrlVersion();
  // Signed-in dot on the Preferences entry (SPEC AC-1): shown only once
  // Deploy is enabled, since that's the only reason this app cares about a
  // Fused account at all.
  const accountLoggedIn = useAccountLoggedIn();
  const deployEnabled = useDeployEnabled();

  // Bounded /api/config re-poll for the learn mount (see useLearnMountReady
  // for the full race notes — the boot snapshot is stale in both directions).
  const learnMountReady = useLearnMountReady(config.learn_mount_ready);
  const sessionsMountReady = useSessionsMountReady(config.sessions_mount_ready);

  return (
    <SidebarFrame title="Fused Render" version={config.version}>
      <div className="sidebar-section">
        <NavItem href="/apps" id="apps-link" label="Apps" icon={APPS_ICON} />
        <NavItem href="/explorer" id="explorer-link" label="File Explorer" icon={<FolderIcon />} />
        {sessionsMountReady && <NavItem href="/sessions" id="sessions-link" label="Sessions" icon={SESSIONS_ICON} />}
        {learnMountReady && <NavItem href="/learn" id="learn-link" label="Learn" icon={<LearnIcon />} />}
      </div>
      {/* Settings — pinned to the bottom edge (margin-top: auto), the same
          list treatment as the sub-app list above. */}
      <div className="sidebar-section sidebar-settings">
        <NavItem
          href="/templates"
          label="Templates"
          icon={
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="3" y="3" width="7" height="7" rx="1" />
              <rect x="14" y="3" width="7" height="7" rx="1" />
              <rect x="3" y="14" width="7" height="7" rx="1" />
              <rect x="14" y="14" width="7" height="7" rx="1" />
            </svg>
          }
        />
        {/* PROTOTYPE: mounts entry — remote mounts. */}
        <NavItem
          href="/mounts"
          label="Mounts"
          icon={
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M17.5 19a4.5 4.5 0 1 0-.9-8.9 6 6 0 1 0-11.4 2.4A3.5 3.5 0 0 0 6.5 19h11z" />
            </svg>
          }
        />
        <NavItem
          href="/preferences"
          label="Preferences"
          icon={
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          }
          // Fused-account signed-in signal (SPEC AC-1), folded onto the
          // Preferences entry. Gated on Deploy being enabled — that's the
          // only reason a Fused account matters here.
          extra={deployEnabled && accountLoggedIn ? <span className="account-signedin-dot" /> : undefined}
        />
      </div>
    </SidebarFrame>
  );
}

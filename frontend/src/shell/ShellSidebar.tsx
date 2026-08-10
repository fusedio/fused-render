// The SHELL's own sidebar — the app-switcher. The sub-app list on top, grouped
// under uppercase category headings (FUSED / LEARN / CLAUDE) so the Claude
// entries read as one family instead of three unrelated rows in a flat list of
// six; settings list (Templates / Mounts / Preferences) pinned to the bottom.
// Composes the shared SidebarFrame chassis like every sub-app sidebar does
// (apps/*/sidebar/*Sidebar.tsx); the shell knows nothing about bookmarks or
// recents — those live with their owners.
//
// A heading never renders alone: every group whose items are all gated off is
// dropped whole, so LEARN disappears with its one entry and CLAUDE survives on
// either of its two gates.
import { SidebarFrame, NavItem } from "@platform/ui/sidebar/SidebarFrame";
import { FolderIcon, LearnIcon } from "@platform/ui/FileIcons";
import type { Config } from "@platform/lib/api";
import { useUrlVersion, useLearnMountReady, useSessionsMountReady } from "@platform/lib/hooks";
import { useAccountLoggedIn } from "@platform/lib/account";
import { useDeployEnabled } from "@platform/lib/prefs";
import { useClaudeConfigAvailable } from "@apps/claude_config";

const APPS_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="3" y="3" width="8" height="8" rx="2" />
    <rect x="13" y="3" width="8" height="8" rx="2" />
    <rect x="3" y="13" width="8" height="8" rx="2" />
    <circle cx="17" cy="17" r="4" />
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

// Lined document — the MD Files page lists every CLAUDE.md on the machine.
const CLAUDE_MD_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <polyline points="14 2 14 8 20 8" />
    <line x1="8" y1="13" x2="16" y2="13" />
    <line x1="8" y1="17" x2="16" y2="17" />
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
  // Claude Config is native (no mount) — availability is just "does ~/.claude
  // exist on this machine", one cached probe (see useClaudeConfigAvailable).
  const claudeConfigAvailable = useClaudeConfigAvailable();

  return (
    <SidebarFrame title="Render" version={config.version}>
      {/* Group headings reuse .sidebar-heading — the same primitive the
          explorer's Bookmarks/Recents headings use, so one dialect of "category
          label above rows" exists in the sidebar rather than two. */}
      <div className="sidebar-section sidebar-group">
        <div className="sidebar-heading">Fused</div>
        <NavItem href="/explorer" id="explorer-link" label="Explorer" icon={<FolderIcon />} />
        <NavItem href="/apps" id="apps-link" label="App" icon={APPS_ICON} />
      </div>
      {learnMountReady && (
        <div className="sidebar-section sidebar-group">
          <div className="sidebar-heading">Learn</div>
          <NavItem href="/learn" id="learn-link" label="Guide" icon={<LearnIcon />} />
        </div>
      )}
      {(sessionsMountReady || claudeConfigAvailable) && (
        <div className="sidebar-section sidebar-group">
          <div className="sidebar-heading">Claude</div>
          {sessionsMountReady && (
            <NavItem href="/sessions" id="sessions-link" label="Inbox" icon={SESSIONS_ICON} />
          )}
          {claudeConfigAvailable && (
            <>
              <NavItem
                href="/claude-md"
                id="claude-md-link"
                label="MD Files"
                icon={CLAUDE_MD_ICON}
              />
              <NavItem
                href="/claude-config"
                id="claude-config-link"
                label="Config"
                icon={CLAUDE_CONFIG_ICON}
              />
            </>
          )}
        </div>
      )}
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

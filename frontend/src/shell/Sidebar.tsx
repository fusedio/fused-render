// Shell sidebar chrome (super-app step 2): brand, resize/collapse, and a body
// that switches on the active sub-app context. The shell itself knows nothing
// about bookmarks or recents — those sections belong to the sub-apps
// (apps/explorer/sidebar/*, apps/builder/sidebar/*) and are composed here.
//
//   shell ctx    — sub-app list (Apps / File Explorer / Learn) on top,
//                  settings list (Templates / Mounts / Preferences) at bottom
//   explorer ctx — Home (→ /explorer) + Bookmarks + file Recents
//   builder ctx  — Home (→ /apps) + app Recents
import React, { useRef, useState } from "react";
import { navigateUrl } from "@platform/lib/router";
import { FolderIcon, LearnIcon } from "@platform/ui/FileIcons";
import type { Config } from "@platform/lib/api";
import {
  loadSidebarState,
  saveSidebarState,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_MAX_WIDTH,
} from "@platform/lib/sidebarstate";
import { useUrlVersion, useLearnMountReady } from "@platform/lib/hooks";
import { useAccountLoggedIn } from "@platform/lib/account";
import { useDeployEnabled } from "@platform/lib/prefs";
import BookmarksSection from "@apps/explorer/sidebar/BookmarksSection";
import ExplorerRecentsSection from "@apps/explorer/sidebar/RecentsSection";
import BuilderRecentsSection from "@apps/builder/sidebar/RecentsSection";

export type SidebarCtx = "shell" | "explorer" | "builder";

interface SidebarProps {
  config: Config;
  ctx: SidebarCtx;
}

const HOME_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M3 10.5 12 3l9 7.5" />
    <path d="M5 9.5V21h14V9.5" />
  </svg>
);

const APPS_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="3" y="3" width="8" height="8" rx="2" />
    <rect x="13" y="3" width="8" height="8" rx="2" />
    <rect x="3" y="13" width="8" height="8" rx="2" />
    <circle cx="17" cy="17" r="4" />
  </svg>
);

// A plain sidebar nav row: highlights on exact pathname match, navigates
// in-shell, keeps href for middle-click/copy-link.
function NavItem({
  href,
  label,
  icon,
  id,
  extra,
}: {
  href: string;
  label: string;
  icon: React.ReactNode;
  id?: string;
  extra?: React.ReactNode;
}) {
  return (
    <a
      href={href}
      id={id}
      className={"sidebar-item" + (location.pathname === href ? " active" : "")}
      onClick={(e) => {
        e.preventDefault();
        navigateUrl(href);
      }}
    >
      <span className="icon">
        {icon}
        {extra}
      </span>{" "}
      {label}
    </a>
  );
}

export default function Sidebar({ config, ctx }: SidebarProps) {
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

  // Sidebar chrome: draggable width + collapsed flag, persisted once per
  // gesture (drag end / toggle), not per mousemove. Width lives in React
  // state — per-pointermove setState is fine (React 18 batches) and there is
  // no transition during a drag, so no jank.
  const [{ width: sidebarWidth, collapsed: sidebarCollapsed }, setSidebarState] =
    useState(loadSidebarState);
  // True only while the handle is captured — used to suppress the collapse
  // transition and text selection mid-drag.
  const [resizing, setResizing] = useState(false);
  const dragRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null);

  // Double-press-to-collapse is detected manually here: preventDefault on
  // pointerdown (needed to stop a text selection starting before the
  // body:has(.resizing) rule commits) suppresses the compatibility mouse
  // events that produce dblclick in several engines, so onDoubleClick on the
  // handle can't be relied on.
  const lastHandlePressRef = useRef<{ time: number; x: number } | null>(null);

  const onHandlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault(); // no text selection while dragging
    const last = lastHandlePressRef.current;
    if (last && e.timeStamp - last.time < 350 && Math.abs(e.clientX - last.x) < 5) {
      // Second press of a double-press: collapse instead of starting a drag.
      lastHandlePressRef.current = null;
      toggleSidebarCollapsed();
      return;
    }
    lastHandlePressRef.current = { time: e.timeStamp, x: e.clientX };
    dragRef.current = { pointerId: e.pointerId, startX: e.clientX, startWidth: sidebarWidth };
    e.currentTarget.setPointerCapture(e.pointerId);
    setResizing(true);
  };

  const onHandlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || e.pointerId !== drag.pointerId) return;
    // A real drag isn't the first half of a double-press.
    if (Math.abs(e.clientX - drag.startX) >= 5) lastHandlePressRef.current = null;
    const width = Math.min(
      SIDEBAR_MAX_WIDTH,
      Math.max(SIDEBAR_MIN_WIDTH, drag.startWidth + (e.clientX - drag.startX))
    );
    setSidebarState((s) => (s.width === width ? s : { ...s, width }));
  };

  const onHandlePointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || e.pointerId !== drag.pointerId) return;
    dragRef.current = null;
    setResizing(false);
    // Persist the final width (functional read — the last pointermove's
    // setState may not have committed yet).
    setSidebarState((s) => {
      saveSidebarState(s);
      return s;
    });
  };

  const toggleSidebarCollapsed = () => {
    // Collapsing unmounts the section components (and their overlay surfaces —
    // icon picker, rename input, tooltip — with them), so no state reset is
    // needed here.
    setSidebarState((s) => {
      const next = { ...s, collapsed: !s.collapsed };
      saveSidebarState(next);
      return next;
    });
  };

  if (sidebarCollapsed) {
    // Collapsed: the whole sidebar shrinks to a slim strip that expands it
    // back. Still the same #sidebar node, so the <=700px media hide applies.
    return (
      <nav id="sidebar" className={"sidebar-collapsed" + (resizing ? " sidebar-no-transition" : "")}>
        <button
          type="button"
          className="sidebar-expand-strip"
          aria-label="Expand sidebar"
          title="Expand sidebar"
          onClick={toggleSidebarCollapsed}
        >
          {/* Bubble protruding into the content area — the visible half of
              the affordance; the whole strip is still the click target. */}
          <span className="sidebar-expand-bubble" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="m9 18 6-6-6-6" />
            </svg>
          </span>
        </button>
      </nav>
    );
  }

  return (
    // The collapse/expand width change glides (shell.css); a pointer DRAG must
    // not, or every pointermove would chase a 200ms transition and the handle
    // would lag the cursor. `sidebar-no-transition` is that suppression.
    <nav
      id="sidebar"
      className={resizing ? "sidebar-no-transition" : undefined}
      style={{ flexBasis: sidebarWidth, width: sidebarWidth }}
    >
      <div className="sidebar-brand">
        {/* Logo + name are one click target that goes to the app home (/apps) —
            the front door is always one click away from anywhere. The collapse
            button stays its own control outside the link. */}
        <a
          href="/apps"
          className="brand-home-link"
          title="Home"
          onClick={(e) => {
            e.preventDefault();
            navigateUrl("/apps");
          }}
        >
          {/* Fused cube mark (brand asset logo-black-bg-transparent.svg), stroke
              follows .logo's color so it stays on the accent token. */}
          <span className="logo">
            <svg width="20" height="20" viewBox="0 0 233 233" fill="none" aria-hidden="true">
              <path
                d="M43.916 84.6995L80.0899 105.742M43.916 84.6995L80.0899 64.13M43.916 84.6995V126.548M80.0899 105.742L114.383 125.69C115.548 126.368 116.264 127.613 116.264 128.96V162.056C116.264 164.973 113.101 166.793 110.579 165.326L43.916 126.548M80.0899 105.742V182.862C80.0899 185.779 76.9269 187.598 74.405 186.131L45.7968 169.49C44.6324 168.813 43.916 167.567 43.916 166.22V126.548M80.0899 105.742L152.674 64.13M80.0899 64.13L114.4 44.6204C115.556 43.9629 116.973 43.961 118.131 44.6152L152.674 64.13M80.0899 64.13L150.785 104.659C151.955 105.329 153.392 105.327 154.559 104.652L183.353 88.0121C185.887 86.5475 185.869 82.883 183.321 81.4432L152.674 64.13"
                stroke="currentColor"
                strokeWidth="12"
              />
            </svg>
          </span>{" "}
          {/* Brand names the current context: the shell is the platform
              ("Fused Render"), each sub-app announces itself. */}
          <span className="brand-title">
            {ctx === "builder" ? "Fused App" : ctx === "explorer" ? "Fused Explorer" : "Fused Render"}
          </span>
        </a>
        <span className="brand-version">v{config.version}</span>
        <button
          type="button"
          className="icon-btn sidebar-collapse-btn"
          aria-label="Collapse sidebar"
          title="Collapse sidebar"
          onClick={toggleSidebarCollapsed}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="m15 18-6-6 6-6" />
          </svg>
        </button>
      </div>

      {ctx === "shell" && (
        <>
          <div className="sidebar-section">
            <NavItem href="/apps" id="apps-link" label="Apps" icon={APPS_ICON} />
            <NavItem href="/explorer" id="explorer-link" label="File Explorer" icon={<FolderIcon />} />
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
        </>
      )}

      {ctx === "explorer" && (
        <>
          <div className="sidebar-section">
            <NavItem href="/explorer" id="explorer-home-link" label="Home" icon={HOME_ICON} />
          </div>
          <BookmarksSection />
          <ExplorerRecentsSection />
        </>
      )}

      {ctx === "builder" && (
        <>
          <div className="sidebar-section">
            <NavItem href="/apps" id="builder-home-link" label="Home" icon={HOME_ICON} />
          </div>
          <BuilderRecentsSection />
        </>
      )}

      {/* Resize handle riding the right border: drag to resize (pointer
          capture keeps the gesture even when the cursor leaves the strip),
          double-press to collapse (detected in pointerdown — see
          lastHandlePressRef). */}
      <div
        className={"sidebar-resize-handle" + (resizing ? " resizing" : "")}
        style={{ left: sidebarWidth - 3 }}
        onPointerDown={onHandlePointerDown}
        onPointerMove={onHandlePointerMove}
        onPointerUp={onHandlePointerUp}
        onPointerCancel={onHandlePointerUp}
      />
    </nav>
  );
}

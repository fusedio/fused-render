// Shared sidebar chassis: brand row (logo + owner-supplied title + version),
// draggable width, collapse strip, and the resize handle. Owns NO body — each
// sub-app (and the shell itself) renders its own sidebar by composing this
// frame with its own sections, so the platform stays ignorant of bookmarks,
// recents, and app lists. Width/collapsed state is shared across all owners
// (platform/lib/sidebarstate): switching sub-apps must not jump the layout.
import React, { useRef, useState } from "react";
import { navigateUrl } from "@platform/lib/router";
import {
  getSidebarState,
  saveSidebarState,
  setSidebarState,
  toggleSidebarCollapsed,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_MAX_WIDTH,
} from "@platform/lib/sidebarstate";
import { useSidebarState } from "@platform/lib/hooks";

export interface SidebarFrameProps {
  /** Brand text next to the cube mark — names the owning context. */
  title: string;
  /** Version chip after the title — shown only by the shell ("Render"). */
  version?: string;
  /** Where the brand click lands; the front door of the owning app. */
  homeHref?: string;
  children: React.ReactNode;
}

// A plain sidebar nav row: highlights on exact pathname match, navigates
// in-shell, keeps href for middle-click/copy-link.
export function NavItem({
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

export function SidebarFrame({ title, version, homeHref = "/apps", children }: SidebarFrameProps) {
  // Sidebar chrome: draggable width + collapsed flag, persisted once per
  // gesture (drag end / toggle), not per mousemove. The state lives in the
  // shared store (platform/lib/sidebarstate) rather than here, because the
  // explorer topbar also reads the collapsed flag — it carries the "Open
  // sidebar" control while the sidebar is away.
  const { width: sidebarWidth, collapsed: sidebarCollapsed } = useSidebarState();
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
    // Not persisted per move — the final width is written at drag end.
    setSidebarState((s) => (s.width === width ? s : { ...s, width }), false);
  };

  const onHandlePointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || e.pointerId !== drag.pointerId) return;
    dragRef.current = null;
    setResizing(false);
    // Persist the final width (functional read — the last pointermove's
    // store write may not be reflected in a stale closure).
    saveSidebarState(getSidebarState());
  };

  if (sidebarCollapsed) {
    // Collapsed: the whole sidebar shrinks to a slim strip. The strip is still
    // a click target that brings it back, but it no longer carries the
    // floating expand BUBBLE — anchored to the right edge of a 10px strip, half
    // of the 32px circle hung off the viewport and it sat at the same height
    // as the topbar's bookmark star, reading as a second star. The affordance
    // now lives docked in the explorer topbar ("Open sidebar", leftmost in the
    // path zone), where it is fully on-screen and in the same row as the other
    // chrome controls. Still the same #sidebar node, so the <=700px media hide
    // applies.
    return (
      <nav id="sidebar" className={"sidebar-collapsed" + (resizing ? " sidebar-no-transition" : "")}>
        <button
          type="button"
          className="sidebar-expand-strip"
          aria-label="Expand sidebar"
          title="Expand sidebar"
          onClick={toggleSidebarCollapsed}
        />
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
        {/* Logo + name are one click target that goes to the owner's home —
            the front door is always one click away from anywhere. The collapse
            button stays its own control outside the link. */}
        <a
          href={homeHref}
          className="brand-home-link"
          title="Home"
          onClick={(e) => {
            e.preventDefault();
            navigateUrl(homeHref);
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
          <span className="brand-title">{title}</span>
        </a>
        {version && <span className="brand-version">v{version}</span>}
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

      {children}

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

// The "Home" row shared by sub-app sidebars (explorer, builder) — same glyph
// everywhere so "back to my app's front page" reads identically.
export const HOME_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M3 10.5 12 3l9 7.5" />
    <path d="M5 9.5V21h14V9.5" />
  </svg>
);

// Shared sidebar chassis: brand row (logo + owner-supplied title + version),
// draggable width, collapse strip, and the resize handle. Owns NO body — each
// sub-app (and the shell itself) renders its own sidebar by composing this
// frame with its own sections, so the platform stays ignorant of bookmarks,
// recents, and app lists. Width/collapsed state is shared across all owners
// (platform/lib/sidebarstate): switching sub-apps must not jump the layout.
import React, { useRef, useState } from "react";
import PanelIcon from "@platform/ui/PanelIcon";
import { navigateUrl } from "@platform/lib/router";
import {
  getSidebarState,
  saveSidebarState,
  setSidebarState,
  toggleSidebarCollapsed,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_RAIL_WIDTH,
  type SidebarState,
} from "@platform/lib/sidebarstate";
import { reopenWidth, resizeWidth } from "@platform/lib/panel-drag";
import { useSidebarState } from "@platform/lib/hooks";

/** One icon on the collapsed rail, and it is always a DESTINATION: click
    navigates there and the rail STAYS collapsed — only the chevron expands.
    One dialect on purpose. The explorer's rail briefly spoke a second one
    (section icons that expanded the frame and revealed Recents/Bookmarks),
    so what a rail click did depended on the route; every owner's icons now
    behave like the shell's. The frame stays ignorant of what the icons
    mean — owners describe them here. */
export interface SidebarRailItem {
  key: string;
  /** Tooltip + accessible name; the rail shows only the icon. */
  label: string;
  icon: React.ReactNode;
  /** Navigate here on click. Highlights as active on exact pathname match,
      like NavItem. Still set even when `onClick` overrides the click, so
      middle-click/copy-link keeps working. */
  href: string;
  /** Overrides the click instead of navigating to `href` — for a rail icon
      that opens a menu (e.g. Settings) rather than going straight to a page.
      Receives the anchor so the caller can anchor a popover off its rect. */
  onClick?: (e: React.MouseEvent<HTMLAnchorElement>) => void;
  /** Hairline above this item — a group boundary, mirroring the expanded
      sidebar's headings. */
  dividerBefore?: boolean;
  /** Set on the FIRST item of a bottom-pinned cluster (the shell's settings
      list) — pushes it and everything after to the rail's bottom edge. */
  pinBottom?: boolean;
  /** Override the exact-pathname highlight, mirroring NavItem's `active` —
      for icons that are "home" to a family of routes. */
  active?: boolean;
  /** A mark ON the icon — the collapsed rail's only way to say anything about
      the page behind it, since there is no label to hang a count on (the
      shell's Tasks dot). Drawn inside the button, which is the positioned
      ancestor it resolves against; the frame never says what it means. */
  badge?: React.ReactNode;
}

export interface SidebarFrameProps {
  /** Brand text next to the cube mark — names the owning context. */
  title: string;
  /** Version chip after the title — shown only by the shell ("Render"). */
  version?: string;
  /** Where the brand click lands; the front door of the owning app. */
  homeHref?: string;
  /** Section icons for the collapsed rail. Optional — a sidebar without them
      collapses to just the expand control. */
  rail?: SidebarRailItem[];
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
  trailing,
  active,
}: {
  href: string;
  label: string;
  icon: React.ReactNode;
  id?: string;
  /** A mark ON the icon (the Settings row's resident-model dot). */
  extra?: React.ReactNode;
  /** Content AFTER the label, pushed to the row's trailing edge — where a count
      or a status readout goes once there is a label for it to belong to. The
      expanded sidebar's answer to the rail's `badge`: same fact, stated in
      words rather than as a dot, because here there is room for words. */
  trailing?: React.ReactNode;
  /** Override the exact-pathname highlight — for entries that are "home" to a
      whole family of routes (the global sidebar's Explorer row is active on
      every fs-path/panel/tab route, not just /explorer itself). */
  active?: boolean;
}) {
  return (
    <a
      href={href}
      id={id}
      className={"sidebar-item" + ((active ?? location.pathname === href) ? " active" : "")}
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
      {trailing && <span className="sidebar-item-trail">{trailing}</span>}
    </a>
  );
}

export function SidebarFrame({ title, version, homeHref = "/apps", rail, children }: SidebarFrameProps) {
  // Sidebar chrome: draggable width + collapsed flag, persisted once per
  // gesture (drag end / toggle), not per mousemove. The state lives in the
  // shared store (platform/lib/sidebarstate) rather than here so that a
  // remount — every sub-app composes its own frame — inherits the live layout
  // instead of re-reading the persisted one.
  const { width: sidebarWidth, collapsed: sidebarCollapsed } = useSidebarState();
  // True only while the handle is captured — used to suppress the collapse
  // transition and text selection mid-drag.
  const [resizing, setResizing] = useState(false);
  // `fromCollapsed` fixes which RULE the whole gesture is read by — the seam of an
  // open panel (`resizeWidth`) or the edge of a shut one (`reopenWidth`) — decided
  // once at pointerdown rather than re-decided from the live collapsed flag on
  // every move. Re-deciding oscillates: the two thresholds sit at different
  // implied widths on purpose (platform/lib/panel-drag), so a gesture that switched
  // rules the instant the panel opened would find itself immediately past the
  // close threshold, shut, be past the open threshold again, and flap once per
  // pointermove across the whole band between them.
  //
  // `startEdge` and `restoreWidth` are TWO NUMBERS because they are two facts, and
  // one field holding both was a bug. `startEdge` is where the panel's outer edge
  // physically STOOD at pointerdown, which the implied width is measured from —
  // and on the collapsed rail that is 44px, not the width the panel remembers.
  // `restoreWidth` is the width the panel gets BACK if this drag shuts it.
  //
  // Conflated, a reopen drag measured its pull from the remembered width (≥180 by
  // definition, since that is the floor), so `implied` cleared the open threshold
  // at zero travel: the rail sprang to full width on a 2px twitch, and OPEN_PULL
  // guarded nothing at all.
  //
  // The restore half is its own rule: dragging a sidebar shut is not the same act
  // as making it narrow, and one gesture should not quietly do both. Shut it from
  // 320px and the button brings back 320px, not the floor the drag stuck at on the
  // way through.
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startEdge: number;
    restoreWidth: number;
    fromCollapsed: boolean;
  } | null>(null);

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
    dragRef.current = {
      pointerId: e.pointerId,
      startX: e.clientX,
      // Where the edge IS, which while collapsed is the rail — never the width
      // the panel is remembering behind it. See `dragRef`.
      startEdge: sidebarCollapsed ? SIDEBAR_RAIL_WIDTH : sidebarWidth,
      restoreWidth: sidebarWidth,
      fromCollapsed: sidebarCollapsed,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
    setResizing(true);
  };

  const onHandlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || e.pointerId !== drag.pointerId) return;
    // A real drag isn't the first half of a double-press.
    if (Math.abs(e.clientX - drag.startX) >= 5) lastHandlePressRef.current = null;
    // IMPLIED WIDTH — how wide the pointer is asking this panel to be, which for a
    // LEFT-hand panel grows with clientX. Measured from where the panel's outer
    // edge stood when the drag began (the rail's 44px when it began collapsed), so
    // the seam stays under the cursor rather than jumping to it.
    const implied = drag.startEdge + (e.clientX - drag.startX);
    const next = drag.fromCollapsed
      ? reopenWidth(implied, SIDEBAR_RAIL_WIDTH, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH)
      : resizeWidth(implied, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH);
    // `null` = the gesture wants the panel SHUT. The width it keeps while shut is
    // the one it had at pointerdown, not the floor it stuck at on the way down —
    // see `dragRef`.
    const value: SidebarState =
      next === null
        ? { width: drag.restoreWidth, collapsed: true }
        : { width: next, collapsed: false };
    // Not persisted per move — the settled state is written at drag end.
    setSidebarState(
      (s) => (s.width === value.width && s.collapsed === value.collapsed ? s : value),
      false
    );
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

  // THE SEAM, and it is ONE element in both states — rendered here, beside the
  // <nav> rather than inside it, and reconciled into the same DOM node whichever
  // subtree the nav is currently showing.
  //
  // That is a requirement and not a tidiness preference. The collapsed rail and
  // the expanded sidebar are different subtrees, so a handle rendered inside each
  // would be UNMOUNTED at the exact moment a reopen drag crosses its threshold —
  // and unmounting the element that holds the pointer capture ends the gesture
  // mid-stroke. The user would pull the rail out to 180px, let go of nothing, and
  // find the drag already over. Kept out here, one node holds the capture from
  // pointerdown to pointerup across any number of open/close flips.
  //
  // Position: fixed (styles/sidebar.css), so being a sibling of #sidebar rather
  // than a child costs it nothing in the flex row — and it is why the sidebar's
  // own overflow-y scroll cannot clip it.
  const handle = (
    <div
      className={"sidebar-resize-handle" + (resizing ? " resizing" : "")}
      style={{ left: (sidebarCollapsed ? SIDEBAR_RAIL_WIDTH : sidebarWidth) - 3 }}
      role="separator"
      aria-orientation="vertical"
      aria-label={sidebarCollapsed ? "Drag to show the sidebar" : "Drag to resize the sidebar"}
      onPointerDown={onHandlePointerDown}
      onPointerMove={onHandlePointerMove}
      onPointerUp={onHandlePointerUp}
      onPointerCancel={onHandlePointerUp}
    />
  );

  if (sidebarCollapsed) {
    // Collapsed: an icon RAIL, not the old anonymous 20px strip (which read as
    // a full-height bar whose only content was an arrow). The expand control
    // stays on top — the ONE reopen control, on every route, pinned to the
    // same 24px line the sidebar header and the app bars stand on. Below it,
    // the owner's destination icons (SidebarRailItem): clicking one navigates
    // and the rail stays collapsed — the chevron alone expands.
    //
    // Still the same #sidebar node, so the <=700px media hide applies.
    //
    // The rail carries the SEAM too (`handle`, below the nav): the button is not
    // the only way back. Pulling the rail's outer edge is the exact reverse of the
    // drag that shut it, which is the whole point — a gesture that only works in
    // one direction teaches you not to trust it.
    return (
      <>
      <nav id="sidebar" className={"sidebar-collapsed" + (resizing ? " sidebar-no-transition" : "")}>
        <button
          type="button"
          className="sidebar-rail-btn sidebar-rail-expand"
          aria-label="Expand sidebar"
          title="Expand sidebar"
          onClick={toggleSidebarCollapsed}
        >
          {/* The SAME glyph the collapse button wears, not a mirrored one: this
              is one panel with one toggle, and the icon names the panel rather
              than the direction of travel (platform/ui/PanelIcon). Which state
              you are in is legible from the sidebar being there or not. */}
          <PanelIcon side="left" />
        </button>
        {rail && rail.length > 0 && (
          <div className="sidebar-rail-items">
            {rail.map((item) => (
              <React.Fragment key={item.key}>
                {item.pinBottom && <span className="sidebar-rail-flex" aria-hidden="true" />}
                {item.dividerBefore && <span className="sidebar-rail-sep" aria-hidden="true" />}
                <a
                  href={item.href}
                  className={
                    "sidebar-rail-btn" +
                    ((item.active ?? location.pathname === item.href) ? " active" : "")
                  }
                  aria-label={item.label}
                  title={item.label}
                  onClick={(e) => {
                    e.preventDefault();
                    if (item.onClick) item.onClick(e);
                    else navigateUrl(item.href);
                  }}
                >
                  {item.icon}
                  {item.badge}
                </a>
              </React.Fragment>
            ))}
          </div>
        )}
      </nav>
      {handle}
      </>
    );
  }

  return (
    <>
    {/* The collapse/expand width change glides (shell.css); a pointer DRAG must
        not, or every pointermove would chase a 200ms transition and the handle
        would lag the cursor. `sidebar-no-transition` is that suppression. */}
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
          {/* The shared glyph (platform/ui/PanelIcon): a frame with its LEFT
              half filled — this panel, on this side — in place of the chevron
              that used to say only "something collapses that way", identically,
              on both edges of the window. */}
          <PanelIcon side="left" />
        </button>
      </div>

      {children}
    </nav>
    {/* Resize handle riding the right border: drag to resize, drag PAST the
        floor to collapse (platform/lib/panel-drag), double-press to collapse
        (detected in pointerdown — see lastHandlePressRef). Pointer capture keeps
        the gesture even when the cursor leaves the strip; rendered outside the
        nav so it also survives the collapse — see `handle` above. */}
    {handle}
    </>
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

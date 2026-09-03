// Shared sidebar chassis: brand row (logo + owner-supplied title), draggable
// width, collapsed icon rail, and the resize handle. Owns NO body — the shell
// composes this frame with its own sections, so the platform stays ignorant of
// bookmarks, recents, and app lists. Width/collapsed state is shared across all
// owners (platform/lib/sidebarstate): switching sub-apps must not jump the layout.
import React, { useEffect, useRef, useState } from "react";
import { Home } from "lucide-react";
import PanelIcon from "@platform/ui/PanelIcon";
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";
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
    navigates there and the rail STAYS collapsed — only the panel button
    expands. The frame stays ignorant of what the icons mean. */
export interface SidebarRailItem {
  key: string;
  /** Tooltip + accessible name; the rail shows only the icon. */
  label: string;
  icon: React.ReactNode;
  /** Navigate here on click. Highlights as active on exact pathname match. */
  href: string;
  /** Overrides the click instead of navigating to `href` — for a rail icon
      that opens a menu rather than going straight to a page. */
  onClick?: (e: React.MouseEvent<HTMLAnchorElement>) => void;
  /** Render this item yourself (e.g. wrap it in a menu trigger). Receives the
      default anchor to compose around. */
  render?: (anchor: React.ReactElement) => React.ReactNode;
  /** Hairline above this item — a group boundary. */
  dividerBefore?: boolean;
  /** Set on the FIRST item of a bottom-pinned cluster. */
  pinBottom?: boolean;
  /** Override the exact-pathname highlight. */
  active?: boolean;
  /** A mark ON the icon (the shell's Tasks dot) — drawn inside the button. */
  badge?: React.ReactNode;
}

export interface SidebarFrameProps {
  /** Brand text next to the cube mark — names the owning context. */
  title: string;
  /** Where the brand click lands; the front door of the owning app. */
  homeHref?: string;
  /** Section icons for the collapsed rail. */
  rail?: SidebarRailItem[];
  children: React.ReactNode;
}

// The dense nav row shared by every sidebar entry: 16px icon, text-sm, the
// sidebar-accent wash on hover and on the current page. Never wraps: the
// expand glide lays these rows out at every width between 44px and the settled
// one, and a wrapping label would flash a phantom second line mid-glide.
export const NAV_ITEM_CLASS =
  "flex w-full items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm text-sidebar-foreground whitespace-nowrap no-underline cursor-pointer select-none hover:bg-sidebar-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring aria-[current=page]:bg-sidebar-accent data-[active]:bg-sidebar-accent motion-safe:transition-colors motion-safe:duration-100";

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
  /** A mark ON the icon (the Tasks row's pulse dot). */
  extra?: React.ReactNode;
  /** Content AFTER the label, pushed to the row's trailing edge. */
  trailing?: React.ReactNode;
  /** Override the exact-pathname highlight. */
  active?: boolean;
}) {
  const isActive = active ?? location.pathname === href;
  return (
    <a
      href={href}
      id={id}
      className={NAV_ITEM_CLASS}
      aria-current={isActive ? "page" : undefined}
      onClick={(e) => {
        e.preventDefault();
        navigateUrl(href);
      }}
    >
      <span className="relative flex size-4 shrink-0 items-center justify-center opacity-85 [&_svg]:size-4">
        {icon}
        {extra}
      </span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {trailing && <span className="ml-auto flex shrink-0 items-center gap-1.5">{trailing}</span>}
    </a>
  );
}

// 28px square, 16px glyph: the rail reads as a column of the same controls the
// bars use. Rail items come as <a> (navigate) or <button> (expand) — one look.
export const RAIL_BTN_CLASS =
  "relative flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground no-underline cursor-pointer hover:bg-sidebar-accent hover:text-sidebar-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring aria-[current=page]:bg-sidebar-accent aria-[current=page]:text-sidebar-foreground [&_svg]:size-4 motion-safe:transition-colors motion-safe:duration-100";

export function SidebarFrame({ title, homeHref = "/apps", rail, children }: SidebarFrameProps) {
  // Sidebar chrome: draggable width + collapsed flag, persisted once per
  // gesture (drag end / toggle), not per mousemove. The state lives in the
  // shared store so a remount inherits the live layout.
  const { width: sidebarWidth, collapsed: sidebarCollapsed } = useSidebarState();
  // True only while the handle is captured — suppresses the collapse
  // transition and text selection mid-drag.
  const [resizing, setResizing] = useState(false);
  // `fromCollapsed` fixes which RULE the whole gesture is read by — the seam of
  // an open panel (`resizeWidth`) or the edge of a shut one (`reopenWidth`) —
  // decided once at pointerdown. Re-deciding per move oscillates, since the two
  // thresholds sit at different implied widths (platform/lib/panel-drag).
  // `startEdge` is where the edge physically STOOD (44px on the rail);
  // `restoreWidth` is what the panel gets BACK if this drag shuts it.
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startEdge: number;
    restoreWidth: number;
    fromCollapsed: boolean;
  } | null>(null);

  // Double-press-to-collapse is detected manually: preventDefault on
  // pointerdown suppresses the compatibility mouse events that produce dblclick.
  const lastHandlePressRef = useRef<{ time: number; x: number } | null>(null);

  // While a drag is captured, keep the cursor stable and text unselected
  // everywhere the pointer sweeps — set on <body>, which no class here reaches.
  useEffect(() => {
    if (!resizing) return;
    document.body.classList.add("cursor-col-resize", "select-none");
    return () => document.body.classList.remove("cursor-col-resize", "select-none");
  }, [resizing]);

  const onHandlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault(); // no text selection while dragging
    const last = lastHandlePressRef.current;
    if (last && e.timeStamp - last.time < 350 && Math.abs(e.clientX - last.x) < 5) {
      lastHandlePressRef.current = null;
      toggleSidebarCollapsed();
      return;
    }
    lastHandlePressRef.current = { time: e.timeStamp, x: e.clientX };
    dragRef.current = {
      pointerId: e.pointerId,
      startX: e.clientX,
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
    if (Math.abs(e.clientX - drag.startX) >= 5) lastHandlePressRef.current = null;
    const implied = drag.startEdge + (e.clientX - drag.startX);
    const next = drag.fromCollapsed
      ? reopenWidth(implied, SIDEBAR_RAIL_WIDTH, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH)
      : resizeWidth(implied, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH);
    const value: SidebarState =
      next === null
        ? { width: drag.restoreWidth, collapsed: true }
        : { width: next, collapsed: false };
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
    saveSidebarState(getSidebarState());
  };

  // THE SEAM, one element in both states — rendered beside the <nav>, never
  // inside it. The rail and the expanded sidebar are different subtrees, so a
  // handle inside each would be UNMOUNTED the moment a reopen drag crosses its
  // threshold, and unmounting the element holding pointer capture ends the
  // gesture mid-stroke. Fixed-positioned so the nav's own scroll cannot clip it.
  const edge = sidebarCollapsed ? SIDEBAR_RAIL_WIDTH : sidebarWidth;
  const handle = (
    <div
      className={cn(
        "fixed top-0 z-10 h-screen w-1.5 cursor-col-resize touch-none hover:bg-foreground/20 max-[700px]:hidden",
        resizing && "bg-foreground/20",
      )}
      style={{ left: edge - 3 }}
      role="separator"
      aria-orientation="vertical"
      aria-label={sidebarCollapsed ? "Drag to show the sidebar" : "Drag to resize the sidebar"}
      onPointerDown={onHandlePointerDown}
      onPointerMove={onHandlePointerMove}
      onPointerUp={onHandlePointerUp}
      onPointerCancel={onHandlePointerUp}
    />
  );

  // Width is inline in BOTH states so the collapse/expand glide has two values
  // to interpolate; the transition is off while a drag is captured (it would
  // make the edge chase the cursor) and under reduced motion. `overflow-x:
  // hidden` keeps the squeeze from popping a horizontal scrollbar mid-glide.
  // `#sidebar` stays as the id: sidebarArrowNav, the tours and preview.css's
  // embed rule all address it.
  const navClass = cn(
    "flex shrink-0 flex-col overflow-x-hidden overflow-y-auto scrollbar-auto-hide border-r border-sidebar-border bg-sidebar text-sidebar-foreground max-[700px]:hidden",
    !resizing && "motion-safe:transition-[width,flex-basis] motion-safe:duration-200 motion-safe:ease-out",
  );
  // The two subtrees are swapped at the animation boundary; fading whichever
  // one is newly mounted keeps that swap from reading as a pop.
  const fadeIn = "motion-safe:animate-in motion-safe:fade-in motion-safe:duration-200";

  if (sidebarCollapsed) {
    // Collapsed: an icon RAIL. The expand control on top — the ONE reopen
    // control, on every route, its centre on the 24px line the sidebar header
    // and the app bars stand on — then the owner's destination icons.
    return (
      <>
        <nav
          id="sidebar"
          data-collapsed=""
          className={cn(navClass, "items-center py-2.5")}
          style={{ flexBasis: SIDEBAR_RAIL_WIDTH, width: SIDEBAR_RAIL_WIDTH }}
        >
          <div className={cn("flex min-h-0 flex-1 flex-col items-center", fadeIn)}>
            <Button
              variant="ghost"
              size="icon-sm"
              className="text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground"
              aria-label="Expand sidebar"
              title="Expand sidebar"
              onClick={toggleSidebarCollapsed}
            >
              {/* The SAME glyph the collapse button wears: one panel, one toggle. */}
              <PanelIcon side="left" />
            </Button>
            {rail && rail.length > 0 && (
              <div className="mt-2 flex min-h-0 flex-1 flex-col items-center gap-1 border-t border-sidebar-border pt-2">
                {rail.map((item) => {
                  const isActive = item.active ?? location.pathname === item.href;
                  const anchor = (
                    <a
                      href={item.href}
                      className={RAIL_BTN_CLASS}
                      aria-label={item.label}
                      aria-current={isActive ? "page" : undefined}
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
                  );
                  return (
                    <React.Fragment key={item.key}>
                      {item.pinBottom && <span className="flex-1" aria-hidden="true" />}
                      {item.dividerBefore && <span className="my-1 h-px w-7 shrink-0 bg-sidebar-border" aria-hidden="true" />}
                      {item.render ? item.render(anchor) : anchor}
                    </React.Fragment>
                  );
                })}
              </div>
            )}
          </div>
        </nav>
        {handle}
      </>
    );
  }

  return (
    <>
      <nav id="sidebar" className={navClass} style={{ flexBasis: sidebarWidth, width: sidebarWidth }}>
        <div className={cn("flex min-h-0 flex-1 flex-col", fadeIn)}>
          {/* `sidebar-brand` is a JS hook (platform/lib/tours reads it to know
              the expanded sidebar is mounted), not a styled class. */}
          <div className="sidebar-brand flex items-center gap-2 border-b border-sidebar-border px-4 py-3 text-sm font-semibold">
            {/* Logo + name are one click target that goes to the owner's home. */}
            <a
              href={homeHref}
              className="flex min-w-0 items-center gap-2 text-sidebar-foreground no-underline hover:text-muted-foreground"
              title="Home"
              onClick={(e) => {
                e.preventDefault();
                navigateUrl(homeHref);
              }}
            >
              {/* Fused cube mark (brand asset logo-black-bg-transparent.svg). */}
              <span className="flex shrink-0 items-center text-sidebar-foreground">
                <svg width="20" height="20" viewBox="0 0 233 233" fill="none" aria-hidden="true">
                  <path
                    d="M43.916 84.6995L80.0899 105.742M43.916 84.6995L80.0899 64.13M43.916 84.6995V126.548M80.0899 105.742L114.383 125.69C115.548 126.368 116.264 127.613 116.264 128.96V162.056C116.264 164.973 113.101 166.793 110.579 165.326L43.916 126.548M80.0899 105.742V182.862C80.0899 185.779 76.9269 187.598 74.405 186.131L45.7968 169.49C44.6324 168.813 43.916 167.567 43.916 166.22V126.548M80.0899 105.742L152.674 64.13M80.0899 64.13L114.4 44.6204C115.556 43.9629 116.973 43.961 118.131 44.6152L152.674 64.13M80.0899 64.13L150.785 104.659C151.955 105.329 153.392 105.327 154.559 104.652L183.353 88.0121C185.887 86.5475 185.869 82.883 183.321 81.4432L152.674 64.13"
                    stroke="currentColor"
                    strokeWidth="12"
                  />
                </svg>
              </span>
              <span className="min-w-0 truncate">{title}</span>
            </a>
            <Button
              variant="ghost"
              size="icon-xs"
              className="ml-auto shrink-0 text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground"
              aria-label="Collapse sidebar"
              title="Collapse sidebar"
              onClick={toggleSidebarCollapsed}
            >
              <PanelIcon side="left" />
            </Button>
          </div>

          {children}
        </div>
      </nav>
      {/* Resize handle riding the right border: drag to resize, drag PAST the
          floor to collapse (platform/lib/panel-drag), double-press to collapse. */}
      {handle}
    </>
  );
}

// The "Home" row glyph shared by sub-app sidebars — same everywhere so "back to
// my app's front page" reads identically.
export const HOME_ICON = <Home size={16} strokeWidth={2} aria-hidden="true" />;

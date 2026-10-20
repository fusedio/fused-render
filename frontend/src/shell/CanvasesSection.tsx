// "Canvases" — the sidebar section under Projects (owner, 2026-10-20: "same
// functioning section for canvases in sidebar"): the canvas clones on this
// machine, every environment at once, read from the server's own listdir
// (`GET /api/canvases/cloned`, canvases.py) — no CLI call, no login, so the
// section paints with the sidebar. A row opens the canvas's PAGE
// (`/canvases/<env>/<name>`, apps/canvases/CanvasPage.tsx) on the tab the
// reader is already on, the Projects rule ("same view, other canvas").
//
// What it borrows from Projects and what it does not. The running dot: the
// tasks pulse rows already carry a `canvas` stamp, so a canvas whose task is
// in flight lights its row without a second poll. NOT here, yet: the unread
// dot and the drag order — both are SERVER-owned facts on the Projects side
// (opened_at / unread / order in current_apps.json, PR #1031) and would need
// a store of their own for canvases; the list is newest clone first until
// then. No ✕ either: the clone IS the row, so the only honest cross is
// "delete the local clone", which is the owner's to ask for.
//
// A "+" row at the top opens the catalog (/canvases), where create and clone
// already live — the section lists what is here, the catalog is where more
// comes from.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { navigate, navigateUrl } from "@platform/lib/router";
import { useUrlVersion } from "@platform/lib/hooks";
import {
  SECTION_BODY_CLASS,
  useSectionContentCap,
} from "@platform/ui/sidebar/useSectionContentCap";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { archiveCurrentAppTasks, readCurrentAppTasks } from "@platform/lib/api";
import {
  ENV_LABELS,
  canvasProjectFromPath,
  canvasProjectPath,
  canvasPageTabFromSearch,
  listClonedCanvases,
  type ClonedCanvas,
} from "@apps/canvases/api";
import { CanvasGlyph } from "@apps/canvases/glyph";
import { inFlight, opensElsewhere, statusColumn } from "@shell/tasks-lib";
import { pokeTasks, useTasksPulseRows } from "@shell/tasksPulse";

// The section fold, "1" when hidden — the Projects section's own key pattern
// (sidebar layout, not a fact: browser storage holds no facts, D1386).
export const COLLAPSED_KEY = "fused-render:canvases-collapsed";

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

/** The clones the pulse says have a run in flight, as `<env>/<name>`. */
function runningCanvases(rows: ReturnType<typeof useTasksPulseRows>): Set<string> {
  const out = new Set<string>();
  for (const row of rows) {
    if (!row.canvas) continue;
    if (!inFlight(statusColumn(row.status))) continue;
    out.add(row.canvas.env + "/" + row.canvas.name);
  }
  return out;
}

function CanvasRow({
  canvas,
  active,
  running,
  showEnv,
  href,
  onMenu,
}: {
  canvas: ClonedCanvas;
  active: boolean;
  running: boolean;
  /** Both environments hold clones: say which this one is. */
  showEnv: boolean;
  href: string;
  onMenu: (e: React.MouseEvent, canvas: ClonedCanvas) => void;
}) {
  const onOpen = (e: React.MouseEvent<HTMLAnchorElement>) => {
    // Middle/modified clicks keep the browser's own new-tab gesture on the href.
    if (opensElsewhere(e)) return;
    e.preventDefault();
    if (!active) navigateUrl(href);
  };
  return (
    <div
      className={
        "bookmark-row current-app-row canvas-row" +
        (active ? " active" : "") +
        (running ? " is-running" : "")
      }
      title={canvas.dir + " — " + ENV_LABELS[canvas.env] + (running ? " — running" : "")}
      onContextMenu={(e) => onMenu(e, canvas)}
    >
      <span className="bookmark-glyph current-app-glyph canvas-row-glyph" aria-hidden="true">
        <CanvasGlyph size={16} />
      </span>
      <a
        className="bookmark-name"
        href={href}
        draggable={false}
        aria-current={active ? "page" : undefined}
        onClick={onOpen}
      >
        {canvas.name}
      </a>
      {showEnv && (
        <span className="canvas-row-env" aria-hidden="true">
          {ENV_LABELS[canvas.env]}
        </span>
      )}
      {running && (
        <span className="sidebar-rail-dot is-running current-app-running" aria-hidden="true" />
      )}
    </div>
  );
}

export default function CanvasesSection() {
  const [canvases, setCanvases] = useState<ClonedCanvas[]>([]);
  const refetch = useCallback(() => {
    listClonedCanvases()
      .then(setCanvases)
      .catch(() => {
        // A failed read keeps the rows it has; the next refetch shows the truth.
      });
  }, []);
  useEffect(refetch, [refetch]);

  // The active row and the tab the hrefs carry are read at render, re-run on
  // every URL event (the version hook). A new clone arrives through the
  // catalog, which navigates to the canvas page: the same event is the
  // refetch (one listdir), so the row is there as its page opens. A clone
  // made in another window lands on this window's next navigation.
  const urlVersion = useUrlVersion();
  useEffect(() => {
    if (urlVersion > 0) refetch();
  }, [urlVersion, refetch]);
  // Rows open the PROJECT page (owner, 2026-10-20: the sidebar is its entry);
  // the editor is reached from that page's header. Active on the project
  // page alone: the editor lights nothing here, like the catalog row.
  const here = canvasProjectFromPath(location.pathname);
  const tab = here ? canvasPageTabFromSearch(location.search) : undefined;

  const rows = useTasksPulseRows();
  const running = useMemo(() => runningCanvases(rows), [rows]);
  const envs = useMemo(() => new Set(canvases.map((c) => c.env)), [canvases]);

  const [menu, setMenu] = useState<{ x: number; y: number; canvas: ClonedCanvas } | null>(null);
  const onRowMenu = useCallback((e: React.MouseEvent, canvas: ClonedCanvas) => {
    e.preventDefault();
    e.stopPropagation();
    setMenu({ x: e.clientX, y: e.clientY, canvas });
  }, []);
  const menuItems = (canvas: ClonedCanvas): MenuEntry[] => [
    {
      label: "Open in Explorer",
      icon: MenuIcons.folder,
      onClick: () => navigate(canvas.dir, { isDir: true }),
    },
    "separator",
    {
      label: "Mark all tasks as read",
      icon: MenuIcons.unread,
      onClick: () => {
        readCurrentAppTasks(canvas.dir)
          .catch(() => {})
          .finally(() => pokeTasks());
      },
    },
    {
      label: "Archive all tasks",
      icon: MenuIcons.compress,
      onClick: () => {
        archiveCurrentAppTasks(canvas.dir)
          .catch(() => {})
          .finally(() => pokeTasks());
      },
    },
  ];

  const [collapsed, setCollapsed] = useState(readCollapsed);
  const toggleCollapsed = () => {
    const next = !collapsed;
    try {
      localStorage.setItem(COLLAPSED_KEY, next ? "1" : "0");
    } catch {
      // No store: the fold lasts as long as the page does.
    }
    setCollapsed(next);
  };
  // Shares the sidebar's free height with Projects and Bookmarks (each its own
  // scroll; a short or folded one yields its remainder) — the hook writes the
  // content-height cap flexbox needs for that.
  const sectionRef = useRef<HTMLDivElement>(null);
  useSectionContentCap(sectionRef);

  return (
    <div className="sidebar-section sidebar-current-apps sidebar-canvases" ref={sectionRef}>
      <div className={SECTION_BODY_CLASS}>
        <div
          className={
            "sidebar-heading recents-heading current-apps-heading" + (collapsed ? " collapsed" : "")
          }
          title={collapsed ? "Show canvases" : "Hide canvases"}
          onClick={toggleCollapsed}
        >
          Canvases
          <span className="sidebar-heading-chevron" aria-hidden="true" />
          {collapsed && <span className="sidebar-count-chip">{canvases.length}</span>}
        </div>
        {!collapsed && (
          <div
            className="bookmark-row current-app-row current-app-new"
            role="button"
            tabIndex={0}
            title="Open a canvas from the catalog"
            onClick={() => navigateUrl("/canvases")}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                navigateUrl("/canvases");
              }
            }}
          >
            <span className="bookmark-glyph current-app-glyph" aria-hidden="true">
              +
            </span>
            <span className="bookmark-name">Open canvas</span>
          </div>
        )}
        {!collapsed &&
          canvases.map((c) => (
            <CanvasRow
              key={c.env + "/" + c.name}
              canvas={c}
              active={!!here && here.env === c.env && here.name === c.name}
              running={running.has(c.env + "/" + c.name)}
              showEnv={envs.size > 1}
              href={canvasProjectPath(c.env, c.name, tab !== undefined ? { tab } : undefined)}
              onMenu={onRowMenu}
            />
          ))}
      </div>
      {menu && (
        <ContextMenu x={menu.x} y={menu.y} items={menuItems(menu.canvas)} onClose={() => setMenu(null)} />
      )}
    </div>
  );
}

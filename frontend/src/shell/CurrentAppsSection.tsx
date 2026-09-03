// "Current apps" — the sidebar section above Bookmarks (D487, redesigned
// 2026-08-26): the apps on the user's desk, read from a STORE of their own
// (`GET /api/current-apps`, fused_render/current_apps.py) — every kind of app,
// workspace or linked. A row opens the app's PAGE (`/apps/<folder>`,
// shell/AppPage.tsx, D488) — the one door that page has; its cross REMOVES the
// app from the desk and, as the side effect, archives every task under it.
//
// The desk is NOT the task list. A new task puts its app on the desk; nothing
// takes it off but the cross. So this section fetches the table itself, and
// re-fetches when the task pulse shows a task key it has not seen — the only
// event that can add a row. The pulse is still read for one thing: the running
// dot. That is a subscription this sidebar already holds, not a second poll.
//
// The ORDER is a sequence per app, not the added order it is seeded from
// (current-apps-lib.ts): a row moves only when the user drags it, so new work
// does not reshuffle the list under the cursor. The store is the module-level
// `appOrder` below, hydrated from localStorage at import and written back BY A
// DRAG AND ONLY BY A DRAG, so an arrangement survives a reload and the next
// launch without a fetch ever having an opinion about it.
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  archiveCurrentAppTasks,
  getCurrentApps,
  readCurrentAppTasks,
  removeAppIcon,
  removeCurrentApp,
  renameCurrentApp,
  setAppIcon,
  type CurrentAppEntry,
} from "@platform/lib/api";
import IconPicker from "@platform/ui/IconPicker";
import { navigateUrl, urlForFsPath } from "@platform/lib/router";
import { pushToast } from "@platform/lib/toast";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { Modal } from "@platform/ui/modal/Modal";
import { HeroComposer } from "@apps/builder/HomeHero";
import { inFlight, isDoneUnread, opensElsewhere, statusColumn } from "@shell/tasks-lib";
import { pokeTasks, useTasksPulseRows } from "@shell/tasksPulse";
import {
  appPageTabFromSearch,
  appPageUrl,
  appPathFromPath,
  assignSequences,
  bySequence,
  currentApps,
  moveSlug,
  orderedSlugs,
  parseSavedOrder,
  reorderTo,
  type AppOrder,
  type CurrentApp,
} from "@shell/current-apps-lib";

// Bumped from `current-apps-order` with the redesign: the saved list was slugs
// and is folder paths now, and a slug-shaped order would match nothing.
export const ORDER_KEY = "fused-render:current-apps-order:v2";

/** The picked emoji as a standalone icon.svg document — square viewBox, no
 *  fixed size, transparent ground (a colour emoji carries its own colours, so
 *  it reads on both themes; see skills/fused-render-app-icon). The same file
 *  a hand-authored icon.svg would be, just generated. */
function emojiIconSvg(emoji: string): string {
  const safe = emoji
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">' +
    '<text x="32" y="32" text-anchor="middle" dominant-baseline="central" ' +
    `font-size="52">${safe}</text></svg>`
  );
}

// The section fold, "1" when hidden — the Bookmarks section's own key pattern.
export const COLLAPSED_KEY = "fused-render:current-apps-collapsed";

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

// The last table this document fetched, kept at module level so the rows do not
// blink empty on every per-navigation remount of the sidebar while the fetch
// round-trips.
let knownApps: CurrentAppEntry[] = [];

// The displayed order: a module-level Map (it outlives the sidebar's
// per-navigation remount — pushState routing) hydrated from localStorage at
// import, so the order is already in hand before the first render and there is
// no window where the fetch wins a race against what the user dragged.
//
// Every store touch sits inside a try: a blocked or full store costs the saved
// order, not the section. Reading a JSON array of paths (top first) rather than
// the sequence numbers themselves keeps the stored shape the one thing that
// matters — nothing on disk has to agree with a numbering scheme this module is
// free to change.
const appOrder: AppOrder = new Map();

function readSavedOrder(): string[] {
  try {
    return parseSavedOrder(localStorage.getItem(ORDER_KEY));
  } catch {
    return [];
  }
}

// Called from the DROP HANDLER and nowhere else — that placement is the whole
// cross-tab design, so it is worth stating plainly. A persist effect keyed on
// the app list looks equivalent and is not: two tabs then take turns saving
// their own view of a world they briefly disagree about (Bugbot twice,
// 2026-08-26 — first a second tab clobbering a drag, then an outright write
// loop). A drag is one user gesture. There is no second writer to race.
//
// The equality guard is belt-and-braces on top of that: re-dragging a row back
// where it was writes nothing.
function saveOrder(paths: string[]): void {
  try {
    const next = JSON.stringify(paths);
    if (localStorage.getItem(ORDER_KEY) === next) return;
    localStorage.setItem(ORDER_KEY, next);
  } catch {
    // A blocked store just means the order lasts as long as the page does.
  }
}

/** Take `paths` as the whole order, replacing what this page held. Empty is NOT
 *  an order — a missing or cleared key must leave the live order alone rather
 *  than flattening it. A live app the incoming list does not mention gets a
 *  fresh sequence and goes on top, which is correct: the tab that dragged did
 *  not have that app, so its arrangement has nothing to say about where it
 *  belongs. Nothing answers back — adopting never writes. */
function adoptSavedOrder(slugs: string[]): void {
  if (!slugs.length) return;
  appOrder.clear();
  reorderTo(appOrder, slugs);
}

// Mounted sections, so another tab's drag can repaint this one.
const orderListeners = new Set<() => void>();

try {
  adoptSavedOrder(readSavedOrder());
  // `storage` fires only in OTHER documents, which makes it exactly the
  // cross-tab channel — the same wiring App.tsx uses to hear the chat's
  // activity stamp. Without it the two tabs disagree until a reload.
  window.addEventListener("storage", (e: StorageEvent) => {
    if (e.key !== ORDER_KEY) return;
    adoptSavedOrder(parseSavedOrder(e.newValue));
    for (const listener of orderListeners) listener();
  });
} catch {
  // No store and no window: the order lives and dies with this page.
}

/** The desk's table, fetched on mount and again whenever `signal` changes —
 *  the caller passes the pulse's set of task keys, since a task this document
 *  has not seen is the one thing that can add a row. Errors keep the last
 *  answer: a failed read is not an empty desk. */
function useCurrentApps(signal: string, refreshEpoch: number): CurrentAppEntry[] {
  const [apps, setApps] = useState<CurrentAppEntry[]>(knownApps);
  useEffect(() => {
    let live = true;
    getCurrentApps().then(
      (r) => {
        if (!live) return;
        knownApps = r.apps ?? [];
        setApps(knownApps);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [signal, refreshEpoch]);
  return apps;
}

interface RowDragProps {
  onDragStart: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragOver: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragLeave: (e: React.DragEvent<HTMLDivElement>) => void;
  onDrop: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragEnd: () => void;
}

function CurrentAppRow({
  app,
  active,
  drag,
  onRemoved,
  onGlyphClick,
  onMenu,
}: {
  app: CurrentApp;
  active: boolean;
  drag: RowDragProps;
  onRemoved: () => void;
  onGlyphClick: (e: React.MouseEvent<HTMLSpanElement>, path: string) => void;
  onMenu: (e: React.MouseEvent, app: CurrentApp) => void;
}) {
  const [busy, setBusy] = useState(false);
  // The destination keeps the TAB the user is on (owner, 2026-08-26): switching
  // apps from the Files tab lands on the next app's Files tab, so the sidebar
  // reads as "same view, other app". Only `_tab` rides along — a tab's own
  // params (`?file=`, `?view=`) name things inside ONE app and are dropped.
  // Off an app page the default tab it is. Read at render: the sidebar
  // remounts on every navigation (App.tsx), so the href cannot go stale.
  const onAppPage = appPathFromPath(location.pathname) !== null;
  const tab = onAppPage ? appPageTabFromSearch(location.search) : undefined;
  const href = appPageUrl(app.path, tab);
  const onOpen = (e: React.MouseEvent<HTMLAnchorElement>) => {
    // Middle/modified clicks keep the browser's own new-tab gesture on the href.
    if (opensElsewhere(e)) return;
    e.preventDefault();
    // The row for the page already on screen, on the tab it already shows, is
    // a no-op; the tab's own params would be the only thing the click cleared.
    if (!active) navigateUrl(href);
  };
  const onRemove = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (busy) return;
    setBusy(true);
    try {
      // One call: the server drops the row AND archives every task under the
      // folder (the side effect the owner asked for). The tasks surfaces learn
      // through the poke; the desk through the refetch the caller runs.
      await removeCurrentApp(app.path);
    } catch {
      // A failed remove leaves the row; the refetch below shows the truth.
    } finally {
      setBusy(false);
      pokeTasks();
      onRemoved();
    }
  };
  const tip =
    app.path +
    (app.kind === "linked" ? " — linked app" : "") +
    (app.exists ? "" : " — folder missing") +
    (app.running ? " — running" : "");
  return (
    <div
      className={
        "bookmark-row current-app-row" +
        (active ? " active" : "") +
        (app.exists ? "" : " is-missing") +
        (app.running ? " is-running" : "") +
        (app.unread && !app.running ? " is-unread" : "")
      }
      title={tip}
      draggable
      onContextMenu={(e) => onMenu(e, app)}
      {...drag}
    >
      {/* The glyph is the icon picker's toggle — the Bookmarks pattern
          (BookmarksSection.onBookmarkGlyphClick), except the pick lands on
          disk as the folder's icon.svg rather than in the bookmarks tree. */}
      {/* `current-app-icon-toggle` marks the glyphs that toggle THIS
          section's picker — the selector the IconPicker below whitelists.
          The "+ New app" glyph deliberately lacks it, so a click there
          closes an open picker instead of leaving it under the modal. */}
      <span
        className="bookmark-glyph current-app-glyph current-app-icon-toggle"
        title="Change icon"
        onClick={(e) => onGlyphClick(e, app.path)}
      >
        {app.iconUrl ? (
          // The app's own icon.svg in the generic mark's slot, drawn as is —
          // the author's colours, no mask or tint (owner, 2026-08-27). Not
          // draggable: an <img> drags natively, and the glyph is the natural
          // handle for the row reorder (same as the name's draggable={false}).
          <img
            className="current-app-icon"
            src={app.iconUrl}
            alt=""
            draggable={false}
          />
        ) : (
          // The brand's four-point star (the app icon's sparkle) as the
          // generic mark, on currentColor so it follows the glyph's tokens
          // (muted at rest, accent on the active row) — the SidebarFrame
          // cube's own posture.
          <svg
            className="current-app-star"
            width="12"
            height="12"
            viewBox="0 0 64 64"
            fill="currentColor"
            aria-hidden="true"
          >
            <path d="M32 2 C36.5 20.5 43.5 27.5 62 32 C43.5 36.5 36.5 43.5 32 62 C27.5 43.5 20.5 36.5 2 32 C20.5 27.5 27.5 20.5 32 2 Z" />
          </svg>
        )}
      </span>
      <a
        className="bookmark-name"
        href={href}
        draggable={false}
        aria-current={active ? "page" : undefined}
        onClick={onOpen}
      >
        {app.name}
      </a>
      {/* The running dot sits AFTER the name (owner, 2026-08-27), so it never
          covers the app's icon: the glyph slot is identity, the dot is state. */}
      {app.running && (
        <span
          className="sidebar-rail-dot is-running current-app-running"
          aria-hidden="true"
        />
      )}
      {/* The unread dot: a task under this app finished and has not been read —
          the Tasks row's green, worn per app, in the running dot's own slot
          after the name (owner, 2026-08-31). Yellow outranks green (one dot per
          row, the Tasks rule), so it hides while anything runs; it clears when
          the task is read, since it draws the raw doneUnread state, not a
          visit-stamped one. */}
      {app.unread && !app.running && (
        <span
          className="sidebar-rail-dot is-unread current-app-unread"
          aria-hidden="true"
        />
      )}
      <span className="bookmark-actions">
        <button
          className="icon-btn delete-btn current-app-archive"
          title="Hide from projects (archives its tasks)"
          aria-label={`Hide ${app.name} from projects and archive its tasks`}
          disabled={busy}
          onClick={onRemove}
        >
          ✕
        </button>
      </span>
    </div>
  );
}

export default function CurrentAppsSection() {
  const rows = useTasksPulseRows();
  // The set of task keys, as one string: it changes exactly when a task
  // appears or leaves, and a new task is the only thing that can add an app.
  // Order-independent (sorted) so a re-sorted pulse does not refetch.
  const keySignal = useMemo(
    () =>
      rows
        .map((r) => r.key)
        .sort()
        .join("\n"),
    [rows],
  );
  const runningProjects = useMemo(
    () => rows.filter((r) => inFlight(statusColumn(r.status))).map((r) => r.project || ""),
    [rows],
  );
  // The projects with a finished-and-unread task — the raw doneUnread state
  // the Tasks row's count chip reads, not the visit-stamped `unseen`: a dot
  // per app clears by the task being READ, not by glancing at some page.
  const unreadProjects = useMemo(
    () => rows.filter(isDoneUnread).map((r) => r.project || ""),
    [rows],
  );
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  const entries = useCurrentApps(keySignal, refreshEpoch);
  // A drop mutates `appOrder`, which React cannot see; this counter is what
  // turns that mutation into a render.
  const [orderEpoch, setOrderEpoch] = useState(0);
  const apps = useMemo(() => {
    const found = currentApps(entries, runningProjects, unreadProjects);
    // Assigning during render is safe because it is idempotent: an app that
    // already has a sequence keeps it, so a double-invoked render (StrictMode)
    // or a re-run on the same rows cannot renumber anything.
    assignSequences(appOrder, found);
    return bySequence(found, appOrder);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- orderEpoch is the drag signal
  }, [entries, runningProjects, unreadProjects, orderEpoch]);
  // NOTHING is saved here. A new app, a removed one, a fetch landing — all of
  // those move rows on screen and write nothing to the store; the saved order is
  // an arrangement the user made, and only they can change it.

  // Repaint when another tab drags. The adopt already happened at the module
  // listener; this is only the render half of it.
  useEffect(() => {
    const bump = () => setOrderEpoch((n) => n + 1);
    orderListeners.add(bump);
    return () => {
      orderListeners.delete(bump);
    };
  }, []);

  // Which row is the page on screen. Read at render: the sidebar remounts on
  // every navigation (App.tsx), so a stale read cannot outlive a route change.
  const onPath = appPathFromPath(location.pathname);

  // ---- reordering by drag ----------------------------------------------------
  // A flat list, so the only question a drop asks is "above or below this row",
  // answered by the row's own midpoint. Deliberately NOT the bookmarks tree's
  // machinery (BookmarksSection): no folders, no subtree guard, no drop-into.
  // The zone and fade CLASSES are that section's, though — the rows already
  // carry `bookmark-row`, so `.dragging` / `.drag-above` / `.drag-below` are
  // painted by sidebar.css with nothing new added.
  const draggedRef = useRef<string | null>(null);
  const clearDrag = () => {
    const marks = ["drag-above", "drag-below", "dragging"];
    const sel = marks.map((m) => `.current-app-row.${m}`).join(", ");
    document
      .querySelectorAll(sel)
      .forEach((el) => el.classList.remove(...marks));
  };
  const isBelow = (e: React.DragEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return e.clientY > r.top + r.height / 2;
  };
  const dragProps = (path: string): RowDragProps => ({
    onDragStart: (e) => {
      draggedRef.current = path;
      e.currentTarget.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", path); // Firefox needs a payload to start
    },
    onDragOver: (e) => {
      const from = draggedRef.current;
      if (from === null || from === path) return;
      e.preventDefault(); // required to allow a drop
      e.dataTransfer.dropEffect = "move";
      const after = isBelow(e);
      e.currentTarget.classList.toggle("drag-above", !after);
      e.currentTarget.classList.toggle("drag-below", after);
    },
    onDragLeave: (e) =>
      e.currentTarget.classList.remove("drag-above", "drag-below"),
    onDrop: (e) => {
      const from = draggedRef.current;
      // Reset BEFORE the re-render: it detaches the source row, and Chrome
      // skips dragend on a removed element (the lesson BookmarksSection
      // records at its own drop handler).
      draggedRef.current = null;
      clearDrag();
      if (from === null || from === path) return;
      e.preventDefault();
      // Moved within the WHOLE store, not the visible run. They are the same
      // list — the store is pruned to the desk on every assignment.
      const next = moveSlug(orderedSlugs(appOrder), from, path, isBelow(e));
      reorderTo(appOrder, next);
      saveOrder(next);
      setOrderEpoch((n) => n + 1);
    },
    onDragEnd: () => {
      // Fires on an Escape-cancelled drag too — the universal cleanup.
      draggedRef.current = null;
      clearDrag();
    },
  });

  const refetch = useCallback(() => setRefreshEpoch((n) => n + 1), []);

  // ---- the icon picker -------------------------------------------------------
  // The glyph toggles the Bookmarks' emoji picker (IconPicker), anchored to
  // itself. A pick is wrapped in a standalone svg and written to the folder's
  // icon.svg (POST /api/apps/icon) — the file the row and the tab favicon
  // already read — and the refetch brings back the new mtime, which is what
  // busts the <img> cache. Remove deletes the file; the row falls back to the
  // generic mark.
  const [iconPicker, setIconPicker] = useState<{
    path: string;
    top: number;
    left: number;
  } | null>(null);
  const onGlyphClick = useCallback(
    (e: React.MouseEvent<HTMLSpanElement>, path: string) => {
      e.preventDefault();
      e.stopPropagation();
      const rect = e.currentTarget.getBoundingClientRect();
      setIconPicker((cur) =>
        cur?.path === path ? null : { path, top: rect.top, left: rect.left },
      );
    },
    [],
  );
  const onPickIcon = async (icon: string | null) => {
    const target = iconPicker;
    setIconPicker(null);
    if (!target) return;
    try {
      if (icon === null) await removeAppIcon(target.path);
      else await setAppIcon(target.path, emojiIconSvg(icon));
    } catch {
      // A failed write leaves the old glyph; the refetch shows the truth.
    }
    refetch();
  };

  // ---- the row's right-click menu ---------------------------------------------
  // "Open app" is the app page header's own button (AppPage.tsx) — the entry
  // page in a new tab. The rest are the desk's own verbs — the dot, the
  // tasks, the row itself.
  const [menu, setMenu] = useState<{
    x: number;
    y: number;
    app: CurrentApp;
  } | null>(null);
  const onRowMenu = useCallback((e: React.MouseEvent, app: CurrentApp) => {
    e.preventDefault();
    e.stopPropagation();
    setMenu({ x: e.clientX, y: e.clientY, app });
  }, []);

  // The rename dialog: prefilled with the folder's current name; submit renames
  // the FOLDER on disk and the server carries the app's sessions and stores
  // along (the D548 move settlement).
  const [renaming, setRenaming] = useState<CurrentApp | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);
  const startRename = (app: CurrentApp) => {
    setRenameDraft(app.name);
    setRenaming(app);
  };
  const submitRename = async () => {
    const app = renaming;
    const name = renameDraft.trim();
    if (!app || renameBusy) return;
    if (!name || name === app.name) {
      setRenaming(null);
      return;
    }
    setRenameBusy(true);
    try {
      const r = await renameCurrentApp(app.path, name);
      // Carry the row's SEQUENCE to the new path in memory, so the rename does
      // not reshuffle the list (assignSequences would put an unknown path on
      // top). The OLD path's entry is left in place: the fetched table still
      // names it until the refetch lands, and deleting it early hands the row
      // a fresh top-of-list sequence in that window (Bugbot). The prune on the
      // next assignment drops it. Deliberately NOT saved — the store is
      // written by a drag and only by a drag (the cross-tab rule above).
      const seq = appOrder.get(app.path);
      if (seq !== undefined && !appOrder.has(r.path)) {
        appOrder.set(r.path, seq);
      }
      setRenaming(null);
      pokeTasks();
      // Refetch UNCONDITIONALLY — the desk row changed either way. Then, if
      // we are on the renamed app's page, follow it to the new folder.
      refetch();
      if (app.path === onPath) {
        navigateUrl(appPageUrl(r.path, appPageTabFromSearch(location.search)));
      }
    } catch (e) {
      pushToast({
        msg: "Could not rename " + app.name + ": " + (e as Error).message,
        tone: "error",
      });
    } finally {
      setRenameBusy(false);
    }
  };

  const menuItems = (app: CurrentApp): MenuEntry[] => [
    {
      // The app page header's own "Open app": the entry page full-size in the
      // explorer, in a new tab so the current page stays put.
      label: "Open app",
      icon: MenuIcons.open,
      disabled: !app.exists || !app.entry,
      onClick: () => {
        if (app.entry) window.open(urlForFsPath(app.entry), "_blank", "noopener");
      },
    },
    {
      label: "Rename…",
      icon: MenuIcons.rename,
      disabled: !app.exists,
      onClick: () => startRename(app),
    },
    "separator",
    {
      label: "Mark all tasks as read",
      onClick: () => {
        readCurrentAppTasks(app.path)
          .catch(() => {})
          .finally(() => pokeTasks());
      },
    },
    {
      label: "Archive all tasks",
      icon: MenuIcons.compress,
      onClick: () => {
        archiveCurrentAppTasks(app.path)
          .catch(() => {})
          .finally(() => pokeTasks());
      },
    },
    "separator",
    // The ✕'s gesture, by name: off the desk, tasks archived with it.
    {
      label: "Hide from projects",
      icon: MenuIcons.trash,
      danger: true,
      onClick: () => {
        removeCurrentApp(app.path)
          .catch(() => {})
          .finally(() => {
            pokeTasks();
            refetch();
          });
      },
    },
  ];

  const render = useCallback(
    (app: CurrentApp) => (
      <CurrentAppRow
        key={app.path}
        app={app}
        active={app.path === onPath}
        drag={dragProps(app.path)}
        onRemoved={refetch}
        onGlyphClick={onGlyphClick}
        onMenu={onRowMenu}
      />
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dragProps closes over `apps`
    [onPath, apps, refetch, onGlyphClick, onRowMenu],
  );
  // The "+ New app" row at the foot of the list opens the /apps composer in a
  // modal (D489). The section ALWAYS renders: a door to "make one" is exactly
  // what an empty desk wants.
  const [composing, setComposing] = useState(false);
  // Whole-section fold, the Bookmarks heading's pattern: local to this machine
  // (localStorage) — sidebar layout, not desk data. The count chip carries the
  // collapsed signal, no chevron.
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
  return (
    <div className="sidebar-section sidebar-current-apps">
      <div
        className={
          "sidebar-heading recents-heading current-apps-heading" +
          (collapsed ? " collapsed" : "")
        }
        title={collapsed ? "Show projects" : "Hide projects"}
        onClick={toggleCollapsed}
      >
        Projects
        <span className="sidebar-heading-chevron" aria-hidden="true" />
        {collapsed && <span className="sidebar-count-chip">{apps.length}</span>}
      </div>
      {!collapsed && apps.map(render)}
      {!collapsed && (
        <div
          className="bookmark-row current-app-row current-app-new"
          role="button"
          tabIndex={0}
          title="New app"
          onClick={() => setComposing(true)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              setComposing(true);
            }
          }}
        >
          <span className="bookmark-glyph current-app-glyph" aria-hidden="true">
            +
          </span>
          <span className="bookmark-name">New app</span>
        </div>
      )}
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={menuItems(menu.app)}
          onClose={() => setMenu(null)}
        />
      )}
      {renaming && (
        <Modal
          title={"Rename " + renaming.name}
          busy={renameBusy}
          onClose={() => setRenaming(null)}
          width={420}
          footer={
            <>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={renameBusy}
                onClick={() => setRenaming(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={renameBusy || !renameDraft.trim()}
                onClick={submitRename}
              >
                {renameBusy ? "Renaming…" : "Rename"}
              </button>
            </>
          }
        >
          <p>
            Renames the app&apos;s folder on disk. Its tasks and Claude
            sessions move with it.
          </p>
          <input
            type="text"
            className="field-control"
            value={renameDraft}
            autoFocus
            onFocus={(e) => e.currentTarget.select()}
            onChange={(e) => setRenameDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void submitRename();
              }
            }}
          />
        </Modal>
      )}
      {iconPicker && (
        <IconPicker
          anchor={iconPicker}
          toggleSelector=".current-app-icon-toggle"
          onPick={(icon) => onPickIcon(icon)}
          onRemove={() => onPickIcon(null)}
          onClose={() => setIconPicker(null)}
        />
      )}
      {composing && (
        // The SAME composer /apps and /home show (apps/builder/HomeHero.tsx):
        // it names, scaffolds and navigates into the new app's chat itself,
        // and that navigation remounts the sidebar (App.tsx), which is what
        // unmounts this modal. `onCreated` closes it for the case where the
        // composer stays put (no chat run started).
        <Modal
          title="New app"
          onClose={() => setComposing(false)}
          width={640}
          dialogClassName="current-apps-compose"
          // The composer arrives with its own skin (chips, pickers, the round
          // send button); the chassis' form vocabulary would re-style every
          // button in it. Owner: "we should not be redesigning anything".
          plainBody
        >
          <HeroComposer onCreated={() => setComposing(false)} />
        </Modal>
      )}
    </div>
  );
}

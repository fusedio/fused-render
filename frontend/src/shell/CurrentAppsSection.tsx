// "Projects" — the sidebar section above Bookmarks (D487, "Current apps" until
// 2026-08-26): the apps on the user's desk, read from a STORE of their own
// (`GET /api/current-apps`, fused_render/current_apps.py). A row opens the app's
// PAGE (`/apps/<folder>`, shell/AppPage.tsx, D488); its cross REMOVES the app
// from the desk and, as the side effect, archives every task under it.
//
// The desk is NOT the task list. A new task puts its app on the desk; nothing
// takes it off but the cross. So this section fetches the table itself, and
// re-fetches when the task pulse shows a task key it has not seen. The pulse is
// still read for the running/unread dots — a subscription this sidebar already
// holds, not a second poll.
//
// The ORDER is a sequence per app (current-apps-lib.ts): a row moves only when
// the user drags it. The store is the module-level `appOrder` below, hydrated
// from localStorage at import and written back BY A DRAG AND ONLY BY A DRAG.
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ChevronRight, Plus, Sparkle, X } from "lucide-react";
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
import { cn } from "@platform/lib/utils";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { StatusDot } from "@platform/ui/flow/StatusIcon";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@platform/shadcn/ui/dialog";
import { Input } from "@platform/shadcn/ui/input";
import { HeroComposer } from "@apps/builder/HomeHero";
import { isDoneUnread, opensElsewhere } from "@shell/tasks-lib";
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
 *  fixed size, transparent ground. The same file a hand-authored icon.svg
 *  would be, just generated. */
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
// blink empty on every per-navigation remount of the sidebar.
let knownApps: CurrentAppEntry[] = [];

// The displayed order: a module-level Map hydrated from localStorage at import,
// so the order is in hand before the first render. Every store touch sits
// inside a try: a blocked store costs the saved order, not the section.
const appOrder: AppOrder = new Map();

function readSavedOrder(): string[] {
  try {
    return parseSavedOrder(localStorage.getItem(ORDER_KEY));
  } catch {
    return [];
  }
}

// Called from the DROP HANDLER and nowhere else — that placement is the whole
// cross-tab design: a persist effect keyed on the app list would have two tabs
// take turns saving their own view of a world they briefly disagree about. A
// drag is one user gesture; there is no second writer to race.
function saveOrder(paths: string[]): void {
  try {
    const next = JSON.stringify(paths);
    if (localStorage.getItem(ORDER_KEY) === next) return;
    localStorage.setItem(ORDER_KEY, next);
  } catch {
    // A blocked store just means the order lasts as long as the page does.
  }
}

/** Take `paths` as the whole order. Empty is NOT an order. Adopting never writes. */
function adoptSavedOrder(slugs: string[]): void {
  if (!slugs.length) return;
  appOrder.clear();
  reorderTo(appOrder, slugs);
}

// Mounted sections, so another tab's drag can repaint this one.
const orderListeners = new Set<() => void>();

try {
  adoptSavedOrder(readSavedOrder());
  // `storage` fires only in OTHER documents — exactly the cross-tab channel.
  window.addEventListener("storage", (e: StorageEvent) => {
    if (e.key !== ORDER_KEY) return;
    adoptSavedOrder(parseSavedOrder(e.newValue));
    for (const listener of orderListeners) listener();
  });
} catch {
  // No store and no window: the order lives and dies with this page.
}

/** The desk's table, fetched on mount and again whenever `signal` changes.
 *  Errors keep the last answer: a failed read is not an empty desk. */
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

// The desk's row chrome, shared by every project row and the "+ New app" door:
// a 14px glyph slot, the name, then whatever trails. Drag marks ride data
// attributes (`data-drag`, `data-dragging`) painted here, so the drop handler
// can toggle them without a stylesheet.
const ROW_CLASS =
  "group/row relative flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm cursor-pointer select-none hover:bg-sidebar-accent motion-safe:transition-colors motion-safe:duration-100 data-[active]:bg-sidebar-accent data-[dragging]:opacity-40 data-[drag=above]:[box-shadow:0_-2px_0_0_var(--ring)] data-[drag=below]:[box-shadow:0_2px_0_0_var(--ring)]";
const GLYPH_CLASS = "flex size-3.5 shrink-0 items-center justify-center text-muted-foreground";

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
  onGlyphClick: (e: React.MouseEvent<HTMLElement>, path: string) => void;
  onMenu: (e: React.MouseEvent, app: CurrentApp) => void;
}) {
  const [busy, setBusy] = useState(false);
  // The destination keeps the TAB the user is on (owner, 2026-08-26): switching
  // apps from the Files tab lands on the next app's Files tab. Only `_tab`
  // rides along. Read at render: the sidebar remounts on every navigation.
  const onAppPage = appPathFromPath(location.pathname) !== null;
  const tab = onAppPage ? appPageTabFromSearch(location.search) : undefined;
  const href = appPageUrl(app.path, tab);
  const onOpen = (e: React.MouseEvent<HTMLAnchorElement>) => {
    // Middle/modified clicks keep the browser's own new-tab gesture on the href.
    if (opensElsewhere(e)) return;
    e.preventDefault();
    // The row for the page already on screen is a no-op.
    if (!active) navigateUrl(href);
  };
  const onRemove = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (busy) return;
    setBusy(true);
    try {
      // One call: the server drops the row AND archives every task under the
      // folder. The tasks surfaces learn through the poke; the desk through
      // the refetch the caller runs.
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
      className={ROW_CLASS}
      data-active={active ? "" : undefined}
      title={tip}
      draggable
      onContextMenu={(e) => onMenu(e, app)}
      {...drag}
    >
      {/* The glyph is the icon picker's toggle — the Bookmarks pattern, except
          the pick lands on disk as the folder's icon.svg. `current-app-icon-
          toggle` is the selector IconPicker whitelists (a JS hook, not a style);
          the "+ New app" glyph deliberately lacks it. */}
      <button
        type="button"
        className={cn(GLYPH_CLASS, "current-app-icon-toggle relative z-10 cursor-pointer border-0 bg-transparent p-0")}
        title="Change icon"
        aria-label={`Change icon for ${app.name}`}
        onClick={(e) => onGlyphClick(e, app.path)}
      >
        {app.iconUrl ? (
          // The app's own icon.svg, drawn as is — the author's colours, no
          // tint (owner, 2026-08-27). Not draggable: the glyph is the natural
          // handle for the row reorder.
          <img className="block size-3.5 object-contain" src={app.iconUrl} alt="" draggable={false} />
        ) : (
          // The brand's four-point star as the generic mark.
          <Sparkle size={12} fill="currentColor" aria-hidden="true" />
        )}
      </button>
      {/* `data-sidebar-row` is the arrow-key chain's hook (sidebarArrowNav.ts);
          the ::after-stretched link makes the whole row the click target. */}
      <a
        className={cn(
          "min-w-0 flex-1 truncate text-sidebar-foreground no-underline after:absolute after:inset-0 after:content-[''] focus-visible:outline-none",
          !app.exists && "line-through opacity-55",
        )}
        data-sidebar-row=""
        href={href}
        draggable={false}
        aria-current={active ? "page" : undefined}
        onClick={onOpen}
      >
        {app.name}
      </a>
      {/* The state dot sits AFTER the name (owner, 2026-08-27) so it never
          covers the identity glyph. Yellow outranks green (one dot per row): the
          unread dot hides while anything runs, and clears when the task is read
          — it draws the raw doneUnread state, not a visit-stamped one. */}
      {app.running && <StatusDot bucket="yellow" label="running" className="relative z-10" />}
      {app.unread && !app.running && <StatusDot bucket="green" label="finished, unread" className="relative z-10" />}
      {/* Hover-revealed cross — `invisible` keeps it out of the tab order while
          hidden, the fade rides opacity. */}
      <Button
        variant="ghost"
        size="icon-xs"
        className="relative z-10 -my-1 -mr-1.5 size-5 shrink-0 text-muted-foreground opacity-0 invisible group-hover/row:visible group-hover/row:opacity-100 focus-visible:visible focus-visible:opacity-100 hover:text-foreground motion-safe:transition-opacity"
        title="Hide from projects (archives its tasks)"
        aria-label={`Hide ${app.name} from projects and archive its tasks`}
        disabled={busy}
        onClick={onRemove}
      >
        <X className="size-3" />
      </Button>
    </div>
  );
}

export default function CurrentAppsSection() {
  const rows = useTasksPulseRows();
  // The set of task keys, as one string: it changes exactly when a task
  // appears or leaves, and a new task is the only thing that can add an app.
  const keySignal = useMemo(
    () =>
      rows
        .map((r) => r.key)
        .sort()
        .join("\n"),
    [rows],
  );
  const runningProjects = useMemo(
    () => rows.filter((r) => r.status === "in_progress").map((r) => r.project || ""),
    [rows],
  );
  // The projects with a finished-and-unread task — the raw doneUnread state,
  // not the visit-stamped `unseen`: a dot per app clears by the task being READ.
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
    // Assigning during render is safe because it is idempotent.
    assignSequences(appOrder, found);
    return bySequence(found, appOrder);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- orderEpoch is the drag signal
  }, [entries, runningProjects, unreadProjects, orderEpoch]);
  // NOTHING is saved here: the saved order is an arrangement the user made.

  // Repaint when another tab drags.
  useEffect(() => {
    const bump = () => setOrderEpoch((n) => n + 1);
    orderListeners.add(bump);
    return () => {
      orderListeners.delete(bump);
    };
  }, []);

  // Which row is the page on screen. Read at render: the sidebar remounts on
  // every navigation, so a stale read cannot outlive a route change.
  const onPath = appPathFromPath(location.pathname);

  // ---- reordering by drag ----------------------------------------------------
  // A flat list, so the only question a drop asks is "above or below this row",
  // answered by the row's own midpoint. The marks are data attributes the row
  // class paints (`ROW_CLASS`).
  const draggedRef = useRef<string | null>(null);
  const clearDrag = () => {
    document
      .querySelectorAll<HTMLElement>("[data-sidebar-row]")
      .forEach((a) => {
        const row = a.parentElement;
        if (!row) return;
        row.removeAttribute("data-drag");
        row.removeAttribute("data-dragging");
      });
  };
  const isBelow = (e: React.DragEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return e.clientY > r.top + r.height / 2;
  };
  const dragProps = (path: string): RowDragProps => ({
    onDragStart: (e) => {
      draggedRef.current = path;
      e.currentTarget.setAttribute("data-dragging", "");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", path); // Firefox needs a payload to start
    },
    onDragOver: (e) => {
      const from = draggedRef.current;
      if (from === null || from === path) return;
      e.preventDefault(); // required to allow a drop
      e.dataTransfer.dropEffect = "move";
      e.currentTarget.setAttribute("data-drag", isBelow(e) ? "below" : "above");
    },
    onDragLeave: (e) => e.currentTarget.removeAttribute("data-drag"),
    onDrop: (e) => {
      const from = draggedRef.current;
      // Reset BEFORE the re-render: it detaches the source row, and Chrome
      // skips dragend on a removed element.
      draggedRef.current = null;
      clearDrag();
      if (from === null || from === path) return;
      e.preventDefault();
      // Moved within the WHOLE store, not the visible run — they are the same
      // list, since the store is pruned to the desk on every assignment.
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
  // A pick is wrapped in a standalone svg and written to the folder's icon.svg
  // (POST /api/apps/icon); the refetch brings back the new mtime, which busts
  // the <img> cache. Remove deletes the file; the row falls back to the mark.
  const [iconPicker, setIconPicker] = useState<{
    path: string;
    top: number;
    left: number;
  } | null>(null);
  const onGlyphClick = useCallback(
    (e: React.MouseEvent<HTMLElement>, path: string) => {
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

  // The rename dialog: submit renames the FOLDER on disk and the server carries
  // the app's sessions and stores along (the D548 move settlement).
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
      // not reshuffle the list. The OLD path's entry is left for the prune.
      // Deliberately NOT saved — the store is written by a drag only.
      const seq = appOrder.get(app.path);
      if (seq !== undefined && !appOrder.has(r.path)) {
        appOrder.set(r.path, seq);
      }
      setRenaming(null);
      pokeTasks();
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
      // The app page header's own "Open app": the entry page full-size, in a
      // new tab so the current page stays put.
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
  // dialog (D489). The section ALWAYS renders: a door to "make one" is exactly
  // what an empty desk wants.
  const [composing, setComposing] = useState(false);
  // Whole-section fold, the Bookmarks heading's pattern: local to this machine.
  // The count chip carries the collapsed signal beside the chevron.
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
    <div className="flex shrink-0 flex-col gap-px p-2 pt-0">
      {/* A foldable section heading: the sm/semibold/uppercase/muted scale, with
          a chevron that says "this folds" — right when collapsed, down when open. */}
      <button
        type="button"
        className="flex w-full cursor-pointer select-none items-center gap-1.5 rounded-md border-0 bg-transparent px-2.5 pb-1 pt-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground hover:text-foreground"
        title={collapsed ? "Show projects" : "Hide projects"}
        aria-expanded={!collapsed}
        onClick={toggleCollapsed}
      >
        Projects
        <ChevronRight
          size={12}
          aria-hidden="true"
          className={cn("shrink-0 motion-safe:transition-transform motion-safe:duration-100", !collapsed && "rotate-90")}
        />
        {collapsed && (
          <Badge variant="secondary" className="ml-auto h-4 px-1.5 text-xs tabular-nums">
            {apps.length}
          </Badge>
        )}
      </button>
      {!collapsed && apps.map(render)}
      {!collapsed && (
        // Muted so it reads as a door rather than another app, full colour on hover.
        <div
          className={cn(ROW_CLASS, "text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring")}
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
          <span className={cn(GLYPH_CLASS, "text-current")} aria-hidden="true">
            <Plus size={14} />
          </span>
          <span className="min-w-0 flex-1 truncate">New app</span>
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
      <Dialog open={renaming !== null} onOpenChange={(o) => !o && !renameBusy && setRenaming(null)}>
        {renaming && (
          <DialogContent className="sm:max-w-[420px]">
            <DialogHeader>
              <DialogTitle>Rename {renaming.name}</DialogTitle>
              <DialogDescription>
                Renames the app&apos;s folder on disk. Its tasks and Claude sessions move with it.
              </DialogDescription>
            </DialogHeader>
            <Input
              type="text"
              value={renameDraft}
              autoFocus
              disabled={renameBusy}
              onFocus={(e) => e.currentTarget.select()}
              onChange={(e) => setRenameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void submitRename();
                }
              }}
            />
            <DialogFooter>
              <Button variant="outline" disabled={renameBusy} onClick={() => setRenaming(null)}>
                Cancel
              </Button>
              <Button disabled={renameBusy || !renameDraft.trim()} onClick={submitRename}>
                {renameBusy ? "Renaming…" : "Rename"}
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
      {iconPicker && (
        <IconPicker
          anchor={iconPicker}
          toggleSelector=".current-app-icon-toggle"
          onPick={(icon) => onPickIcon(icon)}
          onRemove={() => onPickIcon(null)}
          onClose={() => setIconPicker(null)}
        />
      )}
      {/* The SAME composer /apps and /home show (apps/builder/HomeHero.tsx): it
          names, scaffolds and navigates into the new app's chat itself, and that
          navigation remounts the sidebar, which unmounts this dialog.
          `onCreated` closes it for the case where the composer stays put. */}
      <Dialog open={composing} onOpenChange={(o) => !o && setComposing(false)}>
        {composing && (
          <DialogContent className="sm:max-w-[640px]">
            <DialogHeader>
              <DialogTitle>New app</DialogTitle>
            </DialogHeader>
            <HeroComposer onCreated={() => setComposing(false)} />
          </DialogContent>
        )}
      </Dialog>
    </div>
  );
}

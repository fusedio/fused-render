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
  getCurrentApps,
  removeCurrentApp,
  type CurrentAppEntry,
} from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { Modal } from "@platform/ui/modal/Modal";
import { HeroComposer } from "@apps/builder/HomeHero";
import { opensElsewhere } from "@shell/tasks-lib";
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
}: {
  app: CurrentApp;
  active: boolean;
  drag: RowDragProps;
  onRemoved: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const href = appPageUrl(app.path);
  const onOpen = (e: React.MouseEvent<HTMLAnchorElement>) => {
    // Middle/modified clicks keep the browser's own new-tab gesture on the href.
    if (opensElsewhere(e)) return;
    e.preventDefault();
    // `active` is folder-only (the row lights up on either tab); the
    // destination is the OVERVIEW, so from the Tasks tab the click still goes —
    // it is how the sidebar gets back to the running app. Only a click that
    // would land exactly where the page already is stays a no-op.
    if (!active || appPageTabFromSearch(location.search) !== "overview")
      navigateUrl(href);
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
        (app.exists ? "" : " is-missing")
      }
      title={tip}
      draggable
      {...drag}
    >
      <span className="bookmark-glyph current-app-glyph" aria-hidden="true">
        {app.running ? <span className="sidebar-rail-dot is-running" /> : "▣"}
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
      <span className="bookmark-actions">
        <button
          className="icon-btn delete-btn current-app-archive"
          title="Remove from current apps (archives its tasks)"
          aria-label={`Remove ${app.name} from current apps and archive its tasks`}
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
    () => rows.filter((r) => r.status === "in_progress").map((r) => r.project || ""),
    [rows],
  );
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  const entries = useCurrentApps(keySignal, refreshEpoch);
  // A drop mutates `appOrder`, which React cannot see; this counter is what
  // turns that mutation into a render.
  const [orderEpoch, setOrderEpoch] = useState(0);
  const apps = useMemo(() => {
    const found = currentApps(entries, runningProjects);
    // Assigning during render is safe because it is idempotent: an app that
    // already has a sequence keeps it, so a double-invoked render (StrictMode)
    // or a re-run on the same rows cannot renumber anything.
    assignSequences(appOrder, found);
    return bySequence(found, appOrder);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- orderEpoch is the drag signal
  }, [entries, runningProjects, orderEpoch]);
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
  const render = useCallback(
    (app: CurrentApp) => (
      <CurrentAppRow
        key={app.path}
        app={app}
        active={app.path === onPath}
        drag={dragProps(app.path)}
        onRemoved={refetch}
      />
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dragProps closes over `apps`
    [onPath, apps, refetch],
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

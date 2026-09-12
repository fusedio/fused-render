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
  openCurrentApp,
  readCurrentAppTasks,
  removeCurrentApp,
  renameCurrentApp,
  type CurrentAppEntry,
} from "@platform/lib/api";
import { applyIconPick } from "@platform/lib/app-icon";
import { AppStar } from "@platform/ui/AppStar";
import IconPicker, { type IconPick } from "@platform/ui/IconPicker";
import { embedUrlForFsPath, navigateUrl } from "@platform/lib/router";
import { notify } from "@platform/lib/notifications";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { Modal } from "@platform/ui/modal/Modal";
import { HeroComposer } from "@apps/builder/HomeHero";
import { inFlight, isQueued, opensElsewhere, queuedLabel, statusColumn } from "@shell/tasks-lib";
import { pokeTasks, useTasksPulseRows } from "@shell/tasksPulse";
import { CURRENT_APPS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { useThemedIconSrc } from "@platform/lib/app-icon-src";
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

// A cross-window nudge, not a store: set to the stamp after POST
// /api/current-apps/open lands, so the other windows' sections refetch the
// desk (the server row is the truth; this only says "look again").
export const DESK_CHANGED_KEY = "fused-render:current-apps-changed";

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
 *  the caller passes a digest of the task pulse (`pulseSignal`), since a task
 *  appearing adds a row and a task finishing flips a row's `unread`, both on
 *  the server. Errors keep the last answer: a failed read is not an empty desk. */
function useCurrentApps(
  signal: string,
  refreshEpoch: number,
): { entries: CurrentAppEntry[]; adopt: (apps: CurrentAppEntry[]) => void } {
  const [apps, setApps] = useState<CurrentAppEntry[]>(knownApps);
  // Every table this hook shows is SEQUENCED: a read applies only if nothing
  // newer was issued while it was in flight. Without this a slow fetch started
  // before an open could land after the open's answer and put the pre-stamp
  // row — dot and all — back on screen (Bugbot, 2026-09-07). Latest issued
  // wins; a stale answer is dropped, not merged.
  const seq = useRef(0);
  useEffect(() => {
    const mine = ++seq.current;
    getCurrentApps().then(
      (r) => {
        if (mine !== seq.current) return;
        knownApps = r.apps ?? [];
        setApps(knownApps);
      },
      () => {},
    );
  }, [signal, refreshEpoch]);
  // A table handed in from elsewhere — the open's answer carries one — takes
  // the newest sequence, so any read still in flight is stale by definition.
  const adopt = useCallback((next: CurrentAppEntry[]) => {
    seq.current++;
    knownApps = next;
    setApps(next);
  }, []);
  return { entries: apps, adopt };
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
  onSeen,
}: {
  app: CurrentApp;
  active: boolean;
  drag: RowDragProps;
  onRemoved: () => void;
  onGlyphClick: (e: React.MouseEvent<HTMLSpanElement>, path: string) => void;
  onMenu: (e: React.MouseEvent, app: CurrentApp) => void;
  /** The user opened this app — stamp its completions seen (clears the dot). */
  onSeen: (path: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const iconSrc = useThemedIconSrc(app.iconUrl);
  // The destination keeps the TAB the user is on (owner, 2026-08-26): switching
  // apps from the Files tab lands on the next app's Files tab, so the sidebar
  // reads as "same view, other app". Only `_tab` rides along — a tab's own
  // params (`?file=`, `?view=`) name things inside ONE app and are dropped.
  // Off an app page the default tab it is. Read at render: the sidebar
  // remounts on every navigation (App.tsx), so the href cannot go stale.
  const onAppPage = appPathFromPath(location.pathname) !== null;
  const tab = onAppPage ? appPageTabFromSearch(location.search) : undefined;
  const href = appPageUrl(app.path, tab);
  // The dot clears on the OPEN gesture, whatever the tasks under the app say
  // (owner, 2026-09-07) — and on the row already active, since a completion
  // landing while the user is on the app's page is one they are looking at.
  // Safe to key on `app.unread`: onSeen hides the dot at once and a failed
  // POST leaves it hidden (see onSeen), so this cannot re-fire into a retry loop.
  useEffect(() => {
    if (active && app.unread) onSeen(app.path);
  }, [active, app.unread, app.path, onSeen]);
  const onOpen = (e: React.MouseEvent<HTMLAnchorElement>) => {
    // Middle/modified clicks keep the browser's own new-tab gesture on the href.
    if (opensElsewhere(e)) return;
    e.preventDefault();
    onSeen(app.path);
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
    (app.running ? " — running" : "") +
    (app.queued > 0 ? " — " + queuedLabel(app.queued) : "");
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
        {iconSrc ? (
          // The app's own icon.svg in the generic mark's slot, drawn as is —
          // the author's colours, no mask or tint (owner, 2026-08-27); a
          // picker-written glyph names its colour and useThemedIconSrc
          // resolves it for the live theme. Not draggable: an <img> drags
          // natively, and the glyph is the natural handle for the row reorder
          // (same as the name's draggable={false}).
          <img
            className="current-app-icon"
            src={iconSrc}
            alt=""
            draggable={false}
          />
        ) : (
          // The brand's four-point star (AppStar — the app icon's sparkle, the
          // same drawing the /apps cards and the app page's header show) as
          // the generic mark, on currentColor so it follows the glyph's tokens
          // (muted at rest, accent on the active row) — the SidebarFrame
          // cube's own posture.
          <AppStar className="current-app-star" width={12} height={12} />
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
      {/* The unread dot: a task under this app finished since the user last
          opened it — the Tasks row's green, worn per app, in the running dot's
          own slot after the name (owner, 2026-08-31). Yellow outranks green
          (one dot per row, the Tasks rule), so it hides while anything runs.
          It clears when the APP is opened, not when the task is read — the
          app's own state (owner, 2026-09-07; current-apps-lib.projectUnread). */}
      {app.unread && !app.running && (
        <span
          className="sidebar-rail-dot is-unread current-app-unread"
          aria-hidden="true"
        />
      )}
      {/* "· 2 queued" — work asked for in this folder that is waiting on the run
          in it (the project queue). WORDS AND NOT A DOT, which is the whole
          difference from the two marks above: a dot says "something is true
          here" and one is already spoken for by the running state, while what a
          reader wants from a queue is the NUMBER. It is drawn beside the running
          dot rather than instead of it — the two are different facts about one
          folder, and a folder with a run and a line behind it should say both.
          Nothing at all at 0, so a machine with the queue off grows no ink. */}
      {app.queued > 0 && (
        <span className="current-app-queued">{"· " + queuedLabel(app.queued)}</span>
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
  // When to refetch the desk: a digest of the pulse — per task its key, lane,
  // read state and activity — so the table reloads when a task appears or
  // leaves (the one thing that adds a row) AND when one changes lane or speaks
  // (the things that flip a row's `unread` on the server, which is computed
  // inside the same listing the pulse reads). The activity term is
  // `happened_at`, the clock `observe` itself compares — NOT `last_active`,
  // which keeps a scheduled due time and so does not move when a recurring
  // run finishes early (Bugbot, 2026-09-07). Sorted, so a re-ordered pulse
  // does not refetch.
  const pulseSignal = useMemo(
    () =>
      rows
        .map((r) => `${r.key} ${r.status} ${r.happened_at ?? 0}`)
        .sort()
        .join("\n"),
    [rows],
  );
  const runningProjects = useMemo(
    () => rows.filter((r) => inFlight(statusColumn(r.status))).map((r) => r.project || ""),
    [rows],
  );
  // The same pulse rows read for the other half of the sentence: tasks WAITING
  // on their folder (the project queue). A separate memo rather than one pass
  // producing both, for the reason `runningProjects` beside it is its own memo:
  // one list, one question, and neither has to know the other's filter.
  //
  // IT DOES NOT PRESERVE ARRAY IDENTITY and never claimed to be able to. The
  // pulse publishes a fresh `tasks` array on every poll (`tasksPulse.ts`), so
  // this memo and its neighbour both rebuild four times a minute whatever they
  // return — which is why the thing the desk's refetch is keyed on is
  // `pulseSignal`, a STRING digest, and not either of these arrays. (An earlier
  // comment here asserted the opposite; nothing was ever built on it.)
  const queuedProjects = useMemo(
    () => rows.filter((r) => isQueued(r)).map((r) => r.project || ""),
    [rows],
  );
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  const { entries, adopt } = useCurrentApps(pulseSignal, refreshEpoch);
  // A drop mutates `appOrder`, which React cannot see; this counter is what
  // turns that mutation into a render.
  const [orderEpoch, setOrderEpoch] = useState(0);
  // The rows the user opened in THIS window whose open the server has not yet
  // answered — so the dot dies on the click rather than on the refetch. A path
  // leaves the set the moment the POST's answer (which carries the stamped
  // table) is adopted (`onSeen`): from then on the server's flag is drawn as
  // is, so a completion after the stamp — or an `observe` that raced the open
  // — shows rather than being masked for the page's lifetime (Bugbot,
  // 2026-09-07).
  const [clearedHere, setClearedHere] = useState<ReadonlySet<string>>(() => new Set());
  const uncover = useCallback(
    (path: string) =>
      setClearedHere((s) => {
        if (!s.has(path)) return s;
        const next = new Set(s);
        next.delete(path);
        return next;
      }),
    [],
  );
  const apps = useMemo(() => {
    const found = currentApps(entries, runningProjects, clearedHere, queuedProjects);
    // Assigning during render is safe because it is idempotent: an app that
    // already has a sequence keeps it, so a double-invoked render (StrictMode)
    // or a re-run on the same rows cannot renumber anything.
    assignSequences(appOrder, found);
    return bySequence(found, appOrder);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- orderEpoch is the drag signal
  }, [entries, runningProjects, queuedProjects, clearedHere, orderEpoch]);
  // The user opened `path`: clear the dot here at once, tell the server (POST
  // /api/current-apps/open — the row's `opened_at` and `unread` are its to
  // keep), then read the table back and hand the row to the server's flag.
  const refetch = useCallback(() => setRefreshEpoch((n) => n + 1), []);
  const onSeen = useCallback(
    (path: string) => {
      setClearedHere((s) => new Set(s).add(path));
      openCurrentApp(path).then(
        async (r) => {
          // Tell the OTHER windows the desk changed: `storage` fires only in
          // other documents (the ORDER_KEY wiring above, the chat's activity
          // stamp), and their sections refetch on it. Without this a second
          // window keeps the dot until something else makes it refetch (Bugbot,
          // 2026-09-07). Value is the stamp so two opens in one second still
          // differ; a blocked store just means no cross-window nudge.
          try {
            localStorage.setItem(DESK_CHANGED_KEY, String(r.opened_at));
          } catch {
            /* no store: this window is up to date, the others catch up on their own */
          }
          // The answer carries the table after the stamp: adopt it and lift the
          // cover in the same tick. No second read to race, and `adopt` takes
          // the newest sequence so a fetch still in flight from before the POST
          // is dropped rather than putting the pre-stamp row back.
          adopt(r.apps ?? []);
          uncover(path);
        },
        () => {
          // The server never heard. The dot stays hidden for this page — it
          // comes back on the next load, since the server still says unread.
          // Deliberately NOT restored: the active-row effect above keys on
          // `app.unread`, and a restore would re-fire it into a retry loop at
          // network-error cadence.
        },
      );
    },
    [adopt, uncover],
  );
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

  // The explorer's "Open in project" button adds a row (POST
  // /api/current-apps/add) and announces it over the window — apps cannot
  // import this section — so the new row lands on top as its page opens,
  // rather than on the next task pulse (platform/lib/tasksChanged).
  // The same nudge from ANOTHER window (DESK_CHANGED_KEY): an open there
  // stamped a row on the server, and this window's dot has to follow.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === DESK_CHANGED_KEY) refetch();
    };
    window.addEventListener(CURRENT_APPS_CHANGED_EVENT, refetch);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(CURRENT_APPS_CHANGED_EVENT, refetch);
      window.removeEventListener("storage", onStorage);
    };
  }, [refetch]);

  // ---- the icon picker -------------------------------------------------------
  // The glyph toggles the shared IconPicker (emoji + branded lucide icons),
  // anchored to itself. A pick is written as a standalone svg to the folder's
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
  // Closing is the picker's own call (it stays open on a shuffle), so this
  // only writes.
  const onPickIcon = async (pick: IconPick | null) => {
    const target = iconPicker;
    if (!target) return;
    try {
      // The pick → disk rule is shared with the app page's header mark
      // (platform/lib/app-icon): remove, or store the svg an icon pick
      // arrives as, or wrap an emoji in one.
      await applyIconPick(target.path, pick);
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
      notify({
        title: "Could not rename " + app.name + ": " + (e as Error).message,
        tone: "error",
      });
    } finally {
      setRenameBusy(false);
    }
  };

  const menuItems = (app: CurrentApp): MenuEntry[] => [
    {
      // The app page header's own "Open app": the entry page full-size AS AN
      // APP — embed mode, chrome-free — in a new tab so the current page stays
      // put. Same URL as AppPage's button; the two must not diverge.
      label: "Open app",
      icon: MenuIcons.open,
      disabled: !app.exists || !app.entry,
      onClick: () => {
        if (app.entry) window.open(embedUrlForFsPath(app.entry), "_blank", "noopener");
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
      icon: MenuIcons.unread,
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
        onSeen={onSeen}
      />
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dragProps closes over `apps`
    [onPath, apps, refetch, onGlyphClick, onRowMenu, onSeen],
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
          onPick={(pick) => onPickIcon(pick)}
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

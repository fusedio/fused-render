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
// The ORDER is the server's: `apps` arrives in display order and is drawn as it
// arrives (current-apps-lib.ts, D1386). A drag POSTs the whole list and adopts
// the answer; a new app is inserted on top by the server. Nothing here numbers,
// sorts or remembers an order of its own.
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
  setCurrentAppsOrder,
  type CurrentAppEntry,
} from "@platform/lib/api";
import { setSidebarSectionOpen, useSidebarSectionOpen } from "@platform/ui/sidebar/sidebar-accordion";
import SectionFold from "@platform/ui/sidebar/SectionFold";
import { applyIconPick } from "@platform/lib/app-icon";
import { AppStar } from "@platform/ui/AppStar";
import IconPicker, { type IconPick } from "@platform/ui/IconPicker";
import {
  SECTION_BODY_CLASS,
  useSectionContentCap,
} from "@platform/ui/sidebar/useSectionContentCap";
import { navigate, navigateUrl } from "@platform/lib/router";
import { notify } from "@platform/lib/notifications";
import { exportAppFileOnly, openShareApp } from "@platform/lib/share-app";
import { openUninstallApp } from "@platform/lib/uninstall-app";
import { useAppSharingFeature } from "@platform/lib/share-app-flag";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { Modal } from "@platform/ui/modal/Modal";
import { HeroComposer } from "@apps/builder/HomeHero";
import { inFlight, isQueued, liveJobRow, opensElsewhere, queuedLabel, statusColumn } from "@shell/tasks-lib";
import { pokeTasks, useTasksPulseRows } from "@shell/tasksPulse";
import { CURRENT_APPS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { isRasterIconUrl, useThemedIconSrc } from "@platform/lib/app-icon-src";
import {
  appPageTabFromSearch,
  appPageUrl,
  appPathFromPath,
  currentApps,
  moveSlug,
  parseSavedOrder,
  type CurrentApp,
} from "@shell/current-apps-lib";
import {
  ENV_LABELS,
  canvasNameFromPath,
  canvasPagePath,
  canvasPageTabFromSearch,
  canvasProjectFromPath,
  canvasProjectPath,
  isWorkbenchEnv,
} from "@apps/canvases/api";
import { CanvasGlyph } from "@apps/canvases/glyph";

/** Which half of the desk a section draws (owner, 2026-10-20: "add all
 *  options on canvas sidebar as we have on folder sidebar"). ONE store, one
 *  table (`GET /api/current-apps`), one row component and one set of verbs
 *  — the cross, the drag, the dots, the menu — split on the row's `canvas`
 *  stamp: `projects` draws the app folders, `canvases` the canvas clones.
 *  A drag posts only its own rows; `set_order` keeps the unnamed ones ahead
 *  in their relative order, so each section arranges its half alone. */
export type DeskSection = "projects" | "canvases";

function inSection(app: { canvas: unknown }, section: DeskSection): boolean {
  return (app.canvas !== null) === (section === "canvases");
}

// A cross-window nudge, not a store: set to the stamp after POST
// /api/current-apps/open (or a drag's POST /api/current-apps/order) lands, so
// the other windows' sections refetch the desk (the server row is the truth;
// this only says "look again").
export const DESK_CHANGED_KEY = "fused-render:current-apps-changed";

// The last table this document fetched, kept at module level so the rows do not
// blink empty on every per-navigation remount of the sidebar while the fetch
// round-trips.
let knownApps: CurrentAppEntry[] = [];

/** Tell the OTHER windows the desk changed — `storage` fires only in other
 *  documents, and their sections refetch on it. The value is a stamp so two
 *  changes in one second still differ. */
function announceDeskChanged(stamp: number): void {
  try {
    localStorage.setItem(DESK_CHANGED_KEY, String(stamp));
  } catch {
    /* no store: this window is up to date, the others catch up on their own */
  }
}

// ---- the one-shot order import (D1386) -------------------------------------
// The order used to live in localStorage, which is per ORIGIN — and the
// desktop app walks ports 1777..1787 then an ephemeral one, so a relaunch on a
// new port started from an empty store and lost it. The old key is read ONCE
// on the first load of this build and, if it still has something to say,
// pushed through the normal write path. (The section fold's old key is
// imported with the rest of the layout, by platform/lib/ui-layout.)
//
// "If it still has something to say" is the whole gate. The port walk leaves
// a key behind on EVERY origin the user ever ran on, each frozen at whatever
// they dragged there — so the first of those origins to load this build would
// otherwise POST a stale arrangement over the one the user made since, and a
// later visit to another old port would do it again. D1386 keeps no
// server-side "already imported" marker, so the signal has to be derivable
// from the table itself: a desk nobody has arranged is in ADDED order, newest
// first (the server inserts new apps at index 0, and an old oldest-first file
// is read back reversed server-side, so it is newest-first too). The legacy
// list goes up only while the server's rows still equal that order; any other
// order means someone already arranged it here, and the old key just goes.
//
// The key is removed only after that decision lands — the POST's success, or
// the skip. A failed GET or POST leaves it, so the next load decides again.
const LEGACY_ORDER_KEY = "fused-render:current-apps-order:v2";

/** True while `apps` is in the order nobody chose: `added_at` descending, rows
 *  without one last, ties keeping their place. */
function neverArranged(apps: CurrentAppEntry[]): boolean {
  const added = apps
    .map((a, i) => ({ path: a.path, at: a.added_at, i }))
    .sort((x, y) => {
      if (x.at === null || y.at === null) {
        if (x.at === y.at) return x.i - y.i;
        return x.at === null ? 1 : -1;
      }
      return y.at - x.at || x.i - y.i;
    });
  return added.every((a, i) => a.path === apps[i].path);
}

function dropLegacyOrder(): void {
  try {
    localStorage.removeItem(LEGACY_ORDER_KEY);
  } catch {
    /* removal failed: the next load decides again, harmless */
  }
}

try {
  const legacy = parseSavedOrder(localStorage.getItem(LEGACY_ORDER_KEY));
  if (!legacy.length) {
    localStorage.removeItem(LEGACY_ORDER_KEY);
  } else {
    getCurrentApps()
      .then((current) => {
        if (!neverArranged(current.apps ?? [])) {
          dropLegacyOrder();
          return;
        }
        return setCurrentAppsOrder(legacy).then((r) => {
          dropLegacyOrder();
          knownApps = r.apps ?? knownApps;
          // The section's first GET was in flight beside this POST and may
          // have answered with the pre-import order. Same-document `storage`
          // events do not fire, so THIS window's sections are poked over the
          // window event they already refetch on, the other windows over the
          // nudge.
          window.dispatchEvent(new Event(CURRENT_APPS_CHANGED_EVENT));
          announceDeskChanged(Date.now());
        });
      })
      .catch(() => {
        /* server unreachable or refused: keep the key, the next load retries */
      });
  }
} catch {
  // No store and no window: nothing was ever saved here to import.
}

/** The desk's table, fetched on mount and again whenever `signal` changes —
 *  the caller passes a digest of the task pulse (`pulseSignal`), since a task
 *  appearing adds a row and a task finishing flips a row's `unread`, both on
 *  the server. Errors keep the last answer: a failed read is not an empty desk.
 *  `enabled` false skips the fetch (the rail's copy while the sidebar is open). */
function useCurrentApps(
  signal: string,
  refreshEpoch: number,
  enabled = true,
): { entries: CurrentAppEntry[]; adopt: (apps: CurrentAppEntry[]) => void } {
  const [apps, setApps] = useState<CurrentAppEntry[]>(knownApps);
  // Every table this hook shows is SEQUENCED: a read applies only if nothing
  // newer was issued while it was in flight. Without this a slow fetch started
  // before an open could land after the open's answer and put the pre-stamp
  // row — dot and all — back on screen (Bugbot, 2026-09-07). Latest issued
  // wins; a stale answer is dropped, not merged.
  const seq = useRef(0);
  useEffect(() => {
    if (!enabled) return;
    const mine = ++seq.current;
    getCurrentApps().then(
      (r) => {
        if (mine !== seq.current) return;
        knownApps = r.apps ?? [];
        setApps(knownApps);
      },
      () => {},
    );
  }, [signal, refreshEpoch, enabled]);
  // A table handed in from elsewhere — the open's answer carries one — takes
  // the newest sequence, so any read still in flight is stale by definition.
  const adopt = useCallback((next: CurrentAppEntry[]) => {
    seq.current++;
    knownApps = next;
    setApps(next);
  }, []);
  return { entries: apps, adopt };
}

/** The desk in the order the sections show it — for the collapsed rail's app
 *  and canvas icons. Fetches only while `enabled` (the rail is on screen), on
 *  mount and on the same add/open/drag nudges the sections hear; no
 *  pulse-driven refetch, and no running/queued/unread state (the rail draws
 *  icons only). The order is the server's array as fetched (D1386) — the same
 *  one the sections draw; the rail splits it on `canvas` as they do
 *  (sidebar-rail-lib railExtras). */
export function useDeskAppsOrdered(enabled: boolean): CurrentApp[] {
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  const { entries } = useCurrentApps("", refreshEpoch, enabled);
  useEffect(() => {
    if (!enabled) return;
    const refetch = () => setRefreshEpoch((n) => n + 1);
    const onStorage = (e: StorageEvent) => {
      if (e.key === DESK_CHANGED_KEY) refetch();
    };
    window.addEventListener(CURRENT_APPS_CHANGED_EVENT, refetch);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(CURRENT_APPS_CHANGED_EVENT, refetch);
      window.removeEventListener("storage", onStorage);
    };
  }, [enabled]);
  // Expanded: the rail is not drawn and `entries` is the snapshot of the last
  // collapse.
  if (!enabled) return [];
  return currentApps(entries, [], new Set(), []);
}

interface RowDragProps {
  onDragStart: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragOver: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragLeave: (e: React.DragEvent<HTMLDivElement>) => void;
  onDrop: (e: React.DragEvent<HTMLDivElement>) => void;
  onDragEnd: () => void;
}

/** A canvas row's door: the canvas's PROJECT page (/canvases-project/…), on
 *  the tab the reader is on when they are already on a canvas project page
 *  ("same view, other canvas", the Projects rule below). Null for a clone of
 *  an unknown environment, which has no project page — the caller falls back
 *  to the folder's app page. Exported for the collapsed rail's canvas doors
 *  (GlobalSidebar), the same door as the row. */
export function canvasRowHref(app: CurrentApp): string | null {
  const c = app.canvas;
  if (!c || !isWorkbenchEnv(c.env)) return null;
  const onProject = canvasProjectFromPath(location.pathname) !== null;
  const tab = onProject ? canvasPageTabFromSearch(location.search) : undefined;
  return canvasProjectPath(c.env, c.name, tab !== undefined ? { tab } : undefined);
}

function CurrentAppRow({
  app,
  active,
  drag,
  showEnv,
  onRemoved,
  onGlyphClick,
  onMenu,
  onSeen,
}: {
  app: CurrentApp;
  active: boolean;
  drag: RowDragProps;
  /** A canvas row in a section listing both environments: say which. */
  showEnv: boolean;
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
  // A canvas row's door is the canvas's project page, same rule.
  const onAppPage = appPathFromPath(location.pathname) !== null;
  const tab = onAppPage ? appPageTabFromSearch(location.search) : undefined;
  const href = canvasRowHref(app) ?? appPageUrl(app.path, tab);
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
    // A canvas row lights on its EDITOR too (isActive), but its door is the
    // PROJECT page: lit by the editor it still navigates — otherwise the
    // sidebar offers no way from the editor to the project view (owner,
    // 2026-10-22). Lit by the project page, `onCanvas` resolved from that
    // path, so active ⇒ this canvas's project page ⇒ at the door.
    const atDoor = active && (!app.canvas || canvasProjectFromPath(location.pathname) !== null);
    if (!atDoor) navigateUrl(href);
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
    (app.canvas && isWorkbenchEnv(app.canvas.env) ? " — " + ENV_LABELS[app.canvas.env] : "") +
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
      {app.canvas ? (
        // A canvas wears the canvas glyph, fixed: its identity is the
        // workbench's, not a file in the clone to pick an icon for.
        <span className="bookmark-glyph current-app-glyph canvas-row-glyph" aria-hidden="true">
          <CanvasGlyph size={16} />
        </span>
      ) : (
      <span
        className="bookmark-glyph current-app-glyph current-app-icon-toggle"
        title="Change icon"
        onClick={(e) => onGlyphClick(e, app.path)}
      >
        {iconSrc ? (
          // The app's own icon.svg in the generic mark's slot, drawn as is —
          // the author's colours, no mask or tint (owner, 2026-08-27); a
          // picker-written glyph names its colour and useThemedIconSrc
          // resolves it for the live theme. An icon.png (the raster fallback)
          // is the one exception: `is-raster` clips it to the rounded square
          // (sidebar.css), since a photo-like file has no plate of its own.
          // Not draggable: an <img> drags natively, and the glyph is the
          // natural handle for the row reorder (same as the name's
          // draggable={false}).
          <img
            className={
              "current-app-icon" + (isRasterIconUrl(app.iconUrl) ? " is-raster" : "")
            }
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
          <AppStar className="current-app-star" width={16} height={16} />
        )}
      </span>
      )}
      <a
        className="bookmark-name"
        href={href}
        draggable={false}
        aria-current={active ? "page" : undefined}
        // Two environments can hold a clone of one name: the visible tag is
        // aria-hidden, so the link's own name says which (CodeRabbit).
        aria-label={
          showEnv && app.canvas && isWorkbenchEnv(app.canvas.env)
            ? `${app.name} (${ENV_LABELS[app.canvas.env]})`
            : undefined
        }
        onClick={onOpen}
      >
        {app.name}
      </a>
      {showEnv && app.canvas && isWorkbenchEnv(app.canvas.env) && (
        <span className="canvas-row-env" aria-hidden="true">
          {ENV_LABELS[app.canvas.env]}
        </span>
      )}
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
          title={`Hide from ${app.canvas ? "canvases" : "projects"} (archives its tasks)`}
          aria-label={`Hide ${app.name} from ${app.canvas ? "canvases" : "projects"} and archive its tasks`}
          disabled={busy}
          onClick={onRemove}
        >
          ✕
        </button>
      </span>
    </div>
  );
}

export default function CurrentAppsSection({ section = "projects" }: { section?: DeskSection } = {}) {
  const canvases = section === "canvases";
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
    () => rows.filter((r) => inFlight(statusColumn(r.status)) || liveJobRow(r)).map((r) => r.project || ""),
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
  // Drawn in the order `entries` arrive: the server's array IS the display
  // order (D1386), so there is nothing to sort here.
  // This section's HALF of the desk (`inSection`): the other half is the
  // other section's, drawn from the same table.
  const apps = useMemo(
    () =>
      currentApps(entries, runningProjects, clearedHere, queuedProjects).filter((a) =>
        inSection(a, section),
      ),
    [entries, runningProjects, queuedProjects, clearedHere, section],
  );
  // Both environments on the desk: each canvas row says which it is.
  const showEnv = useMemo(
    () => canvases && new Set(apps.map((a) => a.canvas?.env)).size > 1,
    [apps, canvases],
  );
  // The user opened `path`: clear the dot here at once, tell the server (POST
  // /api/current-apps/open — the row's `opened_at` and `unread` are its to
  // keep), then read the table back and hand the row to the server's flag.
  const refetch = useCallback(() => setRefreshEpoch((n) => n + 1), []);
  const onSeen = useCallback(
    (path: string) => {
      setClearedHere((s) => new Set(s).add(path));
      openCurrentApp(path).then(
        async (r) => {
          // Tell the OTHER windows the desk changed. Without this a second
          // window keeps the dot until something else makes it refetch (Bugbot,
          // 2026-09-07).
          announceDeskChanged(r.opened_at);
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
  // NOTHING is saved from here. A new app, a removed one, a fetch landing — all
  // of those are the server's own writes; the order POST below is sent by a
  // drag and only by a drag. A persist effect keyed on the app list looks
  // equivalent and is not: two windows then take turns saving their own view
  // of a world they briefly disagree about (Bugbot twice, 2026-08-26 — first a
  // second tab clobbering a drag, then an outright write loop). A drag is one
  // user gesture; there is no second writer to race.

  // Which row is the page on screen. Read at render: the sidebar remounts on
  // every navigation (App.tsx), so a stale read cannot outlive a route change.
  // For a canvas row: its project page OR its editor (both are "the canvas
  // open"), so the dot clears on either and the row lights on either.
  const onPath = appPathFromPath(location.pathname);
  const onCanvas = canvasProjectFromPath(location.pathname) ?? canvasNameFromPath(location.pathname);
  const isActive = (app: CurrentApp): boolean =>
    app.canvas
      ? !!onCanvas && onCanvas.env === app.canvas.env && onCanvas.name === app.canvas.name
      : app.path === onPath;

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
      // The whole list, rearranged, goes back in one POST. The rows move at
      // once (the table re-sliced into the new order and adopted — newest
      // sequence, so a read in flight from before the drop cannot put the old
      // order back), then the POST's answer, the server's table in its new
      // order, is adopted over it. A failed POST refetches, which puts the
      // rows back where the server still has them. Re-dropping a row where it
      // already sits sends nothing.
      const paths = apps.map((a) => a.path);
      const next = moveSlug(paths, from, path, isBelow(e));
      if (next.every((p, i) => p === paths[i])) return;
      const byPath = new Map(entries.map((en) => [en.path, en]));
      adopt(next.flatMap((p) => byPath.get(p) ?? []));
      setCurrentAppsOrder(next).then(
        (r) => {
          adopt(r.apps ?? []);
          announceDeskChanged(Date.now());
        },
        () => refetch(),
      );
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
  // "Open in Explorer" is the app card menu's own entry (appCardMenu.ts) — the
  // app's folder as a listing. The rest are the desk's own verbs — the dot, the
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
      // The row keeps its place: the server rewrites the entry where it stands
      // in the stored array, so the refetch below brings it back in the same
      // slot under its new path.
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

  // Share sits right under "Open in Explorer": the same flag-branched entry the app
  // card menu carries (appCardMenu.ts) — the unified sheet with the flag on,
  // the plain `.fused` export with it off. A hook, not the sync getter: this
  // menu is built inside a component, so it follows the flag live.
  const sharing = useAppSharingFeature();
  // A canvas row's verbs: the clone's listing, the canvas editor (the two
  // panes with Claude), the task verbs, and the cross by name. No Share /
  // Export / Rename / Uninstall: a canvas is the workbench's to name, share
  // and delete; the clone is a window onto it.
  const canvasMenuItems = (app: CurrentApp): MenuEntry[] => [
    {
      label: "Open in Explorer",
      icon: MenuIcons.folder,
      disabled: !app.exists,
      onClick: () => navigate(app.path, { isDir: true }),
    },
    {
      label: "Open canvas editor",
      icon: MenuIcons.rename,
      disabled: !app.canvas || !isWorkbenchEnv(app.canvas.env),
      onClick: () => {
        if (app.canvas && isWorkbenchEnv(app.canvas.env)) {
          navigateUrl(canvasPagePath(app.canvas.env, app.canvas.name));
        }
      },
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
    {
      label: "Hide from canvases",
      icon: MenuIcons.trash,
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
  const menuItems = (app: CurrentApp): MenuEntry[] => canvases ? canvasMenuItems(app) : [
    {
      // The app's ENTRY PAGE in the explorer — the file view, crumb bar and
      // all — not the bare folder listing (owner, 2026-09-21: "open in
      // explorer should open the index.html not only the folder"). The
      // record already carries `entry`; a folder with no page falls back to
      // its listing, the rule appEntry.ts (D269) applies to app cards.
      label: "Open in Explorer",
      icon: MenuIcons.folder,
      disabled: !app.exists,
      onClick: () =>
        app.entry ? navigate(app.entry) : navigate(app.path, { isDir: true }),
    },
    sharing
      ? {
          label: "Share…",
          icon: MenuIcons.share,
          disabled: !app.exists,
          onClick: () => openShareApp({ path: app.path, name: app.name }),
        }
      : {
          label: "Export App File",
          icon: MenuIcons.download,
          disabled: !app.exists,
          onClick: () => void exportAppFileOnly({ path: app.path, name: app.name }),
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
      onClick: () => {
        removeCurrentApp(app.path)
          .catch(() => {})
          .finally(() => {
            pokeTasks();
            refetch();
          });
      },
    },
    // Off EVERY list and, by the user's choice, off disk — the shared dialog
    // says which first (UninstallAppModal, same entry as the app card menu).
    {
      label: "Uninstall…",
      icon: MenuIcons.trash,
      danger: true,
      onClick: () => openUninstallApp({ path: app.path, name: app.name }),
    },
  ];

  const render = useCallback(
    (app: CurrentApp) => (
      <CurrentAppRow
        key={app.path}
        app={app}
        active={isActive(app)}
        drag={dragProps(app.path)}
        showEnv={showEnv}
        onRemoved={refetch}
        onGlyphClick={onGlyphClick}
        onMenu={onRowMenu}
        onSeen={onSeen}
      />
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dragProps and isActive close over `apps` / the location
    [onPath, onCanvas, apps, showEnv, refetch, onGlyphClick, onRowMenu, onSeen],
  );
  // The "+ New app" row at the foot of the list opens the /apps composer in a
  // modal (D489). The section ALWAYS renders: a door to "make one" is exactly
  // what an empty desk wants.
  const [composing, setComposing] = useState(false);
  // Whole-section fold: the sidebar's sections are an accordion (one open at a
  // time, platform/ui/sidebar/sidebar-accordion.ts), the flags in the ui-layout
  // doc (`ui_layout.json`, D1386). The count chip carries the collapsed signal.
  const sectionKey = canvases ? "canvases" : "projects";
  const collapsed = !useSidebarSectionOpen(sectionKey);
  const toggleCollapsed = () => setSidebarSectionOpen(sectionKey, collapsed);
  // This section and Bookmarks share the sidebar's free height (equal halves,
  // each its own scroll; a short or folded one yields its remainder) — the
  // hook writes the content-height cap flexbox needs for that. The rows live in
  // the always-mounted body wrapper it measures; the pop-ups below stay out.
  const sectionRef = useRef<HTMLDivElement>(null);
  useSectionContentCap(sectionRef);
  return (
    <div
      className={"sidebar-section sidebar-current-apps" + (canvases ? " sidebar-canvases" : "")}
      ref={sectionRef}
    >
      <div className={SECTION_BODY_CLASS}>
        <div
          className={
            "sidebar-heading recents-heading current-apps-heading" +
            (collapsed ? " collapsed" : "")
          }
          title={(collapsed ? "Show " : "Hide ") + (canvases ? "canvases" : "projects")}
          // A fold the keyboard can work too (CodeRabbit): focusable, a
          // button to assistive tech, Enter / Space toggle it.
          role="button"
          tabIndex={0}
          aria-expanded={!collapsed}
          onClick={toggleCollapsed}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              toggleCollapsed();
            }
          }}
        >
          {canvases ? "Canvases" : "Projects"}
          <span className="sidebar-heading-chevron" aria-hidden="true" />
          {collapsed && <span className="sidebar-count-chip">{apps.length}</span>}
        </div>
        {/* The composer trigger sits ABOVE the app rows — the first thing
            in the list, where a new app will also land (the server inserts
            new apps at index 0, D1386). Replaces the heading + reverted in
            #1303. Projects only: a canvas reaches the desk by being opened
            (the catalog's door) or by a task under its clone, not from here
            (owner, 2026-10-20: "remove open canvas option from sidebar"). */}
        <SectionFold open={!collapsed}>
        {!canvases && (
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
        {/* The Canvases half's door: "+ Canvas" opens the catalog (/canvases),
            where create and clone live — the one way in now that the
            primary nav has no Canvases row (owner, 2026-10-20). */}
        {canvases && (
          <div
            className="bookmark-row current-app-row current-app-new"
            role="button"
            tabIndex={0}
            title="Open or create a canvas"
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
            <span className="bookmark-name">Canvas</span>
          </div>
        )}
        {apps.map(render)}
        </SectionFold>
      </div>
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
      {composing && <NewAppModal onClose={() => setComposing(false)} />}
    </div>
  );
}

/** The "New app" composer, in a modal. Opened by the "+ New app" row above
 *  and by the collapsed rail's "+" (GlobalSidebar) — the rail swaps this
 *  section out, so the modal cannot live only here. */
export function NewAppModal({ onClose }: { onClose: () => void }) {
  // The SAME composer /apps and /home show (apps/builder/HomeHero.tsx):
  // it names, scaffolds and navigates into the new app's chat itself,
  // and that navigation remounts the sidebar (App.tsx), which is what
  // unmounts this modal. `onCreated` closes it for the case where the
  // composer stays put (no chat run started).
  //
  // The heading IS the optional name field: "New app" is its placeholder,
  // and whatever is typed over it becomes the folder name, verbatim, with
  // the AI naming step skipped. Left alone, the composer names the app as
  // it does everywhere else. The dialog's accessible name stays "New app"
  // (`ariaLabel`), since an input cannot be the heading it is labelled by.
  const [name, setName] = useState("");
  return (
    <Modal
      title={
        <input
          className="current-apps-compose-name"
          aria-label="App name (optional)"
          placeholder="New app"
          value={name}
          spellCheck={false}
          autoComplete="off"
          onChange={(e) => setName(e.target.value)}
        />
      }
      ariaLabel="New app"
      onClose={onClose}
      width={640}
      dialogClassName="current-apps-compose"
      // The composer arrives with its own skin (chips, pickers, the round
      // send button); the chassis' form vocabulary would re-style every
      // button in it. Owner: "we should not be redesigning anything".
      plainBody
    >
      <HeroComposer onCreated={onClose} givenName={name} />
    </Modal>
  );
}

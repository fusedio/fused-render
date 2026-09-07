// The app page — `/apps/<folder path>` (D488, widened 2026-08-26): one app
// folder — a workspace app under any shelf, or a linked app anywhere on disk —
// as a place rather than as a folder. Five tabs, named by the `_tab` query
// param (absent = overview; `?_tab=tasks`, `?_tab=files`, `?_tab=api` —
// current-apps-lib):
//
//   Overview  the app itself, live in a frame — USE it here, the way the
//             explorer's file view runs an entry page (`/render?path=`, with no
//             `_preview` flag: this is a real open, and GET /render records it
//             as one, D301).
//   Tasks     the Tasks page (shell/Scheduled.tsx) scoped to this folder —
//             the same List / Board / Calendar, the same modal, a new task
//             prefilled with this app.
//   Files     the folder's files as a tree, each rendered in one of its own
//             templates (shell/AppFiles.tsx) — "what is in this app and what
//             does each piece look like", without leaving the page.
//   API       every .py in the folder as an endpoint, Swagger-style
//             (shell/AppApi.tsx): entrypoint, parameters as a form, Execute,
//             response — the api template's view, for the whole app at once.
//
// A VERSION PICKER (AppVersionPicker.tsx), not a sixth Git tab: this page used
// to frame the folder's `git` template as a Git tab, offered only inside a
// work tree. That is GONE (docs/app-page-version-dropdown-plan.html) —
// staging, committing, branches and push/pull are not this page's job; they
// stay in the explorer's folder view, where the `git` template still lives.
// What replaces it is read-only and page-wide: a dropdown beside the tab
// strip puts ALL THREE tabs above (Overview/Files/API — Tasks is unaffected,
// it has no notion of a commit) on a past commit of the app folder, via the
// same `_snapshot` shell URL param and extraction machinery
// (`fused_render/server/routers/git_snapshot.py`) the explorer's own snapshot
// preview already uses. `useAppPageSnapshot.ts` holds this page's own
// resolution of that param; every read below rewrites against it directly
// (`rewritePathAgainst`), never a shared singleton.
//
// Opened from the sidebar's "Current apps" rows and NOWHERE ELSE (owner's
// brief): the hub's cards and the explorer keep opening the entry page as they
// always have. That is why this file adds no link to itself anywhere.
//
// Not the explorer. The explorer answers "what is in this folder"; this page
// answers "how is this app going" — the app, its work and its pieces side by
// side. The folder is one caption-click away for the operations (rename, move,
// new file) this page deliberately does not offer — including, now, every git
// write action: this page is read-only about git.
//
// Mounted per FOLDER, not per nav epoch (App.tsx): the Overview frame holds live
// app state, and a tab switch — a navigation, since the tab is in the path —
// must not reload it. The frame stays mounted behind the Tasks tab for the
// same reason (display:none, not unmount).
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import {
  appIconUrl,
  getAppEntry,
  getAppIcon,
  migrateApp,
  statPath,
  type Config,
} from "@platform/lib/api";
import { useFavicon, useUrlVersion } from "@platform/lib/hooks";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { embedUrlForFsPath, navigateUrl, urlForFsPath } from "@platform/lib/router";
import { snapshotFrameSrc } from "@platform/lib/snapshot-param";
import {
  AppWindow,
  Files,
  ExternalLink,
  Download,
  ListTodo,
  Webhook,
  type LucideIcon,
} from "lucide-react";
import { exportAppFile } from "@platform/lib/appShot";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { AppStar } from "@platform/ui/AppStar";
import IconPicker, { type IconPick } from "@platform/ui/IconPicker";
import { applyIconPick } from "@platform/lib/app-icon";
import { pushToast } from "@platform/lib/toast";
import {
  announceTasksChanged,
  CURRENT_APPS_CHANGED_EVENT,
} from "@platform/lib/tasksChanged";
import { appLandingUrl } from "@platform/lib/appLanding";
import { Button } from "@platform/shadcn/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { basename } from "@platform/lib/format";
import { opensElsewhere, tildePath } from "./tasks-lib";
import {
  APP_PAGE_TABS,
  appPageTabFromSearch,
  appPageUrl,
  type AppPageTab,
} from "./current-apps-lib";
import Scheduled from "./Scheduled";
import AppFiles from "./AppFiles";
import AppApi from "./AppApi";
import AppVersionPicker from "./AppVersionPicker";
import { useAppVersionLabel } from "./appVersionLabel";
import SnapshotError from "./SnapshotError";
import { useAppPageSnapshot, type AppPageSnapshotState } from "./useAppPageSnapshot";

// ---- the tabs, as ONE registry -----------------------------------------------
//
// Adding a tab: one string in APP_PAGE_TABS (current-apps-lib.ts, which is
// also the route) and one entry below. `Record<AppPageTab, …>` is what makes
// the second half compulsory. The strip and the panels are both mapped from
// this, so there is no JSX to touch.
//
// `keepMounted`: the panel stays in the tree behind the other tabs (hidden, not
// unmounted). The Overview needs it — the frame holds live app state and a tab
// switch must not reload it. Nothing else should want it: a hidden panel still
// polls and paints.
type TabCtx = {
  slug: string;
  dir: string;
  entry: string | null;
  folderHref: string;
  /** This page's OWN resolution of the URL's `_snapshot` sha, including the
   *  PENDING window a caller must refuse to render live content into (code
   *  review finding 4: the old shape returned null for "live" and "still
   *  resolving" alike, and a first paint of `/apps/<dir>?_snapshot=<sha>`
   *  read `null` as live and booted the live app before the resolve landed).
   *  Rewrite every read against `snapshot.snap`, never a shared singleton
   *  (code review finding 1, round 2: two apps in one repo share shas, and a
   *  singleton written by whichever view resolves last can hold another
   *  view's resolution by the time a caller reads it — there is no such
   *  singleton here at all, deliberately). */
  snapshot: AppPageSnapshotState;
};

type TabDef = {
  label: string;
  Icon: LucideIcon;
  keepMounted?: boolean;
  render: (ctx: TabCtx) => ReactNode;
};

const TAB_DEFS: Record<AppPageTab, TabDef> = {
  overview: {
    label: "Overview",
    Icon: AppWindow,
    keepMounted: true,
    // Routed through the shared `snapshotFrameSrc` (platform/lib/snapshot-param.ts,
    // the same helper Preview.tsx's own `_render` sentinel now uses) rather
    // than a hand-rolled src string — that hand-rolled version is what
    // findings 1 and 4 trace to: it rewrote `path` but never appended
    // `_snapshot`/`_snapshot_dir`/`_snapshot_app` (so the framed runtime had
    // no snapshot awareness of its own — `snapshotWritable()` saw
    // `snapshotSha === null` and left the write gate open for an absolute
    // live path), and it built a src unconditionally instead of refusing to
    // during the pending window (a bookmarked `?_snapshot=<sha>` booted the
    // LIVE app first, then swapped).
    //
    // `entryPath` is `snapshot.snap.entry` when resolved — the snapshot's OWN
    // entry page, already resolved by the server against the extracted tree
    // (finding 3) — not `entry` (the LIVE tree's) rewritten by directory
    // prefix alone, which gets the wrong FILENAME whenever the app's entry
    // was renamed since that commit.
    render: ({ slug, entry, folderHref, snapshot }) => {
      if (snapshot.pending) {
        // `error` (finding 1, second round): a transient resolve failure
        // stays `pending` forever — nothing re-runs the resolve on its own —
        // so this must not be an indefinite skeleton. `SnapshotError` gives
        // the user a real way out (retry, or back to Live) instead.
        if (snapshot.error) {
          return <SnapshotError onRetry={snapshot.retry} />;
        }
        return <SkeletonLines rows={2} label="Loading app" />;
      }
      const entryPath = snapshot.snap ? snapshot.snap.entry ?? null : entry;
      // `snapshotFrameSrc` returns `null` exactly when `sha` is claimed but
      // `snap` is not yet resolved — a case the `snapshot.pending` return
      // above already rules out here. Checked explicitly rather than
      // asserted away with `as string` (code review finding 5, second
      // round): that cast was only ever correct BECAUSE of the early
      // `pending` return above it, and gave up the null contract
      // `snapshotFrameSrc` was extracted to enforce — a future edit that
      // moves or loosens that gate would silently produce `src="null"` on
      // the iframe instead of a type error surfacing the mistake.
      const frameSrc = entryPath
        ? snapshotFrameSrc({ snap: snapshot.snap, sha: snapshot.sha, path: entryPath })
        : null;
      return frameSrc ? (
        <div className="app-page-frame-wrap">
          <iframe
            className="app-page-frame"
            src={frameSrc}
            title={`App: ${slug}`}
          />
        </div>
      ) : entryPath ? (
        // `entryPath` is set but `snapshotFrameSrc` still returned null — the
        // pending gate above should make this unreachable; fall back to the
        // loading state rather than an iframe with no src.
        <SkeletonLines rows={2} label="Loading app" />
      ) : (
        <p className="app-page-empty">
          This folder has no entry page yet.{" "}
          <a href={folderHref}>Open the folder</a> to see what is there.
        </p>
      );
    },
  },
  tasks: {
    label: "Tasks",
    Icon: ListTodo, // the sidebar's Tasks icon too (GlobalSidebar SCHEDULED_ICON)
    render: ({ dir }) => <Scheduled scope={{ project: dir }} />,
  },
  files: {
    label: "Files",
    Icon: Files,
    // Not keepMounted: the selection is in the URL, so a return costs one walk
    // and one stat — cheaper than a hidden frame that keeps running.
    render: ({ dir, entry, folderHref, snapshot }) => (
      <AppFiles
        dir={dir}
        entry={entry}
        folderHref={folderHref}
        snapshot={snapshot}
      />
    ),
  },
  api: {
    label: "API",
    Icon: Webhook,
    // Not keepMounted: the open row is in the URL (`?ep=`), and a return costs
    // one folder inspection — form values and responses are session scratch.
    render: ({ dir, folderHref, snapshot }) => (
      <AppApi dir={dir} folderHref={folderHref} snapshot={snapshot} />
    ),
  },
};

// What the folder turned out to be. `undefined` = still asking.
type Resolved =
  | { kind: "missing" }
  | { kind: "error"; message: string }
  | {
      kind: "app";
      entry: string | null;
      // The fused API version the entry declares (0 = undeclared, predates
      // the tag) and the one the runtime speaks; "Migrate" shows when behind.
      // Null from an older server that does not report versions.
      apiVersion: number | null;
      currentApiVersion: number | null;
      // A migration task on the entry that has not finished yet (the server's
      // own verdict), so a reload does not offer a second one.
      migrationTask: { id: string; state: string; run_id: string | null } | null;
    };

export default function AppPage({
  dir,
  config,
}: {
  /** The app folder, canonical forward-slash (current-apps-lib appPathFromPath). */
  dir: string;
  config: Config;
}) {
  const slug = useMemo(() => basename(dir) || dir, [dir]);
  const [resolved, setResolved] = useState<Resolved | undefined>(undefined);
  // The tab is the `_tab` query param, re-read on every URL event so
  // back/forward between the two tabs lands on the right one.
  const urlVersion = useUrlVersion();
  const tab = appPageTabFromSearch(location.search);

  // This page's OWN resolution of the URL's `_snapshot` sha — extracted into
  // useAppPageSnapshot.ts, which has its own header comment for why this is
  // a hook and not a shared singleton. Passed straight through to every tab
  // (`snapshotFrameSrc`/`rewritePathAgainst` at each read site), including
  // the `pending` flag a tab must gate its own frame/fetch on rather than
  // treat as "live" (finding 4).
  const snapshot = useAppPageSnapshot(dir, urlVersion);

  // The app's optional icon.svg (`/api/apps/icon`; the same file the Projects
  // row draws) — the header's mark AND, through useFavicon, the tab icon while
  // this page is open. One state for both, and one refetch after a pick: the
  // POST answers `{path, replaced}` with no mtime, and the mtime is exactly
  // what busts the browser's image and (far more stubborn) favicon caches, so
  // an optimistic set would show the old glyph under a new URL-less src.
  const [iconHref, setIconHref] = useState<string | null>(null);
  // The generation token stands in for the effect's usual `live` flag: a pick
  // reloads outside any effect, and a bare boolean captured per-effect cannot
  // cancel THAT read when the folder changes under it. A stale response is one
  // whose token is no longer current, whoever asked for it — which is also
  // what keeps a fast switch between two apps from painting the first one's
  // icon over the second.
  const iconGenRef = useRef(0);
  const loadIcon = useCallback(() => {
    const gen = ++iconGenRef.current;
    getAppIcon(dir)
      .then((r) => {
        if (iconGenRef.current === gen) {
          setIconHref(r.icon ? appIconUrl(r.icon, r.mtime) : null);
        }
      })
      .catch(() => {
        if (iconGenRef.current === gen) setIconHref(null);
      });
  }, [dir]);
  useEffect(() => {
    setIconHref(null);
    loadIcon();
  }, [loadIcon]);
  // The icon can also be changed from the OTHER end of the same app — the
  // sidebar's Projects glyph, whose picker writes the same file while this
  // page is the one on screen. It pokes the desk on a successful write
  // (applyIconPick), so the poke is the signal to re-read: without this the
  // header's mark and the tab favicon both kept the old glyph until the next
  // navigation, and only a pick made HERE ever looked refreshed.
  useEffect(() => {
    window.addEventListener(CURRENT_APPS_CHANGED_EVENT, loadIcon);
    return () => window.removeEventListener(CURRENT_APPS_CHANGED_EVENT, loadIcon);
  }, [loadIcon]);
  useFavicon(iconHref);

  // ---- the header mark's icon picker -----------------------------------------
  // The same picker the sidebar's Projects row opens from its glyph (the emoji
  // + branded-lucide IconPicker), anchored to the mark that opened it, writing
  // the same `icon.svg` through the same shared rule (applyIconPick, which also
  // pokes the sidebar so that row's glyph changes with this one). Its own
  // toggle selector: both glyphs are on screen together, and a loose selector
  // means each leaves the other's picker open (IconPicker's own note).
  const [iconAnchor, setIconAnchor] = useState<{ top: number; left: number } | null>(
    null,
  );
  // The picker decides when it closes (a shuffle leaves it open), so this only
  // writes.
  const onPickIcon = async (pick: IconPick | null) => {
    try {
      await applyIconPick(dir, pick);
    } catch (e) {
      // Louder than the sidebar's silent swallow: this mark is a deliberate
      // click on a page-level action, and a header that simply doesn't change
      // reads as the page being broken rather than the write having failed.
      pushToast({
        msg: "Could not change the icon: " + (e as Error).message,
        tone: "error",
      });
    }
    loadIcon();
  };

  useEffect(() => {
    let live = true;
    setResolved(undefined);
    (async () => {
      try {
        const st = await statPath(dir);
        if (!st.is_dir) {
          if (live) setResolved({ kind: "missing" });
          return;
        }
      } catch {
        // A stat that fails is a folder that is not there (404) or a server
        // that cannot say; either way there is no app to frame.
        if (live) setResolved({ kind: "missing" });
        return;
      }
      try {
        // The server's entry rule (D269/D301), asked at open time — the same
        // question every other surface asks, so this page can never disagree
        // with the card that pictures the app.
        const info = await getAppEntry(dir);
        if (live)
          setResolved({
            kind: "app",
            entry: info.entry,
            apiVersion: info.api_version ?? null,
            currentApiVersion: info.current_api_version ?? null,
            migrationTask: info.migration_task ?? null,
          });
      } catch (e) {
        if (live) setResolved({ kind: "error", message: (e as Error).message });
      }
    })();
    return () => {
      live = false;
    };
  }, [dir]);

  // Left/Right step the tabs (owner, 2026-08-26), the sibling of the sidebar's
  // Up/Down over its rows (sidebarArrowNav.ts): together the two axes make the
  // app page steerable from the keyboard alone. Same ownership rule as there —
  // only when nothing in particular is focused (<body>) or focus is in the
  // sidebar, so a focused control, a text field, or the base-ui tab list's own
  // arrow handling keeps its keys. Ends stop, no wrap.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      if (e.isComposing || e.defaultPrevented) return;
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
      if (isOverlayOpen()) return;
      const el = document.activeElement as HTMLElement | null;
      // A text field keeps its caret keys — the sidebar holds one mid-rename
      // (BookmarksSection's RenameInput), so "in the sidebar" is not enough.
      if (
        el &&
        (el.tagName === "INPUT" ||
          el.tagName === "TEXTAREA" ||
          el.isContentEditable)
      )
        return;
      const onBody =
        !el || el === document.body || el === document.documentElement;
      const inSidebar = !!el && !!document.getElementById("sidebar")?.contains(el);
      if (!onBody && !inSidebar) return;
      // Every tab is always offered now (no more conditional Git tab to
      // skip), so this steps over the route's own static list directly.
      const cur = appPageTabFromSearch(location.search);
      const i = APP_PAGE_TABS.indexOf(cur) + (e.key === "ArrowRight" ? 1 : -1);
      e.preventDefault();
      if (i < 0 || i >= APP_PAGE_TABS.length) return;
      navigateUrl(appPageUrl(dir, APP_PAGE_TABS[i], location.search));
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dir]);

  const pickTab = (e: MouseEvent<HTMLAnchorElement>, next: AppPageTab) => {
    if (opensElsewhere(e)) return;
    e.preventDefault();
    // The query rides along: it is the tab's own (`?view=` on Tasks), and a
    // switch away and back should find it as it was.
    if (next !== tab) navigateUrl(appPageUrl(dir, next, location.search));
  };

  const folderHref = urlForFsPath(dir);
  // Folded ONCE for every tilde below: `home` is raw expanduser (backslashed on
  // Windows) while `dir` and the root are forward-slash, and a prefix test
  // between the two spellings prints the full path instead of "~/…".
  const home = config.home.replace(/\\/g, "/");
  const entry = resolved?.kind === "app" ? resolved.entry : null;

  // The fused-API migration: offered only when the entry declares a version
  // behind the runtime's (an undeclared page is 0, so every pre-tag app
  // qualifies — the task's prompt tells the session to verify the code
  // before touching it). One click creates the task on the entry page (the
  // /api/apps/new shape) and lands in the Claude pane on its run; an app that
  // could not even get its task stores the reason here, not silently.
  const behind =
    resolved?.kind === "app" &&
    !!entry &&
    resolved.apiVersion != null &&
    resolved.currentApiVersion != null &&
    resolved.apiVersion < resolved.currentApiVersion;
  // A migration already underway (server-side fact, survives a reload): the
  // button shows it instead of offering another. Clicking it opens the Tasks
  // tab, where the task is listed.
  const inProgress = resolved?.kind === "app" && !!resolved.migrationTask;
  // `created` is sticky for this page: the entry still declares the OLD
  // version until the session writes the tag, so `behind` stays true and the
  // button would otherwise come straight back, inviting a second task that
  // rewrites the same files. Reset when `dir` changes (below), and by a fresh
  // page load — at which point the tag says whether the migration landed.
  const [migrateState, setMigrateState] = useState<"idle" | "creating" | "created">(
    "idle",
  );
  const [migrateError, setMigrateError] = useState<string | null>(null);
  useEffect(() => {
    setMigrateState("idle");
    setMigrateError(null);
  }, [dir]);
  const startMigration = async () => {
    if (migrateState !== "idle") return;
    setMigrateState("creating");
    setMigrateError(null);
    try {
      const res = await migrateApp(dir);
      if (res.task) announceTasksChanged();
      if (res.task_error) {
        setMigrateError(res.task_error);
        setMigrateState("idle");
        return;
      }
      setMigrateState("created");
      if (res.task?.run_id) {
        navigateUrl(appLandingUrl(res.entry_html, res.task.run_id));
      } else {
        // Stored but not yet running: the Tasks tab lists it.
        navigateUrl(appPageUrl(dir, "tasks", location.search));
      }
    } catch (e) {
      setMigrateError((e as Error).message);
      setMigrateState("idle");
    }
  };

  // ---- export "at the selected version" -------------------------------------
  //
  // The picker only ever writes/reads `_snapshot`; this is the one place that
  // turns "which version is selected" into "which folder to export" — a
  // resolved snapshot's OWN extracted tree (`snap.dir`, never `snap.app_dir`:
  // that is the LIVE folder the sha resolved FROM, and exporting it would
  // silently ship the live app labelled as the picked commit) when one is
  // picked, the live app folder otherwise.
  //
  // Gated on `snapshot.pending`/`snapshot.error` (not just disabled — the
  // click handler also refuses) for the same reason every frame/fetch on this
  // page already gates on them (useAppPageSnapshot's own header comment): a
  // click that lands mid-resolve, before `snap.dir` exists, must not fall
  // through to exporting the LIVE folder while the picker still shows the
  // version being resolved — that is exactly the class of bug this branch
  // has already had several of.
  const versionLabel = useAppVersionLabel(dir, snapshot.sha);
  const [exporting, setExporting] = useState(false);
  const exportDisabled = exporting || snapshot.pending || snapshot.error;
  const handleExport = async () => {
    if (exportDisabled) return;
    setExporting(true);
    try {
      const isLive = snapshot.sha === null;
      const exportPath = snapshot.snap ? snapshot.snap.dir : dir;
      // The filename carries the version so an exported v7 sitting beside a
      // live export in Downloads is never ambiguous about which is which.
      const exportName = isLive ? slug : `${slug}-${versionLabel}`;
      await exportAppFile(
        {
          path: exportPath,
          name: exportName,
          // A preview capture is only attempted for a LIVE export. For a
          // snapshot, `exportAppFile`'s stage fallback (no on-screen capture
          // element is threaded to this page) would reload the ENTRY PAGE'S
          // LIVE copy to shoot it — a screenshot of the wrong era baked into
          // a file labelled as the old commit. Omitting `entry_html` here
          // skips preview capture entirely rather than risk that; the
          // snapshot export ships with no preview.png, which
          // `downloadAppFile` already handles.
          entry_html: isLive ? entry ?? undefined : undefined,
        },
        null,
      );
    } catch (e) {
      pushToast({
        msg: "Could not export " + slug + ": " + (e as Error).message,
        tone: "error",
      });
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="app-page">
      <header className="app-page-head">
        <div className="app-page-title">
          {/* The app's mark, and the way to change it: a click opens the same
              icon picker the sidebar's Projects row does (below). The app's
              own icon.svg drawn as is — the author's colours, no tint — or
              the generic star when it has none, which is also the affordance
              for an app that has never had an icon. */}
          <button
            type="button"
            className="app-page-icon app-page-icon-toggle"
            title="Change icon"
            aria-label="Change icon"
            onClick={(e) => {
              const rect = e.currentTarget.getBoundingClientRect();
              setIconAnchor((cur) =>
                cur ? null : { top: rect.top, left: rect.left },
              );
            }}
          >
            {iconHref ? (
              <img src={iconHref} alt="" draggable={false} />
            ) : (
              <AppStar />
            )}
          </button>
          {/* The name and the folder keep their own baseline row: the mark is
              a column beside the PAIR (it centers against both), and a
              baseline-aligned box in the same row would drop the text off
              center against it. */}
          <div className="app-page-name">
            <h1>{slug}</h1>
            {/* Reads as the folder and IS the folder: opens its listing in the
                explorer. The app itself is the "Open app" button opposite. */}
            <a className="app-page-folder" href={folderHref} title={dir}>
              {tildePath(dir, home)}
            </a>
          </div>
        </div>
        {entry && (
          <div className="app-page-actions">
            {behind && resolved?.kind === "app" && (
              /* The app is on an older fused API than the runtime: one click
                 creates the migration task on its entry page, carrying the
                 changelog for every version being crossed. */
              <Button
                size="sm"
                className="app-page-migrate"
                variant="outline"
                disabled={!inProgress && migrateState !== "idle"}
                title={
                  inProgress
                    ? "A migration task for this app is still running; it is listed under Tasks. The button returns once it finishes."
                    : migrateState === "created"
                      ? "A migration task was created for this app; it is listed under Tasks."
                      : `This app declares fused API version ${resolved.apiVersion}; the runtime is on ${resolved.currentApiVersion}. Creates a task that updates the code and the tag.`
                }
                onClick={
                  inProgress
                    ? () => navigateUrl(appPageUrl(dir, "tasks", location.search))
                    : startMigration
                }
              >
                {inProgress
                  ? "Migration in progress"
                  : migrateState === "creating"
                    ? "Creating task…"
                    : migrateState === "created"
                      ? "Migration task created"
                      : "Migrate to new version"}
              </Button>
            )}
            {/* The app full-size AS AN APP — its entry page in embed mode,
                chrome-free — in a new tab so this page and its live frame stay
                put. The top-level embed's strip (EmbedStrip) is the way back
                into the explorer from there; the folder link opposite is the
                explorer route from here. */}
            <Button
              size="sm"
              variant="outline"
              className="app-page-open"
              onClick={() => window.open(embedUrlForFsPath(entry), "_blank", "noopener")}
            >
              Open app
              <ExternalLink data-icon="inline-end" />
            </Button>
            {/* Exports the folder AT THE PICKER'S SELECTED VERSION — the live
                folder for "Live", the resolved snapshot's own extracted tree
                for a commit (see the `handleExport` comment above). Disabled
                through the same pending/error window every other read on
                this page already gates on, so a click mid-resolve can never
                silently export the wrong era. */}
            <Button
              size="sm"
              variant="outline"
              className="app-page-export"
              disabled={exportDisabled}
              title={
                snapshot.pending
                  ? "Waiting for this version to finish loading"
                  : snapshot.error
                    ? "This version failed to load; retry it from the version picker"
                    : versionLabel === "Live"
                      ? "Export the live app as a .fused file"
                      : `Export the app as of ${versionLabel} as a .fused file`
              }
              onClick={handleExport}
            >
              {exporting ? "Exporting…" : "Export"}
              <Download data-icon="inline-end" />
            </Button>
          </div>
        )}
      </header>
      {iconAnchor && (
        <IconPicker
          anchor={iconAnchor}
          toggleSelector=".app-page-icon-toggle"
          onPick={(pick) => onPickIcon(pick)}
          onRemove={() => onPickIcon(null)}
          onClose={() => setIconAnchor(null)}
        />
      )}
      {migrateError && (
        <ErrorBanner>Could not create the migration task: {migrateError}</ErrorBanner>
      )}

      <div className="app-page-body">
        {/* The tab strip and the version picker share one row: the picker is
            page-wide state (task 3 puts all three visible tabs on the
            selected commit), so it sits beside the strip rather than inside
            any one panel. */}
        <div className="app-page-tabbar flex-none">
          {/* Controlled by the URL and ONLY the URL: no onValueChange, so a
              ctrl/middle-click on a trigger opens the address elsewhere without
              also switching this page. Real anchors under the triggers (base-ui's
              `render`), same reason as before — a tab is an address (D420). */}
          <Tabs value={tab} className="app-page-tabs flex-none">
            <TabsList
              variant="line"
              aria-label="App page"
              className="h-auto w-full justify-start rounded-none border-b-0 p-0 pb-1"
            >
              {APP_PAGE_TABS.map((id) => {
                const { label, Icon } = TAB_DEFS[id];
                return (
                  <TabsTrigger
                    key={id}
                    value={id}
                    className="flex-none px-4 py-2.5"
                    // Base UI assumes a native <button> unless told otherwise:
                    // without this the anchor gets type="button" and Space
                    // does not activate it (Bugbot on #851).
                    nativeButton={false}
                    render={
                      <a
                        href={appPageUrl(dir, id, location.search)}
                        onClick={(e) => pickTab(e, id)}
                      />
                    }
                  >
                    <Icon data-icon="inline-start" />
                    {label}
                  </TabsTrigger>
                );
              })}
            </TabsList>
          </Tabs>
          <AppVersionPicker dir={dir} />
        </div>

        {resolved === undefined && (
          <SkeletonLines rows={2} label="Loading app" />
        )}
        {resolved?.kind === "missing" && (
          <ErrorBanner>
            No folder at <strong>{tildePath(dir, home)}</strong>.
          </ErrorBanner>
        )}
        {resolved?.kind === "error" && (
          <ErrorBanner>
            Could not open {slug}: {resolved.message}
          </ErrorBanner>
        )}

        {resolved?.kind === "app" &&
          APP_PAGE_TABS.map((id) => {
            const def = TAB_DEFS[id];
            const active = tab === id;
            if (!active && !def.keepMounted) return null;
            return (
              <section
                key={id}
                className={
                  "app-page-panel app-page-" + id + (active ? "" : " is-hidden")
                }
                role="tabpanel"
                aria-hidden={!active}
              >
                {def.render({ slug, dir, entry, folderHref, snapshot })}
              </section>
            );
          })}
      </div>
    </div>
  );
}

// The app page — `/apps/<folder path>` (D488, widened 2026-08-26): one app
// folder — a workspace app under any shelf, or a linked app anywhere on disk —
// as a place rather than as a folder. Three tabs, named by the `_tab` query
// param (absent = overview; `?_tab=tasks`, `?_tab=files` — current-apps-lib):
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
//
// Opened from the sidebar's "Current apps" rows and NOWHERE ELSE (owner's
// brief): the hub's cards and the explorer keep opening the entry page as they
// always have. That is why this file adds no link to itself anywhere.
//
// Not the explorer. The explorer answers "what is in this folder"; this page
// answers "how is this app going" — the app, its work and its pieces side by
// side. The folder is one caption-click away for the operations (rename, move,
// new file) this page deliberately does not offer.
//
// Mounted per FOLDER, not per nav epoch (App.tsx): the Overview frame holds live
// app state, and a tab switch — a navigation, since the tab is in the path —
// must not reload it. The frame stays mounted behind the Tasks tab for the
// same reason (display:none, not unmount).
import {
  useEffect,
  useMemo,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import { getAppEntry, statPath, type Config } from "@platform/lib/api";
import { useUrlVersion } from "@platform/lib/hooks";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { navigateUrl, urlForFsPath } from "@platform/lib/router";
import { AppWindow, Files, ListTodo, type LucideIcon } from "lucide-react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
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
    render: ({ slug, entry, folderHref }) =>
      entry ? (
        <iframe
          className="app-page-frame"
          src={`/render?path=${encodeURIComponent(entry)}`}
          title={`App: ${slug}`}
        />
      ) : (
        <p className="app-page-empty">
          This folder has no entry page yet.{" "}
          <a href={folderHref}>Open the folder</a> to see what is there.
        </p>
      ),
  },
  tasks: {
    label: "Tasks",
    Icon: ListTodo,
    render: ({ dir }) => <Scheduled scope={{ project: dir }} />,
  },
  files: {
    label: "Files",
    Icon: Files,
    // Not keepMounted: the selection is in the URL, so a return costs one walk
    // and one stat — cheaper than a hidden frame that keeps running.
    render: ({ dir, entry, folderHref }) => (
      <AppFiles dir={dir} entry={entry} folderHref={folderHref} />
    ),
  },
};

// What the folder turned out to be. `undefined` = still asking.
type Resolved =
  | { kind: "missing" }
  | { kind: "error"; message: string }
  | { kind: "app"; entry: string | null };

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
  useUrlVersion();
  const tab = appPageTabFromSearch(location.search);

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
        const { entry } = await getAppEntry(dir);
        if (live) setResolved({ kind: "app", entry });
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

  return (
    <div className="app-page">
      <header className="app-page-head">
        <div className="app-page-title">
          <h1>{slug}</h1>
          {/* The folder, as a link: the one door from this page into the
              explorer, for when the files are the question. */}
          <a className="app-page-folder" href={folderHref} title={dir}>
            {tildePath(dir, home)}
          </a>
        </div>
      </header>

      <div className="app-page-body">
        {/* Controlled by the URL and ONLY the URL: no onValueChange, so a
            ctrl/middle-click on a trigger opens the address elsewhere without
            also switching this page. Real anchors under the triggers (base-ui's
            `render`), same reason as before — a tab is an address (D420). */}
        <Tabs value={tab} className="app-page-tabs flex-none">
          <TabsList
            variant="line"
            aria-label="App page"
            className="h-auto w-full justify-start rounded-none border-b border-border p-0 pb-1"
          >
            {APP_PAGE_TABS.map((id) => {
              const { label, Icon } = TAB_DEFS[id];
              return (
                <TabsTrigger
                  key={id}
                  value={id}
                  className="flex-none px-2 py-1.5"
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
                {def.render({ slug, dir, entry, folderHref })}
              </section>
            );
          })}
      </div>
    </div>
  );
}

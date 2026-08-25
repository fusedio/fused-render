// The app page — `/apps/<slug>` (D488): one workspace app, <fused_dir>/local/<slug>,
// as a place rather than as a folder. Two tabs:
//
//   Overview  the app itself, live in a frame — USE it here, the way the
//             explorer's file view runs an entry page (`/render?path=`, with no
//             `_preview` flag: this is a real open, and GET /render records it
//             as one, D301).
//   Tasks     the Tasks page (shell/Scheduled.tsx) scoped to this folder —
//             the same List / Board / Calendar, the same modal, a new task
//             prefilled with this app.
//
// Opened from the sidebar's "Current apps" rows and NOWHERE ELSE (owner's
// brief): the hub's cards and the explorer keep opening the entry page as they
// always have. That is why this file adds no link to itself anywhere.
//
// Not the explorer. The explorer answers "what is in this folder"; this page
// answers "how is this app going" — the app and its work side by side. The
// folder is one caption-click away for when the files are the question.
//
// Mounted per SLUG, not per nav epoch (App.tsx): the Overview frame holds live
// app state, and a tab switch — a navigation, since the tab is in the URL —
// must not reload it. The frame stays mounted behind the Tasks tab for the
// same reason (display:none, not unmount).
import { useEffect, useMemo, useState, type MouseEvent } from "react";
import { getAppEntry, statPath, type Config } from "@platform/lib/api";
import { useUrlVersion } from "@platform/lib/hooks";
import { navigateUrl, urlForFsPath } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { opensElsewhere, tildePath } from "./tasks-lib";
import {
  appPageTab,
  localAppsRoot,
  withAppPageTab,
  type AppPageTab,
} from "./current-apps-lib";
import Scheduled from "./Scheduled";

const TABS: { id: AppPageTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "tasks", label: "Tasks" },
];

// What the folder turned out to be. `undefined` = still asking.
type Resolved =
  | { kind: "missing" }
  | { kind: "error"; message: string }
  | { kind: "app"; entry: string | null };

export default function AppPage({ slug, config }: { slug: string; config: Config }) {
  const dir = useMemo(() => localAppsRoot(config.fused_dir) + slug, [config.fused_dir, slug]);
  const [resolved, setResolved] = useState<Resolved | undefined>(undefined);
  // The tab is the URL's (`?tab=`), re-read on every URL event so back/forward
  // between the two tabs lands on the right one.
  useUrlVersion();
  const tab = appPageTab(location.search);

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

  const pickTab = (e: MouseEvent<HTMLAnchorElement>, next: AppPageTab) => {
    if (opensElsewhere(e)) return;
    e.preventDefault();
    if (next !== tab) navigateUrl(location.pathname + withAppPageTab(location.search, next));
  };

  const folderHref = urlForFsPath(dir);
  const entry = resolved?.kind === "app" ? resolved.entry : null;

  return (
    <div className="app-page">
      <header className="app-page-head">
        <div className="app-page-title">
          <h1>{slug}</h1>
          {/* The folder, as a link: the one door from this page into the
              explorer, for when the files are the question. */}
          <a className="app-page-folder" href={folderHref} title={dir}>
            {tildePath(dir, config.home.replace(/\\/g, "/"))}
          </a>
        </div>
        <div className="app-page-tabs" role="tablist" aria-label="App page">
          {TABS.map((t) => (
            // Real anchors: a tab is an address, so middle-click and copy-link
            // reach it (same shape as the AI Models strip, D420).
            <a
              key={t.id}
              role="tab"
              aria-selected={tab === t.id}
              className={"app-page-tab" + (tab === t.id ? " active" : "")}
              href={location.pathname + withAppPageTab(location.search, t.id)}
              onClick={(e) => pickTab(e, t.id)}
            >
              {t.label}
            </a>
          ))}
        </div>
      </header>

      {resolved === undefined && <SkeletonLines rows={2} label="Loading app" />}
      {resolved?.kind === "missing" && (
        <ErrorBanner>
          No app named <strong>{slug}</strong> under {tildePath(localAppsRoot(config.fused_dir), config.home)}.
        </ErrorBanner>
      )}
      {resolved?.kind === "error" && <ErrorBanner>Could not open {slug}: {resolved.message}</ErrorBanner>}

      {resolved?.kind === "app" && (
        <>
          {/* Overview stays MOUNTED behind the Tasks tab (hidden, not gone) so
              the running app keeps its state across a tab switch. */}
          <section
            className={"app-page-overview" + (tab === "overview" ? "" : " is-hidden")}
            role="tabpanel"
            aria-hidden={tab !== "overview"}
          >
            {entry ? (
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
            )}
          </section>
          {tab === "tasks" && (
            <section className="app-page-tasks" role="tabpanel">
              <Scheduled scope={{ project: dir }} />
            </section>
          )}
        </>
      )}
    </div>
  );
}

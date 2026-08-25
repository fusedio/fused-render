// "Current apps" — the sidebar section above Bookmarks (D487): the workspace
// apps (<fused_dir>/local/<slug>) that still have a task not filed away, at
// most five, newest activity first. A row opens the app's PAGE (`/apps/<slug>`,
// shell/AppPage.tsx, D488) — the one door that page has; its cross archives
// every task under it, which is the one gesture that takes an app off this list.
//
// Fed by the task pulse store (useTasksPulseRows) rather than a poll of its
// own: the sidebar and the Tasks page already share ONE /api/tasks(/pulse)
// reader and a second one is the double-poll that store exists to prevent.
// `project` rides the compact row for exactly this reader.
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { archiveTask, getConfig } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";
import { opensElsewhere } from "@shell/tasks-lib";
import { pokeTasks, useTasksPulseRows } from "@shell/tasksPulse";
import {
  appPageTab,
  appPageUrl,
  currentApps,
  slugFromAppPath,
  type CurrentApp,
} from "@shell/current-apps-lib";

// Read once per mount; the sidebar remounts per navigation, which is cheap
// enough (Scheduled.tsx reads config the same way). Cached at module level so
// the row list does not blink empty on every remount while the config
// round-trips. `fused_dir`, not `home`: the workspace root honours
// FUSED_RENDER_DIR, and a root built from home would list nothing under it.
let knownRoot = "";

function useFusedDir(): string {
  const [root, setRoot] = useState(knownRoot);
  useEffect(() => {
    if (knownRoot) return;
    getConfig().then(
      (c) => {
        knownRoot = c.fused_dir || "";
        setRoot(knownRoot);
      },
      () => {},
    );
  }, []);
  return root;
}

function CurrentAppRow({ app, active }: { app: CurrentApp; active: boolean }) {
  const [busy, setBusy] = useState(false);
  const href = appPageUrl(app.slug);
  const onOpen = (e: React.MouseEvent<HTMLAnchorElement>) => {
    // Middle/modified clicks keep the browser's own new-tab gesture on the href.
    if (opensElsewhere(e)) return;
    e.preventDefault();
    // `active` is slug-only (the row lights up on either tab); the destination
    // is the OVERVIEW, so from the Tasks tab the click still goes — it is how
    // the sidebar gets back to the running app. Only a click that would land
    // exactly where the page already is stays a no-op.
    if (!active || appPageTab(location.search) !== "overview") navigateUrl(href);
  };
  const onArchive = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (busy) return;
    setBusy(true);
    try {
      // Every task under the app, tolerant of one failing: the row leaves the
      // list only once the pulse re-reads, so a half-archived app simply stays
      // with fewer tasks rather than lying about being gone.
      await Promise.allSettled(app.taskKeys.map((k) => archiveTask(k)));
    } finally {
      setBusy(false);
      pokeTasks();
    }
  };
  const n = app.taskKeys.length;
  const tip = `${app.dir} — ${n} ${n === 1 ? "task" : "tasks"}${app.running ? ", running" : ""}`;
  return (
    <div className={"bookmark-row current-app-row" + (active ? " active" : "")} title={tip}>
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
        {app.slug}
      </a>
      <span className="bookmark-actions">
        <button
          className="icon-btn delete-btn current-app-archive"
          title={`Archive ${n === 1 ? "its task" : `all ${n} tasks`}`}
          aria-label={`Archive all tasks for ${app.slug}`}
          disabled={busy}
          onClick={onArchive}
        >
          ✕
        </button>
      </span>
    </div>
  );
}

export default function CurrentAppsSection() {
  const rows = useTasksPulseRows();
  const fusedDir = useFusedDir();
  const apps = useMemo(() => currentApps(rows, fusedDir), [rows, fusedDir]);
  // Which row is the page on screen. Read at render: the sidebar remounts on
  // every navigation (App.tsx), so a stale read cannot outlive a route change.
  const onSlug = slugFromAppPath(location.pathname);
  const render = useCallback(
    (app: CurrentApp) => <CurrentAppRow key={app.dir} app={app} active={app.slug === onSlug} />,
    [onSlug],
  );
  // Nothing on the desk → no section. Unlike Bookmarks this one is contextual,
  // and an empty heading above the permanent section is noise.
  if (apps.length === 0) return null;
  return (
    <div className="sidebar-section sidebar-current-apps">
      <div className="sidebar-heading">Current apps</div>
      {apps.map(render)}
    </div>
  );
}

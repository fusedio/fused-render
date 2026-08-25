// "Current apps" — the sidebar section above Bookmarks (D487): the workspace
// apps (~/Fused/local/<slug>) that still have a task not filed away, at most
// five, newest activity first. A row opens the app; its cross archives every
// task under it, which is the one gesture that takes an app off this list.
//
// Fed by the task pulse store (useTasksPulseRows) rather than a poll of its
// own: the sidebar and the Tasks page already share ONE /api/tasks(/pulse)
// reader and a second one is the double-poll that store exists to prevent.
// `project` rides the compact row for exactly this reader.
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { archiveTask, getAppEntry, getConfig } from "@platform/lib/api";
import { navigate, urlForFsPath } from "@platform/lib/router";
import { opensElsewhere } from "@shell/tasks-lib";
import { pokeTasks, useTasksPulseRows } from "@shell/tasksPulse";
import { currentApps, type CurrentApp } from "@shell/current-apps-lib";

// Read once per mount; the sidebar remounts per navigation, which is cheap
// enough (Scheduled.tsx reads home the same way). Cached at module level so the
// row list does not blink empty on every remount while the config round-trips.
let knownHome = "";

function useHome(): string {
  const [home, setHome] = useState(knownHome);
  useEffect(() => {
    if (knownHome) return;
    getConfig().then(
      (c) => {
        knownHome = c.home || "";
        setHome(knownHome);
      },
      () => {},
    );
  }, []);
  return home;
}

// Open the app's ENTRY PAGE, resolved by the server at click time — the same
// question the explorer's "Open app" button asks, because the entry rule is the
// server's (D301/D269) and a copy in the shell drifts. No page → the folder.
async function openApp(app: CurrentApp) {
  let entry: string | null = null;
  try {
    entry = (await getAppEntry(app.dir)).entry;
  } catch {
    // Unreachable server: the folder listing is still the honest destination.
  }
  if (entry) navigate(entry, { isDir: false });
  else navigate(app.dir, { isDir: true });
}

function CurrentAppRow({ app }: { app: CurrentApp }) {
  const [busy, setBusy] = useState(false);
  const onOpen = (e: React.MouseEvent<HTMLAnchorElement>) => {
    // Middle/modified clicks keep the browser's own new-tab gesture on the href
    // (the folder — a new tab has no click-time entry lookup to lean on).
    if (opensElsewhere(e)) return;
    e.preventDefault();
    void openApp(app);
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
    <div className="bookmark-row current-app-row" title={tip}>
      <span className="bookmark-glyph current-app-glyph" aria-hidden="true">
        {app.running ? <span className="sidebar-rail-dot is-running" /> : "▣"}
      </span>
      <a className="bookmark-name" href={urlForFsPath(app.dir)} draggable={false} onClick={onOpen}>
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
  const home = useHome();
  const apps = useMemo(() => currentApps(rows, home), [rows, home]);
  const render = useCallback((app: CurrentApp) => <CurrentAppRow key={app.dir} app={app} />, []);
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

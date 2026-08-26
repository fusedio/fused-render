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
import { Modal } from "@platform/ui/modal/Modal";
import { HeroComposer } from "@apps/builder/HomeHero";
import { opensElsewhere } from "@shell/tasks-lib";
import { pokeTasks, useTasksPulseRows } from "@shell/tasksPulse";
import {
  appPageTabFromPath,
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
    if (!active || appPageTabFromPath(location.pathname) !== "overview")
      navigateUrl(href);
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
    <div
      className={"bookmark-row current-app-row" + (active ? " active" : "")}
      title={tip}
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
    (app: CurrentApp) => (
      <CurrentAppRow key={app.dir} app={app} active={app.slug === onSlug} />
    ),
    [onSlug],
  );
  // The + opens the /apps composer in a modal (D489). The section therefore
  // ALWAYS renders now — it first hid itself with zero current apps (D487),
  // but a door to "make one" is exactly what an empty desk wants, and hiding
  // the heading would hide the door with it. Empty = heading + plus, no rows.
  const [composing, setComposing] = useState(false);
  return (
    <div className="sidebar-section sidebar-current-apps">
      <div className="sidebar-heading current-apps-heading">
        Current apps
        <button
          type="button"
          className="icon-btn current-apps-add"
          title="New app"
          aria-label="New app"
          onClick={() => setComposing(true)}
        >
          +
        </button>
      </div>
      {apps.map(render)}
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

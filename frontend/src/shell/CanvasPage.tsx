// The canvas page — `/canvases/<env>/<name>?_tab=` (owner, 2026-10-20: "a
// project view similar as we have for fused-app for the canvas"): one canvas
// as a place, the app page's shape (shell/AppPage.tsx, D488). Three tabs,
// named by the `_tab` query param (absent = overview; api.ts
// CANVAS_PAGE_TABS):
//
//   Overview  the canvas itself — the embedded live workbench under its sync
//             strip (CanvasWorkspace.tsx), full width; READ ONLY in the sense
//             that this page offers no editing surface of its own: no canvas
//             chat beside it any more (a Claude task on the clone is the
//             Tasks tab's job). The one pane it keeps is a NODE's — selecting
//             a node in the workbench, or arriving with `?node=` (the Tasks
//             page's door on a canvas task, PR #1791), opens that node's
//             Claude / Code peek over the right edge.
//   Tasks     the Tasks page (shell/Scheduled.tsx) scoped to the clone folder
//             — same List / Board / Calendar, same modal, a new task prefilled
//             with the clone.
//   Files     the clone's files as a tree, each in one of its own templates
//             (shell/AppFiles.tsx): canvas.toml, CLAUDE.md, the UDFs' .py,
//             the widgets' .json / .html.
//
// No API and no Doctor tab: a canvas is not an app folder with endpoints to
// call or a share-readiness to check. No version picker, no git peek.
//
// THE EMBED IS LOADED ONCE PER VISIT. The workbench frame takes seconds to
// boot (auth handshake, hydration, sync/start), so a tab switch must not
// unmount it: the Overview panel is the ONLY `keepMounted` tab, hidden with
// `visibility:hidden` and its layout box KEPT (not `display:none`, the app
// page's way) — the workbench is React Flow plus deck.gl, and a frame whose
// box collapses to 0 and back fires a resize inside the embed that a
// fit-to-view would answer by moving the camera the user left. Its sync
// loops keep running while hidden, on purpose: a Claude task on the Tasks
// tab edits the clone, the watcher pushes it, and the embed is patched in
// place, so Overview is current the moment it is shown again. Leaving the
// route — the catalog, another canvas, anywhere — unmounts this page and
// with it the frame, the sockets and the relay queue (App.tsx keys this
// component on (env, name), not on the navigation epoch, so a tab hop is a
// URL change and nothing else).
//
// Lives in shell/, not apps/canvases/: the Tasks and Files tabs are shell
// pages (Scheduled, AppFiles) and an app may not import those
// (scripts/check-boundaries.mjs); the shell may import anything, so the page
// sits here and frames the sub-app's workspace.
//
// Chrome: the app page's header and tab-strip classes (`.app-page-head`,
// `.app-page-tabbar`, their fit hooks) as they are; the BODY is this page's
// own `.canvas-page-body` — full width, no 1050px column (the canvas wants
// the room; `.app-page-body`'s frame is not touched). Inside it the Tasks
// panel keeps the Tasks page's own column (schedule.css).
import { useCallback, useEffect, useMemo, useState, type MouseEvent, type ReactNode } from "react";
import { AppWindow, ExternalLink, Files, ListTodo, type LucideIcon } from "lucide-react";
import { useUrlVersion } from "@platform/lib/hooks";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { navigateUrl, spaLinkProps } from "@platform/lib/router";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { Button } from "@platform/shadcn/ui/button";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { APP_PAGE_FIT_LABEL, useAppHeadFit, useAppTabbarFit } from "@shell/app-page-fit";
import type { AppPageSnapshotState } from "./useAppPageSnapshot";
import AppFiles from "./AppFiles";
import Scheduled from "./Scheduled";
import { peekSearch } from "./task-peek-store";
import {
  CANVAS_PAGE_TABS,
  ENV_LABELS,
  canvasPagePath,
  canvasPageTabFromSearch,
  getCanvasesStatus,
  type CanvasPageTab,
  type WorkbenchEnv,
} from "@apps/canvases/api";
import { CanvasWorkspace } from "@apps/canvases";
import { CanvasGlyph } from "@apps/canvases/glyph";

/** The Files tab always reads the LIVE clone: no version picker on this page,
 *  so the snapshot state AppFiles takes is one constant, never pending. */
const LIVE_SNAPSHOT: AppPageSnapshotState = {
  sha: null,
  snap: null,
  pending: false,
  error: false,
  retry: () => undefined,
};

type TabCtx = {
  env: WorkbenchEnv;
  name: string;
  /** The clone folder, `<canvases_dir>/<name>` — null until status answers. */
  dir: string | null;
  onWorkbenchUrl: (url: string | null) => void;
};

type TabDef = {
  label: string;
  Icon: LucideIcon;
  /** The panel stays in the tree behind the other tabs (hidden, not
   *  unmounted). Only the Overview wants it — see the header. */
  keepMounted?: boolean;
  render: (ctx: TabCtx) => ReactNode;
};

const TAB_DEFS: Record<CanvasPageTab, TabDef> = {
  overview: {
    label: "Overview",
    Icon: AppWindow,
    keepMounted: true,
    render: ({ env, name, onWorkbenchUrl }) => (
      <CanvasWorkspace env={env} name={name} onWorkbenchUrl={onWorkbenchUrl} />
    ),
  },
  tasks: {
    label: "Tasks",
    Icon: ListTodo,
    // Not keepMounted: the view is in the URL (`?view=`), and a return costs
    // one listing read the pulse already holds.
    // `ownFrame`: this page wraps the tabs in no TaskPeekFrame of its own (the
    // app page does, one level up, to share a right edge with its git peek;
    // there is no such sibling here), so the Tasks page draws the peek's frame
    // itself — the framed `/tasks?project=` route's posture.
    render: ({ dir }) =>
      dir ? (
        <Scheduled scope={{ project: dir, entry: null, ownFrame: true }} />
      ) : (
        <SkeletonLines rows={2} label="Loading tasks" />
      ),
  },
  files: {
    label: "Files",
    Icon: Files,
    // Not keepMounted: the selection is in the URL (`?file=`), so a return
    // costs one walk and one stat.
    render: ({ dir }) =>
      dir ? (
        <AppFiles dir={dir} entry={null} snapshot={LIVE_SNAPSHOT} />
      ) : (
        <SkeletonLines rows={2} label="Loading files" />
      ),
  },
};

export default function CanvasPage({ env, name }: { env: WorkbenchEnv; name: string }) {
  // The tab is the `_tab` query param, re-read on every URL event so
  // back/forward between tabs lands on the right one.
  useUrlVersion();
  const tab = canvasPageTabFromSearch(location.search);

  // The clone folder, derived from status (one cached GET) rather than
  // waited for from sync/start's answer: Tasks and Files mount before the
  // embed's handshake finishes, and a clone another process owns (sync/start
  // 409) still has a folder to list.
  const [dir, setDir] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setDir(null);
    setError(null);
    getCanvasesStatus()
      .then((status) => {
        if (cancelled) return;
        const base = status.envs[env]?.canvases_dir;
        if (!base) {
          setError(`No clone folder for the ${ENV_LABELS[env]} environment.`);
          return;
        }
        setDir(base.replace(/\/+$/, "") + "/" + name);
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, [env, name]);

  // The hosted editor's URL, reported by the Overview once it knows the
  // base URL and the account handle.
  const [workbenchUrl, setWorkbenchUrl] = useState<string | null>(null);
  const onWorkbenchUrl = useCallback((url: string | null) => setWorkbenchUrl(url), []);

  // A tab's address: the current query rides along (a tab's own params —
  // `?node=` on Overview, `?view=` on Tasks, `?file=` on Files — are
  // harmless on the others and come back intact), only `_tab` is rewritten.
  // `?peek=` is the ONE param that does not ride: it names a panel only the
  // Tasks tab can show, and a switch away is a close — carried along it
  // would re-open the panel on the way back (Bugbot on the app page, #1249).
  const tabUrl = (next: CanvasPageTab) =>
    canvasPagePath(env, name, { tab: next, search: peekSearch(location.search, null) });
  const pickTab = (e: MouseEvent<HTMLAnchorElement>, next: CanvasPageTab) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    if (next !== tab) navigateUrl(tabUrl(next));
  };

  // Left/Right step the tabs (the app page's rule, owner 2026-08-26): only
  // when nothing in particular is focused or focus is in the sidebar, so a
  // text field, the embed, or the tab list's own arrow handling keeps its keys.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      if (e.isComposing || e.defaultPrevented) return;
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
      if (isOverlayOpen()) return;
      const el = document.activeElement as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      const onBody = !el || el === document.body || el === document.documentElement;
      const inSidebar = !!el && !!document.getElementById("sidebar")?.contains(el);
      if (!onBody && !inSidebar) return;
      const cur = canvasPageTabFromSearch(location.search);
      const i = CANVAS_PAGE_TABS.indexOf(cur) + (e.key === "ArrowRight" ? 1 : -1);
      e.preventDefault();
      if (i < 0 || i >= CANVAS_PAGE_TABS.length) return;
      navigateUrl(
        canvasPagePath(env, name, {
          tab: CANVAS_PAGE_TABS[i],
          search: peekSearch(location.search, null),
        }),
      );
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [env, name]);

  const [headFit, headRef] = useAppHeadFit();
  const [tabbarFit, tabbarRef] = useAppTabbarFit();
  const ctx = useMemo<TabCtx>(() => ({ env, name, dir, onWorkbenchUrl }), [env, name, dir, onWorkbenchUrl]);

  return (
    <div className="app-page canvas-page">
      <header className="app-page-head" ref={headRef} data-fit={headFit}>
        <div className="app-page-title">
          <span className="app-page-icon canvas-page-icon" aria-hidden="true">
            <CanvasGlyph size={26} />
          </span>
          <div className="app-page-name">
            <h1>
              {name}
              <span
                className="canvases-env-chip canvas-page-env"
                title={`${name} syncs with ${ENV_LABELS[env]}`}
              >
                {ENV_LABELS[env]}
              </span>
            </h1>
            {/* Reads as the clone folder and IS the folder: its listing in
                the explorer, for the operations this page does not offer. */}
            {dir && (
              <a className="app-page-folder" title={dir} {...spaLinkProps(dir, { isDir: true })}>
                {dir}
              </a>
            )}
          </div>
        </div>
        <div className="app-page-actions">
          {/* The hosted workbench editor, in a new tab — the strip used to
              carry this link; it is the page's one outward door now. */}
          {workbenchUrl ? (
            // A real anchor under the button (base-ui `render`), as the strip's
            // link was: the native shell routes a target=_blank link to the
            // system browser, which a window.open from a click need not get.
            <Button
              size="sm"
              variant="default"
              className="app-page-open"
              nativeButton={false}
              render={<a href={workbenchUrl} target="_blank" rel="noopener noreferrer" />}
              title="Open this canvas in the Fused workbench, in a new tab"
            >
              <span className={APP_PAGE_FIT_LABEL}>Open in Workbench</span>
              <ExternalLink data-icon="inline-end" />
            </Button>
          ) : (
            <Button
              size="sm"
              variant="default"
              className="app-page-open"
              disabled
              title="Waiting for the workbench to answer"
            >
              <span className={APP_PAGE_FIT_LABEL}>Open in Workbench</span>
              <ExternalLink data-icon="inline-end" />
            </Button>
          )}
        </div>
      </header>
      <div className="canvas-page-body">
        <div className="app-page-tabbar flex-none" ref={tabbarRef} data-fit={tabbarFit}>
          {/* Controlled by the URL and ONLY the URL: no onValueChange, so a
              ctrl/middle-click on a trigger opens the address elsewhere without
              also switching this page. Real anchors under the triggers — a tab
              is an address (D420). */}
          <Tabs value={tab} className="app-page-tabs flex-none">
            <TabsList
              variant="line"
              aria-label="Canvas page"
              className="h-auto w-full justify-start rounded-none border-b-0 p-0 pb-1"
            >
              {CANVAS_PAGE_TABS.map((id) => {
                const { label, Icon } = TAB_DEFS[id];
                return (
                  <TabsTrigger
                    key={id}
                    value={id}
                    className="flex-none px-4 py-2.5"
                    nativeButton={false}
                    render={
                      <a
                        href={tabUrl(id)}
                        title={label}
                        aria-label={label}
                        onClick={(e) => pickTab(e, id)}
                      />
                    }
                  >
                    <Icon data-icon="inline-start" />
                    <span className={APP_PAGE_FIT_LABEL}>{label}</span>
                  </TabsTrigger>
                );
              })}
            </TabsList>
          </Tabs>
        </div>

        {error && <ErrorBanner>{error}</ErrorBanner>}

        {/* The stage: the Overview sizes it and stays in flow, shown or
            hidden; the other panels are laid over it (absolute, inset 0), so
            the workbench frame's box is the same on every tab — see the
            header, and `.canvas-page-panel` in canvases.css. */}
        <div className="canvas-page-stage">
          {CANVAS_PAGE_TABS.map((id) => {
            const def = TAB_DEFS[id];
            const active = tab === id;
            if (!active && !def.keepMounted) return null;
            return (
              <section
                key={id}
                className={"canvas-page-panel canvas-page-" + id + (active ? "" : " is-hidden")}
                role="tabpanel"
                aria-hidden={!active}
              >
                {def.render(ctx)}
              </section>
            );
          })}
        </div>
      </div>
    </div>
  );
}

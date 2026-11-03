// The canvas PROJECT page — `/canvases-project/<env>/<name>?_tab=` (owner,
// 2026-10-20: "a project view similar as we have for fused-app for the
// canvas"; "tabbed layout; read only canvas; for visibility"): one canvas as
// a place, the app page's shape (shell/AppPage.tsx, D488). A SEPARATE ROUTE
// from the editor, `/canvases/<env>/<name>` (CanvasWorkspace.tsx on its own:
// two panes, canvas + Claude chat, sync, the edit workflow) — the editor is
// the owner's and is not folded into tabs; the header's "Open" button
// (the app page's, folder icon and all) is the door from here to there. Three tabs, named by the `_tab`
// query param (absent = overview; api.ts CANVAS_PAGE_TABS):
//
//   Overview  the canvas itself — the workbench framed as a VIEWER
//             (CanvasWorkspace `embedded`: `fused_embed_readonly=1`, no
//             drag / connect / add / rename / delete, no autosave) under its
//             sync strip, full width, no right pane. The host's deltas still
//             patch it in place, so a Claude task editing the clone (the
//             Tasks tab) shows live. Sync and its log stay reachable through
//             the strip and its drawer, as in the editor.
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
// Chrome: the app page's header, tab strip and body classes (`.app-page-head`,
// `.app-page-tabbar`, `.app-page-body`, their fit hooks) as they are — the
// same 1050px column and gutters on every tab as the app page (owner,
// 2026-10-20: "the exact same gutters"). Only the panels are this page's.
import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import {
  AppWindow,
  Files,
  FolderOpen,
  ListTodo,
  RefreshCw,
  type LucideIcon,
} from "lucide-react";
import { useUrlVersion } from "@platform/lib/hooks";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { confirmLeave, navigateUrl, replaceSearch } from "@platform/lib/router";
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
  ENV_NAMES,
  canvasPagePath,
  canvasPageTabFromSearch,
  canvasProjectPath,
  getCanvasesStatus,
  type CanvasPageTab,
  type WorkbenchEnv,
} from "@apps/canvases/api";
import { CanvasWorkspace } from "@apps/canvases";
import { CanvasGlyph } from "@apps/canvases/glyph";
import { ChatMount } from "@apps/claude";
import { ClaudeRail } from "./claude-rail/ClaudeRail";
import { chatDoorOnApp, chatDoorOnCanvas, useAppPageClaudePane } from "./useAppPageClaudePane";

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
  /** The Sync tab's panel element while that tab is on screen, else null:
   *  the Overview's CanvasWorkspace portals the sync chrome into it. */
  syncSlot: HTMLElement | null;
  /** The Sync panel's ref callback (a state setter: stable, and a portal
   *  target read from a ref on the first render would be null). */
  setSyncSlot: (el: HTMLElement | null) => void;
  /** The rail's card is pinned or held (ClaudeRail `onCardPinned`): the
   *  Overview's workbench must not take the focus on a pointer enter
   *  meanwhile (CanvasWorkspace `railCardPinned`; Bugbot on #2091). */
  railCardPinned: boolean;
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
    render: ({ env, name, onWorkbenchUrl, syncSlot, railCardPinned }) => (
      <CanvasWorkspace
        env={env}
        name={name}
        embedded
        onWorkbenchUrl={onWorkbenchUrl}
        syncSlot={syncSlot}
        railCardPinned={railCardPinned}
      />
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
  sync: {
    label: "Sync",
    Icon: RefreshCw,
    // Not keepMounted, and EMPTY of its own: the panel is a slot the
    // Overview's CanvasWorkspace portals the sync chrome into (the strip,
    // the log pinned open, Pull / Hard pull, a failed push's detail). The
    // state and the loops live with the Overview, which stays mounted behind
    // every tab; this tab is only where they are shown.
    render: ({ setSyncSlot }) => <div className="canvas-page-sync-slot" ref={setSyncSlot} />,
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
          setError(`No clone folder for the ${ENV_NAMES[env].label} environment.`);
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
    canvasProjectPath(env, name, { tab: next, search: peekSearch(location.search, null) });
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
        canvasProjectPath(env, name, {
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
  const [syncSlot, setSyncSlot] = useState<HTMLElement | null>(null);
  // The rail's card pinned or held (below): the Overview's workbench stops
  // taking the focus on a pointer enter meanwhile, as the editor's does
  // (CanvasWorkspace `focusCanvasOnEnter`).
  const [railCardPinned, setRailCardPinned] = useState(false);
  const ctx = useMemo<TabCtx>(
    () => ({ env, name, dir, onWorkbenchUrl, syncSlot, setSyncSlot, railCardPinned }),
    [env, name, dir, onWorkbenchUrl, syncSlot, railCardPinned],
  );

  // THE CLAUDE PANE AND ITS RAIL (owner, 2026-11-03: "we show sidebar in
  // projects page then we should show it in canvas page as well —
  // consistency"): the app page's column, as is (AppPage.tsx "THE CLAUDE
  // PANE AND ITS RAIL", useAppPageClaudePane.ts) — the canvas chat on the
  // clone folder beside the WHOLE page, header and tabs included; open or
  // shut by the shared flag and `side=off`; the Claude rail while shut, with
  // the canvas's chats on it (claude-rail-lib `RailPlace` kind "canvas", no
  // node: the whole canvas, as the editor's rail with nothing selected). The
  // Overview's embedded workbench draws no pane of its own
  // (CanvasWorkspace `embedded`), so the two never double up.
  const claudeSplitRef = useRef<HTMLDivElement | null>(null);
  const claude = useAppPageClaudePane(claudeSplitRef);
  // A ring's door: the CANVAS chat opens in this pane (`chatDoorOnCanvas`;
  // a queued row's explorer door on the clone folder too, `chatDoorOnApp`),
  // behind the composer's leave guard; a NODE's chat, or anything else,
  // navigates as it says — the editor shows a node's chat on that node.
  const [claudeSeq, setClaudeSeq] = useState(0);
  const openChatHere = (href: string) => {
    const door = chatDoorOnCanvas(href, env, name) ?? (dir ? chatDoorOnApp(href, dir) : null);
    if (!door) {
      navigateUrl(href);
      return;
    }
    void confirmLeave().then((ok) => {
      if (!ok) return;
      const next = new URL(location.href);
      for (const k of ["session_id", "queued", "backend"]) {
        const v = door.params[k];
        if (v) next.searchParams.set(k, v);
        else next.searchParams.delete(k);
      }
      next.searchParams.delete("side");
      if (next.href !== location.href) replaceSearch(next.pathname + next.search + next.hash);
      setClaudeSeq((n) => n + 1);
      claude.show();
    });
  };
  const railPlace = useMemo(() => ({ kind: "canvas" as const, env, name, dir }), [env, name, dir]);

  return (
    <div className={"app-page-split" + (claude.dragging ? " is-dragging" : "")} ref={claudeSplitRef}>
    <div className="app-page-left">
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
                title={`${name} syncs with ${ENV_NAMES[env].label}`}
              >
                {ENV_NAMES[env].tag}
              </span>
            </h1>
            {/* The hosted workbench editor's URL, as the caption under the
                name (owner, 2026-10-20: the workbench link here, not the
                folder; the clone folder is the Files tab's and the sidebar
                menu's). A real anchor in a new tab: the native shell routes
                target=_blank to the system browser. "Loading…" until the
                URL is known. */}
            {workbenchUrl ? (
              <a
                className="app-page-folder"
                href={workbenchUrl}
                target="_blank"
                rel="noopener noreferrer"
                title="Open this canvas in the Fused workbench, in a new tab"
              >
                {workbenchUrl}
              </a>
            ) : (
              // Not the folder meanwhile (owner, 2026-10-20): a caption that
              // changes meaning as it loads reads as two facts.
              <span className="app-page-folder" aria-busy="true">
                Loading…
              </span>
            )}
          </div>
        </div>
        <div className="app-page-actions">
          {/* The canvas EDITOR (/canvases/<env>/<name>): the workbench beside
              the Claude chat, sync and the edit workflow — the page this
              read-only view is a window onto. In-app, same tab. */}
          <Button
            size="sm"
            variant="default"
            className="app-page-open"
            nativeButton={false}
            render={
              <a
                href={canvasPagePath(env, name)}
                onClick={(e) => {
                  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                  e.preventDefault();
                  navigateUrl(canvasPagePath(env, name));
                }}
              />
            }
            title="Open this canvas in the editor: the workbench with Claude beside it"
          >
            <span className={APP_PAGE_FIT_LABEL}>Open</span>
            <FolderOpen data-icon="inline-end" />
          </Button>
          {/* No "Open in Workbench" button: the caption under the name IS
              the workbench link (owner, 2026-10-20). */}
        </div>
      </header>
      {/* THE APP PAGE'S BODY, as is (`.app-page-body`, app-page.css): the same
          1050px column and gutters on every tab as the app page has (owner,
          2026-10-20: "the exact same gutters on canvas project view"). */}
      <div className="app-page-body canvas-page-body">
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
    </div>
      {claude.open ? (
        <div className="app-page-claude" style={{ width: claude.width }}>
          <div
            className={"preview-side-divider" + (claude.dragging ? " dragging" : "")}
            role="separator"
            aria-orientation="vertical"
            aria-label="Drag to resize the Claude panel"
            title="Drag to resize"
            onPointerDown={claude.onSeamPointerDown}
          />
          <div className="app-page-claude-chat">
            {dir ? (
              <ChatMount
                // Remounted on a ring's door (`openChatHere`): the session is
                // read off the URL at boot.
                key={claudeSeq}
                file={dir}
                chatOnly
                paramsSource="url"
                // The workbench in the Overview is what the chat's notes point
                // at (the editor hands its own frame over the same way).
                annotateTarget={() => document.querySelector<HTMLIFrameElement>(".canvas-page-overview iframe")}
                onHidePane={claude.hide}
                onNavigate={openChatHere}
              />
            ) : (
              <SkeletonLines rows={3} label="Loading the canvas" />
            )}
          </div>
        </div>
      ) : (
        <ClaudeRail
          place={railPlace}
          // The + makes a task on the clone (the editor's own seed with no
          // node selected); held until the clone folder is known.
          newTask={{
            target: dir,
            waiting: dir === null ? "Loading the canvas…" : null,
            on: null,
            where: { canvas: name, node: null },
          }}
          activeSessionId={new URLSearchParams(location.search).get("session_id")}
          onOpen={claude.show}
          onOpenChat={openChatHere}
          onPullStart={claude.onRailPull}
          onCardPinned={setRailCardPinned}
        />
      )}
    </div>
  );
}

// Browser Bots: OpenBot's page as React, mounted by the shell for every /bots/* path (shell/App.tsx, routes.ts)
// inside #content — ONE mount for the whole URL space; the shell keeps this branch unkeyed, and useBotsRoute reads
// the path. The root `.bots-page` element carries the scoped stylesheet (styles/bots.css) and the layout classes
// (lib/layout.ts, lib/root.ts); inside it the banner and the <main> grid (bot list · gutter · chat · gutter ·
// preview), then the route's page above it (Tasks, Apps, Preferences — absolute overlays, so the chat and its Stage
// stay mounted beneath) and the fixed layers (build dialog, dialogs, context menu). The live view lives in the
// preview column (PreviewPane); the store's `fast` flag drives the Stage layout (installStage below). Boot order
// mirrors OpenBot: layout first (useLayout, before paint), then the store's poll loop, the builds feed,
// notifications and the face animator. The theme is the shell's (`data-theme` on <html>); the page only maps it
// onto its own tokens.
import { useEffect } from "react";
import "./styles/bots.css";
import { AppsPanel } from "./apps/AppsPanel";
import { BuildDialog } from "./builds/BuildDialog";
import { openBuilds, startBuilds } from "./builds/builds";
import { TasksPage } from "./builds/TasksPage";
import { BotList } from "./components/BotList";
import { BotMenu } from "./components/BotMenu";
import { ChatPane } from "./components/ChatPane";
import { useFaceAnimator } from "./components/faceAnim";
import { PreviewPane } from "./components/PreviewPane";
import { Dialogs } from "./dialogs/Dialogs";
import { openApps } from "./apps/apps";
import { getSideApp, sideAppBrowser, sideAppShow } from "./apps/side";
import { useBotsRoute } from "./hooks/useBotsRoute";
import { prefsHref, searchSansTask } from "./routes";
import { navigateUrl } from "@platform/lib/router";
import { PrefsPanel } from "./components/PrefsPanel";
import { useLayout } from "./hooks/useLayout";
import { handBack, openFull } from "./lib/cdp";
import { onDragEnd, onStageOpen, onStageShut, setStage } from "./lib/layout";
import { fitViewport } from "./lib/live-page";
import { installNotify } from "./lib/notify";
import { botsRoot, detachPortalHost, portalHost, setBotsRoot } from "./lib/root";
import { isBot } from "@platform/lib/flavor";
import { getState, goChat, hideBanner, hydrateBotsPrefs, openDialog, openMenu, poll, startStore, subscribe, useBotsSelector } from "./state/store";

/** Stage follows the store's `fast` (the live view is open). A store listener, not an effect: it runs inside the commit, so
 *  openFull's flushSync(setFast(true)) has #full laid out in the column before it focuses anything there. On entry the
 *  Tasks / Apps / Preferences page (a route) gives way to the chat and a showing side app switches to Browser; on exit the side app comes back if it still exists. */
function installStage(): () => void {
  let on = false, sideWas = false;
  const sync = () => {
    const fast = getState().fast;
    if (fast === on) return;
    on = fast;
    if (fast) {
      sideWas = botsRoot().classList.contains("sideapp");
      if (sideWas) sideAppBrowser();
      setStage(true);
      goChat();  // navigates; the store commits again on the way, and `on` is already set, so this listener ignores it
    } else {
      setStage(false);
      if (sideWas && getSideApp()) sideAppShow();
      sideWas = false;
    }
  };
  sync();
  const stop = subscribe(sync);
  // The right gutter crosses the live page's half-width floor: the preview dragged up to it opens the live view (watch mode, the
  // keyboard stays where it is), the rail dragged back under it is the same exit as the Back button.
  onStageOpen(() => openFull({ focus: false }));
  onStageShut(() => { void handBack(true); });
  // The live page's Chrome window takes the stage's size once, when the gutter is let go (fitViewport holds while root.dragging).
  onDragEnd(fitViewport);
  return () => { stop(); onStageOpen(null); onStageShut(null); onDragEnd(null); if (on) setStage(false); };
}

function Banner() {
  const banner = useBotsSelector((s) => s.banner);
  return (
    <div id="banner" className={banner.show ? "show" : ""}>
      <span id="bannerText">{banner.text}</span>
      <button id="bannerBtn" onClick={() => { hideBanner(); void poll(); }}>Retry</button>
    </div>
  );
}

export function Bots() {
  const { gutter } = useLayout();
  const route = useBotsRoute();
  useEffect(() => { portalHost(); return detachPortalHost; }, []);
  // The page-wide switches + the one-shot localStorage import (D1377). Once per document (it guards itself), and
  // before startStore so an imported seen map is in the store when the first poll merges the server's.
  useEffect(() => { void hydrateBotsPrefs(); }, []);
  useEffect(() => startStore(), []);
  useEffect(() => startBuilds(), []);
  useEffect(() => installStage(), []);
  // `/bots?new=1` opens the "+ New bot" chooser (FusedBot's setup wizard handed
  // over this way). Read once, then dropped from the URL so a refresh does not
  // reopen it.
  // `/bots?phone=1` (Preferences' Phone row) opens Super Bot's Settings on the Phone tab once the list knows it.
  useEffect(() => {
    const u = new URL(location.href);
    const wantNew = u.searchParams.get("new") === "1", wantPhone = u.searchParams.get("phone") === "1";
    if (!wantNew && !wantPhone) return;
    u.searchParams.delete("new"); u.searchParams.delete("phone");
    history.replaceState(history.state, "", u.pathname + (u.search || "") + u.hash);
    if (wantNew) { openDialog({ kind: "newBot" }); return; }
    const superId = (): string | undefined => getState().bots.find((b) => b.kind === "super")?.id;
    const now = superId();
    if (now) { openDialog({ kind: "settings", id: now, tab: "phone" }); return; }
    // Not listed yet: open on the first poll that carries it. Unsubscribe BEFORE opening — openDialog commits to the
    // store, which notifies this very listener, and a listener that opens again from inside that notification recurses
    // until the stack is gone.
    let done = false;
    const stop = subscribe(() => {
      if (done) return;
      const id = superId();
      if (!id) return;
      done = true; stop();
      openDialog({ kind: "settings", id, tab: "phone" });
    });
    return () => { done = true; stop(); };
  }, []);
  useEffect(() => installNotify(), []);
  useFaceAnimator();
  return (
    <div className="bots-page" ref={setBotsRoot}>
      <Banner />
      <main>
        <BotList
          onAddBot={() => openDialog({ kind: "newBot" })}
          onOpenUsage={() => openDialog({ kind: "usage" })}
          onOpenBrowsers={() => openDialog({ kind: "browsers" })}
          onOpenBuilds={() => openBuilds()}
          onOpenApps={openApps}
          onOpenPrefs={isBot() ? () => navigateUrl(prefsHref(searchSansTask(location.search))) : undefined}
          onContextMenu={(id, x, y) => openMenu({ id, x, y })}
        />
        <div className="gutter l" title="Drag to resize · double-click to reset" {...gutter("l")} />
        <ChatPane />
        <div className="gutter r" title="Drag to resize · double-click to reset" {...gutter("r")} />
        <PreviewPane />
      </main>
      {/* The route's page, above the chat (routes.ts). Each mounts only while its path is on screen, so its iframe is fresh per visit. */}
      {route.kind === "tasks" ? <TasksPage task={route.task} /> : null}
      {route.kind === "apps" ? <AppsPanel folder={route.folder} /> : null}
      {route.kind === "prefs" && isBot() ? <PrefsPanel /> : null}
      <BuildDialog />
      <Dialogs />
      <BotMenu />
    </div>
  );
}

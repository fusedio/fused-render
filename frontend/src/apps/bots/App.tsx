// Browser Bots: OpenBot's page as React, mounted by the shell at /bots (shell/App.tsx) inside #content. The root
// `.bots-page` element carries the scoped stylesheet (styles/bots.css) and the layout classes (lib/layout.ts,
// lib/root.ts); inside it the banner and the <main> grid (bot list · gutter · chat · gutter · preview), then the
// fixed layers (Builds, Apps, dialogs, context menu). The live view lives in the preview column (PreviewPane); the
// store's `fast` flag drives the Stage layout (installStage below). Boot order mirrors OpenBot: layout first
// (useLayout, before paint), then the store's poll loop, notifications and the face animator. The theme is the
// shell's (`data-theme` on <html>); the page only maps it onto its own tokens.
import { useEffect } from "react";
import "./styles/bots.css";
import { AppsPanel } from "./apps/AppsPanel";
import { BuildsPanel } from "./builds/BuildsPanel";
import { BotList } from "./components/BotList";
import { BotMenu } from "./components/BotMenu";
import { ChatPane } from "./components/ChatPane";
import { useFaceAnimator } from "./components/faceAnim";
import { PreviewPane } from "./components/PreviewPane";
import { Dialogs } from "./dialogs/Dialogs";
import { closeView } from "./apps/apps";
import { getSideApp, sideAppBrowser, sideAppShow } from "./apps/side";
import { PrefsPanel } from "./components/PrefsPanel";
import { useLayout } from "./hooks/useLayout";
import { handBack } from "./lib/cdp";
import { onStageShut, setStage } from "./lib/layout";
import { installNotify } from "./lib/notify";
import { botsRoot, detachPortalHost, portalHost, setBotsRoot } from "./lib/root";
import { isBot } from "@platform/lib/flavor";
import { closePanel, getState, hideBanner, openDialog, openMenu, openPanel, poll, startStore, subscribe, useBotsSelector } from "./state/store";

/** Stage follows the store's `fast` (the live view is open). A store listener, not an effect: it runs inside the commit, so
 *  openFull's flushSync(setFast(true)) has #full laid out in the column before it focuses anything there. On entry the
 *  Builds / Apps panels and the app viewer close and a showing side app switches to Browser; on exit the side app comes back if it still exists. */
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
      closeView();
      closePanel();  // commits again; `on` is already set, so this listener ignores it
    } else {
      setStage(false);
      if (sideWas && getSideApp()) sideAppShow();
      sideWas = false;
    }
  };
  sync();
  const stop = subscribe(sync);
  onStageShut(() => { void handBack(true); });  // the rail dragged past the live page's half-width floor: same exit as the Back button
  return () => { stop(); onStageShut(null); if (on) setStage(false); };
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
  useEffect(() => { portalHost(); return detachPortalHost; }, []);
  useEffect(() => startStore(), []);
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
          onOpenBuilds={() => openPanel("builds")}
          onOpenApps={() => openPanel("apps")}
          onOpenPrefs={isBot() ? () => openPanel("prefs") : undefined}
          onContextMenu={(id, x, y) => openMenu({ id, x, y })}
        />
        <div className="gutter l" title="Drag to resize · double-click to reset" {...gutter("l")} />
        <ChatPane />
        <div className="gutter r" title="Drag to resize · double-click to reset" {...gutter("r")} />
        <PreviewPane />
      </main>
      <BuildsPanel />
      <AppsPanel />
      <PrefsPanel />
      <Dialogs />
      <BotMenu />
    </div>
  );
}

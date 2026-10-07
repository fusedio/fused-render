// Browser Bots: OpenBot's page as React, mounted by the shell at /bots (shell/App.tsx) inside #content. The root
// `.bots-page` element carries the scoped stylesheet (styles/bots.css) and the layout classes (lib/layout.ts,
// lib/root.ts); inside it the banner and the <main> grid (bot list · gutter · chat · gutter · preview), then the
// fixed layers (live view, Builds, Apps, dialogs, context menu). Boot order mirrors OpenBot: layout first
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
import { LiveView } from "./components/LiveView";
import { PreviewPane } from "./components/PreviewPane";
import { Dialogs } from "./dialogs/Dialogs";
import { useLayout } from "./hooks/useLayout";
import { installNotify } from "./lib/notify";
import { detachPortalHost, portalHost, setBotsRoot } from "./lib/root";
import { getState, hideBanner, openDialog, openMenu, openPanel, poll, startStore, subscribe, useBotsSelector } from "./state/store";

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
    const tryOpen = (): boolean => {
      const sb = getState().bots.find((b) => b.kind === "super");
      if (!sb) return false;
      openDialog({ kind: "settings", id: sb.id, tab: "phone" });
      return true;
    };
    if (tryOpen()) return;
    const stop = subscribe(() => { if (tryOpen()) stop(); });
    return stop;
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
          onOpenBuilds={() => openPanel("builds")}
          onOpenApps={() => openPanel("apps")}
          onContextMenu={(id, x, y) => openMenu({ id, x, y })}
        />
        <div className="gutter l" title="Drag to resize · double-click to reset" {...gutter("l")} />
        <ChatPane />
        <div className="gutter r" title="Drag to resize · double-click to reset" {...gutter("r")} />
        <PreviewPane />
      </main>
      <LiveView />
      <BuildsPanel />
      <AppsPanel />
      <Dialogs />
      <BotMenu />
    </div>
  );
}

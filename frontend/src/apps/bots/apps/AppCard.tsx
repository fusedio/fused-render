// The app card under a thread message (OpenBot apps.js appCardHtml + toggleAppInline). The thread appends it under a
// "your app is ready" message, a `show` action, or a message carrying a /render?path=… link (appFromText).
// Collapsed it is one row with no iframe, so a long thread does not run every app it ever built. "Open here" loads the
// app inline at full thread width; "Beside chat" hands it to the right column.
import { useEffect, useRef, useState } from "react";
import type { AppRef } from "../lib/api";
import { appEmbedBase, appOpenUrl, applyAppParams, paramString } from "./apps";
import { showAppBeside } from "./side";

export interface AppCardProps {
  app: AppRef;
  /** "Beside chat" (default: showAppBeside). Gets {name, dir, params} with params as a query string. */
  onBeside?: (app: AppRef) => void;
}

export function AppCard({ app, onBeside }: AppCardProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const folder = (app.dir || "").split("/").pop() || "";
  const name = app.name || folder;
  const params = paramString(app.params);
  const toggle = (on: boolean) => {
    if (on && !open) applyAppParams(params);  // before the frame loads, so the app reads its state
    setOpen(on);
  };
  useEffect(() => { if (open) ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [open]);
  return (
    <div ref={ref} className={`appmsg${open ? " open" : ""}`} data-dir={app.dir} data-name={name} data-params={params}>
      <div className="hd"><span className="ico">⧉</span>
        <div className="txt">
          <b>{name}{app.tools ? <> <span className="tools" title="Bots can call these tools">⚒ {String(app.tools)}</span></> : null}</b>
          <small>{app.dir}</small>
        </div>
        <span className="abtns">
          <button data-app="here" className="primary" onClick={() => toggle(!open)}>{open ? "Close" : "Open here"}</button>
          <button data-app="side" onClick={() => { toggle(false); (onBeside || showAppBeside)({ name, dir: app.dir, params }); }}>Beside chat</button>
          <a className="btnlink" href={appOpenUrl(app.dir, params)} target="_blank" rel="noopener">New tab</a>
        </span>
      </div>
      {/* Unmounting the iframe on close stops the app running. */}
      <div className="frame">{open ? <iframe src={appEmbedBase(app.dir, params)} title={name} /> : null}</div>
    </div>
  );
}

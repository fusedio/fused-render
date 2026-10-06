// #astrip (OpenBot apps.js renderStarters / starterAction): the starter apps that are not installed yet, one small row
// above the gallery with an Install button each. The strip is a catalog, not a list of apps: an installed starter has
// left it and shows as a gallery card (AppsPanel.tsx Card, with its badge and Update). With nothing left to install
// the strip is gone altogether. AppsPanel owns the rows and their load order; this draws them and runs Install, which
// reloads the apps and goes straight into the new app's viewer (its Setup box is the next step); a failure lands in
// the banner and re-enables the button.
import { useState } from "react";
import { api, starterIconUrl, type AppRow } from "../lib/api";
import { errMsg, showBanner } from "../state/store";
import { viewApp } from "./apps";
import { shipsWith, busyLabel, toInstall, type StarterRow } from "./starters";

export interface StartersStripProps {
  /** Every starter; the strip keeps the ones not installed. */
  rows: StarterRow[];
  /** Rescan the apps folder (and the starters after it); resolves with the fresh gallery rows. */
  reloadApps: () => Promise<AppRow[] | null>;
}

export function StartersStrip({ rows, reloadApps }: StartersStripProps) {
  const [busy, setBusy] = useState<Record<string, true>>({});
  const todo = toInstall(rows);
  if (!todo.length) return <div className="astrip" id="astrip" hidden />;

  const install = async (s: StarterRow) => {
    setBusy((b) => ({ ...b, [s.key]: true }));
    try {
      const r = await api.starterInstall(s.key);
      const fresh = await reloadApps();  // the row leaves the strip and the card appears in one redraw
      const a = (fresh || []).find((x) => x.dir === r.dir);
      if (a) viewApp(a);  // straight into the app: its Setup box is the next step
    } catch (e) { showBanner(`Could not install ${s.name}: ${errMsg(e)}`); }
    finally { setBusy((b) => { const n = { ...b }; delete n[s.key]; return n; }); }
  };

  return (
    <div className="astrip" id="astrip">
      <div className="ahd"><b>Starter apps</b><small>Ready-made apps that ship with {shipsWith()}; installed ones are in the gallery below. A bot made from the matching preset installs its own.</small></div>
      <div className="srow">
        {todo.map((s, i) => (
          <div key={s.key} className="scard" data-i={i}>
            {s.icon ? <img className="ico" src={starterIconUrl(s.key)} alt="" /> : <span className="ico ph">{s.name.slice(0, 1)}</span>}
            <div className="txt"><b>{s.name}</b><small title={s.desc}>{s.desc}</small></div>
            {s.tools ? <span className="badge" title="Bots can call these tools">⚒ {String(s.tools)}</span> : null}
            <button data-s="install" className="primary" disabled={!!busy[s.key]} onClick={() => { void install(s); }}>
              {busy[s.key] ? busyLabel("install") : "Install"}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

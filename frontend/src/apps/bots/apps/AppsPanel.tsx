// #apanel (OpenBot index.html + apps.js): a gallery of every fused app under the apps root (builds, uploads and
// installed starters alike), shown while ui.panel === "apps". Each card is a live, scaled-down render of the app's
// entry page (a sandboxed lazy iframe with _preview=1); clicking it opens the app full-size inside this page (#vpanel,
// AppViewer), with modifier clicks keeping the plain /render link (new tab). A card whose folder is an installed
// starter (matched on dir) also carries the starter's badge (Installed / Needs setup / Ready) and an Update button
// when a newer version ships. Above the gallery, the strip of starters not installed yet (StartersStrip.tsx).
// Upload: a zipped fused app (Finder "Compress", a .fused export),
// by button or dropped anywhere on the panel, is unpacked into a new folder (POST /api/bot-apps/import). Esc steps back one
// level: app → gallery → bots (never while the build dialog is open).
import { ArrowLeftIcon, PlusIcon, RefreshCwIcon, UploadIcon } from "lucide-react";
import { useEffect, useRef, useState, type DragEvent, type MouseEvent } from "react";
import { askConfirm } from "../dialogs/ask";
import { api, appIconUrl, type AppRow } from "../lib/api";
import { errMsg, showBanner, useBotsSelector } from "../state/store";
import { isBuildDialogOpen, newBuild } from "../builds/builds";
import { agoShort, appEmbed, appOpenUrl, closeApps, closeView, getViewedApp, useAppsRoot, viewApp } from "./apps";
import { AppViewer } from "./AppViewer";
import { StartersStrip } from "./StartersStrip";
import { busyLabel, needsStatus, starterBadge, starterOf, starterRows, updateButton, updateConfirm, withStatus, type StarterRow } from "./starters";

const UPLOAD_MAX = 64 * 1024 * 1024;
const fileB64 = (f: File) => new Promise<string>((ok, no) => {
  const r = new FileReader();
  r.onload = () => ok(String(r.result).split(",")[1] || "");
  r.onerror = () => no(r.error);
  r.readAsDataURL(f);
});

type Grid = { kind: "looking" } | { kind: "blank" } | { kind: "rows"; rows: AppRow[] } | { kind: "error"; msg: string };

/** `starter` is set when this app folder is an installed starter: the card shows its state and offers Update. */
function Card({ a, starter, reloadApps }: { a: AppRow; starter: StarterRow | null; reloadApps: () => Promise<AppRow[] | null> }) {
  const [updating, setUpdating] = useState(false);
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;  // modifier click keeps the plain link: new tab
    e.preventDefault(); viewApp(a);
  };
  // The button sits inside the card link: stop the click there so it never opens the viewer.
  const onUpdate = async (e: MouseEvent<HTMLButtonElement>) => {
    e.preventDefault(); e.stopPropagation();
    if (!starter) return;
    const [t, x] = updateConfirm(starter);
    if (!(await askConfirm(t, x, "Update", false))) return;
    setUpdating(true);
    try { await api.starterUpdate(starter.key); await reloadApps(); }
    catch (err) { showBanner(`Could not update ${starter.name}: ${errMsg(err)}`); }
    finally { setUpdating(false); }
  };
  const badge = starter ? starterBadge(starter) : null, upd = starter ? updateButton(starter) : null;
  return (
    <a className="appcard" href={appOpenUrl(a.dir)} title={`Open ${a.name} here (⌘-click for a new tab)`} onClick={onClick}>
      <div className="shot"><iframe src={appEmbed(a.dir)} tabIndex={-1} loading="lazy" title={`${a.name} preview`} sandbox="allow-scripts allow-same-origin" /></div>
      <div className="meta">
        {a.icon ? <img className="ico" src={appIconUrl(a.dir)} alt="" /> : <span className="ico ph">{(a.name || "?").slice(0, 1).toUpperCase()}</span>}
        <div className="txt"><b>{a.name}</b><small>{a.desc || a.folder}</small></div>
        {a.tools ? <span className="tools" title="Bots can call these tools">⚒ {String(a.tools)}</span> : null}
        {badge ? <span className={badge.cls} title={badge.title}>{badge.text}</span> : null}
        {upd ? <button data-s="update" title={upd.title} disabled={updating} onClick={(e) => { void onUpdate(e); }}>{updating ? busyLabel("update") : upd.label}</button> : null}
        <time>{agoShort(a.mtime)}</time>
      </div>
    </a>
  );
}

export function AppsPanel() {
  const open = useBotsSelector((s) => s.ui.panel === "apps");
  const root = useAppsRoot();
  const [grid, setGrid] = useState<Grid>({ kind: "looking" });
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null), fileRef = useRef<HTMLInputElement>(null);
  const seq = useRef(0);
  const [starters, setStarters] = useState<StarterRow[]>([]);

  // The starters list loads after the gallery (the two calls are chained, never in flight together), so a card's
  // starter badge lands a moment after the card; the slow status call follows only when some installed starter has a
  // setup tool. A failed status leaves every badge at "Installed".
  const loadStarters = async (mine: number): Promise<void> => {
    let rows: StarterRow[];
    try { rows = starterRows((await api.starters()).starters); }
    catch { rows = []; }
    if (mine !== seq.current) return;
    setStarters(rows);
    if (!needsStatus(rows)) return;
    try {
      const r = await api.starterStatus();
      if (mine === seq.current) setStarters((cur) => withStatus(cur, r?.ready));
    } catch { /* badge stays at "Installed" */ }
  };

  const loadApps = async (): Promise<AppRow[] | null> => {
    const mine = ++seq.current;
    let out: AppRow[] | null;
    try {
      const rows = (await api.apps()).apps || [];
      if (mine === seq.current) setGrid({ kind: "rows", rows });
      out = rows;
    } catch (e) {
      if (mine === seq.current) setGrid({ kind: "error", msg: errMsg(e) });
      out = null;
    }
    await loadStarters(mine);  // after the gallery
    return out;
  };

  // Opening lists the folder; closing (Back, Esc, another panel taking the slot) closes the viewer, drops the
  // gallery's iframes so idle apps stop running, and hides the starter strip.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open) { wasOpen.current = true; void loadApps(); return; }
    if (!wasOpen.current) return;
    seq.current++;
    closeView();
    setGrid({ kind: "blank" });
    setStarters([]);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || isBuildDialogOpen()) return;
      if (getViewedApp()) closeView(); else closeApps();  // Esc steps back one level: app → gallery → bots
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  // importapp strips the wrapper folder, refuses paths that escape the target and never overwrites an existing app.
  const uploadApp = async (file: File | null | undefined) => {
    if (!file) return;
    if (!/\.(zip|fused)$/i.test(file.name)) { showBanner(`${file.name}: upload a .zip of the app folder or a .fused export.`); return; }
    if (file.size > UPLOAD_MAX) { showBanner(`${file.name} is ${(file.size / 1048576).toFixed(0)} MB; app uploads are limited to 64 MB.`); return; }
    setBusy(true);
    try {
      const r = await api.importApp(file.name, await fileB64(file));
      const rows = await loadApps();
      const a = (rows || []).find((x) => x.dir === r.dir || x.folder === r.folder);
      if (a) viewApp(a); else showBanner(`${file.name} landed in ${r.dir} but has no fused-app marker in its index.html, so it is not listed as an app.`);
    } catch (e) { showBanner(`Could not import ${file.name}: ${errMsg(e)}`); }
    finally { setBusy(false); }
  };

  const dropOk = () => open && !getViewedApp();
  const onDragOver = (e: DragEvent<HTMLDivElement>) => { if (dropOk()) { e.preventDefault(); setDrag(true); } };
  const onDragLeave = (e: DragEvent<HTMLDivElement>) => { if (!panelRef.current?.contains(e.relatedTarget as Node | null)) setDrag(false); };
  const onDrop = (e: DragEvent<HTMLDivElement>) => { e.preventDefault(); setDrag(false); if (dropOk()) void uploadApp(e.dataTransfer?.files?.[0]); };

  const under = root ? ` under ${root}` : "";
  return (
    <>
      <div id="apanel" ref={panelRef} className={[open ? "show" : "", drag ? "drag" : "", busy ? "busy" : ""].filter(Boolean).join(" ")}
        onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}>
        <div className="topbar">
          <button id="aback" className="backtxt" aria-label="Back" title="Back to bots (Esc)" onClick={closeApps}><ArrowLeftIcon /></button>
          <b className="ttl">Apps</b><small className="muted">Fused apps the builds have made{under}</small>
          <span className="winacts">
            <button id="areload" title="Rescan the apps folder" onClick={() => void loadApps()}><RefreshCwIcon />Refresh</button>
            <button id="aupload" disabled={busy} onClick={() => fileRef.current?.click()}
              title={`Upload a .fused export or a zipped app folder (or drop it anywhere on this panel); it is unpacked into a new folder${under}`}><UploadIcon />{busy ? "Uploading…" : "Upload app"}</button>
            <input type="file" id="auploadfile" ref={fileRef} accept=".zip,.fused,application/zip" hidden
              onChange={(e) => { const f = e.currentTarget.files?.[0]; e.currentTarget.value = ""; void uploadApp(f); }} />
            <button id="anew" className="primary" title="Describe an app; Claude builds it as a task" onClick={() => { closeApps(); void newBuild(); }}><PlusIcon />New app</button>
          </span>
        </div>
        <div className="ascroll">
          <StartersStrip rows={open ? starters : []} reloadApps={loadApps} />
          <div className="agrid" id="agrid">
            {!open || grid.kind === "blank" ? null
              : grid.kind === "looking" ? <div className="empty">Looking for apps…</div>
              : grid.kind === "error" ? <div className="empty">Could not read the apps folder.<br />{grid.msg}</div>
              : !grid.rows.length ? <div className="empty">No apps yet.<br />Start one with New app; it shows up here when Claude has written its page.</div>
              : grid.rows.map((a) => <Card key={a.dir} a={a} starter={starterOf(starters, a.dir)} reloadApps={loadApps} />)}
          </div>
        </div>
      </div>
      <AppViewer />
    </>
  );
}

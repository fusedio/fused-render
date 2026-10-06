// The right column (OpenBot index.html section.preview + chat.js render()'s preview half): header (hide, ☰ menu),
// the side app strip, the screenshot (click → live view), the status toast, the caption and the info sections
// (model download, task, Inbox, attached files, routines, usage).
import { useEffect, type MouseEvent } from "react";
import { SideApp } from "../apps/SideApp";
import { api, rawFileUrl, shotUrl, type Artifact, type Bot } from "../lib/api";
import { mirrorThumb, openFromThumb } from "../lib/cdp";
import { fmtAgo, fmtBytes, fmtWhenShort } from "../lib/format";
import { closePreview } from "../lib/layout";
import { act, closeMenu, getState, openDialog, openMenu, useBotsSelector } from "../state/store";
import { Toast } from "./Toast";

const KIND_LABEL: Record<string, string> = { save: "saved", download: "download", build: "app" };
const ext = (n: string) => (/\.([a-z0-9]{1,5})$/i.exec(n)?.[1] || "").toUpperCase();

// Inbox: what this bot produced for you (save results, downloads, built apps), newest first, one card each.
// OpenBot opens a card's file in the explorer (/explorer/view/<path>); lite has no file explorer (that route opens
// the chat here), so a card opens the raw file inline in a new tab instead (the browser shows what it can: images,
// PDFs, text). A small ↓ beside each file card keeps the raw download. Built-app cards open the app, as before.
// "Open folder" reveals the bot's Inbox in Finder.
function InboxCard({ a }: { a: Artifact }) {
  const build = a.kind === "build";
  const sub = build ? "Built app" : `${KIND_LABEL[a.kind] || a.kind}${a.size ? " · " + fmtBytes(a.size) : ""}`;
  return (
    <span className="cardrow">
      <a className="card" href={build ? a.link || "#" : rawFileUrl(a.path)} target="_blank" rel="noopener" title={a.task || ""}>
        <span className="ico">{build ? "⧉" : ext(a.name) || "•"}</span>
        <span className="body">
          <span className="nm">{build ? a.title || a.name : a.name}</span>
          <span className="sub">{sub}{a.task ? " · " + a.task.slice(0, 60) : ""}</span>
        </span>
        <small>{fmtWhenShort(a.ts)}</small>
      </a>
      {build ? null : <a className="dl" href={rawFileUrl(a.path)} download={a.name} title={`Download ${a.name}`} onClick={(e) => e.stopPropagation()}>↓</a>}
    </span>
  );
}

function Inbox({ b }: { b: Bot }) {
  const arts = b.browser?.artifacts || [];
  return (
    <section className="inbox">
      <h4>Inbox</h4>
      {arts.length ? arts.map((a, i) => <InboxCard key={`${a.ts}:${a.path}:${i}`} a={a} />)
        : <div className="fine">Nothing yet. Files the bot saves or downloads for you land here.</div>}
      <div className="fine" style={{ marginTop: 6 }}>
        <button className="lnk" data-reveal="1" title={b.browser?.artifacts_dir || ""} onClick={() => { void act(() => api.reveal(b.id), true); }}>Open folder</button>
      </div>
    </section>
  );
}

function Routines({ b }: { b: Bot }) {
  const rs = b.routines || [];
  const open = () => openDialog({ kind: "routines", id: b.id });
  return (
    <section>
      {rs.length ? <h4>Routines</h4> : null}
      {rs.length ? (
        <>
          {rs.slice(0, 4).map((r) => (
            <div key={r.id} className={`rt ${r.enabled ? "" : "off"}`}>
              <span title={r.task}>{r.task}</span>
              <small>{r.enabled ? fmtWhenShort(r.next) || "soon" : "paused"}</small>
            </div>
          ))}
          <div className="fine" style={{ marginTop: 6 }}>
            <button className="lnk" data-open="routines" onClick={open}>{rs.length > 4 ? `All ${rs.length} routines` : "Manage routines"}</button>
          </div>
        </>
      ) : (
        <div className="note">Routines are recurring tasks this bot runs on a schedule.<br /><button className="lnk" data-open="routines" onClick={open}>Add one</button></div>
      )}
    </section>
  );
}

/** One line for the bot you are looking at, in its info panel (OpenBot dialogs.js usageStrip). */
function UsageStrip({ b }: { b: Bot }) {
  const row = useBotsSelector((s) => (s.usage?.bots || []).find((x) => x.id === b.id));
  if (!row) return null;
  return (
    <section>
      <h4>Usage</h4>
      <div className="ustrip">
        <span title="Model calls today">{row.today} today</span>
        <span title="Model calls in the last 7 days">{row.week} this week</span>
        {row.errors ? <span className="warn">{row.errors} failed</span> : null}
        <span title="Last model call">{fmtAgo(row.last)}</span>
      </div>
    </section>
  );
}

function Info({ b }: { b: Bot }) {
  const files = b.browser?.files || [];
  return (
    <div className="info" id="info">
      {b.dl_pct != null ? (
        <section><h4>Downloading model</h4><div className="dlbar"><div className="dlfill" style={{ width: `${b.dl_pct}%` }} /></div><div className="fine">{b.dl_pct}%</div></section>
      ) : null}
      {b.task ? <section><h4>{b.status === "idle" ? "Last task" : "Task"}</h4><div className="task">{b.task}</div></section> : null}
      <Inbox b={b} />
      {files.length ? (
        <section>
          <h4>Attached files</h4>
          {/* Same two links as an Inbox card: the name opens the file inline in a new tab, ↓ downloads it. */}
          {files.map((d) => (
            <div key={d.path} className="rt">
              <a href={rawFileUrl(d.path)} target="_blank" rel="noopener" title={`${d.kind} · ${d.size} bytes`}>{d.name}</a>
              <a className="dl" href={rawFileUrl(d.path)} download={d.name} title={`Download ${d.name}`}>↓</a>
            </div>
          ))}
        </section>
      ) : null}
      <Routines b={b} />
      <UsageStrip b={b} />
    </div>
  );
}

export function PreviewPane() {
  const b = useBotsSelector((s) => s.bots.find((x) => x.id === s.sel));
  const u = b ? shotUrl(b) : "";
  // Until frames arrive the live view shows the latest thumbnail too.
  useEffect(() => { mirrorThumb(u); }, [u]);

  // The preview header's ☰ opens the same bot menu as the chat header's, with "Open live view" on top.
  const onMore = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    const s = getState(); if (!s.sel) return;
    if (s.ui.menu) { closeMenu(); return; }
    const r = e.currentTarget.getBoundingClientRect();
    openMenu({ id: s.sel, x: r.right, y: r.bottom + 6, full: true, live: true, alignRight: true });
  };

  const asleep = !!b && !!b.shot && !b.browser?.running && !b.control && b.status !== "running" && b.status !== "waiting";
  const tabs = b?.browser?.tabs?.length || 0;
  const purl = b ? b.browser?.url || b.url || "about:blank" : "";
  return (
    <section className="preview">
      <header>
        <button id="pclose" className="ptog" title="Hide the browser preview" onClick={closePreview}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
        </button>
        <span style={{ flex: 1 }} />
        <button id="pmore" className="ptog" title="Open live view, settings, routines, skills, clone, delete" disabled={!b} onClick={onMore}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16" /></svg>
        </button>
      </header>
      {/* Side app: a built app running in this column instead of the browser view. The strip switches between the two; × unloads the app. */}
      <SideApp />
      <div className={`shotwrap${asleep ? " asleep" : ""}`} id="shotwrap" onClick={openFromThumb}
        title={asleep ? "Browser is asleep to save memory. Click to wake it." : "Click to watch this bot's browser"}>
        {/* One shared <img> whose src follows the selected bot's shot URL, so switching bots never leaves the old page up. */}
        <img id="shot" alt="" src={u || undefined} />
        <div className="ph" id="ph" style={{ display: b?.shot ? "none" : "flex" }}>
          {!b ? "No browser yet" : b.browser?.running ? "Waiting for first screenshot…" : "Browser starts with the first task"}
        </div>
        <div className="live" id="live" style={{ display: b?.status === "running" ? "flex" : "none" }}><span className="dot running" />live</div>
        <div className="zz" id="zz">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z" /></svg>
          <span>Asleep · click to wake</span>
        </div>
      </div>
      <Toast id="toast" />
      <div className="cap">
        <span id="pcap">{b ? `${b.name}'s screen` + (b.control ? " · you have control" : tabs > 1 ? ` · ${tabs} tabs` : "") : ""}</span>
        <span className="url" id="purl" title={purl}>{purl}</span>
      </div>
      {b ? <Info b={b} /> : <div className="info" id="info" />}
    </section>
  );
}

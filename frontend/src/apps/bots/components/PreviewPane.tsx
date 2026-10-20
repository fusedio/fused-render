// The right column (OpenBot index.html section.preview + chat.js render()'s preview half): header (hide, ☰ menu),
// the side app strip, the screenshot (click → live view), the status toast, the caption and the info sections
// (model download, task, Inbox, attached files, routines, usage). In Stage (root .stage) all of that but the header steps
// aside and the live view (#full, always mounted here) fills the column.
import { useEffect, useMemo, useState, type MouseEvent } from "react";
import { appOpenUrl } from "../apps/apps";
import { botTasks, openBuilds, useBuildRows } from "../builds/builds";
import { SideApp } from "../apps/SideApp";
import { embedUrlForFsPath } from "@platform/lib/router";
import { api, rawFileUrl, shotUrl, type Artifact, type Bot } from "../lib/api";
import { mirrorThumb, openFull } from "../lib/cdp";
import { fmtAgo, fmtBytes, fmtIn, fmtWhen, fmtWhenShort } from "../lib/format";
import { closePreview } from "../lib/layout";
import { LiveView } from "./LiveView";
import { act, closeMenu, eventsOf, getState, openDialog, openMenu, useBotsSelector } from "../state/store";
import { Toast } from "./Toast";
import TerminalView from "@platform/ui/TerminalView";

const KIND_LABEL: Record<string, string> = { save: "saved", download: "download", build: "app" };
const ext = (n: string) => (/\.([a-z0-9]{1,5})$/i.exec(n)?.[1] || "").toUpperCase();

// Inbox: what this bot produced for you (save results, downloads, built apps), newest first, one card each.
// A file card opens the file in the explorer's chrome-free embed (/explorer/embed/<path>) in a new tab: bots run
// inside fused-render, so an .html artifact renders as a fused view and every other kind gets the explorer's own
// file template (CSV grid, image, PDF, text) instead of the browser's bare /api/fs/raw bytes (owner, 2026-10-07).
// A small ↓ beside each file card keeps the raw download. A built-app card opens the app through the same relative
// `/render?path=<dir>/index.html` URL the gallery and viewer use (apps.ts appOpenUrl), derived from the row's `path`:
// the absolute link the bot once recorded carried the server's host:port of that launch, which changes every start,
// so every older card opened a dead origin (owner, 2026-10-13). "Open folder" reveals the bot's Inbox in Finder.
function InboxCard({ a }: { a: Artifact }) {
  const build = a.kind === "build";
  const sub = build ? "Built app" : `${KIND_LABEL[a.kind] || a.kind}${a.size ? " · " + fmtBytes(a.size) : ""}`;
  return (
    <span className="cardrow">
      <a className="card" href={build ? appOpenUrl(a.path) : embedUrlForFsPath(a.path)} target="_blank" rel="noopener" title={a.task || ""}>
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
  const open = () => openDialog({ kind: "settings", id: b.id, tab: "routines" });
  return (
    <section>
      {rs.length ? <h4>Routines</h4> : null}
      {rs.length ? (
        <>
          {/* "in 53 min" at a glance (owner, 2026-10-13); the clock time sits in the tooltip. Red when the newest run
              failed. The poll (1.5–3 s) repaints the countdown. */}
          {rs.slice(0, 4).map((r) => {
            const bad = r.enabled && (r.last_result === "error" || (r.fails || 0) > 0);
            // `last_message` is the newest FINISHED run's; a start or a skip since leaves it behind, so the tip
            // quotes it only while `last_result` is still that run's outcome.
            const ended = r.last_result === "done" || r.last_result === "error" || r.last_result === "stopped";
            // A fired one-shot is disabled while it runs: "running" comes before every enabled check (label, fade, tip).
            const running = r.last_result === "started", paused = !r.enabled && !running;
            return (
              <div key={r.id} className={`rt ${paused ? "off" : ""}`}>
                <span title={r.task}>{r.task}</span>
                <small className={bad ? "warn" : undefined} title={running ? "Running now" : paused ? "paused" : `${fmtWhen(r.next)}${ended && r.last_message ? `\nlast run: ${r.last_result} — ${r.last_message}` : ""}`}>
                  {running ? "running" : paused ? "paused" : fmtIn(r.next) || "soon"}
                </small>
              </div>
            );
          })}
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
        {/* The count opens the reasons (dialogs/Failures.tsx): the same rows the ledger counted, each with its error text. */}
        {row.errors ? <button type="button" className="lnk warn" title="Show the failed calls and their errors"
          onClick={() => openDialog({ kind: "failures", id: b.id })}>{row.errors} failed</button> : null}
        <span title="Last model call">{fmtAgo(row.last)}</span>
      </div>
    </section>
  );
}

/** The bot's own task as a row (owner, 2026-10-14: "the task row UI there"): the page's status dot, the task, and
 *  the one fact the status adds. The dot is the list row's (bots.css .dot.<status>): green pulse while it runs, red
 *  pulse while it waits on you, amber paused, red failed, grey once it is the last task. */
function BotTask({ b }: { b: Bot }) {
  const step = b.step && b.step_cap ? `step ${b.step}/${b.step_cap}` : "";
  const fact = b.status === "running" ? step || "running" : b.status === "waiting" ? "waiting for you"
    : b.status === "paused" ? "paused" : b.status === "error" ? "failed" : "";
  return (
    <section>
      <h4>{b.status === "idle" ? "Last task" : "Task"}</h4>
      <div className="trow" title={b.task}>
        <span className={`dot ${b.status}`} />
        <span className="ttl">{b.task}</span>
        {fact ? <small className={b.status === "error" ? "warn" : undefined} title={b.status === "error" ? b.note || "" : ""}>{fact}</small> : null}
      </div>
    </section>
  );
}

// A shell task row's status (routes/tasks.py `_status`) in this page's two vocabularies: the dot's colour class and
// the word after the title. A queued or upcoming task is not running yet, so it keeps the bare grey dot.
const TASK_DOT: Record<string, string> = { in_progress: "running", needs_attention: "waiting", blocked: "error" };
const TASK_WORD: Record<string, string> = {
  in_progress: "running", queued: "queued", upcoming: "upcoming", needs_attention: "needs you", blocked: "failed", done: "done", archived: "archived",
};

/** The Claude tasks this bot started (bot.py `build` → /api/tasks/create, tagged with the bot's id; builds/builds.ts
 *  botTasks), newest first, four at most. A row opens the task on the Tasks page (/bots/tasks?task=<key>). The rows
 *  are the builds feed's (startBuilds), so they move as the task does, on the same long poll the footer chip rides. */
function BotTasks({ b }: { b: Bot }) {
  const rows = useBuildRows();
  const mine = useMemo(() => botTasks(rows, b.id, b.builds), [rows, b.id, b.builds]);
  if (!mine.length) return null;
  return (
    <section>
      <h4>Tasks</h4>
      {mine.slice(0, 4).map((t) => {
        const live = t.status === "in_progress";
        const when = live ? "" : fmtAgo(t.last_active || t.started);
        return (
          <button key={t.key} type="button" className="trow" title={t.last_reply || t.title || ""} onClick={() => openBuilds(t.key)}>
            <span className={`dot ${TASK_DOT[t.status] || ""}`} />
            <span className="ttl">{t.title || "Task"}</span>
            <small className={t.status === "blocked" ? "warn" : undefined}>{TASK_WORD[t.status] || t.status}{when ? ` · ${when}` : ""}</small>
          </button>
        );
      })}
      {mine.length > 4 ? (
        <div className="fine" style={{ marginTop: 6 }}>
          <button className="lnk" onClick={() => openBuilds()}>All {mine.length} tasks</button>
        </div>
      ) : null}
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
      {b.task ? <BotTask b={b} /> : null}
      <BotTasks b={b} />
      <Inbox b={b} />
      {files.length ? (
        <section>
          <h4>Attached files</h4>
          {/* Same two links as an Inbox card: the name opens the file in the explorer embed in a new tab, ↓ downloads it. */}
          {files.map((d) => (
            <div key={d.path} className="rt">
              <a href={embedUrlForFsPath(d.path)} target="_blank" rel="noopener" title={`${d.kind} · ${d.size} bytes`}>{d.name}</a>
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

// The terminal tools label their steps "$ <command>", "read the terminal" and "press Ctrl-C in the terminal" (tools.describe).
const isTerminalStep = (t: string) => t.startsWith("$ ") || t === "read the terminal" || t === "press Ctrl-C in the terminal";

export function PreviewPane() {
  const b = useBotsSelector((s) => s.bots.find((x) => x.id === s.sel));
  // Browser | Terminal: the Terminal side exists while the bot has a live terminal session (docs §16), and a new
  // terminal step brings it forward.
  const termId = b?.terminal_id || null;
  const lastTermSeq = useBotsSelector((s) => {
    const evs = eventsOf(s.sel);
    for (let i = evs.length - 1; i >= 0 && i >= evs.length - 8; i--) if (evs[i].role === "action" && isTerminalStep(evs[i].text)) return evs[i].seq;
    return 0;
  });
  const [view, setView] = useState<"browser" | "terminal">("browser");
  useEffect(() => { setView("browser"); }, [b?.id]);
  useEffect(() => { if (lastTermSeq) setView("terminal"); }, [b?.id, lastTermSeq]);
  const showTerm = !!termId && view === "terminal";
  const u = b ? shotUrl(b) : "";
  // Until frames arrive the live view shows the latest thumbnail too.
  useEffect(() => { mirrorThumb(u); }, [u]);

  // The preview header's ☰ opens the same bot menu as the chat header's, with "Open live view" on top.
  const onMore = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    const s = getState(); if (!s.sel) return;
    if (s.ui.menu) { closeMenu(); return; }
    const r = e.currentTarget.getBoundingClientRect();
    openMenu({ id: s.sel, x: r.right, y: r.bottom + 6, live: true, alignRight: true });
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
      {termId ? (
        <div className="viewsw" id="viewsw">
          <button className={`satab${showTerm ? "" : " on"}`} id="vswb" onClick={() => setView("browser")}>Browser</button>
          <button className={`satab${showTerm ? " on" : ""}`} id="vswt" title="The bot's own terminal, live" onClick={() => setView("terminal")}>Terminal</button>
        </div>
      ) : null}
      {showTerm ? <div className="termwrap" id="termwrap"><TerminalView key={termId} id={termId} /></div> : null}
      <div className={`shotwrap${asleep ? " asleep" : ""}${showTerm ? " termup" : ""}`} id="shotwrap" onClick={() => openFull()}
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
      <LiveView />
    </section>
  );
}

// An expert's preview column (docs/experts.md "UI"): no browser, so in place of the screenshot, live view and info it
// shows two tabs in the Browser | Terminal strip's style:
//   Memory    memory.md as it stands, rendered as markdown (read-only), with its path, its size against the compaction
//             limit, a note when it was edited by hand since the last session wrote it, and "Changes": the sessions that
//             changed it (kind, where it came from, when). Clicking a change opens that session on the Sessions tab.
//   Sessions  the expert's side sessions (and the chat turns that changed memory), newest first: kind, where it came
//             from, when, status, and a "changed memory" tag when its diff is not empty. A row opens in place to its
//             error, its memory diff (read as a document: DiffView) and its transcript (role-tagged lines).
// Data: GET /api/experts/<id>/memory and …/sessions (lib/api.ts expertApi), on mount, when the bot's `seq` moves (a
// chat turn may have changed memory) and every 10 s while the page is visible; an opened row's full record (…/sessions/
// <sid>) when it opens and again when its status or end moves. Each of the three keeps a request generation, so only
// the latest request's answer lands (two refreshes can finish out of order). A failed fetch says so in the pane, never
// the banner.
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { expertApi, type Bot, type ExpertChange, type ExpertMemory, type ExpertSession } from "../lib/api";
import { agoShort, changedMemory, diffDoc, duration, kindLabel, memoryUsage, newestFirst, originOf, roleTag, statusOf, turnIsMarkdown } from "../lib/expert";
import { fmtWhen, fmtWhenShort } from "../lib/format";
import { revealWithin } from "../lib/scroll";
import { errMsg } from "../state/store";
import { MdDoc } from "./MdDoc";

export type ExpertTab = "memory" | "sessions";
const TABS: [ExpertTab, string][] = [["memory", "Memory"], ["sessions", "Sessions"]];
const REFRESH_MS = 10_000;

// ------------------------------------------------------------------ pieces ----

/** One diff line's markdown on its background, with a + / − gutter mark. */
function DiffLine({ tone, text }: { tone: "add" | "del" | "ctx"; text: string }) {
  const mark = tone === "add" ? "+" : tone === "del" ? "−" : "";
  // A code fence alone parses as an empty code block: the line would show its mark and no text. Its source instead.
  const fence = /^\s*(`{3,}|~{3,})/.test(text);
  return (
    <div className={`xp-dr ${tone}${text.trim() ? "" : " blank"}`}>
      <span className="xp-g" aria-hidden="true">{mark}</span>
      {mark ? <span className="xp-sr">{tone === "add" ? "Added: " : "Removed: "}</span> : null}
      <div className="xp-dc xp-md">{fence ? <code>{text.trim()}</code> : text.trim() ? <MdDoc text={text} /> : "\u00a0"}</div>
    </div>
  );
}

/** A memory diff read as a document (lib/expert.ts diffDoc): no headers, each line's markdown rendered, added lines on
 *  green with +, removed on red with −, struck; a removed line answered by an added one as a pair; context muted.
 *  "No change to memory." for an empty one. */
export function DiffView({ diff }: { diff: string | null | undefined }) {
  const rows = diffDoc(diff);
  if (!rows.length) return <p className="xp-none">{(diff || "").trim() ? "Only blank lines changed." : "No change to memory."}</p>;
  return (
    <div className="xp-diff" role="group" aria-label="Memory diff">
      {rows.map((r, i) => r.tone === "gap" ? <div key={i} className="xp-dgap" aria-hidden="true">⋯</div>
        : r.tone === "pair" ? (
          <div key={i} className="xp-dpair"><DiffLine tone="del" text={r.del} /><DiffLine tone="add" text={r.add} /></div>
        ) : <DiffLine key={i} tone={r.tone} text={r.text} />)}
    </div>
  );
}

/** The transcript, one role-tagged block per turn: the expert's own text rendered as markdown (MdDoc, as the diff and
 *  memory.md are); the prompt, tool and refused lines as plain preformatted text. */
export function Transcript({ turns }: { turns: ExpertSession["transcript"] }) {
  if (!turns?.length) return <p className="xp-none">No transcript.</p>;
  return (
    <ol className="xp-tr">
      {turns.map((t, i) => (
        <li key={i} className={`xp-turn role-${t.role}`}>
          <span className="xp-role">{roleTag(t.role)}</span>
          {turnIsMarkdown(t.role)
            ? <div className="xp-ttext xp-md"><MdDoc text={t.text} /></div>
            : <span className="xp-ttext">{t.text}</span>}
        </li>
      ))}
    </ol>
  );
}

/** An opened session: its error, its memory diff, then the prompt and the rest of the transcript (`full` once fetched;
 *  the list row until then). A chat row has no transcript unless the record carries one. */
export function SessionDetail({ row, full, err }: { row: ExpertSession; full: ExpertSession | null; err?: string }) {
  const s = full || row;
  return (
    <div className="xp-detail">
      {s.error ? <p className="xp-err" role="alert">{s.error}</p> : null}
      {err ? <p className="xp-err" role="alert">{err}</p> : null}
      <div className="xp-part">
        <p className="xp-lbl">Memory diff</p>
        {full || row.memory_diff !== undefined ? <DiffView diff={s.memory_diff} /> : <p className="xp-none">Loading…</p>}
      </div>
      {s.kind === "chat" && !full?.transcript?.length ? null : (
        <div className="xp-part">
          <p className="xp-lbl">Transcript</p>
          {full ? <Transcript turns={full.transcript} /> : <p className="xp-none">Loading…</p>}
        </div>
      )}
    </div>
  );
}

/** One session row's header line: kind, status, "changed memory" when its diff is not empty, when; under it, where it
 *  came from. */
export function SessionHead({ s, nowMs }: { s: ExpertSession; nowMs?: number }) {
  const st = statusOf(s), origin = originOf(s.kind, s.trigger), took = duration(s);
  return (
    <>
      <span className="xp-line">
        <span className={`xp-kind k-${s.kind}`}>{kindLabel(s.kind)}</span>
        <span className={`xp-st tone-${st.tone}`}><span className="xp-dot" aria-hidden="true" />{st.text}</span>
        {changedMemory(s) ? <span className="xp-chg" title="This session changed memory.md">changed memory</span> : null}
        <span className="xp-when" title={fmtWhen(s.started)}>{agoShort(s.started, nowMs, fmtWhenShort)}{took ? ` · ${took}` : ""}</span>
      </span>
      {origin ? <span className="xp-origin">{origin}</span> : null}
    </>
  );
}

// ------------------------------------------------------------------ panels ----

/** The Memory tab: path, size, the hand-edit note, the file rendered as markdown, then the sessions that changed it. */
export function MemoryPanel({ memory, err, onChange, nowMs }: {
  memory: ExpertMemory | null; err?: string; onChange: (c: ExpertChange) => void; nowMs?: number;
}) {
  if (!memory) return <p className={err ? "xp-err" : "xp-empty"}>{err || "Loading…"}</p>;
  const use = memoryUsage(memory), changes = memory.changes || [];
  return (
    <>
      {err ? <p className="xp-err" role="alert">{err}</p> : null}
      <p className="xp-path" title={memory.path}>{memory.path}</p>
      <p className={`xp-size${use.over ? " over" : ""}`}>{use.text}{use.over ? " · over the limit; a dream session shortens it after the next session that writes it" : ""}</p>
      {memory.writing ? <p className="xp-hand">A session is working on memory now.</p>
        : memory.edited_by_hand ? <p className="xp-hand">Edited by hand since the last session.</p> : null}
      {memory.text.trim() ? <div className="xp-mem xp-md" aria-label="memory.md"><MdDoc text={memory.text} /></div> : <p className="xp-empty">Nothing remembered yet.</p>}
      <p className="xp-note">Read-only here. You can edit the file by hand at the path above; the expert reads it at the start of every session.</p>
      <p className="xp-lbl xp-chhead">Changes</p>
      {changes.length ? (
        <ul className="xp-changes">
          {changes.map((c) => {
            const origin = originOf(c.kind, c.trigger);
            return (
              <li key={c.id}>
                <button type="button" className="xp-change" onClick={() => onChange(c)} title="Open this session and its diff">
                  <span className={`xp-kind k-${c.kind}`}>{kindLabel(c.kind)}</span>
                  <span className="xp-origin">{origin}</span>
                  <span className="xp-when" title={fmtWhen(c.started)}>{agoShort(c.started, nowMs, fmtWhenShort)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : <p className="xp-none">No session has changed it yet.</p>}
    </>
  );
}

/** The Sessions tab: the rows, newest first; the open row shows its detail in place. A session opened from the Memory
 *  tab that the list does not hold (yet) shows on top: its detail once fetched, else "Loading…" or the fetch's error. */
export function SessionsPanel({ rows, err, open, onOpen, full, fullErr, nowMs }: {
  rows: ExpertSession[] | null; err?: string;
  open: string | null; onOpen: (id: string | null) => void; full: ExpertSession | null; fullErr?: string; nowMs?: number;
}) {
  if (!rows) return <p className={err ? "xp-err" : "xp-empty"}>{err || "Loading…"}</p>;
  const shown = newestFirst(rows);
  const orphanId = open && !shown.some((s) => s.id === open) ? open : null;
  const orphan = orphanId && full && full.id === orphanId ? full : null;
  return (
    <>
      {err ? <p className="xp-err" role="alert">{err}</p> : null}
      {orphan ? (
        <div className="xp-row open" id={`xps-${orphan.id}`}><div className="xp-head static"><SessionHead s={orphan} nowMs={nowMs} /></div>
          <SessionDetail row={orphan} full={full} err={fullErr} /></div>
      ) : orphanId ? (
        <div className="xp-row open" id={`xps-${orphanId}`}>
          <p className={fullErr ? "xp-err xp-pending" : "xp-none xp-pending"} role={fullErr ? "alert" : undefined}>{fullErr || "Loading this session…"}</p>
        </div>
      ) : null}
      {!shown.length ? <p className="xp-empty">No sessions yet. Asks from tasks, learning from tasks that used this expert, and dreams (memory compaction) show here.</p> : (
        <ul className="xp-rows">
          {shown.map((s) => {
            const on = s.id === open;
            return (
              <li key={s.id} id={`xps-${s.id}`} className={`xp-row${on ? " open" : ""}${changedMemory(s) ? " changed" : ""}`}>
                <button type="button" className="xp-head" aria-expanded={on} onClick={() => onOpen(on ? null : s.id)}>
                  <SessionHead s={s} nowMs={nowMs} />
                </button>
                {on ? <SessionDetail row={s} full={full && full.id === s.id ? full : null} err={fullErr} /> : null}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

// ------------------------------------------------------------------ data + frame ----

/** memory and the session list for expert `id`, kept fresh (see the header). Each resource has its own request
 *  generation: an answer lands only if no later request for it (same expert or another) has started since. */
function useExpertData(id: string, seq: number) {
  const [memory, setMemory] = useState<ExpertMemory | null>(null);
  const [rows, setRows] = useState<ExpertSession[] | null>(null);
  const [memErr, setMemErr] = useState(""), [rowsErr, setRowsErr] = useState("");
  const [tick, setTick] = useState(0);  // one per list load: a running session's record is fetched again with each
  const gen = useRef({ mem: 0, rows: 0 });
  const load = useCallback(() => {
    const m = ++gen.current.mem, r = ++gen.current.rows;
    void expertApi.memory(id).then((x) => { if (gen.current.mem === m) { setMemory(x); setMemErr(""); } },
      (e) => { if (gen.current.mem === m) setMemErr(errMsg(e)); });
    void expertApi.sessions(id).then((x) => { if (gen.current.rows === r) { setRows(Array.isArray(x?.sessions) ? x.sessions : []); setRowsErr(""); setTick((n) => n + 1); } },
      (e) => { if (gen.current.rows === r) setRowsErr(errMsg(e)); });
  }, [id]);
  useEffect(() => { load(); }, [load, seq]);
  useEffect(() => {
    const t = window.setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
    return () => window.clearInterval(t);
  }, [load]);
  return { memory, rows, memErr, rowsErr, tick };
}

/** The open session's full record, fetched when it opens and again when its list row's status or end moves; only the
 *  latest request's answer lands. */
function useSession(id: string, sid: string | null, stamp: string) {
  const [full, setFull] = useState<ExpertSession | null>(null);
  const [err, setErr] = useState("");
  const gen = useRef(0);
  useEffect(() => {
    const g = ++gen.current;
    if (!sid) { setFull(null); setErr(""); return; }
    setErr("");
    setFull((f) => (f && f.id === sid ? f : null));
    void expertApi.session(id, sid).then((s) => { if (gen.current === g) setFull(s); }, (e) => { if (gen.current === g) setErr(errMsg(e)); });
    return () => { if (gen.current === g) gen.current++; };
  }, [id, sid, stamp]);
  return { full, err };
}

/** The preview column's body for an expert: what it is about, the tab strip, the open tab. */
export function ExpertPane({ b }: { b: Bot }) {
  const [tab, setTab] = useState<ExpertTab>("memory");
  const [open, setOpen] = useState<string | null>(null);
  const { memory, rows, memErr, rowsErr, tick } = useExpertData(b.id, b.seq);
  const row = open ? rows?.find((s) => s.id === open) : undefined;
  // A running session's transcript grows; refetch it with each list refresh until it ends.
  const stamp = row ? `${row.status}:${row.ended ?? ""}:${row.status === "running" ? tick : ""}` : "";
  const { full, err: fullErr } = useSession(b.id, open, stamp);
  const uid = useId();
  const paneRef = useRef<HTMLDivElement>(null);
  const about = b.expert?.ask_me_about || "";

  // A change clicked on the Memory tab: the Sessions tab, that row open and scrolled to.
  const openChange = (c: ExpertChange) => { setTab("sessions"); setOpen(c.id); };
  const listed = !!rows;  // the row may only mount when the list arrives: reveal it then too (once, not per refresh)
  useEffect(() => {
    if (tab !== "sessions" || !open) return;
    // Only the pane scrolls (it is the scroller, .xpane): scrollIntoView would move the app shell too.
    const t = window.setTimeout(() => revealWithin(paneRef.current, document.getElementById(`xps-${open}`)), 0);
    return () => window.clearTimeout(t);
  }, [tab, open, listed]);
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const next: ExpertTab = tab === "memory" ? "sessions" : "memory";
    setTab(next);
    document.getElementById(`${uid}-${next}`)?.focus();
  };

  return (
    <div className="xpane" id="xpane" ref={paneRef}>
      {about ? <p className="xp-about" title="What tasks ask this expert about">{about}</p> : null}
      <div className="viewsw" role="tablist" aria-label="Expert" onKeyDown={onKey}>
        {TABS.map(([t, l]) => (
          <button key={t} type="button" id={`${uid}-${t}`} role="tab" className={`satab${tab === t ? " on" : ""}`} aria-selected={tab === t}
            aria-controls={`${uid}-panel`} tabIndex={tab === t ? 0 : -1} onClick={() => setTab(t)}>{l}</button>
        ))}
      </div>
      <div className="xp-panel" id={`${uid}-panel`} role="tabpanel" aria-labelledby={`${uid}-${tab}`} data-tab={tab}>
        {tab === "memory" ? <MemoryPanel memory={memory} err={memErr} onChange={openChange} />
          : <SessionsPanel rows={rows} err={rowsErr} open={open} onOpen={setOpen} full={full} fullErr={fullErr} />}
      </div>
    </div>
  );
}

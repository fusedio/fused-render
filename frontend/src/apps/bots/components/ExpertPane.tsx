// An expert's preview column (docs/experts.md "UI"): no browser, so in place of the screenshot, live view and info it
// shows two tabs in the Browser | Terminal strip's style:
//   Memory    memory.md as it stands, read-only, with its path, its size against the compaction limit, a note when it
//             was edited by hand since the last session wrote it, and "Changes": the sessions that changed it (kind,
//             where it came from, when). Clicking a change opens that session on the Sessions tab.
//   Sessions  the expert's side sessions (and the chat turns that changed memory), newest first: kind, where it came
//             from, when, status and +/− when it changed memory; a filter keeps only those (the change log). A row
//             opens in place to its transcript (role-tagged lines) and its memory diff, added / removed lines coloured.
// Data: GET /api/experts/<id>/memory and …/sessions (lib/api.ts expertApi), on mount, when the bot's `seq` moves (a
// chat turn may have changed memory) and every 10 s while the page is visible; an opened row's full record (…/sessions/
// <sid>) when it opens and again when its status or end moves. A failed fetch says so in the pane, never the banner.
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { expertApi, type Bot, type ExpertChange, type ExpertMemory, type ExpertSession } from "../lib/api";
import { changedMemory, costLabel, diffLines, diffStat, duration, filterSessions, kindLabel, memoryUsage, originOf, roleTag, statusOf, type SessionFilter } from "../lib/expert";
import { fmtAgo, fmtWhen } from "../lib/format";
import { errMsg } from "../state/store";

export type ExpertTab = "memory" | "sessions";
const TABS: [ExpertTab, string][] = [["memory", "Memory"], ["sessions", "Sessions"]];
const REFRESH_MS = 10_000;

// ------------------------------------------------------------------ pieces ----

/** A unified diff, one line per row, added green and removed red; "No change to memory." for an empty one. */
export function DiffView({ diff }: { diff: string | null | undefined }) {
  const lines = diffLines(diff);
  if (!lines.length) return <p className="xp-none">No change to memory.</p>;
  return (
    <pre className="xp-diff" aria-label="Memory diff">
      {lines.map((l, i) => <span key={i} className={`xp-dl ${l.tone}`}>{l.text || " "}{"\n"}</span>)}
    </pre>
  );
}

/** The transcript, one role-tagged block per turn. */
export function Transcript({ turns }: { turns: ExpertSession["transcript"] }) {
  if (!turns?.length) return <p className="xp-none">No transcript.</p>;
  return (
    <ol className="xp-tr">
      {turns.map((t, i) => (
        <li key={i} className={`xp-turn role-${t.role}`}>
          <span className="xp-role">{roleTag(t.role)}</span>
          <span className="xp-ttext">{t.text}</span>
        </li>
      ))}
    </ol>
  );
}

/** An opened session: its error, transcript and memory diff (`full` once fetched; the list row until then). */
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
      {s.kind === "chat" ? null : (
        <div className="xp-part">
          <p className="xp-lbl">Transcript</p>
          {full ? <Transcript turns={full.transcript} /> : <p className="xp-none">Loading…</p>}
        </div>
      )}
    </div>
  );
}

/** One session row's header line: kind, status, when, and +/− when it changed memory; under it, where it came from. */
export function SessionHead({ s, nowMs }: { s: ExpertSession; nowMs?: number }) {
  const st = statusOf(s), stat = diffStat(s.memory_diff), origin = originOf(s.kind, s.trigger), took = duration(s);
  return (
    <>
      <span className="xp-line">
        <span className={`xp-kind k-${s.kind}`}>{kindLabel(s.kind)}</span>
        <span className={`xp-st tone-${st.tone}`}><span className="xp-dot" aria-hidden="true" />{st.text}</span>
        {changedMemory(s) ? <span className="xp-chg" title="Changed memory.md">{stat || "changed memory"}</span> : null}
        <span className="xp-when" title={fmtWhen(s.started)}>{fmtAgo(s.started, nowMs)}{took ? ` · ${took}` : ""}{costLabel(s.cost_usd)}</span>
      </span>
      {origin ? <span className="xp-origin">{origin}</span> : null}
    </>
  );
}

// ------------------------------------------------------------------ panels ----

/** The Memory tab: path, size, the hand-edit note, the file, then the sessions that changed it. */
export function MemoryPanel({ memory, err, onChange, nowMs }: {
  memory: ExpertMemory | null; err?: string; onChange: (c: ExpertChange) => void; nowMs?: number;
}) {
  if (!memory) return <p className={err ? "xp-err" : "xp-empty"}>{err || "Loading…"}</p>;
  const use = memoryUsage(memory), changes = memory.changes || [];
  return (
    <>
      {err ? <p className="xp-err" role="alert">{err}</p> : null}
      <p className="xp-path" title={memory.path}>{memory.path}</p>
      <p className={`xp-size${use.over ? " over" : ""}`}>{use.text}{use.over ? " · over the limit, a compact session will shorten it" : ""}</p>
      {memory.edited_by_hand ? <p className="xp-hand">Edited by hand since the last session.</p> : null}
      {memory.text.trim() ? <pre className="xp-mem">{memory.text}</pre> : <p className="xp-empty">Nothing remembered yet.</p>}
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
                  <span className="xp-when" title={fmtWhen(c.started)}>{fmtAgo(c.started, nowMs)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : <p className="xp-none">No session has changed it yet.</p>}
    </>
  );
}

/** The Sessions tab: the filter, then the rows; the open row shows its detail in place. A session opened from the
 *  Memory tab that the list does not hold (yet) shows its detail on top. */
export function SessionsPanel({ rows, err, filter, onFilter, open, onOpen, full, fullErr, nowMs }: {
  rows: ExpertSession[] | null; err?: string; filter: SessionFilter; onFilter: (f: SessionFilter) => void;
  open: string | null; onOpen: (id: string | null) => void; full: ExpertSession | null; fullErr?: string; nowMs?: number;
}) {
  if (!rows) return <p className={err ? "xp-err" : "xp-empty"}>{err || "Loading…"}</p>;
  const shown = filterSessions(rows, filter), nChanged = rows.filter(changedMemory).length;
  const orphan = open && !shown.some((s) => s.id === open) ? (full && full.id === open ? full : null) : null;
  return (
    <>
      {err ? <p className="xp-err" role="alert">{err}</p> : null}
      <div className="xp-filters" role="group" aria-label="Show">
        <button type="button" className={`xp-chip${filter === "all" ? " on" : ""}`} aria-pressed={filter === "all"} onClick={() => onFilter("all")}>
          All <span className="xp-n">{rows.length}</span></button>
        <button type="button" className={`xp-chip${filter === "changed" ? " on" : ""}`} aria-pressed={filter === "changed"} onClick={() => onFilter("changed")}
          title="Only the sessions that changed memory.md">Changed memory <span className="xp-n">{nChanged}</span></button>
      </div>
      {orphan ? (
        <div className="xp-row open"><div className="xp-head static"><SessionHead s={orphan} nowMs={nowMs} /></div>
          <SessionDetail row={orphan} full={full} err={fullErr} /></div>
      ) : null}
      {!shown.length ? <p className="xp-empty">{filter === "changed" ? "No session has changed memory yet." : "No sessions yet. Asks from tasks, learning after a task goes quiet, and compaction show here."}</p> : (
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

/** memory and the session list for expert `id`, kept fresh (see the header). */
function useExpertData(id: string, seq: number) {
  const [memory, setMemory] = useState<ExpertMemory | null>(null);
  const [rows, setRows] = useState<ExpertSession[] | null>(null);
  const [memErr, setMemErr] = useState(""), [rowsErr, setRowsErr] = useState("");
  const [tick, setTick] = useState(0);  // one per list load: a running session's record is fetched again with each
  const live = useRef(id);
  live.current = id;
  const load = useCallback(() => {
    const want = id;
    void expertApi.memory(want).then((m) => { if (live.current === want) { setMemory(m); setMemErr(""); } },
      (e) => { if (live.current === want) setMemErr(errMsg(e)); });
    void expertApi.sessions(want).then((r) => { if (live.current === want) { setRows(Array.isArray(r?.sessions) ? r.sessions : []); setRowsErr(""); setTick((n) => n + 1); } },
      (e) => { if (live.current === want) setRowsErr(errMsg(e)); });
  }, [id]);
  useEffect(() => { load(); }, [load, seq]);
  useEffect(() => {
    const t = window.setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
    return () => window.clearInterval(t);
  }, [load]);
  return { memory, rows, memErr, rowsErr, tick };
}

/** The open session's full record, fetched when it opens and again when its list row's status or end moves. */
function useSession(id: string, sid: string | null, stamp: string) {
  const [full, setFull] = useState<ExpertSession | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    if (!sid) { setFull(null); setErr(""); return; }
    let on = true;
    setErr("");
    setFull((f) => (f && f.id === sid ? f : null));
    void expertApi.session(id, sid).then((s) => { if (on) setFull(s); }, (e) => { if (on) setErr(errMsg(e)); });
    return () => { on = false; };
  }, [id, sid, stamp]);
  return { full, err };
}

/** The preview column's body for an expert: what it is about, the tab strip, the open tab. */
export function ExpertPane({ b }: { b: Bot }) {
  const [tab, setTab] = useState<ExpertTab>("memory");
  const [filter, setFilter] = useState<SessionFilter>("all");
  const [open, setOpen] = useState<string | null>(null);
  const { memory, rows, memErr, rowsErr, tick } = useExpertData(b.id, b.seq);
  const row = open ? rows?.find((s) => s.id === open) : undefined;
  // A running session's transcript grows; refetch it with each list refresh until it ends.
  const stamp = row ? `${row.status}:${row.ended ?? ""}:${row.status === "running" ? tick : ""}` : "";
  const { full, err: fullErr } = useSession(b.id, open, stamp);
  const uid = useId();
  const about = b.expert?.ask_me_about || "";

  // A change clicked on the Memory tab: the Sessions tab, that row open and scrolled to.
  const openChange = (c: ExpertChange) => { setTab("sessions"); setOpen(c.id); };
  useEffect(() => {
    if (tab !== "sessions" || !open) return;
    const t = window.setTimeout(() => document.getElementById(`xps-${open}`)?.scrollIntoView({ block: "nearest" }), 0);
    return () => window.clearTimeout(t);
  }, [tab, open]);
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const next: ExpertTab = tab === "memory" ? "sessions" : "memory";
    setTab(next);
    document.getElementById(`${uid}-${next}`)?.focus();
  };

  return (
    <div className="xpane" id="xpane">
      {about ? <p className="xp-about" title="What tasks ask this expert about">{about}</p> : null}
      <div className="viewsw" role="tablist" aria-label="Expert" onKeyDown={onKey}>
        {TABS.map(([t, l]) => (
          <button key={t} type="button" id={`${uid}-${t}`} role="tab" className={`satab${tab === t ? " on" : ""}`} aria-selected={tab === t}
            aria-controls={`${uid}-panel`} tabIndex={tab === t ? 0 : -1} onClick={() => setTab(t)}>{l}</button>
        ))}
      </div>
      <div className="xp-panel" id={`${uid}-panel`} role="tabpanel" aria-labelledby={`${uid}-${tab}`} data-tab={tab}>
        {tab === "memory" ? <MemoryPanel memory={memory} err={memErr} onChange={openChange} />
          : <SessionsPanel rows={rows} err={rowsErr} filter={filter} onFilter={setFilter} open={open} onOpen={setOpen} full={full} fullErr={fullErr} />}
      </div>
    </div>
  );
}

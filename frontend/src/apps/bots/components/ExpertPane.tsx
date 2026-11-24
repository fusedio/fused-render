// An expert's preview column (docs/experts.md): no browser, so in place of the screenshot, live view and info
// it shows three tabs in the Browser | Terminal strip's style:
//   Memory    memory.md rendered, with Edit opening the file in the explorer.
//   Skills    each skill's name and description; a row opens to its text. Read-only: the user asks the expert.
//   Learning  Waiting: the inbox (a note opens to its text, a conversation to one sentence; Delete is the one way to
//             remove an entry), with Learn now. Learned: its history, newest first; a learning run opens to what it
//             read, its reply and its diff, any other change to its diff. Opening the tab clears the row's dot.
// Data: /api/experts/<id>/… on mount, when the row moves and every 10 s while the page is visible; the history
// (a git log, which first commits a hand edit) only while the Learning tab is open.
import { useEffect, useId, useState, type ReactNode } from "react";
import { spaLinkProps } from "@platform/lib/router";
import type { Expert } from "@platform/lib/experts";
import { expertApi, type ExpertChange, type ExpertEntry } from "../lib/api";
import { fmtWhen, fmtWhenShort } from "../lib/format";
import { agoShort } from "../lib/memdiff";
import { actExpert, errMsg } from "../state/store";
import { MdDoc } from "./MdDoc";
import { DiffView, PaneTabs } from "./PaneParts";

type Tab = "memory" | "skills" | "learning";
const REFRESH_MS = 10_000;
const KIND: Record<string, string> = { note: "Note", task: "Task", breakout: "Breakout", chat: "Chat" };

type Data = {
  memory: string;
  skills: { name: string; description: string; text: string }[];
  notes: ExpertEntry[];
};
type Detail = { sha: string; diff: string; reply?: string; read?: { kind: string; title: string }[]; err?: string };

function useExpertData(s: Expert) {
  const [data, setData] = useState<Data | null>(null);
  const [err, setErr] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => { if (!document.hidden) setTick((n) => n + 1); }, REFRESH_MS);
    return () => window.clearInterval(t);
  }, []);
  useEffect(() => {
    let live = true;
    void Promise.all([expertApi.memory(s.id), expertApi.skills(s.id), expertApi.notes(s.id)])
      .then(([m, sk, n]) => { if (live) { setData({ memory: m.text, skills: sk.skills, notes: n.notes }); setErr(""); } },
        (e) => { if (live) setErr(errMsg(e)); });
    return () => { live = false; };
  }, [s.id, s.last?.at, s.working, s.updating, tick]);
  return { data, err, reload: () => setTick((n) => n + 1) };
}

/** One row that opens in place (.xp-row): its head line, then `children` while open. */
function Row({ head, open, onOpen, children }: { head: ReactNode; open: boolean; onOpen: () => void; children: ReactNode }) {
  return (
    <li className={`xp-row${open ? " open" : ""}`}>
      <button type="button" className="xp-head" aria-expanded={open} onClick={onOpen}>{head}</button>
      {open ? <div className="xp-detail">{children}</div> : null}
    </li>
  );
}

const when = (at: number) => <span className="xp-when" title={fmtWhen(at)}>{agoShort(at, undefined, fmtWhenShort)}</span>;
const runLabel = (c: ExpertChange) => c.label !== "Update" ? c.label
  : c.changed ? `Learned from ${c.read} ${c.read === 1 ? "entry" : "entries"}` : `Read ${c.read} ${c.read === 1 ? "entry" : "entries"}, changed nothing`;

export function ExpertPane({ s }: { s: Expert }) {
  const [tab, setTab] = useState<Tab>("memory");
  const [open, setOpen] = useState("");  // the open skill, entry or change
  const [detail, setDetail] = useState<Detail | null>(null);
  const [changes, setChanges] = useState<ExpertChange[] | null>(null);
  const { data, err, reload } = useExpertData(s);
  const uid = useId();
  const path = `${s.home}/memory.md`;
  const waiting = data ? [...data.notes].reverse() : [];
  const toggle = (key: string) => setOpen(open === key ? "" : key);
  const openChange = (sha: string) => {
    toggle(sha);
    if (detail?.sha !== sha) void expertApi.diff(s.id, sha).then((r) => setDetail({ sha, ...r }), (e) => setDetail({ sha, diff: "", err: errMsg(e) }));
  };
  const deleteNote = (name: string) => { void actExpert(() => expertApi.deleteNote(s.id, name)).then(reload); };
  const learnNow = () => { void actExpert(() => expertApi.update(s.id)).then(reload); };
  useEffect(() => {
    if (tab !== "learning") return;
    if (s.new) void actExpert(() => expertApi.seen(s.id));
    let live = true;
    void expertApi.changes(s.id).then((c) => { if (live) setChanges(c.changes); }, () => {});
    return () => { live = false; };
  }, [tab, s.id, s.new, s.updating, s.last?.at, data?.memory]);

  const tabs: [Tab, string][] = [["memory", "Memory"], ["skills", "Skills"], ["learning", data?.notes.length ? `Learning · ${data.notes.length}` : "Learning"]];
  return (
    <div className="xpane" id="xpane">
      <PaneTabs label="Expert" tabs={tabs} tab={tab} onTab={setTab} uid={uid} />
      <div className="xp-panel" id={`${uid}-panel`} role="tabpanel" aria-labelledby={`${uid}-${tab}`} data-tab={tab}>
        {err ? <p className="xp-err" role="alert">{err}</p> : null}
        {!data ? (err ? null : <p className="xp-empty">Loading…</p>) : tab === "memory" ? <>
          <p className="xp-path" title={path}>{path} <a className="xp-edit" {...spaLinkProps(path)} title="Open memory.md in the explorer to edit it">Edit</a></p>
          {data.memory.trim() ? <div className="xp-mem xp-md" aria-label="memory.md"><MdDoc text={data.memory} /></div> : <p className="xp-empty">Nothing remembered yet.</p>}
        </> : tab === "skills" ? (data.skills.length ? (
          <ul className="xp-rows">
            {data.skills.map((k) => (
              <Row key={k.name} open={open === k.name} onOpen={() => toggle(k.name)}
                head={<><span className="xp-line">{k.name}</span><span className="xp-origin">{k.description}</span></>}>
                <div className="xp-mem xp-md"><MdDoc text={k.text} /></div>
              </Row>
            ))}
          </ul>
        ) : <p className="xp-empty">No skills yet. To add or change one, tell the expert in its chat.</p>) : <>
          <p className="xp-none muted">It learns from notes, breakouts, tasks that use it and its own chat. They wait here until it next learns from them, once a day.</p>
          <div className="xp-chbox">
            <p className="xp-lbl xp-line">Waiting · {waiting.length}
              <button type="button" className="xp-edit xp-when" disabled={!waiting.length || s.updating} onClick={learnNow}>Learn now</button></p>
            {s.updating ? <p className="xp-none muted">Learning now…</p>
              : s.last_update ? <p className="xp-none muted">Last learned {agoShort(s.last_update, undefined, fmtWhenShort)}.</p> : null}
            {waiting.length ? (
              <ul className="xp-rows">
                {waiting.map((n) => (
                  <Row key={n.name} open={open === n.name} onOpen={() => toggle(n.name)}
                    head={<span className="xp-line">{KIND[n.kind]}<span className="xp-origin">{n.title}</span>{when(n.at)}</span>}>
                    {n.kind === "note" ? <div className="xp-md"><MdDoc text={n.text.split("\n").slice(1).join("\n")} /></div>
                      : <p className="xp-none">It will read this conversation from where it last stopped.</p>}
                    <button type="button" className="lnk" onClick={() => deleteNote(n.name)}>Delete</button>
                  </Row>
                ))}
              </ul>
            ) : <p className="xp-none">Nothing is waiting.</p>}
          </div>
          <div className="xp-chbox">
            <p className="xp-lbl">Learned</p>
            {!changes ? <p className="xp-none">Loading…</p> : changes.length ? (
              <ul className="xp-rows">
                {changes.map((c) => (
                  <Row key={c.sha} open={open === c.sha} onOpen={() => openChange(c.sha)}
                    head={<span className="xp-line"><span className="xp-origin">{runLabel(c)}</span>{when(c.at)}</span>}>
                    {detail?.sha !== c.sha ? <p className="xp-none">Loading…</p> : detail.err ? <p className="xp-err" role="alert">{detail.err}</p> : <>
                      {detail.read ? <>
                        <p className="xp-lbl">Read</p>
                        <ul className="xp-rows">
                          {detail.read.map((r, i) => (
                            <li key={i} className="xp-row"><div className="xp-head static">
                              <span className="xp-line">{KIND[r.kind] || "Note"}<span className="xp-origin">{r.title}</span></span>
                            </div></li>
                          ))}
                        </ul>
                      </> : null}
                      {detail.reply ? <div className="xp-md"><MdDoc text={detail.reply} /></div> : null}
                      {detail.read && !detail.diff.trim() ? <p className="xp-none">It changed nothing.</p> : <DiffView diff={detail.diff} />}
                    </>}
                  </Row>
                ))}
              </ul>
            ) : <p className="xp-none">Nothing learned yet.</p>}
          </div>
        </>}
      </div>
    </div>
  );
}

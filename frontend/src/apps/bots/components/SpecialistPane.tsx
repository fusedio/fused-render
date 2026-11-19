// A specialist's preview column (docs/specialists-v2.md), on the Expert pane's markup (ExpertPane.tsx):
//   Memory    memory.md rendered, with Edit opening the file in the explorer as the Expert pane's does.
//   Skills    each skill's name and description; a row opens to its text. Read-only: the user asks the specialist.
// Under both: "Notes waiting · n" (each note with Delete, the one place a note is removed) and "Changes · n", the
// specialist's git history over memory and skills, newest first; a row opens to its diff.
// Data: /api/specialists/<id>/… on mount, when a turn moves the row and every 10 s while the page is visible;
// Changes (a git log, which first commits a hand edit) only while its box is open, and again when memory changed.
import { useEffect, useId, useState, type ReactNode } from "react";
import { spaLinkProps } from "@platform/lib/router";
import type { Specialist } from "@platform/lib/specialists";
import { specialistApi } from "../lib/api";
import { agoShort } from "../lib/expert";
import { fmtWhen, fmtWhenShort } from "../lib/format";
import { actSpecialist, errMsg } from "../state/store";
import { DiffView, PaneTabs } from "./ExpertPane";
import { MdDoc } from "./MdDoc";

type Tab = "memory" | "skills";
const TABS: [Tab, string][] = [["memory", "Memory"], ["skills", "Skills"]];
const REFRESH_MS = 10_000;

type Data = {
  memory: string;
  skills: { name: string; description: string; text: string }[];
  notes: { name: string; text: string; at: number }[];
};
type Change = { sha: string; at: number; label: string };

function useSpecialistData(s: Specialist) {
  const [data, setData] = useState<Data | null>(null);
  const [err, setErr] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => { if (!document.hidden) setTick((n) => n + 1); }, REFRESH_MS);
    return () => window.clearInterval(t);
  }, []);
  useEffect(() => {
    let live = true;
    void Promise.all([specialistApi.memory(s.id), specialistApi.skills(s.id), specialistApi.notes(s.id)])
      .then(([m, sk, n]) => { if (live) { setData({ memory: m.text, skills: sk.skills, notes: n.notes }); setErr(""); } },
        (e) => { if (live) setErr(errMsg(e)); });
    return () => { live = false; };
  }, [s.id, s.last?.at, s.working, tick]);
  return { data, err, reload: () => setTick((n) => n + 1) };
}

/** One row that opens in place (the Logs tab's .xp-row): its head line, then `children` while open. */
function Row({ head, open, onOpen, children }: { head: ReactNode; open: boolean; onOpen: () => void; children: ReactNode }) {
  return (
    <li className={`xp-row${open ? " open" : ""}`}>
      <button type="button" className="xp-head" aria-expanded={open} onClick={onOpen}>{head}</button>
      {open ? <div className="xp-detail">{children}</div> : null}
    </li>
  );
}

export function SpecialistPane({ s }: { s: Specialist }) {
  const [tab, setTab] = useState<Tab>("memory");
  const [open, setOpen] = useState("");  // the open skill, or the open change's sha
  const [diff, setDiff] = useState<{ sha: string; text: string; err?: string } | null>(null);
  const { data, err, reload } = useSpecialistData(s);
  const uid = useId();
  const path = `${s.home}/memory.md`;
  const toggle = (key: string) => setOpen(open === key ? "" : key);
  const openChange = (sha: string) => {
    toggle(sha);
    if (diff?.sha !== sha) void specialistApi.diff(s.id, sha).then((r) => setDiff({ sha, text: r.diff }), (e) => setDiff({ sha, text: "", err: errMsg(e) }));
  };
  const deleteNote = (name: string) => { void actSpecialist(() => specialistApi.deleteNote(s.id, name)).then(reload); };
  const [chOpen, setChOpen] = useState(false);
  const [changes, setChanges] = useState<Change[] | null>(null);
  useEffect(() => {
    if (!chOpen) return;
    let live = true;
    void specialistApi.changes(s.id).then((c) => { if (live) setChanges(c.changes); }, () => {});
    return () => { live = false; };
  }, [chOpen, s.id, s.last?.at, s.working, data?.memory]);

  return (
    <div className="xpane" id="xpane">
      <PaneTabs label="Specialist" tabs={TABS} tab={tab} onTab={setTab} uid={uid} />
      <div className="xp-panel" id={`${uid}-panel`} role="tabpanel" aria-labelledby={`${uid}-${tab}`} data-tab={tab}>
        {err ? <p className="xp-err" role="alert">{err}</p> : null}
        {!data ? (err ? null : <p className="xp-empty">Loading…</p>) : <>
          {tab === "memory" ? <>
            <p className="xp-path" title={path}>{path} <a className="xp-edit" {...spaLinkProps(path)} title="Open memory.md in the explorer to edit it">Edit</a></p>
            {data.memory.trim() ? <div className="xp-mem xp-md" aria-label="memory.md"><MdDoc text={data.memory} /></div> : <p className="xp-empty">Nothing remembered yet.</p>}
          </> : data.skills.length ? (
            <ul className="xp-rows">
              {data.skills.map((k) => (
                <Row key={k.name} open={open === k.name} onOpen={() => toggle(k.name)}
                  head={<><span className="xp-line">{k.name}</span><span className="xp-origin">{k.description}</span></>}>
                  <div className="xp-mem xp-md"><MdDoc text={k.text} /></div>
                </Row>
              ))}
            </ul>
          ) : <p className="xp-empty">No skills yet. To add or change one, tell the specialist in its chat.</p>}
          {data.notes.length ? (
            <details className="xp-chbox">
              <summary className="xp-lbl xp-chhead">Notes waiting · {data.notes.length}</summary>
              <ul className="xp-rows">
                {data.notes.map((n) => (
                  <li key={n.name} className="xp-row"><div className="xp-detail">
                    <span className="xp-line"><span className="xp-when" title={fmtWhen(n.at)}>{agoShort(n.at, undefined, fmtWhenShort)}</span>
                      <button type="button" className="lnk" onClick={() => deleteNote(n.name)}>Delete</button></span>
                    <div className="xp-md"><MdDoc text={n.text} /></div>
                  </div></li>
                ))}
              </ul>
            </details>
          ) : null}
          <details className="xp-chbox" onToggle={(e) => setChOpen(e.currentTarget.open)}>
            <summary className="xp-lbl xp-chhead">Changes{changes ? ` · ${changes.length}` : ""}</summary>
            {!changes ? <p className="xp-none">Loading…</p> : changes.length ? (
              <ul className="xp-rows">
                {changes.map((c) => (
                  <Row key={c.sha} open={open === c.sha} onOpen={() => openChange(c.sha)}
                    head={<span className="xp-line"><span className="xp-origin">{c.label}</span><span className="xp-when" title={fmtWhen(c.at)}>{agoShort(c.at, undefined, fmtWhenShort)}</span></span>}>
                    {diff?.sha !== c.sha ? <p className="xp-none">Loading…</p> : diff.err ? <p className="xp-err" role="alert">{diff.err}</p> : <DiffView diff={diff.text} />}
                  </Row>
                ))}
              </ul>
            ) : <p className="xp-none">Nothing has changed memory or skills yet.</p>}
          </details>
        </>}
      </div>
    </div>
  );
}

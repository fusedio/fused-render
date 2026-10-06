// #rmodal (OpenBot dialogs.js routines): the bot's routines (Run, Pause/Enable, ✕ with a confirm) and the add form
// (Every N min / Daily at HH:MM on chosen weekdays, default Mon–Fri / Once at a date and time).
import { useEffect, useRef, useState } from "react";
import { api, type Bot, type RoutineBody } from "../lib/api";
import { fmtWhen } from "../lib/format";
import { DAYS, routineLabel } from "../lib/live";
import { act, showBanner, useBotsSelector } from "../state/store";
import { askConfirm } from "./ask";

type Kind = "interval" | "daily" | "once";

export function RoutinesDialog({ b, onClose }: { b: Bot; onClose: () => void }) {
  const tasks = useBotsSelector((s) => s.usage?.tasks);
  const [task, setTask] = useState("");
  const [kind, setKind] = useState<Kind>("interval");
  const [minutes, setMinutes] = useState("60");
  const [time, setTime] = useState("09:00");
  const [days, setDays] = useState<number[]>([0, 1, 2, 3, 4]);
  const [at, setAt] = useState("");
  const taskRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { taskRef.current?.focus(); }, []);

  const rs = b.routines || [];
  const onOp = async (rid: string, op: "run" | "enable" | "disable" | "delete") => {
    if (op === "delete" && !(await askConfirm("Delete this routine?", "It stops running and is removed from the list."))) return;
    await act(() => api.routines(b.id, { op, rid }));
  };
  const add = async () => {
    const text = task.trim(); if (!text) return;
    const p: Extract<RoutineBody, { op: "add" }> = { op: "add", text, kind };
    if (kind === "interval") p.minutes = Number(minutes) || 60;
    if (kind === "daily") { p.time = time || "09:00"; p.weekdays = [...days].sort((x, y) => x - y); }
    if (kind === "once") { if (!at) { showBanner("Pick a date and time"); return; } p.at = new Date(at).getTime() / 1000; }
    const r = await act(() => api.routines(b.id, p));
    if (r?.ok) setTask("");
  };

  return (
    <div id="rmodal" className="modal show" role="dialog" aria-modal="true" aria-labelledby="rtitle" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="box">
        <h3 id="rtitle">Routines · {b.name}</h3>
        <div id="rlist" className="rlist">
          {rs.length ? rs.map((r) => {
            const warn = r.last_result === "error" || (r.fails || 0) > 0;
            return (
              <div key={r.id} className={`r ${r.enabled ? "" : "off"}`} data-rid={r.id}>
                <div className="t">{r.task}</div>
                <div className="b">
                  <button data-op="run" title="Start this task now" onClick={() => { void onOp(r.id, "run"); }}>Run</button>
                  <button data-op={r.enabled ? "disable" : "enable"} onClick={() => { void onOp(r.id, r.enabled ? "disable" : "enable"); }}>{r.enabled ? "Pause" : "Enable"}</button>
                  <button data-op="delete" className="danger" onClick={() => { void onOp(r.id, "delete"); }}>✕</button>
                </div>
                <div className="s">
                  {routineLabel(r)} · {tasks?.[r.task] || 0} calls today · next: {r.enabled ? fmtWhen(r.next) : "paused"}
                  {r.last ? <> · last: {fmtWhen(r.last)} <span className={warn ? "warn" : ""}>({r.last_result || ""}{(r.fails || 0) > 1 ? `, ${r.fails} in a row` : ""})</span></> : null}
                  {r.last_message ? <> <span className="msg">{r.last_message}</span></> : null}
                </div>
              </div>
            );
          }) : <div className="muted">No routines yet. Add one below.</div>}
        </div>
        <div className="radd">
          <label className="field">Task<textarea id="rtask" ref={taskRef} rows={2} value={task} onChange={(e) => setTask(e.target.value)}
            placeholder="e.g. Check my LinkedIn feed and summarise the 10 newest posts" /></label>
          <div className="rrow">
            <select id="rkind" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
              <option value="interval">Every</option>
              <option value="daily">Daily at</option>
              <option value="once">Once at</option>
            </select>
            <span id="rk-interval" style={{ display: kind === "interval" ? "" : "none" }}>
              <input id="rmin" type="number" min={5} step={5} value={minutes} onChange={(e) => setMinutes(e.target.value)} style={{ width: 70 }} /> min
            </span>
            <span id="rk-daily" style={{ display: kind === "daily" ? "" : "none" }}>
              <input id="rtime" type="time" value={time} onChange={(e) => setTime(e.target.value)} />
              <span className="days" id="rdays">
                {DAYS.map((d, i) => (
                  <label key={d}><input type="checkbox" value={i} checked={days.includes(i)}
                    onChange={(e) => setDays((cur) => (e.target.checked ? [...cur, i] : cur.filter((x) => x !== i)))} /> {d}</label>
                ))}
              </span>
            </span>
            <span id="rk-once" style={{ display: kind === "once" ? "" : "none" }}>
              <input id="rat" type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} />
            </span>
            <button id="radd" className="primary" onClick={() => { void add(); }}>Add</button>
          </div>
          <p className="muted">Runs start only when the bot is idle; a busy bot skips that slot. The bot may still ask you questions during a scheduled run.</p>
        </div>
        <div className="row"><button id="rclose" onClick={onClose}>Close</button></div>
      </div>
    </div>
  );
}

// #smodal (OpenBot dialogs.js skills): the bot's playbooks (Edit, ✕ with a confirm), "Learn from last task" (the
// dialog closes; progress shows in the thread) and the hand-written form (Title, Trigger words, Steps).
import { useRef, useState } from "react";
import { api, type Bot } from "../lib/api";
import { act } from "../state/store";
import { askConfirm } from "./ask";

export function SkillsDialog({ b, onClose }: { b: Bot; onClose: () => void }) {
  const [form, setForm] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);  // the skill's name while editing an existing one
  const [title, setTitle] = useState(""), [trigger, setTrigger] = useState(""), [body, setBody] = useState("");
  const titleRef = useRef<HTMLInputElement>(null);
  const focusTitle = () => requestAnimationFrame(() => titleRef.current?.focus());

  const sk = b.skills || [];
  const edit = (name: string) => {
    const k = sk.find((x) => x.name === name); if (!k) return;
    setEditing(name); setTitle(k.title); setTrigger(k.trigger); setBody(k.body); setForm(true); focusTitle();
  };
  const del = async (name: string) => {
    const k = sk.find((x) => x.name === name);
    if (!(await askConfirm(`Delete the skill "${k?.title || name}"?`, "The bot stops using this playbook."))) return;
    await act(() => api.skills(b.id, { op: "delete", rid: name }));
  };
  const save = async () => {
    const r = await act(() => api.skills(b.id, { op: "save", name: title, trigger, text: body, ...(editing ? { rid: editing } : {}) }));
    if (!r?.ok) return;
    setForm(false); setEditing(null);
  };
  const learn = async () => {
    const r = await act(() => api.skills(b.id, { op: "learn" }));
    if (r?.ok) onClose();  // progress shows in the thread
  };

  return (
    <div id="smodal" className="modal show" role="dialog" aria-modal="true" aria-labelledby="stitle" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="box">
        <h3 id="stitle">Skills · {b.name}</h3>
        <p className="muted">A skill is a step-by-step playbook. When a task contains one of its trigger words, the bot gets the playbook in its prompt and follows it instead of exploring. The bot can also save one itself with <code>learn</code>.</p>
        <div id="slist" className="rlist">
          {sk.length ? sk.map((k) => (
            <div key={k.name} className="r" data-name={k.name}>
              <div className="t">{k.title}<small>trigger: {k.trigger}</small></div>
              <div className="b">
                <button data-op="edit" onClick={() => edit(k.name)}>Edit</button>
                <button data-op="delete" className="danger" onClick={() => { void del(k.name); }}>✕</button>
              </div>
              <div className="body">{k.body}</div>
            </div>
          )) : <div className="muted">No skills yet. Finish a task, then click "Learn from last task", or write one by hand.</div>}
        </div>
        <div className="radd">
          <div className="rrow">
            <button id="slearn" className="primary" title="Ask the model to condense this bot's most recent finished task into a playbook" onClick={() => { void learn(); }}>Learn from last task</button>
            <button id="snew" onClick={() => { setEditing(null); setTitle(""); setTrigger(""); setBody(""); setForm(true); focusTitle(); }}>Write one by hand</button>
          </div>
          <div id="sform" style={{ display: form ? "" : "none" }}>
            <label className="field">Title<input id="sktitle" ref={titleRef} placeholder="e.g. LinkedIn feed summary" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
            <label className="field">Trigger words <small>· comma-separated; any one appearing in a task mounts this playbook</small>
              <input id="sktrig" placeholder="linkedin feed, linkedin posts" value={trigger} onChange={(e) => setTrigger(e.target.value)} /></label>
            <label className="field">Steps<textarea id="skbody" rows={7} placeholder={"1. Go to https://…\n2. Dismiss the cookie banner\n3. …"} value={body} onChange={(e) => setBody(e.target.value)} /></label>
            <div className="rrow">
              <button id="sksave" className="primary" onClick={() => { void save(); }}>Save</button>
              <button id="skcancel" onClick={() => { setForm(false); setEditing(null); }}>Cancel</button>
            </div>
          </div>
        </div>
        <div className="row"><button id="sclose" onClick={onClose}>Close</button></div>
      </div>
    </div>
  );
}

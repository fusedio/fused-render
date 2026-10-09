// Settings › Skills (OpenBot dialogs.js skills, once its own #smodal): the bot's playbooks as cards (Edit, delete with a
// confirm), "Learn from last task" and the hand-written form (Title, Trigger words, Steps). Every op posts at once and
// the next poll repaints the list; nothing here touches the Settings snapshot or its Save. The confirm is a sibling
// modal, so it goes through the dialog's `confirm` (holds `busy`) and not askConfirm directly.
import { useState } from "react";
import { Trash2Icon } from "lucide-react";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { api, type Bot } from "../lib/api";
import { act } from "../state/store";
import { CARD_CLASS } from "./shell";

export interface SectionProps {
  b: Bot;
  /** askConfirm with the dialog held open while it is up (BotSettings owns the `busy` ref). */
  confirm: (title: string, text: string, ok?: string, danger?: boolean) => Promise<boolean>;
}

export function SkillsSection({ b, confirm }: SectionProps) {
  const [form, setForm] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);  // the skill's name while editing an existing one
  const [title, setTitle] = useState(""), [trigger, setTrigger] = useState(""), [body, setBody] = useState("");
  const [learning, setLearning] = useState(false);
  // By id, not ref: the shadcn Input forwards no ref on React 18.
  const focusTitle = () => requestAnimationFrame(() => document.getElementById("sktitle")?.focus());

  const sk = b.skills || [];
  const edit = (name: string) => {
    const k = sk.find((x) => x.name === name); if (!k) return;
    setEditing(name); setTitle(k.title); setTrigger(k.trigger); setBody(k.body); setForm(true); focusTitle();
  };
  const del = async (name: string) => {
    const k = sk.find((x) => x.name === name);
    if (!(await confirm(`Delete the skill "${k?.title || name}"?`, "The bot stops using this playbook."))) return;
    await act(() => api.skills(b.id, { op: "delete", rid: name }));
  };
  const save = async () => {
    const r = await act(() => api.skills(b.id, { op: "save", name: title, trigger, text: body, ...(editing ? { rid: editing } : {}) }));
    if (!r?.ok) return;
    setForm(false); setEditing(null);
  };
  const learn = async () => {
    const r = await act(() => api.skills(b.id, { op: "learn" }));
    if (r?.ok) setLearning(true);  // progress shows in the thread; the new card lands with the next poll
  };
  const blank = () => { setEditing(null); setTitle(""); setTrigger(""); setBody(""); setForm(true); focusTitle(); };

  return (
    <div className="flex flex-col gap-4">
      <p className="m-0 text-[13px] leading-5 text-muted-foreground">A skill is a step-by-step playbook. When a task contains one of its trigger words, the bot gets the playbook in its prompt and follows it instead of exploring. The bot can also save one itself with <code>learn</code>.</p>
      <div className="flex flex-col gap-3" id="slist">
        {sk.length ? sk.map((k) => (
          <section key={k.name} className={CARD_CLASS} data-name={k.name}>
            <div className="flex items-start justify-between gap-4">
              <div className="flex min-w-0 flex-col gap-1.5">
                <div className="text-[15px] font-medium">{k.title}</div>
                <div className="flex flex-wrap items-center gap-1.5">
                  {k.trigger.split(",").map((t) => t.trim()).filter(Boolean).map((t) => <Badge key={t} variant="secondary">{t}</Badge>)}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <Button variant="outline" size="sm" data-op="edit" onClick={() => edit(k.name)}>Edit</Button>
                <Button variant="ghost" size="icon-xs" data-op="delete" aria-label={`Delete ${k.title}`} title="Delete" className="text-muted-foreground hover:text-destructive" onClick={() => { void del(k.name); }}><Trash2Icon /></Button>
              </div>
            </div>
            <div className="max-h-32 overflow-y-auto whitespace-pre-wrap break-words text-[13px] leading-5 text-muted-foreground">{k.body}</div>
          </section>
        )) : <p className="m-0 text-sm text-muted-foreground">No skills yet. Finish a task, then click "Learn from last task", or write one by hand.</p>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button id="slearn" title="Ask the model to condense this bot's most recent finished task into a playbook" disabled={learning} onClick={() => { void learn(); }}>Learn from last task</Button>
        <Button id="snew" variant="outline" onClick={blank}>Write one by hand</Button>
        {learning ? <span className="text-[13px] text-muted-foreground">Learning from the last task; progress shows in the thread.</span> : null}
      </div>
      {form ? (
        <FieldGroup id="sform" className={CARD_CLASS}>
          <Field>
            <FieldLabel htmlFor="sktitle">Title</FieldLabel>
            <Input id="sktitle" placeholder="e.g. LinkedIn feed summary" value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field>
            <FieldLabel htmlFor="sktrig">Trigger words</FieldLabel>
            <Input id="sktrig" placeholder="linkedin feed, linkedin posts" value={trigger} onChange={(e) => setTrigger(e.target.value)} />
            <FieldDescription>Comma-separated; any one appearing in a task mounts this playbook.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="skbody">Steps</FieldLabel>
            <Textarea id="skbody" rows={7} placeholder={"1. Go to https://…\n2. Dismiss the cookie banner\n3. …"} value={body} onChange={(e) => setBody(e.target.value)} />
          </Field>
          <div className="flex justify-end gap-2">
            <Button id="skcancel" variant="outline" onClick={() => { setForm(false); setEditing(null); }}>Cancel</Button>
            <Button id="sksave" disabled={!title.trim() || !trigger.trim()} onClick={() => { void save(); }}>{editing ? "Save" : "Add playbook"}</Button>
          </div>
        </FieldGroup>
      ) : null}
    </div>
  );
}

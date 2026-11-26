// An expert's Settings › Skills: its Claude Code skills (.claude/skills/<name>/SKILL.md in its home) as cards (Edit,
// delete with a confirm) and the hand-written form (Name, When to use, Instructions). The bots' SkillsSection.tsx in
// look; an expert's skill has a description in place of trigger words, and it learns by itself, so no Learn button.
// Every op saves at once and answers with the new list; nothing here goes through the dialog's Save.
import { useEffect, useRef, useState } from "react";
import { Trash2Icon } from "lucide-react";
import type { Expert } from "@platform/lib/experts";
import { Button } from "@platform/shadcn/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { expertApi, type ExpertSkill } from "../lib/api";
import { revealWithin } from "../lib/scroll";
import { actExpert, errMsg } from "../state/store";
import { CARD_CLASS } from "./shell";

export interface ExpertSkillsSectionProps {
  s: Expert;
  /** askConfirm with the dialog held open while it is up (ExpertDialog owns the `busy` ref). */
  confirm: (title: string, text: string) => Promise<boolean>;
}

/** What the server takes for a new skill's folder name (home.py SKILL_NAME). */
const slug = (v: string) => v.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+/, "").slice(0, 64);

export function ExpertSkillsSection({ s, confirm }: ExpertSkillsSectionProps) {
  const [sk, setSk] = useState<ExpertSkill[] | null>(null);
  const [err, setErr] = useState("");
  const [form, setForm] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);  // the skill's name while editing an existing one
  const [name, setName] = useState(""), [description, setDescription] = useState(""), [body, setBody] = useState("");
  useEffect(() => {
    let live = true;
    void expertApi.skills(s.id).then((r) => { if (live) setSk(r.skills); }, (e) => { if (live) setErr(errMsg(e)); });
    return () => { live = false; };
  }, [s.id]);
  // The form opens under the list: bring it into view inside the dialog's own scroller only ([data-sec]).
  const focusForm = (id: string) => requestAnimationFrame(() => {
    const el = document.getElementById("xsform");
    revealWithin(el?.closest<HTMLElement>("[data-sec]") ?? null, el);
    document.getElementById(id)?.focus({ preventScroll: true });
  });
  // Which draft the form holds: a save that finishes closes the form only if it still holds the draft it sent.
  const draft = useRef(0);
  const edit = (k: ExpertSkill) => {
    draft.current++;
    setEditing(k.name); setName(k.name); setDescription(k.description); setBody(k.body); setForm(true); focusForm("xsdesc");
  };
  const blank = () => { draft.current++; setEditing(null); setName(""); setDescription(""); setBody(""); setForm(true); focusForm("xsname"); };
  const del = async (k: ExpertSkill) => {
    if (!(await confirm(`Delete the skill "${k.name}"?`, "The expert stops using it."))) return;
    const r = await actExpert(() => expertApi.deleteSkill(s.id, k.name));
    if (!r) return;
    setSk(r.skills);
    if (editing === k.name) { setForm(false); setEditing(null); }
  };
  const finalName = editing ?? slug(name).replace(/-+$/, "");
  const save = async () => {
    const mine = draft.current;
    const r = await actExpert(() => expertApi.saveSkill(s.id, finalName, { description, body, ...(editing ? {} : { new: true }) }));
    if (!r) return;  // refused (the banner says why): the draft stays for another try
    setSk(r.skills);
    if (draft.current === mine) { setForm(false); setEditing(null); }
  };

  return (
    <div className="flex flex-col gap-4">
      <p className="m-0 text-[13px] leading-5 text-muted-foreground">A skill is a set of instructions for one kind of work. The expert, and a task it helps, reads a skill when its description fits the work. The expert also writes skills itself as it learns.</p>
      {err ? <p className="m-0 text-sm text-destructive" role="alert">{err}</p> : null}
      <div className="flex flex-col gap-3" id="xslist">
        {!sk ? (err ? null : <p className="m-0 text-sm text-muted-foreground">Loading…</p>) : sk.length ? sk.map((k) => (
          <section key={k.name} className={CARD_CLASS} data-name={k.name}>
            <div className="flex items-start justify-between gap-4">
              <div className="flex min-w-0 flex-col gap-1">
                <div className="text-[15px] font-medium">{k.name}</div>
                <div className="text-[13px] leading-5 text-muted-foreground">{k.description}</div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <Button variant="outline" size="sm" data-op="edit" onClick={() => edit(k)}>Edit</Button>
                <Button variant="ghost" size="icon-xs" data-op="delete" aria-label={`Delete ${k.name}`} title="Delete" className="text-muted-foreground hover:text-destructive" onClick={() => { void del(k); }}><Trash2Icon /></Button>
              </div>
            </div>
            {k.body ? <div className="max-h-32 overflow-y-auto whitespace-pre-wrap break-words text-[13px] leading-5 text-muted-foreground">{k.body}</div> : null}
          </section>
        )) : <p className="m-0 text-sm text-muted-foreground">No skills yet. Write one here, or tell the expert in its chat.</p>}
      </div>
      <div><Button id="xsnew" variant="outline" disabled={!sk} onClick={blank}>Write a skill</Button></div>
      {form ? (
        <FieldGroup id="xsform" className={CARD_CLASS}>
          <Field>
            <FieldLabel htmlFor="xsname">Name</FieldLabel>
            <Input id="xsname" placeholder="e.g. canvas-layout" disabled={!!editing} value={name} onChange={(e) => setName(slug(e.target.value))} />
            <FieldDescription>{editing ? "A skill keeps its name." : "Lowercase letters, digits and dashes."}</FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="xsdesc">When to use it</FieldLabel>
            <Textarea id="xsdesc" rows={2} placeholder="e.g. Use when laying out a canvas or moving its nodes." value={description} onChange={(e) => setDescription(e.target.value)} />
            <FieldDescription>The expert picks a skill by this description.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="xsbody">Instructions</FieldLabel>
            <Textarea id="xsbody" rows={9} className="max-h-80" placeholder={"1. …\n2. …"} value={body} onChange={(e) => setBody(e.target.value)} />
          </Field>
          <div className="flex justify-end gap-2">
            <Button id="xscancel" variant="outline" onClick={() => { setForm(false); setEditing(null); }}>Cancel</Button>
            <Button id="xssave" disabled={!finalName || !description.trim()} onClick={() => { void save(); }}>{editing ? "Save skill" : "Add skill"}</Button>
          </div>
        </FieldGroup>
      ) : null}
    </div>
  );
}

// Settings › Skills (PRODUCT §1 "Skills: procedures in Claude Code's skill
// format; nightly job, user"): the list, and one skill's SKILL.md to read,
// edit, save or delete. A new skill starts from a frontmatter template.
import { useCallback, useEffect, useState } from "react";
import { Field, TextArea, TextInput } from "@platform/ui/field/fields";
import { deleteSkill, errorText, getSkill, listSkills, putSkill, type SkillRow } from "@platform/lib/specialists-api";

export function skillTemplate(name: string): string {
  return `---\nname: ${name}\ndescription: When to use this skill, in one sentence.\n---\n\n# ${name}\n\n1. \n`;
}

/** A skill folder name: lower-case words joined by dashes. */
export function skillSlug(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

export function SkillsTab({
  specialistId,
  api = { listSkills, getSkill, putSkill, deleteSkill },
}: {
  specialistId: string;
  api?: { listSkills: typeof listSkills; getSkill: typeof getSkill; putSkill: typeof putSkill; deleteSkill: typeof deleteSkill };
}) {
  const [skills, setSkills] = useState<SkillRow[] | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [saved, setSaved] = useState("");
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const { listSkills: list, getSkill: get, putSkill: put, deleteSkill: del } = api;

  const load = useCallback(() => {
    list(specialistId).then((r) => setSkills(r.skills || []), (e) => setError(errorText(e)));
  }, [list, specialistId]);
  useEffect(load, [load]);

  const open = async (name: string) => {
    setSel(name);
    setError("");
    setNote("");
    try {
      const r = await get(specialistId, name);
      setText(r.text);
      setSaved(r.text);
    } catch (e) {
      setError(errorText(e));
    }
  };
  const save = async () => {
    if (!sel) return;
    setBusy(true);
    setError("");
    try {
      const r = await put(specialistId, sel, text);
      setSaved(r.text ?? text);
      setNote("Saved. The specialist uses it from its next session.");
      load();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!sel || !window.confirm(`Delete the skill ${sel}? It stays in the specialist's history.`)) return;
    setBusy(true);
    setError("");
    try {
      await del(specialistId, sel);
      setSel(null);
      setText("");
      setSaved("");
      load();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const create = () => {
    const name = skillSlug(newName);
    if (!name) return;
    setSel(name);
    setText(skillTemplate(name));
    setSaved("");
    setNewName("");
    setNote("Not saved yet.");
  };

  return (
    <div className="spx-tab spx-split">
      <div className="spx-split-list">
        <ul className="spx-plain-list" aria-label="Skills">
          {(skills ?? []).map((s) => (
            <li key={s.name}>
              <button
                type="button"
                className={"spx-entry" + (s.name === sel ? " is-selected" : "")}
                aria-pressed={s.name === sel}
                onClick={() => void open(s.name)}
              >
                <span className="spx-strong">{s.name}</span>
                <span className="spx-muted spx-small">{s.description}</span>
              </button>
            </li>
          ))}
        </ul>
        {skills && !skills.length ? <div className="spx-muted spx-small">No skills yet.</div> : null}
        <form
          className="spx-row-gap"
          onSubmit={(e) => {
            e.preventDefault();
            create();
          }}
        >
          <Field label="New skill">
            <TextInput value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="publish-checklist" />
          </Field>
          <button type="submit" className="btn btn-secondary" disabled={!skillSlug(newName)}>
            Add
          </button>
        </form>
      </div>
      <div className="spx-split-main">
        {error ? (
          <div className="spx-error" role="alert">
            {error}
          </div>
        ) : null}
        {sel ? (
          <>
            <Field label={`${sel}/SKILL.md`}>
              <TextArea rows={16} className="spx-mono" value={text} onChange={(e) => setText(e.target.value)} />
            </Field>
            <div className="spx-row-gap">
              <button type="button" className="btn btn-primary" onClick={() => void save()} disabled={busy || text === saved}>
                Save
              </button>
              {saved ? (
                <button type="button" className="btn btn-danger" onClick={() => void remove()} disabled={busy}>
                  Delete
                </button>
              ) : null}
              {note ? (
                <span className="spx-muted spx-small" role="status">
                  {note}
                </span>
              ) : null}
            </div>
          </>
        ) : (
          <div className="spx-muted">Pick a skill to read or edit it.</div>
        )}
      </div>
    </div>
  );
}

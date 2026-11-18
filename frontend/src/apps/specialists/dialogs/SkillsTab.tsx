// Settings › Skills (PRODUCT §1 "Skills: procedures in Claude Code's skill
// format; nightly job, user"): the list, and one skill to read, edit, save or
// delete. The file is SKILL.md with a front-matter head; the reader edits its
// name and description as fields and the steps as text, never the raw YAML.
// Any other front-matter keys are kept as they were.
import { useCallback, useEffect, useState } from "react";
import { Field, TextArea, TextInput } from "@platform/ui/field/fields";
import { deleteSkill, errorText, getSkill, listSkills, putSkill, type SkillRow } from "@platform/lib/specialists-api";
import { ConfirmDialog } from "../components/ConfirmDialog";

/** A skill folder name: lower-case words joined by dashes. */
export function skillSlug(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

export interface SkillParts {
  name: string;
  description: string;
  body: string;
  /** Other front-matter lines, verbatim, in order. */
  extra: string[];
}

const unquote = (v: string) => {
  const t = v.trim();
  if ((t.startsWith('"') && t.endsWith('"')) || (t.startsWith("'") && t.endsWith("'"))) return t.slice(1, -1);
  return t;
};
const yamlValue = (v: string) => {
  const t = v.replace(/\s+/g, " ").trim();
  return /^[\w\s.,()/'-]*$/.test(t) && !/^[-'\s]/.test(t) && !t.includes(": ") ? t : JSON.stringify(t);
};

/** SKILL.md → its parts. A file with no front matter is all body. */
export function parseSkill(text: string): SkillParts {
  const m = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/.exec(text);
  if (!m) return { name: "", description: "", body: text, extra: [] };
  let name = "";
  let description = "";
  const extra: string[] = [];
  const lines = m[1].split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const kv = /^(name|description):\s*(.*)$/.exec(line);
    if (!kv) {
      extra.push(line);
      continue;
    }
    let value = kv[2];
    // A folded / literal block (`description: >`): the indented lines after it.
    if (/^[>|][+-]?\s*$/.test(value)) {
      const parts: string[] = [];
      while (i + 1 < lines.length && /^\s+\S/.test(lines[i + 1])) parts.push(lines[++i].trim());
      value = parts.join(" ");
    }
    if (kv[1] === "name") name = unquote(value);
    else description = unquote(value);
  }
  return { name, description, body: text.slice(m[0].length).replace(/^\r?\n/, ""), extra };
}

/** Parts → SKILL.md. */
export function buildSkill(p: SkillParts): string {
  const head = [`name: ${yamlValue(p.name)}`, `description: ${yamlValue(p.description)}`, ...p.extra.filter((l) => l.trim())];
  return `---\n${head.join("\n")}\n---\n\n${p.body.replace(/^\s*\n/, "")}`;
}

export function skillTemplate(name: string): string {
  return buildSkill({ name, description: "", body: "1. \n", extra: [] });
}

const sameParts = (a: SkillParts, b: SkillParts) =>
  a.name === b.name && a.description === b.description && a.body === b.body;

export function SkillsTab({
  specialistId,
  api = { listSkills, getSkill, putSkill, deleteSkill },
}: {
  specialistId: string;
  api?: { listSkills: typeof listSkills; getSkill: typeof getSkill; putSkill: typeof putSkill; deleteSkill: typeof deleteSkill };
}) {
  const [skills, setSkills] = useState<SkillRow[] | null>(null);
  /** The folder of the skill on screen ("" = a new one, not saved yet). */
  const [sel, setSel] = useState<string | null>(null);
  const [parts, setParts] = useState<SkillParts>({ name: "", description: "", body: "", extra: [] });
  const [saved, setSaved] = useState<SkillParts | null>(null);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [confirming, setConfirming] = useState(false);
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
      const p = parseSkill(r.text);
      if (!p.name) p.name = name;
      setParts(p);
      setSaved(p);
    } catch (e) {
      setError(errorText(e));
    }
  };
  const save = async () => {
    if (sel === null) return;
    const folder = skillSlug(parts.name);
    if (!folder) {
      setError("Give the skill a name.");
      return;
    }
    if (!parts.description.trim()) {
      setError("Say in one sentence when to use it: the specialist reads that to decide.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const next = { ...parts, name: folder };
      await put(specialistId, folder, buildSkill(next));
      // A rename: the skill now lives under its new folder.
      if (sel && sel !== folder) await del(specialistId, sel);
      setSel(folder);
      setParts(next);
      setSaved(next);
      setNote("Saved. The specialist uses it from its next session.");
      load();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!sel) return;
    setError("");
    try {
      await del(specialistId, sel);
      setSel(null);
      setSaved(null);
      setNote("");
      load();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setConfirming(false);
    }
  };
  const create = () => {
    const name = skillSlug(newName);
    if (!name) return;
    setSel("");
    setParts({ name, description: "", body: "1. \n", extra: [] });
    setSaved(null);
    setNewName("");
    setError("");
    setNote("Not saved yet.");
  };
  const set = (k: keyof SkillParts, v: string) => {
    setParts((p) => ({ ...p, [k]: v }));
    setNote("");
  };
  const dirty = !saved || !sameParts(parts, saved);
  const fieldId = `spx-skill-${specialistId}`;

  return (
    <div className="spx-tab spx-split">
      <div className="spx-split-list">
        {skills && skills.length ? (
          <ul className="spx-plain-list" aria-label="Skills">
            {skills.map((s) => (
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
        ) : null}
        <form
          className="spx-skill-new"
          onSubmit={(e) => {
            e.preventDefault();
            create();
          }}
        >
          <Field label="New skill" hint="A short name, like publish-checklist.">
            <TextInput value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. publish-checklist" />
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
        {sel !== null ? (
          <form
            className="spx-form"
            onSubmit={(e) => {
              e.preventDefault();
              void save();
            }}
          >
            <Field label="Name" required hint="Lower-case words joined by dashes.">
              <TextInput id={`${fieldId}-name`} value={parts.name} onChange={(e) => set("name", e.target.value)} maxLength={64} />
            </Field>
            <Field label="When to use it" required hint="One sentence. The specialist reads this to decide whether the skill applies.">
              <TextInput id={`${fieldId}-desc`} value={parts.description} onChange={(e) => set("description", e.target.value)} maxLength={300} />
            </Field>
            <Field label="Steps" hint="What to do, in order. Markdown is fine.">
              <TextArea id={`${fieldId}-body`} rows={12} className="spx-prose" value={parts.body} onChange={(e) => set("body", e.target.value)} />
            </Field>
            <div className="spx-row-gap">
              <button type="submit" className="btn btn-primary" disabled={busy || !dirty}>
                {busy ? "Saving…" : "Save"}
              </button>
              {sel ? (
                <button type="button" className="btn btn-danger" onClick={() => setConfirming(true)} disabled={busy}>
                  Delete
                </button>
              ) : null}
              {note ? (
                <span className="spx-muted spx-small" role="status">
                  {note}
                </span>
              ) : null}
            </div>
          </form>
        ) : skills && !skills.length ? (
          <div className="spx-empty-note">
            <p className="spx-strong">No skills yet</p>
            <p>
              A skill is a procedure the specialist follows step by step, like a publish checklist. The update adds one when it
              sees the same steps needed again and again; you can also add one here.
            </p>
          </div>
        ) : skills ? (
          <div className="spx-empty-note">Pick a skill to read or edit it.</div>
        ) : (
          <div className="spx-muted">Loading…</div>
        )}
      </div>
      {confirming && sel ? (
        <ConfirmDialog
          title={`Delete the skill ${sel}?`}
          text="The specialist stops using it from its next session. It stays in the specialist's history."
          onCancel={() => setConfirming(false)}
          onConfirm={remove}
        />
      ) : null}
    </div>
  );
}

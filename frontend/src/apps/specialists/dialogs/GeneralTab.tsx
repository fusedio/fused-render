// Settings › General (PRODUCT §8, §9): name, subject, triggers, model, the
// charter, the counts we keep, and delete.
import { useEffect, useState } from "react";
import { Field, Select, TextArea, TextInput } from "@platform/ui/field/fields";
import {
  deleteSpecialist,
  errorText,
  getCharter,
  getCounts,
  patchSpecialist,
  putCharter,
  type Counts,
  type SpecialistDetail,
} from "@platform/lib/specialists-api";
import { triggersFrom } from "./CreateSpecialist";

export const MODELS = ["", "sonnet", "opus", "haiku"] as const;
export const EFFORTS = ["", "low", "medium", "high"] as const;

/** The §9 counts as label/value rows. */
export function countRows(c: Counts): [string, number][] {
  const src = c.notes_by_source || {};
  const by = (k: string) => Number(src[k] || 0);
  return [
    ["Breakouts opened", c.breakouts_opened || 0],
    ["Repeat breakouts (same problem again)", c.repeat_breakouts || 0],
    ["Pulls", c.pulls || 0],
    ["Pulls confirmed", c.pulls_confirmed || 0],
    ["Corrections received", c.corrections_received || 0],
    ["Notes from tasks", by("task")],
    ["Notes from breakouts", by("breakout")],
    ["Notes from its own chat", by("main")],
    ["Notes from reviews", by("reviewer")],
    ["Notes you sent", by("user")],
    ["Notes from the nightly update", by("nightly")],
  ];
}

export interface GeneralApi {
  patchSpecialist: typeof patchSpecialist;
  deleteSpecialist: typeof deleteSpecialist;
  getCounts: typeof getCounts;
  getCharter: typeof getCharter;
  putCharter: typeof putCharter;
}
const DEFAULT_API: GeneralApi = { patchSpecialist, deleteSpecialist, getCounts, getCharter, putCharter };

export function GeneralTab({
  specialist,
  onSaved,
  onDeleted,
  api = DEFAULT_API,
  confirm = (t: string) => window.confirm(t),
}: {
  specialist: SpecialistDetail;
  onSaved: (s: SpecialistDetail) => void;
  onDeleted: () => void;
  api?: GeneralApi;
  confirm?: (text: string) => boolean;
}) {
  const [name, setName] = useState(specialist.name);
  const [subject, setSubject] = useState(specialist.subject);
  const [folders, setFolders] = useState((specialist.triggers?.folders ?? []).join("\n"));
  const [keywords, setKeywords] = useState((specialist.triggers?.keywords ?? []).join(", "));
  const [model, setModel] = useState(specialist.model || "");
  const [effort, setEffort] = useState(specialist.effort || "");
  const [charter, setCharter] = useState<string | null>(null);
  const [charterSaved, setCharterSaved] = useState("");
  const [counts, setCounts] = useState<Counts | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    let live = true;
    api.getCounts(specialist.id).then((c) => live && setCounts(c), () => {});
    api.getCharter(specialist.id).then(
      (c) => {
        if (!live) return;
        setCharter(c.text);
        setCharterSaved(c.text);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [api, specialist.id]);

  const save = async () => {
    if (!name.trim() || !subject.trim()) {
      setError("A specialist needs a name and a subject.");
      return;
    }
    setBusy(true);
    setError("");
    setNote("");
    try {
      const next = await api.patchSpecialist(specialist.id, {
        name: name.trim(),
        subject: subject.trim(),
        triggers: triggersFrom(folders, keywords),
        model,
        effort,
      });
      if (charter !== null && charter !== charterSaved) {
        await api.putCharter(specialist.id, charter);
        setCharterSaved(charter);
      }
      onSaved(next);
      setNote("Saved.");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (
      !confirm(
        `Delete the ${specialist.name} specialist? Its home folder moves to the trash folder of specialists; open breakouts stop.`,
      )
    )
      return;
    setBusy(true);
    setError("");
    try {
      await api.deleteSpecialist(specialist.id);
      onDeleted();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  };

  return (
    <div className="spx-tab">
      <form
        className="spx-form"
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <Field label="Name" required>
          <TextInput value={name} onChange={(e) => setName(e.target.value)} maxLength={60} />
        </Field>
        <Field label="Subject" required>
          <TextInput value={subject} onChange={(e) => setSubject(e.target.value)} maxLength={200} />
        </Field>
        <Field label="Bring me in for these folders" hint="One folder per line.">
          <TextArea rows={2} value={folders} onChange={(e) => setFolders(e.target.value)} />
        </Field>
        <Field label="…and these words" hint="Separate with commas.">
          <TextInput value={keywords} onChange={(e) => setKeywords(e.target.value)} />
        </Field>
        <div className="spx-row-gap">
          <Field label="Model for its side sessions">
            <Select value={model} onChange={(e) => setModel(e.target.value)}>
              {MODELS.map((m) => (
                <option key={m} value={m}>
                  {m ? m[0].toUpperCase() + m.slice(1) : "Default"}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Effort">
            <Select value={effort} onChange={(e) => setEffort(e.target.value)}>
              {EFFORTS.map((m) => (
                <option key={m} value={m}>
                  {m ? m[0].toUpperCase() + m.slice(1) : "Default"}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <Field label="Charter" hint="What the subject is and how it behaves. Its memory is kept apart and is not edited here.">
          <TextArea rows={8} className="spx-mono" value={charter ?? ""} disabled={charter === null} onChange={(e) => setCharter(e.target.value)} />
        </Field>
        {error ? (
          <div className="spx-error" role="alert">
            {error}
          </div>
        ) : null}
        <div className="spx-row-gap">
          <button type="submit" className="btn btn-primary" disabled={busy}>
            Save
          </button>
          {note ? (
            <span className="spx-muted spx-small" role="status">
              {note}
            </span>
          ) : null}
        </div>
      </form>
      <section aria-label="Counts">
        <h3>How it is doing</h3>
        <p className="spx-muted spx-small">For you to see whether it is working. Nothing acts on these.</p>
        {counts ? (
          <dl className="spx-grid spx-counts">
            {countRows(counts).map(([k, v]) => (
              <div key={k} className="spx-count">
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <div className="spx-muted spx-small">Loading…</div>
        )}
      </section>
      <section aria-label="Delete" className="spx-danger-zone">
        <button type="button" className="btn btn-danger" onClick={() => void remove()} disabled={busy}>
          Delete this specialist
        </button>
      </section>
    </div>
  );
}

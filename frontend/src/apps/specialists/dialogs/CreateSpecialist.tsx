// "+ New specialist" (PRODUCT §1): a name, a one-line subject, and the
// optional "when to bring me in" triggers — folders and keywords.
import { useRef, useState } from "react";
import { Modal } from "@platform/ui/modal/Modal";
import { Field, Select, TextArea, TextInput } from "@platform/ui/field/fields";
import {
  createSpecialist,
  errorText,
  patchSpecialist,
  type SpecialistDetail,
  type Triggers,
} from "@platform/lib/specialists-api";
import { MODELS, MODEL_HINT, optionLabel } from "../lib/api-extra";

/** One folder per line, blanks dropped. */
export function parseFolders(text: string): string[] {
  return text
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Comma- or line-separated keywords, trimmed, deduped (case-insensitive). */
export function parseKeywords(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(/[,\n]/)) {
    const k = raw.trim();
    if (!k || seen.has(k.toLowerCase())) continue;
    seen.add(k.toLowerCase());
    out.push(k);
  }
  return out;
}

export function triggersFrom(folders: string, keywords: string): Triggers {
  return { folders: parseFolders(folders), keywords: parseKeywords(keywords) };
}

export function CreateSpecialist({
  onClose,
  onCreated,
  create = createSpecialist,
  patch = patchSpecialist,
}: {
  onClose: () => void;
  onCreated: (s: SpecialistDetail) => void;
  // Seams for tests.
  create?: typeof createSpecialist;
  patch?: typeof patchSpecialist;
}) {
  const [name, setName] = useState("");
  const [subject, setSubject] = useState("");
  const [folders, setFolders] = useState("");
  const [keywords, setKeywords] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [fieldErr, setFieldErr] = useState<{ name?: string; subject?: string }>({});
  const first = useRef<HTMLInputElement>(null);
  const subjectRef = useRef<HTMLInputElement>(null);

  const submit = async () => {
    const errs: { name?: string; subject?: string } = {};
    if (!name.trim()) errs.name = "Give it a name.";
    if (!subject.trim()) errs.subject = "Say in one line what it is an expert on.";
    setFieldErr(errs);
    if (errs.name || errs.subject) {
      (errs.name ? first : subjectRef).current?.focus();
      return;
    }
    setBusy(true);
    setError("");
    try {
      const t = triggersFrom(folders, keywords);
      let made = await create({
        name: name.trim(),
        subject: subject.trim(),
        ...(t.folders.length || t.keywords.length ? { triggers: t } : {}),
      });
      if (model) {
        try {
          made = await patch(made.id, { model });
        } catch {
          // The specialist exists; its model can be set in Settings › General.
        }
      }
      onCreated(made);
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };
  const errId = (k: string) => `spx-create-${k}-error`;
  const errHint = (k: "name" | "subject", hint?: string) =>
    fieldErr[k] ? (
      <span id={errId(k)} className="spx-field-error">
        {fieldErr[k]}
      </span>
    ) : (
      hint
    );

  return (
    <Modal
      title="New specialist"
      onClose={onClose}
      busy={busy}
      initialFocus={first}
      width="min(580px, calc(100vw - 32px))"
      dialogClassName="spx-dialog spx-create"
      dirty={!!(name || subject || folders || keywords)}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form="spx-create-form" className="btn btn-primary" disabled={busy} aria-busy={busy}>
            {busy ? (
              <>
                <span className="spx-spinner" aria-hidden="true" />
                Creating…
              </>
            ) : (
              "Create"
            )}
          </button>
        </>
      }
    >
      <form
        id="spx-create-form"
        className="spx-form"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <Field label="Name" required hint={errHint("name")}>
          <TextInput
            ref={first}
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (fieldErr.name) setFieldErr((f) => ({ ...f, name: undefined }));
            }}
            placeholder="e.g. Canvas"
            maxLength={60}
            disabled={busy}
            aria-invalid={!!fieldErr.name}
            {...(fieldErr.name ? { "aria-describedby": errId("name") } : {})}
          />
        </Field>
        <Field label="Subject" required hint={errHint("subject", "One line: what it is an expert on.")}>
          <TextInput
            ref={subjectRef}
            value={subject}
            onChange={(e) => {
              setSubject(e.target.value);
              if (fieldErr.subject) setFieldErr((f) => ({ ...f, subject: undefined }));
            }}
            placeholder="e.g. Fused Workbench canvases: layouts, filters, sync and publishing"
            maxLength={200}
            disabled={busy}
            aria-invalid={!!fieldErr.subject}
            {...(fieldErr.subject ? { "aria-describedby": errId("subject") } : {})}
          />
        </Field>
        <Field label="Bring me in for these folders" hint="Optional. One folder per line. Tasks working there get its guidance.">
          <TextArea rows={2} value={folders} onChange={(e) => setFolders(e.target.value)} placeholder="e.g. ~/work/q4-canvas" disabled={busy} />
        </Field>
        <Field label="…and these words" hint="Optional. Separate with commas. A task message with one of them gets its guidance.">
          <TextInput value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder="e.g. canvas, sync, publish" disabled={busy} />
        </Field>
        <Field label="Model" hint={MODEL_HINT}>
          <Select value={model} onChange={(e) => setModel(e.target.value)} disabled={busy}>
            {MODELS.map((m) => (
              <option key={m} value={m}>
                {optionLabel(m)}
              </option>
            ))}
          </Select>
        </Field>
        {error ? (
          <div className="spx-error" role="alert">
            {error}
          </div>
        ) : null}
      </form>
    </Modal>
  );
}

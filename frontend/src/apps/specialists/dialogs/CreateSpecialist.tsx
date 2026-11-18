// "+ New specialist" (PRODUCT §1): a name, a one-line subject, and the
// optional "when to bring me in" triggers — folders and keywords.
import { useRef, useState } from "react";
import { Modal } from "@platform/ui/modal/Modal";
import { Field, TextArea, TextInput } from "@platform/ui/field/fields";
import { createSpecialist, errorText, type SpecialistDetail, type Triggers } from "@platform/lib/specialists-api";

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
}: {
  onClose: () => void;
  onCreated: (s: SpecialistDetail) => void;
  // Seam for tests.
  create?: typeof createSpecialist;
}) {
  const [name, setName] = useState("");
  const [subject, setSubject] = useState("");
  const [folders, setFolders] = useState("");
  const [keywords, setKeywords] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const first = useRef<HTMLInputElement>(null);

  const submit = async () => {
    if (!name.trim() || !subject.trim()) {
      setError("Give it a name and say what it is an expert on.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const t = triggersFrom(folders, keywords);
      const made = await create({
        name: name.trim(),
        subject: subject.trim(),
        ...(t.folders.length || t.keywords.length ? { triggers: t } : {}),
      });
      onCreated(made);
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };

  return (
    <Modal
      title="New specialist"
      onClose={onClose}
      busy={busy}
      initialFocus={first}
      dirty={!!(name || subject || folders || keywords)}
      footer={
        <>
          <button type="button" className="btn btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form="spx-create-form" className="btn btn-primary" disabled={busy}>
            Create
          </button>
        </>
      }
    >
      <form
        id="spx-create-form"
        className="spx-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <Field label="Name" required>
          <TextInput ref={first} value={name} onChange={(e) => setName(e.target.value)} placeholder="Canvas" maxLength={60} />
        </Field>
        <Field label="Subject" required hint="One line: what it is an expert on.">
          <TextInput
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            placeholder="Fused Workbench canvases: layouts, filters, sync and publishing"
            maxLength={200}
          />
        </Field>
        <Field label="Bring me in for these folders" hint="Optional. One folder per line. Tasks working there get its guidance.">
          <TextArea rows={2} value={folders} onChange={(e) => setFolders(e.target.value)} placeholder="~/work/q4-canvas" />
        </Field>
        <Field label="…and these words" hint="Optional. Separate with commas. A task message with one of them gets its guidance.">
          <TextInput value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder="canvas, sync, publish" />
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

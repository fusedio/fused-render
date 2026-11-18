// THE GENERAL INBOX (PRODUCT §4 "No specialist fits"): notes that went to no
// specialist. Each can be given to one (POST …/general/notes/{nid}/move) or
// deleted. Opened from the quiet line under the list.
import { useState } from "react";
import { Modal } from "@platform/ui/modal/Modal";
import {
  deleteNote,
  errorText,
  moveGeneralNote,
  type Note,
  type SpecialistSummary,
} from "@platform/lib/specialists-api";
import { NOTE_KIND, noteSource } from "./MemoryTab";
import { dayLabel } from "../components/FoldedSession";
import { secondPerson } from "../lib/api-extra";

export function GeneralInbox({
  notes,
  specialists,
  onClose,
  onChanged,
  api = { moveGeneralNote, deleteNote },
}: {
  notes: readonly Note[];
  specialists: readonly SpecialistSummary[];
  onClose: () => void;
  /** Re-read the general inbox. */
  onChanged: () => void;
  api?: { moveGeneralNote: typeof moveGeneralNote; deleteNote: typeof deleteNote };
}) {
  const [target, setTarget] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");

  const act = async (nid: string, fn: () => Promise<unknown>) => {
    setBusy(nid);
    setError("");
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Modal
      title="Notes that fit no specialist"
      onClose={onClose}
      width="min(640px, calc(100vw - 32px))"
      dialogClassName="spx-dialog spx-general"
    >
      <div className="spx-tab">
        {error ? (
          <div className="spx-error" role="alert">
            {error}
          </div>
        ) : null}
        {!notes.length ? <div className="spx-empty-note">None left. Every note has a specialist.</div> : null}
        {notes.length ? (
          <p className="spx-muted spx-small">
            These notes came from your tasks but match no specialist's subject. Give each to the one that should learn it, or
            delete it.
          </p>
        ) : null}
        {notes.map((n) => {
          const pick = target[n.id] ?? specialists[0]?.id ?? "";
          return (
            <div key={n.id} className="spx-note">
              <div className="spx-note-head">
                <span className="spx-warn">{NOTE_KIND[n.kind] || n.kind}</span>
                <span className="spx-muted">{dayLabel(n.at)}</span>
              </div>
              <span>{secondPerson(n.claim)}</span>
              <span className="spx-muted spx-small">{noteSource(n)}</span>
              <div className="spx-row-gap">
                <label className="spx-small spx-muted" htmlFor={`spx-give-${n.id}`}>
                  Give to
                </label>
                <select
                  id={`spx-give-${n.id}`}
                  className="field-control spx-give"
                  value={pick}
                  onChange={(e) => setTarget({ ...target, [n.id]: e.target.value })}
                  disabled={!specialists.length}
                >
                  {specialists.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} specialist
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={!pick || busy !== null}
                  onClick={() => void act(n.id, () => api.moveGeneralNote(n.id, pick))}
                >
                  Give
                </button>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={busy !== null}
                  onClick={() => void act(n.id, () => api.deleteNote("general", n.id))}
                >
                  Delete
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </Modal>
  );
}

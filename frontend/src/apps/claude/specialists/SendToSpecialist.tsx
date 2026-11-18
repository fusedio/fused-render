// "SEND TO A SPECIALIST" on a message (PRODUCT §4, §8; PLAN D.2 #8): any
// message of a task can go to a specialist's inbox as a note — basis
// `user-said`, so it is in effect at once — or to the general inbox when none
// fits. No approval card; a sent chip with Undo instead.
import { useEffect, useRef, useState } from "react";
import { addNote, deleteNote, errorText, type SpecialistSummary } from "@platform/lib/specialists-api";

export const GENERAL_INBOX = "general";

/** One sentence (≤300 chars, B.2) out of a message, for the note's claim. */
export function claimOf(text: string): string {
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= 300) return flat;
  const cut = flat.slice(0, 299);
  const end = cut.lastIndexOf(". ");
  return (end > 80 ? cut.slice(0, end + 1) : cut.trimEnd() + "…").slice(0, 300);
}

export function SendToSpecialist({
  text,
  uuid,
  sessionId,
  title,
  specialists,
  onSent,
  add = addNote,
  undo = deleteNote,
}: {
  text: string;
  uuid?: string;
  sessionId: string;
  title?: string;
  specialists: readonly SpecialistSummary[];
  /** The task's events changed (a `note` event). */
  onSent?: () => void;
  // Seams for tests.
  add?: typeof addNote;
  undo?: typeof deleteNote;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState<{ spec: string; name: string; id: string } | null>(null);
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrap.current && e.target instanceof Node && !wrap.current.contains(e.target)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const send = async (spec: string, name: string) => {
    setOpen(false);
    setBusy(true);
    setError("");
    try {
      const note = await add(spec, {
        kind: "case",
        claim: claimOf(text),
        evidence: text.slice(0, 4000),
        basis: "user-said",
        source: { session_id: sessionId, ...(uuid ? { uuid } : {}), ...(title ? { title } : {}) },
      });
      setSent({ spec, name, id: note.id });
      onSent?.();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const onUndo = async () => {
    if (!sent) return;
    setBusy(true);
    setError("");
    try {
      await undo(sent.spec, sent.id);
      setSent(null);
      onSent?.();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="spx-send" ref={wrap}>
      {sent ? (
        <>
          <span className="spx-send-sent" role="status">
            {sent.spec === GENERAL_INBOX ? "Sent to the general inbox" : `Sent to the ${sent.name} specialist's inbox`}
          </span>
          <button type="button" className="spx-linkbtn" onClick={() => void onUndo()} disabled={busy}>
            Undo
          </button>
        </>
      ) : (
        <button
          type="button"
          className="spx-send-btn"
          aria-haspopup="menu"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          disabled={busy}
        >
          Send to a specialist
        </button>
      )}
      {open ? (
        <div className="spx-bmenu-pop spx-send-pop" role="menu" aria-label="Send this message to">
          {specialists.map((s) => (
            <button key={s.id} type="button" role="menuitem" className="spx-bmenu-item" onClick={() => void send(s.id, s.name)}>
              <span className="spx-chip-dot" aria-hidden="true" />
              {s.name} specialist
            </button>
          ))}
          <button type="button" role="menuitem" className="spx-bmenu-item" onClick={() => void send(GENERAL_INBOX, "")}>
            None fits: the general inbox
          </button>
        </div>
      ) : null}
      {error ? (
        <span className="spx-chip-error" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}

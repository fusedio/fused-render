// Settings › Memory (PRODUCT §8; mock-up Memory.dc.html): the always-loaded
// entries with their provenance, the topic knowledge, and the inbox — each
// note's kind, basis and source, "In effect now" for what the user said.
import { useEffect, useState } from "react";
import {
  errorText,
  getInbox,
  getMemory,
  type MemoryEntry,
  type Note,
  type SpecialistMemory,
} from "@platform/lib/specialists-api";

const BASIS: Record<string, string> = {
  verified: "Verified",
  "user-said": "You said so",
  guess: "A guess · not yet confirmed",
};

export function basisLabel(basis: string): string {
  return BASIS[basis] || basis || "Unknown basis";
}

export function entryMeta(e: MemoryEntry): string {
  const parts = [basisLabel(e.basis)];
  const seen = Number(e.seen);
  if (seen > 0) parts.push(`used ${seen} ${seen === 1 ? "time" : "times"}`);
  if (e.confirmed) parts.push(`confirmed ${e.confirmed}`);
  return parts.join(" · ");
}

const KIND: Record<string, string> = {
  gap: "Gap",
  correction: "Correction",
  convention: "Convention",
  "suspected-bug": "Suspected bug",
  behaviour: "Behaviour",
  case: "Case",
};

export const NOTE_KIND = KIND;

export function noteSource(n: Note): string {
  const s = n.source || { type: "" };
  const where = s.title ? ` in "${s.title}"` : "";
  const how: Record<string, string> = {
    task: "From a task",
    breakout: "From a breakout",
    main: "From your chat with it",
    reviewer: "From the review of a task",
    user: "You sent it",
    nightly: "From the nightly update",
  };
  const basis = n.basis === "user-said" ? " · you said so" : n.basis === "verified" ? " · verified" : n.basis === "guess" ? " · a guess" : "";
  return `${how[s.type] || "From " + (s.type || "somewhere")}${where}${basis}`;
}

export function MemoryTab({
  specialistId,
  sendToChat,
  api = { getMemory, getInbox },
}: {
  specialistId: string;
  sendToChat: (text: string) => boolean;
  api?: { getMemory: typeof getMemory; getInbox: typeof getInbox };
}) {
  const [mem, setMem] = useState<SpecialistMemory | null>(null);
  const [notes, setNotes] = useState<Note[] | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [error, setError] = useState("");
  const getM = api.getMemory;
  const getI = api.getInbox;

  useEffect(() => {
    let live = true;
    getM(specialistId).then(
      (m) => {
        if (!live) return;
        setMem(m);
        setSel((s) => s ?? m.entries[0]?.id ?? null);
      },
      (e) => live && setError(errorText(e)),
    );
    getI(specialistId).then(
      (r) => live && setNotes(r.notes || []),
      () => live && setNotes([]),
    );
    return () => {
      live = false;
    };
  }, [specialistId, getM, getI]);

  const entry = mem?.entries.find((e) => e.id === sel) ?? null;
  const ask = () => {
    if (!entry) return;
    const msg =
      `I want to change your memory entry [${entry.id}] "${entry.text}". ` +
      "Ask me what should change, then change it and tell me what you changed.";
    if (!sendToChat(msg)) setError("The chat is not ready yet.");
  };

  return (
    <div className="spx-tab spx-memory">
      <div className="spx-memory-col">
        <div className="spx-run-head">
          <h3>Always loaded</h3>
          <span className="spx-muted spx-small">
            Kept small on purpose · {mem ? `${mem.entries.length} ${mem.entries.length === 1 ? "entry" : "entries"}` : "…"}
            {mem ? ` · ${mem.lines}/${mem.limit_lines} lines` : ""}
          </span>
        </div>
        {error ? (
          <div className="spx-error" role="alert">
            {error}
          </div>
        ) : null}
        {mem && !mem.entries.length ? <div className="spx-muted">Nothing in memory yet.</div> : null}
        <div className="spx-entries" role="listbox" aria-label="Memory entries">
          {(mem?.entries ?? []).map((e) => (
            <button
              key={e.id}
              type="button"
              role="option"
              aria-selected={e.id === sel}
              className={"spx-entry" + (e.id === sel ? " is-selected" : "")}
              onClick={() => setSel(e.id)}
            >
              <span className="spx-strong">{e.text || e.raw}</span>
              <span className={"spx-small " + (e.basis === "guess" ? "spx-warn" : "spx-muted")}>{entryMeta(e)}</span>
            </button>
          ))}
        </div>
        <h3>Topic knowledge</h3>
        <div className="spx-muted spx-small">Opened only when the work needs it</div>
        <ul className="spx-knowledge">
          {(mem?.knowledge ?? []).map((k) => (
            <li key={k.path} title={k.path}>
              {k.title}
            </li>
          ))}
          {mem && !mem.knowledge.length ? <li className="spx-muted">None yet.</li> : null}
        </ul>
      </div>
      <div className="spx-memory-col">
        {entry ? (
          <section className="spx-entry-detail" aria-label="Selected entry">
            <div className="spx-eyebrow">Selected entry</div>
            <div className="spx-strong">{entry.text}</div>
            <dl className="spx-grid">
              <dt>Basis</dt>
              <dd>{basisLabel(entry.basis)}</dd>
              <dt>Came from</dt>
              <dd>{entry.from || "—"}</dd>
              <dt>Last confirmed</dt>
              <dd>{entry.confirmed || "—"}</dd>
              <dt>Retires when</dt>
              <dd>{entry.retires || "never"}</dd>
              <dt>Id</dt>
              <dd>{entry.id}</dd>
            </dl>
            <div className="spx-row-gap">
              <button type="button" className="btn btn-secondary" onClick={ask}>
                Ask the specialist to change this
              </button>
            </div>
          </section>
        ) : null}
        <section aria-label="Inbox">
          <div className="spx-run-head">
            <h3>Inbox</h3>
            <span className="spx-muted spx-small">
              {notes === null ? "…" : `${notes.length} ${notes.length === 1 ? "note" : "notes"} waiting for tonight's update`}
            </span>
          </div>
          <div className="spx-notes">
            {(notes ?? []).map((n) => (
              <div key={n.id} className={"spx-note" + (n.in_effect === true ? " is-said" : "")}>
                <div className="spx-note-head">
                  <span className="spx-warn">{KIND[n.kind] || n.kind}</span>
                  <span className={n.in_effect === true ? "spx-tint" : "spx-muted"}>
                    {n.in_effect === true ? "In effect now" : "Waiting"}
                  </span>
                </div>
                <span>{n.claim}</span>
                <span className="spx-muted spx-small">{noteSource(n)}</span>
              </div>
            ))}
            {notes && !notes.length ? <div className="spx-muted spx-small">The inbox is empty.</div> : null}
          </div>
          <p className="spx-muted spx-small">
            Notes are held apart from memory until the nightly update weighs them. Only what you said yourself takes effect
            straight away.
          </p>
        </section>
      </div>
    </div>
  );
}

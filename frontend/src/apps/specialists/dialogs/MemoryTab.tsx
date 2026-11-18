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
import { clip, secondPerson } from "../lib/api-extra";

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

/**
 * An entry's `from` (written by the model or the nightly job: "user main
 * chat", "breakout b-12ab", "nightly 2026-11-18") in plain words, ids dropped.
 */
export function fromLabel(from: string): string {
  const raw = (from || "").trim();
  if (!raw) return "";
  const t = raw.toLowerCase();
  if (/\buser\b/.test(t) && /\bmain\b|\bchat\b/.test(t)) return "You told it in its chat";
  if (/\bmain\b/.test(t) && /\bchat\b/.test(t)) return "Its chat with you";
  if (/\buser\b/.test(t)) return "You told it";
  if (/\bbreakouts?\b/.test(t)) return "A breakout from one of your tasks";
  if (/\breview/.test(t)) return "The review of a task";
  if (/\bnightly\b|\bupdate\b/.test(t)) return "An update";
  if (/\btasks?\b/.test(t)) return "One of your tasks";
  if (/\bseed\b|\bshipped\b/.test(t)) return "It came with the specialist";
  // Unknown words: keep them, without ids, hashes or dates in machine form.
  const words = raw
    .replace(/\b[a-z]{1,2}-[0-9a-f][0-9a-f-]{3,}\b/gi, "")
    .replace(/\b[0-9a-f]{7,}\b/gi, "")
    .replace(/\s+/g, " ")
    .trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "";
}

export function noteSource(n: Note): string {
  const s = n.source || { type: "" };
  const where = s.title ? ` in "${clip(s.title, 60)}"` : "";
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
    // In the user's own words: it shows in the chat as what they said (R1).
    const msg = `I want to change this in your memory: "${entry.text}". Ask me what should change, then change it.`;
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
        {mem && !mem.entries.length ? (
          <div className="spx-empty-note">Nothing in memory yet. Tell it something to remember in its chat.</div>
        ) : null}
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
              <span className="spx-strong">{secondPerson(e.text || e.raw)}</span>
              <span className={"spx-small " + (e.basis === "guess" ? "spx-warn" : "spx-muted")}>{entryMeta(e)}</span>
            </button>
          ))}
        </div>
        <div className="spx-run-head">
          <h3>Topic knowledge</h3>
          <span className="spx-muted spx-small">Opened only when the work needs it</span>
        </div>
        {mem && !mem.knowledge.length ? (
          <div className="spx-empty-note">No topic knowledge yet. The update writes it when a subject needs more than a line.</div>
        ) : (
          <ul className="spx-knowledge">
            {(mem?.knowledge ?? []).map((k) => (
              <li key={k.path}>{k.title}</li>
            ))}
          </ul>
        )}
      </div>
      <div className="spx-memory-col">
        {entry ? (
          <section className="spx-entry-detail" aria-label="Selected entry">
            <div className="spx-eyebrow">Selected entry</div>
            <div className="spx-strong">{secondPerson(entry.text)}</div>
            <dl className="spx-grid">
              <dt>Basis</dt>
              <dd>{basisLabel(entry.basis)}</dd>
              <dt>Came from</dt>
              <dd>{fromLabel(entry.from) || "Not recorded"}</dd>
              <dt>Last confirmed</dt>
              <dd>{entry.confirmed || "Not yet"}</dd>
              <dt>Retires when</dt>
              <dd>{entry.retires ? secondPerson(entry.retires) : "Never"}</dd>
            </dl>
            <details className="spx-details">
              <summary>Details</summary>
              <dl className="spx-grid">
                <dt>Entry id</dt>
                <dd>{entry.id}</dd>
                {entry.from ? (
                  <>
                    <dt>Recorded as</dt>
                    <dd>{entry.from}</dd>
                  </>
                ) : null}
              </dl>
            </details>
            <div className="spx-row-gap">
              <button type="button" className="btn btn-secondary" onClick={ask}>
                Ask the specialist to change this
              </button>
            </div>
          </section>
        ) : null}
        <section aria-label="Inbox" className="spx-inbox">
          <div className="spx-run-head">
            <h3>Inbox</h3>
            <span className="spx-muted spx-small">
              {notes === null
                ? "…"
                : notes.length
                  ? `${notes.length} ${notes.length === 1 ? "note" : "notes"} waiting for the next update`
                  : ""}
            </span>
          </div>
          <div className="spx-notes" tabIndex={notes && notes.length > 2 ? 0 : undefined} aria-label="Notes">
            {(notes ?? []).map((n) => (
              <div key={n.id} className={"spx-note" + (n.in_effect === true ? " is-said" : "")}>
                <div className="spx-note-head">
                  <span className="spx-warn">{KIND[n.kind] || n.kind}</span>
                  <span className={n.in_effect === true ? "spx-tint" : "spx-muted"}>
                    {n.in_effect === true ? "In effect now" : "Waiting"}
                  </span>
                </div>
                <span>{secondPerson(n.claim)}</span>
                <span className="spx-muted spx-small">{noteSource(n)}</span>
              </div>
            ))}
            {notes && !notes.length ? <div className="spx-empty-note">No notes waiting.</div> : null}
          </div>
          <p className="spx-muted spx-small spx-inbox-foot">
            Notes are held apart from memory until the nightly update weighs them. Only what you said yourself takes effect
            straight away.
          </p>
        </section>
      </div>
    </div>
  );
}

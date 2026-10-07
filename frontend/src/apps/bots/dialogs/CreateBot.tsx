// "+ New bot", after the preset chooser: three things and a bot exists. Name, what it should do, model; the effort
// menu sits behind the cog. Everything else (approvals, builds, browser profile, encryption, trusted apps, the
// phone) has a first-run default that is right and lives in Settings once the bot exists. Enter in Name creates;
// Escape, the backdrop and Cancel dismiss (null).
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { Face } from "../components/Face";
import type { Face as FaceT } from "../lib/api";
import { EFFORTS, modelsFor } from "../lib/botform";
import { faceOf } from "../lib/face";
import { newBotInit, type NewBotPick } from "../lib/presets";
import { getState } from "../state/store";
import type { BotDialogValue } from "./actions";
import { pickFace } from "./ask";

export interface CreateBotProps {
  pick: NewBotPick;
  onClose: (v: BotDialogValue | null) => void;
}

export function CreateBot({ pick, onClose }: CreateBotProps) {
  const isSuper = pick.kind === "super";
  const [fresh] = useState(() => newBotInit(pick));
  const [name, setName] = useState(fresh.name || `Bot ${getState().bots.length + 1}`);
  const [model, setModel] = useState(fresh.model || "sonnet");
  const [effort, setEffort] = useState("low");
  const [instructions, setInstructions] = useState(fresh.instructions || "");
  const [face, setFace] = useState<FaceT | null | undefined>(fresh.face);
  const [more, setMore] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null), instrRef = useRef<HTMLTextAreaElement>(null);

  // The avatar subject: the face is hashed from the name the dialog opened with until one is picked.
  const bm = useMemo(() => ({ name: fresh.name, face }), [fresh.name, face]);
  const fit = () => { const t = instrRef.current; if (t) { t.style.height = "0"; t.style.height = t.scrollHeight + 2 + "px"; } };
  useLayoutEffect(() => { nameRef.current?.focus(); nameRef.current?.select(); fit(); }, []);

  const read = (): BotDialogValue => ({
    name: name.trim(), model, effort, instructions, memory: "", approval: "ask", buildAccess: "scoped", encrypt: false, profile: "",
    face: faceOf(bm), imessage: "", imessageEnabled: false, imessageTo: "", preset: fresh.preset || "",
    kind: isSuper ? "super" : "bot", superAccess: "ask", trustedApps: [],
  });
  const ok = () => { if (name.trim()) onClose(read()); };
  const editAvatar = async () => { setFace(await pickFace(bm, (d) => setFace(d))); };

  return (
    <div id="bmodal" className="modal show" role="dialog" aria-modal="true" aria-labelledby="bmtitle"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(null); }}>
      <div className="box">
        <h3 id="bmtitle">{fresh.title}</h3>
        <div className="body">
          <div className={`avwrap${isSuper ? " nopick" : ""}`} id="bmavwrap" title={isSuper ? "Super Bot's avatar is fixed" : "Edit avatar"}
            onClick={isSuper ? undefined : () => { void editAvatar(); }}>
            <span className="av" id="bmface"><Face b={bm} /></span>{isSuper ? null : <span className="hint">Edit avatar</span>}
          </div>
          <label className="field">Name<input id="bmname" ref={nameRef} placeholder="e.g. LinkedIn scout" value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") ok(); if (e.key === "Escape") onClose(null); }} /></label>
          <label className="field">What it should do
            <textarea id="bminstr" ref={instrRef} rows={2} value={instructions} onChange={(e) => { setInstructions(e.target.value); fit(); }}
              placeholder={isSuper ? "Standing rules for every task. Blank keeps Super Bot's own: files in its Inbox folder, say before anything destructive."
                : "Standing rules for every task, e.g. “Browse LinkedIn for me. Answer in bullets. Never like or comment.”"} /></label>
          <label className="field">Model
            <select id="bmmodel" value={model} onChange={(e) => setModel(e.target.value)}>
              {modelsFor(isSuper).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          {more ? (
            <label className="field">Thinking
              <select id="bmeffort" value={effort} onChange={(e) => setEffort(e.target.value)}>
                {EFFORTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              <span className="why">How much it thinks per step. Applies from the next task.</span>
            </label>
          ) : null}
          <p className="muted preset" id="bmpreset">
            {fresh.presetNote ? fresh.presetNote + " " : ""}
            {isSuper ? "" : "It asks you before anything it cannot undo; change that and more under Settings once it exists."}
          </p>
        </div>
        <div className="row">
          <button id="bmmore" className="cog" title="Thinking effort" aria-label="More options" aria-pressed={more} onClick={() => setMore((m) => !m)}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h.1a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></svg>
          </button>
          <span className="spacer" />
          <button id="bmcancel" onClick={() => onClose(null)}>Cancel</button>
          <button id="bmok" className="primary" disabled={!name.trim()} onClick={ok}>{isSuper ? "Make Super Bot" : "Create bot"}</button>
        </div>
      </div>
    </div>
  );
}

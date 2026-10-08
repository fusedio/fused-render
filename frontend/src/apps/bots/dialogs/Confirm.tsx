// #cmodal (OpenBot dialogs.js askConfirm): renders the head of the confirm queue (dialogs/ask.ts). Enter = OK,
// Escape / Cancel / the backdrop = no. The key listener runs in the capture phase and stops propagation so nothing
// underneath (the bot dialog's Enter, the live view's Esc) also reacts. Cancel takes focus on open.
import { useEffect, useRef } from "react";
import { settleConfirm, useConfirm } from "./ask";

export function Confirm() {
  const c = useConfirm();
  const cancelRef = useRef<HTMLButtonElement>(null);
  const userRef = useRef<HTMLInputElement>(null);
  const passRef = useRef<HTMLInputElement>(null);
  const creds = () => ({ user: userRef.current?.value ?? "", pass: passRef.current?.value ?? "" });
  useEffect(() => {
    if (!c) return;
    (c.onAuth ? userRef.current : cancelRef.current)?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); settleConfirm(c.id, false); }
      else if (e.key === "Enter") { e.stopPropagation(); e.preventDefault(); settleConfirm(c.id, true, creds()); }
    };
    document.addEventListener("keydown", key, true);
    return () => document.removeEventListener("keydown", key, true);
  }, [c]);
  return (
    <div id="cmodal" className={`modal${c ? " show" : ""}`} role="alertdialog" aria-modal="true" aria-labelledby="cmtitle"
      onClick={(e) => { if (c && e.target === e.currentTarget) settleConfirm(c.id, false); }}>
      <div className="box">
        <h3 id="cmtitle">{c?.title ?? "Delete?"}</h3>
        <p id="cmtext">{c?.text ?? ""}</p>
        {c?.onAuth ? (
          <div className="authfields">
            <input ref={userRef} id="cmuser" type="text" placeholder="User name" autoComplete="username" autoCapitalize="off" spellCheck={false} />
            <input ref={passRef} id="cmpass" type="password" placeholder="Password" autoComplete="current-password" />
          </div>
        ) : null}
        <div className="row">
          <button id="cmcancel" ref={cancelRef} onClick={() => c && settleConfirm(c.id, false)}>Cancel</button>
          <button id="cmok" className={c && !c.danger ? "primary" : "danger"} onClick={() => c && settleConfirm(c.id, true, creds())}>{c?.okLabel ?? "Delete"}</button>
        </div>
      </div>
    </div>
  );
}

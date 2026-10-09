// In-app replacement for window.confirm (OpenBot dialogs.js askConfirm) as an imperative promise API any module can
// await; Confirm.tsx renders the head of the queue. A second ask while one is up waits its turn instead of orphaning
// the first promise. (The face picker's request slot lived here too until it became a Popover, dialogs/FacePopover.tsx.)
import { useSyncExternalStore } from "react";

export interface Credentials { user: string; pass: string }
/** What the dialog collected besides the button: the auth fields, or the prompt's text. */
export interface ConfirmVals { user?: string; pass?: string; text?: string }
export interface ConfirmReq {
  id: number; title: string; text: string; okLabel: string; danger: boolean;
  /** `fields`: none (a plain confirm), "auth" (user name + password) or "prompt" (one text field, prefilled with `defaultValue`). */
  fields?: "auth" | "prompt"; defaultValue?: string;
  resolve: (ok: boolean, vals?: ConfirmVals) => void;
}

let confirms: ConfirmReq[] = [], seq = 0;
const listeners = new Set<() => void>();
const emit = () => { for (const l of [...listeners]) l(); };
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };

/** Resolves true on the OK button (or Enter), false on Cancel / Escape / the backdrop. danger=false styles OK as a plain primary action. */
export function askConfirm(title: string, text: string, okLabel = "Delete", danger = true): Promise<boolean> {
  return new Promise((resolve) => { confirms = [...confirms, { id: ++seq, title, text, okLabel, danger, resolve }]; emit(); });
}
/** A confirm with user name + password fields (an HTTP auth challenge from the live view). Resolves null on Cancel. */
export function askAuth(title: string, text: string): Promise<Credentials | null> {
  return new Promise((resolve) => {
    confirms = [...confirms, { id: ++seq, title, text, okLabel: "Sign in", danger: false, fields: "auth",
      resolve: (ok, v) => resolve(ok ? { user: v?.user ?? "", pass: v?.pass ?? "" } : null) }]; emit();
  });
}
/** window.prompt's shape: a title, the page's message, one text field. Resolves the text on OK, null on Cancel. */
export function askPrompt(title: string, text: string, defaultValue = ""): Promise<string | null> {
  return new Promise((resolve) => {
    confirms = [...confirms, { id: ++seq, title, text, okLabel: "OK", danger: false, fields: "prompt", defaultValue,
      resolve: (ok, v) => resolve(ok ? v?.text ?? "" : null) }]; emit();
  });
}
/** Settle the confirm on screen (`vals` from the auth / prompt fields). */
export function settleConfirm(id: number, ok: boolean, vals?: ConfirmVals): void {
  const c = confirms.find((x) => x.id === id); if (!c) return;
  confirms = confirms.filter((x) => x.id !== id); emit(); c.resolve(ok, vals);
}
export const useConfirm = (): ConfirmReq | null => useSyncExternalStore(subscribe, () => confirms[0] || null, () => confirms[0] || null);

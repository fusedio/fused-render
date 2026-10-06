// In-app replacement for window.confirm (OpenBot dialogs.js askConfirm) and the face picker's request slot
// (core.js pickFace), as imperative promise APIs any module can await. Confirm.tsx / FacePicker.tsx render them.
// Confirms queue: a second ask while one is up waits its turn instead of orphaning the first promise.
import { useSyncExternalStore } from "react";
import type { Face } from "../lib/api";
import { faceOf, type FaceDraft, type FaceSubject } from "../lib/face";

export interface ConfirmReq { id: number; title: string; text: string; okLabel: string; danger: boolean; resolve: (v: boolean) => void }
export interface FacePickReq { id: number; draft: FaceDraft; onPick?: (f: Face) => void; resolve: (f: FaceDraft) => void }

let confirms: ConfirmReq[] = [], pick: FacePickReq | null = null, seq = 0;
const listeners = new Set<() => void>();
const emit = () => { for (const l of [...listeners]) l(); };
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };

/** Resolves true on the OK button (or Enter), false on Cancel / Escape / the backdrop. danger=false styles OK as a plain primary action. */
export function askConfirm(title: string, text: string, okLabel = "Delete", danger = true): Promise<boolean> {
  return new Promise((resolve) => { confirms = [...confirms, { id: ++seq, title, text, okLabel, danger, resolve }]; emit(); });
}
/** Settle the confirm on screen. */
export function settleConfirm(id: number, v: boolean): void {
  const c = confirms.find((x) => x.id === id); if (!c) return;
  confirms = confirms.filter((x) => x.id !== id); emit(); c.resolve(v);
}
export const useConfirm = (): ConfirmReq | null => useSyncExternalStore(subscribe, () => confirms[0] || null, () => confirms[0] || null);

/** Face picker: every pick repaints and calls onPick; Done, Enter, Escape or the backdrop resolve with the draft. */
export function pickFace(b: FaceSubject, onPick?: (f: Face) => void): Promise<FaceDraft> {
  return new Promise((resolve) => {
    if (pick) pick.resolve({ ...pick.draft });  // one picker at a time
    pick = { id: ++seq, draft: { ...faceOf(b) }, onPick, resolve }; emit();
  });
}
/** A swatch was picked: update the draft and tell the opener. */
export function updatePick(patch: Partial<FaceDraft>): void {
  if (!pick) return;
  pick = { ...pick, draft: { ...pick.draft, ...patch } }; emit();
  pick.onPick?.({ ...pick.draft });
}
export function settlePick(): void {
  const p = pick; if (!p) return;
  pick = null; emit(); p.resolve({ ...p.draft });
}
export const useFacePick = (): FacePickReq | null => useSyncExternalStore(subscribe, () => pick, () => pick);

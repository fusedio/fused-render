// THE +'s NEW TASK CARD OUTLIVES THE PAGE (owner, 2026-11-09: "it doesn't
// behave as window A — on a view change it closes; check Tasks, it doesn't").
//
// The Claude rail is a page's (AppPage, the explorer, a canvas) and comes and
// goes with it; the card it used to hold in its own state went with it. Now the
// card's STATE lives here and the card is DRAWN by one host mounted in the
// shell (`PlusCardHost`, App.tsx, beside TasksPeek), so a hop never remounts
// it: whichever rail is up publishes how to draw the card (its anchor, its
// placement, its doors), and the host draws that. Between one rail going and
// the next arriving (a page's chunk still loading) the card is kept, hidden
// (`plus-card-orphan` on <html>); with no rail back within ORPHAN_MS it goes,
// its draft autosaved as on any close.
//
// With no host mounted (a rail rendered alone, as the suites do) the rail
// draws the card itself, exactly as before.
import { useSyncExternalStore, type ReactNode } from "react";

export const ORPHAN_MS = 4000;

type Render = (card: unknown) => ReactNode;

let card: unknown = null;
/** Mounted rails' renders, newest last: the top one draws. */
let renders: { id: object; fn: Render }[] = [];
let hosts = 0;
let orphanTimer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<() => void>();
let version = 0;

function emit() {
  version++;
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function setOrphan(on: boolean) {
  if (typeof document !== "undefined") document.documentElement.classList.toggle("plus-card-orphan", on);
}

function reconcileOrphan() {
  const orphan = hosts > 0 && card !== null && renders.length === 0;
  setOrphan(orphan);
  if (orphan && !orphanTimer) {
    orphanTimer = setTimeout(() => {
      orphanTimer = null;
      if (renders.length === 0) {
        card = null;
        setOrphan(false);
        emit();
      }
    }, ORPHAN_MS);
  } else if (!orphan && orphanTimer) {
    clearTimeout(orphanTimer);
    orphanTimer = null;
  }
}

export function getPlusCard<C>(): C | null {
  return card as C | null;
}

export function setPlusCard<C>(next: C | null | ((was: C | null) => C | null)): void {
  const value = typeof next === "function" ? (next as (was: C | null) => C | null)(card as C | null) : next;
  if (value === card) return;
  card = value;
  reconcileOrphan();
  emit();
}

/** The card, as React state that every rail and the host share. */
export function usePlusCard<C>(): [C | null, typeof setPlusCard<C>] {
  const value = useSyncExternalStore(subscribe, () => card as C | null, () => card as C | null);
  return [value, setPlusCard<C>];
}

/** A rail says how to draw the card (every render: its closures move). */
export function publishPlusRender(id: object, fn: Render): void {
  const at = renders.findIndex((r) => r.id === id);
  if (at >= 0) renders[at] = { id, fn };
  else renders = [...renders, { id, fn }];
  reconcileOrphan();
  emit();
}

export function withdrawPlusRender(id: object): void {
  const before = renders.length;
  renders = renders.filter((r) => r.id !== id);
  if (renders.length === before) return;
  reconcileOrphan();
  emit();
}

/** The host's mount: while one stands, rails publish instead of drawing. */
export function registerPlusHost(): () => void {
  hosts++;
  emit();
  return () => {
    hosts--;
    reconcileOrphan();
    emit();
  };
}

export function usePlusHosted(): boolean {
  return useSyncExternalStore(subscribe, () => hosts > 0, () => false);
}

/** What the host draws: the top rail's render of the card — or, while no
 *  rail stands, the last one, hidden (header). */
let lastRender: Render | null = null;
export function plusHostElement(): ReactNode {
  if (card === null) return null;
  const top = renders[renders.length - 1]?.fn ?? null;
  if (top) lastRender = top;
  return (top ?? lastRender)?.(card) ?? null;
}

export function usePlusHostVersion(): number {
  return useSyncExternalStore(subscribe, () => version, () => version);
}

export function resetPlusCardStoreForTests(): void {
  card = null;
  renders = [];
  hosts = 0;
  lastRender = null;
  if (orphanTimer) clearTimeout(orphanTimer);
  orphanTimer = null;
  setOrphan(false);
  emit();
}

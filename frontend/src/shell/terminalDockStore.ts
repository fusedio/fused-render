// Whether the status-bar terminal drawer is open — shared between
// `TerminalDock.tsx` (the chip, the only writer) and `TerminalDrawer.tsx`
// (the reader, rendered as a separate sibling in App.tsx's tree; see
// PLAN-status-bar-terminal.md Task 5) without either one importing the
// other.
//
// A MODULE-LEVEL STORE, deliberately NOT `useStatusChip`/`useExclusiveSection`
// (`platform/lib/statusChip.ts`/`platform/lib/exclusiveSection.ts`): those
// give a chip hover-to-preview and "closes when a sibling section opens"
// behaviour, both wrong here — hover-to-preview on a surface you type into
// would open a shell under your pointer, and the drawer reserves its own
// height rather than floating over the other three panels' `.dl-panel`, so
// it has nothing to arbitrate space with (PLAN's Decisions). This is
// therefore its own minimal store, the same shape `apps/ai_models/lib/aiRuntime.ts`
// uses for a single shared value with no context provider — one boolean, one
// setter, subscribed to with `useSyncExternalStore` so a re-render only
// happens when the value actually changes.
import { useSyncExternalStore } from "react";

let open = false;
const listeners = new Set<() => void>();

function set(next: boolean): void {
  if (next === open) return;
  open = next;
  for (const listener of listeners) listener();
}

export function toggleTerminalDock(): void {
  set(!open);
}

export function closeTerminalDock(): void {
  set(false);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): boolean {
  return open;
}

export function useTerminalDockOpen(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot);
}

/** Test-only: reset between test files (bun runs the whole suite in one
 * module registry, and this store is module-level — see the header). */
export function resetTerminalDockForTests(): void {
  open = false;
  listeners.clear();
}

// Whether the first-run wizard is open — one module-level store, so the three
// parties that care share one answer without a context: the wizard itself
// (renders off it), App.tsx (holds the auto-tours while it is up — two
// first-run moments must not fire on top of each other), and the sidebar's
// Help menu ("Setup" reopens it, flags untouched).
import { useSyncExternalStore } from "react";

import type { Config } from "@platform/lib/api";

let open = false;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

export function openOnboarding(): void {
  if (open) return;
  open = true;
  emit();
}

export function closeOnboarding(): void {
  if (!open) return;
  open = false;
  emit();
}

export function isOnboardingOpen(): boolean {
  return open;
}

export function useOnboardingOpen(): boolean {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => open,
    () => false,
  );
}

/** The auto-show rule, from the server's flag: never completed AND never
    dismissed. A backend without the field (older server) shows nothing. */
export function shouldAutoShow(config: Config): boolean {
  const s = config.onboarding;
  if (!s) return false;
  return s.completed_at == null && s.dismissed_at == null;
}

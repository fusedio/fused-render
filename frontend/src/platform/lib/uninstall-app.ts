// "Uninstall…" from anywhere: the app card menu, the sidebar's Projects
// menu and the bots app menu all call `openUninstallApp(app)`, and ONE
// `UninstallAppHost` mounted in the shell renders the dialog. Same shape as
// share-app.ts, for the same reason: a context menu is built as a plain
// function at click time and has no component to hang a dialog on.
import { useEffect, useState } from "react";

export interface UninstallableApp {
  path: string;
  name: string;
}

export interface UninstallRequest {
  app: UninstallableApp;
  /** Bumped per request so the same app twice remounts the dialog. */
  seq: number;
}

let current: UninstallRequest | null = null;
let seq = 0;
const listeners = new Set<(r: UninstallRequest | null) => void>();

function emit() {
  for (const l of listeners) l(current);
}

export function openUninstallApp(app: UninstallableApp): void {
  seq += 1;
  current = { app, seq };
  emit();
}

export function closeUninstallApp(): void {
  if (current === null) return;
  current = null;
  emit();
}

export function useUninstallRequest(): UninstallRequest | null {
  const [req, setReq] = useState(current);
  useEffect(() => {
    listeners.add(setReq);
    setReq(current);
    return () => {
      listeners.delete(setReq);
    };
  }, []);
  return req;
}

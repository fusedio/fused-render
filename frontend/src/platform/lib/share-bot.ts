// Share a file with a bot — the one-request store the ShareBotHost reads
// (shell/ShareBotModal.tsx). The same shape as share-file.ts's open-request
// store, for the same reason: the explorer's menus are plain entry lists that
// cannot own a dialog, so each entry point calls `openShareBot(file)` and the
// one host mounted in the shell renders whichever request is current.
//
// Only the store lives here: the dialog draws bot faces and sends through the
// bots client (apps/bots), which platform may not import.
import { useEffect, useState } from "react";

export interface BotShareFile {
  path: string;
  name: string;
  /** Bytes, when the caller knows (the preview's stat). The dialog refuses an
   *  oversize file up front with it; without it the check runs on Send. */
  size?: number | null;
}

export interface ShareBotRequest {
  file: BotShareFile;
  /** Bumped per request so opening the same file twice remounts the dialog. */
  seq: number;
}

let current: ShareBotRequest | null = null;
let seq = 0;
const listeners = new Set<(r: ShareBotRequest | null) => void>();

function emit() {
  for (const l of listeners) l(current);
}

/** Open the "Share with bot" sheet for `file`. */
export function openShareBot(file: BotShareFile): void {
  seq += 1;
  current = { file, seq };
  emit();
}

export function closeShareBot(): void {
  if (current === null) return;
  current = null;
  emit();
}

export function useShareBotRequest(): ShareBotRequest | null {
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

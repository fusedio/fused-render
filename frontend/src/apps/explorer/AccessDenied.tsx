// The OS refused a read. Shown in place of the raw "Failed to list <path>:
// [Errno 1] Operation not permitted" plate, which named the symptom and
// offered nothing to do about it.
//
// One plain sentence, then the Full Disk Access strip (platform/ui/FdaStrip)
// right where the user is looking — the same strip Home and /apps show, so
// the fix is one click away from the folder that just failed rather than a
// trip back to the front door. The strip gates itself off /api/config's `fda`
// field: absent (non-mac, dev server, inconclusive probe) it renders nothing,
// so the sentence has to stand on its own. The server flips `fda.denied`
// inside the very request that failed, before it answers, so the strip's
// mount-time config fetch already sees the denial — no race to wait out.
//
// Deliberately NOT inside `.status-message.error`: that is the red monospace
// failure plate, and the strip's warning-toned "nothing red" posture would
// inherit its colour and font. A refused read is a setup gap, not an outage.
import { FdaStrip } from "@platform/ui/FdaStrip";

// Whether a failed fs read was REFUSED rather than missing/broken. The read
// routes (list, stat, read) all answer 403 for a PermissionError and nothing
// else on a read path answers 403 (the readonly guard is write-only). The text
// fallback covers an error state that lost its status on the way here: macOS
// TCC denies land as EPERM "Operation not permitted", classic mode bits as
// EACCES "Permission denied".
export function isAccessDenied(err: { status?: number; message?: string }): boolean {
  if (err.status === 403) return true;
  const msg = (err.message ?? "").toLowerCase();
  return msg.includes("permission denied") || msg.includes("operation not permitted");
}

// No file/folder wording: a refused stat can't tell which it was.
export function AccessDenied({ path }: { path: string }) {
  return (
    <div className="access-denied">
      <p className="access-denied-text">
        The system won’t let FusedRender read <code>{path}</code>.
      </p>
      <FdaStrip />
    </div>
  );
}

export default AccessDenied;

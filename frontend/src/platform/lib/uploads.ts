// Reading a mount's async upload queue (D221).
//
// With the VFS cache in "full" mode a save completes as soon as it lands on
// local disk and the upload happens afterwards, so the queue is the only thing
// that knows whether the bytes actually reached the remote. Getting the states
// wrong is silent data loss from the user's point of view — they watched the
// save succeed — so the two decisions the UI makes from it live here as pure
// functions with tests, rather than inline in a component we cannot test.
import type { Mount, MountUploads } from "./api";

// What to tell the user about one mount's queue.
//  none    — nothing to say: either the question doesn't apply (a read-only or
//            unhealthy mount, which the server sends as null) or the queue was
//            read and is genuinely empty.
//  unknown — the read was ATTEMPTED and failed. Distinct from `none` on
//            purpose: files may be stranded and we cannot see them, so this
//            must be shown, never swallowed.
//  failed  — items whose upload already came back unsuccessfully.
//  pending — items still on their way.
export type UploadNotice =
  | { kind: "none" }
  | { kind: "unknown"; reason: string }
  | { kind: "failed"; failed: number; names: string[]; truncated: boolean }
  | { kind: "pending"; pending: number };

export function uploadNotice(uploads: MountUploads | null | undefined): UploadNotice {
  if (!uploads) return { kind: "none" };
  if (uploads.unknown) return { kind: "unknown", reason: uploads.reason };
  const { pending, failed, failed_names } = uploads;
  if (failed > 0) {
    const names = failed_names.filter(Boolean);
    return { kind: "failed", failed, names, truncated: failed > names.length };
  }
  return pending > 0 ? { kind: "pending", pending } : { kind: "none" };
}

// Whether any mount has an upload that can still make progress — the gate on
// the Mounts page's refresh timer.
//
// `pending > failed`, NOT `pending > 0`: rclone re-queues a failed item with a
// doubled delay and never drops it, so a quota-exhausted or permission-denied
// upload keeps `pending` above zero for the life of the mount. Polling on that
// would re-probe every mount every few seconds forever. A NEW failure still
// wakes the page, because an item counts as pending before its first attempt
// returns; once everything left is known-failed, the page goes quiet again.
//
// An `unknown` queue does not schedule polling either: we already show a
// warning for it, and a daemon that cannot answer is the last thing to ask
// repeatedly.
export function hasDrainingUploads(mounts: Pick<Mount, "uploads">[]): boolean {
  return mounts.some((m) => {
    const u = m.uploads;
    return !!u && !u.unknown && u.pending > u.failed;
  });
}

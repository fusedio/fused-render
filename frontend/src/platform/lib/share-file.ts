// Share any file — the client half of fused_render/share_file.py, and the
// one-request store the ShareFileModal host reads. Sibling of share-app.ts,
// generalised from "the .fused app" to "whatever the Fused catalog can
// render" (share-any-file-plan.md's task 6); it reuses share-app.ts's shape
// (withAuthCode, an open-request store, ExportableApp-style path+name slice)
// rather than importing it, because platform/lib modules are meant to stand
// alone the way share-app.ts itself does.
//
// TWO SHARE ACTIONS, NOT A VISIBILITY PICKER: "Share publicly" (mode
// "public") and "Share for 30 minutes" (mode "temporary", team-scoped with a
// session token — see _fused_share_app.py's SESSION_MAX_AGE_S). No third
// mode, and no separate "private" state; a file that is not shared has no
// record at all.
//
// LARGE FILES GO THROUGH THE DETACHED UPLOAD. `publish` below is a thin POST
// — it does not know a file's size and does not decide when to route through
// /upload. The server does that (share_file.py's INLINE_PUBLISH_MAX_BYTES)
// and answers a 409 naming the call to make first; `publishShareFile` folds
// that specific 409 onto `code: "upload_required"` so the modal can drive the
// detached-upload flow without re-parsing the sentence itself.
import { useEffect, useState } from "react";
import { getJson, postJson } from "./api";

export interface ShareableFile {
  path: string;
  name: string;
}

export type ShareMode = "public" | "temporary";

export interface SharedFileRecord {
  file_id: string;
  path: string;
  name: string;
  viewer: string | null;
  url: string | null;
  canvas_id: string | null;
  canvas_name: string | null;
  share_token: string | null;
  slug: string | null;
  remote: string | null;
  workbench_url: string | null;
  mode: ShareMode;
  session_token: string | null;
  session_expires: string | number | null;
  shared_at: number;
  updated_at: number;
  adopted?: boolean;
  /** Computed at read time by share_file.py's `is_expired` — a `temporary`
   *  share whose session has lapsed. Always false for `public`. */
  expired: boolean;
}

export interface ShareFileStatus {
  file_id: string | null;
  can_share: boolean;
  refusal: string | null;
  viewer: string | null;
  cli_found: boolean;
  logged_in: boolean;
  creds_stamp: number | null;
  shared: SharedFileRecord | null;
}

export const getShareFileStatus = (path: string) =>
  getJson<ShareFileStatus>("/api/share/file/status?path=" + encodeURIComponent(path));

export type ShareError = Error & { status?: number; code?: string };

// The same fold share-app.ts's withAuthCode does: 401 (a token that got
// refused) and a 409 whose sentence says "not signed in" both become
// `code: "not_logged_in"`, so the modal branches on one field. Also folds the
// ONE OTHER 409 this router's `/publish` route can send — "call
// /api/share/file/upload first" for a file over the inline cap — onto
// `code: "upload_required"`, so the modal does not have to pattern-match the
// sentence to know it needs to drive the upload flow instead of just erroring.
function withCode<T>(p: Promise<T>): Promise<T> {
  return p.catch((e: ShareError) => {
    if (e.status === 401 || (e.status === 409 && /not signed in/i.test(e.message))) {
      e.code = e.code ?? "not_logged_in";
    } else if (e.status === 409 && /upload first/i.test(e.message)) {
      e.code = e.code ?? "upload_required";
    }
    throw e;
  });
}

export const lookupShareFile = (path: string) =>
  withCode(
    postJson<{ found: boolean; file_id: string | null; shared?: SharedFileRecord }>(
      "/api/share/file/lookup",
      { path },
    ),
  );

export const removeShareFile = (path: string) =>
  withCode(postJson<{ ok: boolean; deleted_canvas: boolean }>("/api/share/file/remove", { path }));

/**
 * Publish (or update) the share in `mode`. `uploadId` is the finished
 * detached-upload job's id (== the file's share id — share_file.py's
 * `file_identity`) for a file that went through /upload first; omitted for
 * one small enough to publish inline.
 */
export async function publishShareFile(
  path: string,
  mode: ShareMode = "public",
  uploadId?: string,
): Promise<SharedFileRecord> {
  const body: { path: string; mode: ShareMode; upload_id?: string } = { path, mode };
  if (uploadId) body.upload_id = uploadId;
  const res = await withCode(
    postJson<{ ok: boolean; shared: SharedFileRecord }>("/api/share/file/publish", body),
  );
  return res.shared;
}

// -- the detached upload (share_file.py's /upload, /upload/status, /upload/cancel) --

export type UploadState = "running" | "done" | "failed" | "cancelled" | "none";

export interface UploadStatus {
  id: string;
  state: UploadState;
  bytes?: number | null;
  elapsed?: number | null;
  remote?: string | null;
  s3_uri?: string | null;
  error?: string;
}

export const startUpload = (path: string) =>
  withCode(postJson<UploadStatus>("/api/share/file/upload", { path }));

export const uploadStatus = (id: string) =>
  getJson<UploadStatus>("/api/share/file/upload/status?id=" + encodeURIComponent(id));

export const cancelUpload = (id: string) =>
  withCode(postJson<UploadStatus>("/api/share/file/upload/cancel", { id }));

// -- the open-request store, mirroring share-app.ts's exactly -----------------
//
// Same reason: the explorer's kebab menu and crumb-bar right-click are plain
// entry lists that cannot own a dialog, so every entry point calls
// `openShareFile(file)` and one `ShareFileHost` mounted in the shell renders
// whichever request is current.

export interface ShareFileRequest {
  file: ShareableFile;
  /** Bumped per request so opening the same file twice remounts the dialog. */
  seq: number;
}

let current: ShareFileRequest | null = null;
let seq = 0;
const listeners = new Set<(r: ShareFileRequest | null) => void>();

function emit() {
  for (const l of listeners) l(current);
}

/** Open the share sheet for `file`. */
export function openShareFile(file: ShareableFile): void {
  seq += 1;
  current = { file, seq };
  emit();
}

export function closeShareFile(): void {
  if (current === null) return;
  current = null;
  emit();
}

export function useShareFileRequest(): ShareFileRequest | null {
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

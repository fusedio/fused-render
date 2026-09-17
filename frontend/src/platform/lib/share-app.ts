// Share an app as a public link — the client half of fused_render/share_app.py,
// plus the one-request store the ShareAppModal host reads.
//
// The store exists because two of the four places a Share entry lives are
// MENUS (the /apps card's right-click menu is a plain function of the AppInfo;
// the explorer's kebab is a list of entries) that cannot own a dialog. So every
// entry — menu item, hover chip, header button — calls `openShareApp(app,
// captureEl)`, and ONE `ShareAppHost` mounted in the shell renders the dialog
// for whichever request is current. Same shape as the export entry: the
// `ExportableApp` slice is all the dialog needs, and `captureEl` is the
// no-flash screenshot source appShot documents (pixels that ARE the app now).
import { useEffect, useState } from "react";
import { getJson, postJson } from "./api";
import type { ExportableApp } from "./appShot";
import { captureAppPreview } from "./appShot";

export interface SharedAppRecord {
  app_id: string;
  path: string;
  name: string;
  /** The public link, `https://udf.ai/<token>/<slug>.html`. Null only for a
   *  canvas adopted by `lookup` that is not public yet — Update fixes it. */
  url: string | null;
  canvas_id: string | null;
  canvas_name: string | null;
  workbench_url: string | null;
  exported_at?: string | null;
  shared_at: number;
  updated_at: number;
  /** Found on the account by name rather than published from here: the
   *  canvas may carry an older file than this folder. */
  adopted?: boolean;
}

export interface ShareStatus {
  app_id: string | null;
  can_share: boolean;
  refusal: string | null;
  cli_found: boolean;
  logged_in: boolean;
  creds_stamp: number | null;
  shared: SharedAppRecord | null;
}

export const getShareStatus = (path: string) =>
  getJson<ShareStatus>("/api/share/status?path=" + encodeURIComponent(path));

export type ShareError = Error & { status?: number; code?: string };

// `postJson` throws an HttpError with the status but drops the body's `code`.
// The dialog branches on one code — `not_logged_in`, which the server sends
// as a 409 (no credentials file) or a 401 (a file whose token was refused) —
// so both are folded onto the code here and the dialog reads one field.
function withAuthCode<T>(p: Promise<T>): Promise<T> {
  return p.catch((e: ShareError) => {
    // 409 is also `busy`; the body code is gone, so the sentence decides.
    if (e.status === 401 || (e.status === 409 && /not signed in/i.test(e.message))) {
      e.code = e.code ?? "not_logged_in";
    }
    throw e;
  });
}

export const lookupShare = (path: string) =>
  withAuthCode(
    postJson<{ found: boolean; app_id: string | null; shared?: SharedAppRecord }>(
      "/api/share/lookup",
      { path },
    ),
  );

export const removeShare = (path: string) =>
  withAuthCode(
    postJson<{ ok: boolean; deleted_canvas: boolean }>("/api/share/remove", { path }),
  );

/**
 * Publish (or update) the share. Multipart like the export POST: the optional
 * screenshot becomes the file's `preview.png`, which the shared landing page
 * shows above the README — so it is worth the capture here even more than for
 * a download. A capture that fails publishes plain, same contract as export.
 */
export async function publishShare(
  app: ExportableApp,
  captureEl?: Element | null,
): Promise<SharedAppRecord> {
  const preview =
    !app.preview_image && app.entry_html
      ? await captureAppPreview(app.entry_html, captureEl)
      : undefined;
  const form = new FormData();
  form.set("path", app.path);
  if (preview) form.set("preview", preview, "preview.png");
  const res = await fetch("/api/share/publish", {
    method: "POST",
    headers: { "X-Fused": "1" },
    body: form,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(
      typeof body?.error === "string" ? body.error : `sharing failed (${res.status})`,
    ) as ShareError;
    err.status = res.status;
    err.code = typeof body?.code === "string" ? body.code : undefined;
    throw err;
  }
  return body.shared as SharedAppRecord;
}

// -- the open-request store ------------------------------------------------------

export interface ShareAppRequest {
  app: ExportableApp;
  captureEl: Element | null;
  /** Bumped per request so opening the same app twice remounts the dialog. */
  seq: number;
}

let current: ShareAppRequest | null = null;
let seq = 0;
const listeners = new Set<(r: ShareAppRequest | null) => void>();

function emit() {
  for (const l of listeners) l(current);
}

/** Open the share dialog for `app`. `captureEl` follows appShot's contract:
 *  an element whose pixels ARE the app right now, or nothing. */
export function openShareApp(app: ExportableApp, captureEl?: Element | null): void {
  seq += 1;
  current = { app, captureEl: captureEl ?? null, seq };
  emit();
}

export function closeShareApp(): void {
  if (current === null) return;
  current = null;
  emit();
}

export function useShareAppRequest(): ShareAppRequest | null {
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

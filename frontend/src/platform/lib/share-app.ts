// Share an app — the client half of fused_render/share_app.py, plus the
// one-request store the ShareAppModal host reads.
//
// ONE entry, two ways out. Every surface that used to offer "Export App
// File" and "Share…" side by side now offers Share alone, and the dialog
// behind it holds both routes as two cards: a public link on the user's Fused
// account, and the `.fused` file download (SPEC §43). The two produce the
// SAME artifact — the link route uploads what the file route saves — so
// they belong on one sheet, the way Figma/Notion put "copy link" and
// "export" behind one Share button.
//
// BEHIND A FLAG (share-app-flag.ts, `app_sharing_enabled`, default off). Every
// surface asks the flag first: ON it calls `openShareApp`; OFF it calls
// `exportAppFileOnly` below — the plain Export / Download the surfaces carried
// before the sheet (PR #1207): save the `.fused` to Downloads, toast the path
// with Reveal / Open. The sheet is the only thing that reaches the link route,
// so with the flag off that route has no entry point at all.
//
// The store exists because two of the four places a Share entry lives are
// MENUS (the /apps card's right-click menu is a plain function of the AppInfo;
// the explorer's kebab is a list of entries) that cannot own a dialog. So every
// entry — menu item, hover chip, header button — calls `openShareApp(app,
// opts)`, and ONE `ShareAppHost` mounted in the shell renders the dialog for
// whichever request is current. The `ExportableApp` slice is all the dialog
// needs.
//
// NO SCREENSHOT. Both routes ship the folder's authored `preview.png` or none
// (the server bakes it in). Until 2026-09-18 a folder without one got a native
// screen shot at share time (appShot.ts's header has the why-not); App
// Doctor's `preview` check is where a missing thumbnail surfaces now.
//
// `opts.file` is the FILE route's target when it differs from `app`: the app
// page and the explorer kebab export "at the selected version" — a resolved
// snapshot's own extracted tree under a version-suffixed name — while the link
// route is LIVE ONLY (the shared canvas is named after the app's id and always
// carries "the app"; publishing an old commit under it would silently downgrade
// every link already sent). A request for a snapshot therefore passes
// `link: false` and the dialog's link card explains itself instead of acting.
import { useEffect, useState } from "react";
import { getJson, postJson, revealPath, saveAppFileToDisk } from "./api";
import { notify } from "./notifications";
import { navigate } from "./router";

// The slice of AppInfo the share routes read — structural, so the app page
// header (which has a folder + entry page but no listing row) can open the
// sheet without inventing a fake AppInfo.
export interface ExportableApp {
  path: string;
  name: string;
}

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
 * Publish (or update) the share. The server reads the folder's own
 * `preview.png` (if any) for the landing page's still above the README.
 */
export async function publishShare(app: ExportableApp): Promise<SharedAppRecord> {
  const form = new FormData();
  form.set("path", app.path);
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

// -- the flag-off action ---------------------------------------------------------

/**
 * The plain export — what every Share surface does while `app_sharing_enabled`
 * is off. Saves `file` as a `.fused` in Downloads (server-side, so the real
 * path comes back) and raises a toast pointing at it: "Reveal folder"
 * (revealPath handles a file path — it selects it inside its parent) and
 * "Open file" (navigate, not navigateToJobPage — that helper's extension
 * allowlist would misclassify a `.fused` path as a directory). Errors come
 * back as an error toast; the promise resolves either way so a caller can
 * clear its busy state without a try/catch of its own.
 */
export async function exportAppFileOnly(file: ExportableApp): Promise<void> {
  try {
    const realPath = await saveAppFileToDisk(file.path, file.name);
    notify({
      title: "Exported " + file.name + " to " + realPath,
      tone: "info",
      action: {
        label: "Reveal folder",
        onClick: () => {
          revealPath(realPath).catch(() => {});
        },
      },
      extraAction: {
        label: "Open file",
        onClick: () => navigate(realPath, { isDir: false }),
      },
    });
  } catch (e) {
    notify({ title: "Could not export " + file.name + ": " + (e as Error).message, tone: "error" });
  }
}

// -- the open-request store ------------------------------------------------------

export interface ShareAppOptions {
  /** The `.fused` download's target when it is not `app` itself (a snapshot's
   *  extracted tree under a version-suffixed name). Defaults to `app`. */
  file?: ExportableApp;
  /** Whether the public-link route applies. False for a snapshot: links
   *  always publish the live app. Defaults to true. */
  link?: boolean;
  /** The version the file route exports ("Live", "v7", a short sha) — named
   *  on the Download button when it is not the live app. */
  versionLabel?: string;
}

export interface ShareAppRequest {
  app: ExportableApp;
  file: ExportableApp;
  link: boolean;
  versionLabel: string | null;
  /** Bumped per request so opening the same app twice remounts the dialog. */
  seq: number;
}

let current: ShareAppRequest | null = null;
let seq = 0;
const listeners = new Set<(r: ShareAppRequest | null) => void>();

function emit() {
  for (const l of listeners) l(current);
}

/** Open the share dialog for `app`. */
export function openShareApp(app: ExportableApp, opts: ShareAppOptions = {}): void {
  seq += 1;
  current = {
    app,
    file: opts.file ?? app,
    link: opts.link ?? true,
    versionLabel: opts.versionLabel ?? null,
    seq,
  };
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

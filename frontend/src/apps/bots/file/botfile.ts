// The `.fusedbot` package client (BUILD_SPEC §8): the install page's read-only probe, its install POST, the icon
// URL, and the upload the Bots page's drop target uses to land a dropped package on disk first (a browser drop
// carries bytes, not a path, and every other entry point — double-click, deeplink, CLI — hands the page a path).
// Same transport as lib/api.ts (`request`: the X-Fused write guard on POSTs, the server's `{error}` sentence as the
// thrown message), so the page's error callout reads exactly what the server said. The upload alone sends the raw
// File as its body (a package can be up to 1 GiB; base64 in a JSON string would hold ~1.33x of it in memory twice),
// so it has its own small fetch with the same guard and error contract.
import { ApiError, request } from "../lib/api";

export type BotFileSourceKind = "bundled" | "marketplace" | "starter" | "github";

export interface BotFileApp {
  folder: string;
  name: string;
  app_id: string | null;
  /** github: `url` is the https://github.com/<owner>/<name> link the install clones, `repo` its owner/name, `commit` the exporter's HEAD (display only). */
  source: { kind: BotFileSourceKind; slug?: string; key?: string; url?: string; repo?: string; commit?: string };
  /** bundled: appfile's install state (new/update/same/…); starter/marketplace/github: "installed" | "missing". */
  state: string;
  /** The probe's default: install it (a bundled app that is new/updated, a reference not installed yet). */
  ticked: boolean;
  /** False when installing it would need a force or would clash; `reason` says why. */
  tickable: boolean;
  reason?: string;
}

export interface BotFileProbe {
  ok: true;
  name: string;
  /** The packaged bot.json (the public half). Only the keys the page reads are typed. */
  public: Record<string, unknown> & {
    model?: string;
    effort?: string;
    instructions?: string;
    approval?: string;
    build_access?: string;
    terminal?: boolean;
    routines?: unknown[];
    face?: unknown;
  };
  skills: { name: string; title: string; trigger: string }[];
  /** Whether the file carries the bot's memory. */
  memory: boolean;
  exported_at: string;
  origin: { bot_id: string; exported_at: string };
  apps: BotFileApp[];
}

export interface BotFileInstallBody {
  apps: string[];
  memory: boolean;
  /** Dedup key: a resend with the same id answers the first result instead of creating a second bot. */
  request_id: string;
  /** A per-row retry: add these apps to the bot the first install made, instead of creating one. */
  bot_id?: string;
}

export interface BotFileAppResult {
  /** The index's spelling of the folder (the probe row's `folder`), never a rewritten disk path. */
  folder: string;
  ok: boolean;
  /** `settings` (with ok false): the app installed but the bot could not be updated; `error` names why. */
  state: "installed" | "settings" | "missing" | "error" | (string & {});
  error?: string;
}

export interface BotFileInstallResult {
  ok: true;
  /** The NEW bot (import never updates an existing one), or the `bot_id` a per-row retry named. */
  bot_id: string;
  apps: BotFileAppResult[];
  /** Things that went sideways without failing the install (a skill that did not save …); the page stays open. */
  warnings?: string[];
}

/** A fresh install request id. randomUUID needs a secure context (a LAN http origin is not one), so fall back to
 *  16 random bytes as hex. */
export function newRequestId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

/** The warnings the install page shows: the first install's plus each row's latest Retry's (deduped). A first
 *  install whose bot update failed (`link_apps`) says `settings: …` AND flips its installed rows to `settings`;
 *  once Retries leave no row in `settings`, that warning is stale and dropped. Only when the first install HAD such
 *  a row (`firstHadSettings`): a `settings: …` from the bot's own settings step (`apply_meta`) flips no row, no
 *  Retry fixes it, and it stays. Every other first-install warning stays too. */
export function visibleWarnings(
  first: string[],
  firstHadSettings: boolean,
  rows: Record<string, BotFileAppResult>,
  rowWarnings: Record<string, string[]>,
): string[] {
  const fixed = firstHadSettings && !Object.values(rows).some((r) => r.state === "settings");
  const kept = fixed ? first.filter((w) => !w.startsWith("settings:")) : first;
  return [...new Set([...kept, ...Object.values(rowWarnings).flat()])];
}

/** The upload's cap, the server's too. */
export const UPLOAD_MAX_BYTES = 1 << 30;

const q = (path: string) => "?path=" + encodeURIComponent(path);

/** What installing would do — nothing is written. A non-package or corrupt file throws the server's sentence. */
export const probe = (path: string) => request<BotFileProbe>("GET", "/api/botfile/install" + q(path), undefined, "botfile-probe");

/** Install the chosen apps, then create the bot. */
export const install = (path: string, body: BotFileInstallBody) =>
  request<BotFileInstallResult>("POST", "/api/botfile/install", { path, ...body }, "botfile-install");

/** The package's icon (svg); 404 when it has none — AppIcon falls back to the initials tile. */
export const iconUrl = (path: string) => "/api/botfile/icon" + q(path);

/** Land a dropped package's bytes on disk (the File itself is the body); answers the path the install page opens. */
export async function upload(file: File): Promise<{ ok: true; path: string }> {
  const r = await fetch("/api/botfile/upload?name=" + encodeURIComponent(file.name), {
    method: "POST",
    headers: { "X-Fused": "1", "Content-Type": "application/octet-stream" },
    body: file,
    cache: "no-store",
  });
  const text = await r.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!r.ok) {
    const err = data && typeof data === "object" ? (data as { error?: unknown }).error : undefined;
    throw new ApiError((err ? String(err) : "") || text.trim() || `${r.status} ${r.statusText}`, r.status);
  }
  return data as { ok: true; path: string };
}

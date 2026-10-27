// The GitHub CLI setup records (fused_render/server/routers/github.py) as the
// Share sheet's GitHub card reads them (docs/CONTRIB_LOOP.md §19.3). The Git
// panel's publish modal (templates/git/template.html `ghTick`) reads the same
// four routes; this is their React client.
//
// THE GET IS THE AUTHORITY, NEVER THE CLICK. A POST only starts a record (or
// refuses with a 409 sentence — `postJson` throws it as the Error's message);
// what happened is read back from the matching GET until it settles. A record
// is single-flight and outlives the page that started it, so a settled record
// from an EARLIER attempt (another sheet, the Git panel) must not paint into
// this one. A card therefore adopts a record at open only while it is still
// running (ghTick's baseline rule); anything else it shows is the record its
// own POST opened and its own poll read back.
import { getJson, postJson } from "@platform/lib/api";

/** `GET /api/github/status` — `github_setup.summary()`. */
export interface GithubStatus {
  /** A runnable `gh` was found. */
  found: boolean;
  path: string | null;
  source: string | null;
  version: string | null;
  signed_in: boolean;
  account: string | null;
  checked_at: number | null;
  stale?: boolean;
}

/** The install and publish records (`github_setup._install_state` /
 *  `_publish_state`). Publish's `detail` is its progress line while running
 *  and the repo URL once done; `error` is gh's own words. */
export interface SetupRecord {
  state: "idle" | "running" | "done" | "error";
  detail: string;
  error: string | null;
  started_at: number | null;
  finished_at: number | null;
  /** Publish only: the repo root it publishes. */
  root?: string;
}

/** The sign-in record (`github_login._record`) — NOT shaped like the other
 *  two: `in_flight` instead of `state`, and no `signed_in` (that is the
 *  status read's answer). `code` is the one-time device code while open. */
export interface LoginRecord {
  in_flight: boolean;
  started_at: number | null;
  error: string | null;
  code: string | null;
  url: string | null;
  canceled?: boolean;
}

export const getGithubStatus = () => getJson<GithubStatus>("/api/github/status");
export const refreshGithubStatus = () => postJson<GithubStatus>("/api/github/status/refresh", {});
export const startGithubInstall = () => postJson<SetupRecord>("/api/github/install", {});
export const getGithubInstall = () => getJson<SetupRecord>("/api/github/install");
export const startGithubLogin = () => postJson<LoginRecord>("/api/github/login", {});
export const getGithubLogin = () => getJson<LoginRecord>("/api/github/login");
export const cancelGithubLogin = () => postJson<LoginRecord>("/api/github/login/cancel", {});
export const getGithubPublish = () => getJson<SetupRecord>("/api/github/publish");

export const GITHUB_POLL_MS = 1500;

/** Running or settled, for either record shape. */
export function recordRunning(rec: SetupRecord | LoginRecord | null | undefined): boolean {
  if (!rec) return false;
  return "in_flight" in rec ? rec.in_flight : rec.state === "running";
}

/** Poll `get` every GITHUB_POLL_MS until the record settles; `onTick` sees
 *  every running read (the progress sentence). A transient read failure is
 *  retried, three in a row give up with the last good record. Resolves
 *  `null` when `isGone()` turns true (the card unmounted). */
export async function pollRecord<R extends SetupRecord | LoginRecord>(
  get: () => Promise<R>,
  opts: { isGone: () => boolean; onTick?: (rec: R) => void; intervalMs?: number },
): Promise<R | null> {
  const every = opts.intervalMs ?? GITHUB_POLL_MS;
  let last: R | null = null;
  let misses = 0;
  for (;;) {
    await new Promise((resolve) => window.setTimeout(resolve, every));
    if (opts.isGone()) return null;
    try {
      last = await get();
      misses = 0;
    } catch {
      if (++misses >= 3) return last;
      continue;
    }
    if (opts.isGone()) return null;
    if (!recordRunning(last)) return last;
    opts.onTick?.(last);
  }
}

/** A folder name as a GitHub repository name: lowercase, anything outside
 *  `[a-z0-9._-]` becomes a dash, runs collapsed, no leading/trailing dash or
 *  dot (the server refuses a leading dash — `_validate_repo_name`). */
export function slugRepoName(name: string): string {
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^[-.]+|[-.]+$/g, "");
  return slug || "my-app";
}

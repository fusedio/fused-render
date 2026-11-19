// Remote (S3) locations: typed wrappers over /api/remote/* and
// /api/remote-index/rank, plus the address helpers the explorer's location
// plumbing shares. An `s3://bucket/key` string is an explorer LOCATION (the
// thing the address bar, tab title and history hold), never a filesystem
// path — no local route (/api/fs/*, listing, index, git) may receive one.
// Opening a remote file downloads it into the server's cache; the viewer then
// renders `RemoteStat.path`, the cached local copy.
//
// Same transport as every other wrapper (`getJson`/`postJson` in ./api), so the
// ambient headers and the X-Fused write guard come for free. A failure is
// rethrown as a `RemoteApiError` carrying the server's `code` — the view page
// picks its message by code, not by parsing text.
import { getJson, postJson } from "./api";
import type { HttpError, TemplateEntry } from "./api";
import { isRemoteLocation } from "./path-url";

/** The server's size threshold above which an uncached file asks first
 *  (`remote_fetch.WARN_BYTES`). `RemoteStat.warn_bytes` is authoritative;
 *  this is the fallback for a stat that predates the field. */
export const REMOTE_WARN_BYTES = 100 * 1024 * 1024;

type RemoteErrorCode =
  | "disabled"
  | "bad_url"
  | "not_found"
  | "not_public"
  | "network"
  | "unsupported_bucket"
  | "index_not_ready"
  | "no_space"
  | "unknown_download"
  | "internal";

const KNOWN_CODES: ReadonlySet<string> = new Set([
  "disabled",
  "bad_url",
  "not_found",
  "not_public",
  "network",
  "unsupported_bucket",
  "index_not_ready",
  "no_space",
  "unknown_download",
  "internal",
]);

/** A remote route's refusal (or a request that never reached the server, as
 *  `network`). `code` is the server's `code` field when it sent one. */
export class RemoteApiError extends Error {
  readonly code: RemoteErrorCode;
  readonly status: number | null;
  constructor(code: RemoteErrorCode, message: string, status: number | null = null) {
    super(message);
    this.name = "RemoteApiError";
    this.code = code;
    this.status = status;
  }
}

function codeForStatus(status: number): RemoteErrorCode {
  if (status === 400) return "bad_url";
  if (status === 404) return "not_found";
  if (status === 429 || status === 502 || status === 503 || status === 504) return "network";
  return "internal";
}

// Folds whatever `getJson`/`postJson` threw into a RemoteApiError. An
// HttpError carries the parsed body (`{error, code}`); a non-JSON body (a route
// this server does not have yet answering an HTML 404) makes `res.json()`
// reject with a SyntaxError, which becomes `internal`; only a fetch that never
// connected becomes `network`. An abort is rethrown untouched so callers can
// ignore it.
function toRemoteError(err: unknown): unknown {
  if (err instanceof RemoteApiError) return err;
  if (err instanceof Error && err.name === "AbortError") return err;
  const http = err as HttpError | null;
  if (http && typeof http === "object" && typeof http.status === "number") {
    const body = http.body as { code?: unknown } | null | undefined;
    const raw = body && typeof body.code === "string" ? body.code : "";
    const code = KNOWN_CODES.has(raw) ? (raw as RemoteErrorCode) : codeForStatus(http.status);
    return new RemoteApiError(code, http.message || `HTTP ${http.status}`, http.status);
  }
  // The server answered, but not with JSON (a proxy's HTML error page, a route
  // this server lacks): the request reached something, so this is not a
  // connection problem.
  if (err instanceof SyntaxError) return new RemoteApiError("internal", "Unexpected answer from the app", null);
  const message = err instanceof Error && err.message ? err.message : "The request did not reach the server";
  return new RemoteApiError("network", message, null);
}

async function wrap<T>(p: Promise<T>): Promise<T> {
  try {
    return await p;
  } catch (err) {
    throw toRemoteError(err);
  }
}

// -- Address helpers ---------------------------------------------------------

// `isRemoteLocation` lives in ./path-url (dependency-free, so the URL codec can
// use it without importing this module's transport); re-exported here as part
// of the remote surface.
export { isRemoteLocation };

const BUCKET_RE = /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/;
// The contract's three HTTPS spellings: `s3.<region>.amazonaws.com/<bucket>/…`,
// `<bucket>.s3.amazonaws.com/…` and `<bucket>.s3.<region>.amazonaws.com/…`.
const PATH_STYLE_HOST_RE = /^s3\.[a-z0-9-]+\.amazonaws\.com$/;
const VHOST_RE = /^(.+)\.s3(?:\.[a-z0-9-]+)?\.amazonaws\.com$/;

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/**
 * Canonicalises typed or pasted text to `s3://<bucket>/<key>`, or null when it
 * is not an S3 address. Accepts the canonical form (a bare `s3://bucket` gains
 * its root `/`) and the contract's three HTTPS spellings. A key's trailing `/`
 * (a prefix) is kept. Does not decide file vs folder — the server's stat does.
 */
export function canonicalRemoteUrl(input: string): string | null {
  // Leading whitespace only, for an s3:// address: a key may legally END in a
  // space, and trimming it would make that object unaddressable. A pasted
  // HTTPS link has its key percent-encoded, so trimming both ends is safe.
  let text = input.replace(/^\s+/, "");
  if (!text) return null;
  if (/^s3:\/\//i.test(text)) {
    const rest = text.slice(5);
    const slash = rest.indexOf("/");
    const bucket = (slash < 0 ? rest : rest.slice(0, slash)).toLowerCase();
    const key = slash < 0 ? "" : rest.slice(slash + 1);
    return BUCKET_RE.test(bucket) ? `s3://${bucket}/${key}` : null;
  }
  text = text.trim();
  if (!/^https:\/\//i.test(text)) return null;
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  const host = url.hostname.toLowerCase();
  // `pathname` is percent-encoded; S3 keys are not.
  const path = safeDecode(url.pathname.replace(/^\/+/, ""));
  let bucket: string;
  let key: string;
  if (PATH_STYLE_HOST_RE.test(host)) {
    const slash = path.indexOf("/");
    bucket = slash < 0 ? path : path.slice(0, slash);
    key = slash < 0 ? "" : path.slice(slash + 1);
  } else {
    const m = VHOST_RE.exec(host);
    if (!m) return null;
    bucket = m[1];
    key = path;
  }
  bucket = bucket.toLowerCase();
  return BUCKET_RE.test(bucket) ? `s3://${bucket}/${key}` : null;
}

/** Splits a canonical `s3://bucket/key` into its parts. Null if not remote. */
export function parseRemoteLocation(loc: string): { bucket: string; key: string } | null {
  if (!isRemoteLocation(loc)) return null;
  const rest = loc.slice(5);
  const slash = rest.indexOf("/");
  return slash < 0 ? { bucket: rest, key: "" } : { bucket: rest.slice(0, slash), key: rest.slice(slash + 1) };
}

// -- Wire types --------------------------------------------------------------

export interface RemoteStat {
  url: string;
  bucket: string;
  key: string;
  name: string;
  kind: "file" | "dir";
  size: number | null;
  mtime: number | null;
  etag: string | null;
  cached: boolean;
  /** The cached local copy — only when `cached`. */
  path: string | null;
  warn_bytes: number;
  /** The modes the object opens in before it is downloaded (D1492): the
   *  registry's list for its name, without native or non-streaming gated modes. `stream`
   *  marks a template that reads the `s3://` address itself (its
   *  pyproject.toml `[tool.fused-render.template] stream`); the others open
   *  on a downloaded copy. Absent from an older server; empty for a folder. */
  templates?: RemoteTemplateEntry[];
}

export interface RemoteTemplateEntry extends TemplateEntry {
  stream: boolean;
}

/** One row of a folder listing. A folder's `size` and `mtime` are its whole
 *  subtree's (bytes, newest file); null from a server older than that. */
export interface RemoteEntry {
  url: string;
  name: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
}

interface RemoteListResult {
  url: string;
  entries: RemoteEntry[];
  cursor: string | null;
}

interface RemoteFetchStart {
  id: string;
  state: "running" | "done";
}

export interface RemoteFetchState {
  state: "running" | "done" | "error" | "cancelled";
  done: number;
  total: number | null;
  path: string | null;
  error: string | null;
  code: RemoteErrorCode | null;
}

export interface RemoteRankHit {
  url: string;
  /** A folder hit's `key` and `url` end in "/"; its size and mtime are null. */
  key: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
}

export interface RemoteRankResult {
  ready: boolean;
  hits: RemoteRankHit[];
  total: number;
  truncated: boolean;
}

// -- Routes ------------------------------------------------------------------

export function remoteStat(url: string, opts: { signal?: AbortSignal } = {}): Promise<RemoteStat> {
  return wrap(getJson<RemoteStat>("/api/remote/stat?url=" + encodeURIComponent(url), { signal: opts.signal }));
}

export function remoteList(
  url: string,
  opts: { cursor?: string | null; signal?: AbortSignal } = {},
): Promise<RemoteListResult> {
  const params = new URLSearchParams({ url });
  if (opts.cursor) params.set("cursor", opts.cursor);
  return wrap(getJson<RemoteListResult>("/api/remote/list?" + params.toString(), { signal: opts.signal }));
}

/** Starts (or joins) the download of one object into the cache. */
export function remoteFetch(url: string): Promise<RemoteFetchStart> {
  return wrap(postJson<RemoteFetchStart>("/api/remote/fetch", { url }));
}

export function remoteFetchStatus(id: string): Promise<RemoteFetchState> {
  return wrap(getJson<RemoteFetchState>("/api/remote/fetch/status?id=" + encodeURIComponent(id)));
}

export function remoteFetchCancel(id: string): Promise<{ ok: boolean }> {
  return wrap(postJson<{ ok: boolean }>("/api/remote/fetch/cancel", { id }));
}

/** Ranked search over the `fused-asset` index. Flag off or no store answers
 *  200 `{ready:false, hits:[]}`, not an error. `under` (an `s3://` folder
 *  address) scopes it to that folder's subtree — the S3 folder view's own
 *  search box; hit keys stay whole bucket keys. */
export function remoteRank(
  query: string,
  opts: { signal?: AbortSignal; limit?: number; ranked?: boolean; under?: string } = {},
): Promise<RemoteRankResult> {
  const params = new URLSearchParams({ q: query });
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.ranked !== undefined) params.set("ranked", String(opts.ranked));
  if (opts.under) params.set("under", opts.under);
  return wrap(getJson<RemoteRankResult>("/api/remote-index/rank?" + params.toString(), { signal: opts.signal }));
}

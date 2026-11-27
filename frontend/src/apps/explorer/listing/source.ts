// Where the folder view's data comes from: the local routes for a filesystem
// path, the bucket's index for an `s3://` location.
//
// The explorer's folder view (Listing.tsx and its hooks) is the same
// component over both: an S3 folder lists, searches, completes and resolves
// typed addresses exactly the way a local one does, so the hooks call these
// instead of `@platform/lib/api` directly. A local path goes to the very
// same function it always did; an `s3://` one goes to platform/lib/remote,
// answered in the local route's shape, so nothing downstream branches on
// where a row came from. No local route ever receives an `s3://` string
// (platform/lib/remote's header).
import {
  indexRank,
  listDir,
  prefetchListDir,
  requestFolderScan,
  statPath,
  type FolderScanRequest,
  type IndexRankResult,
  type ListResult,
} from "@platform/lib/api";
import { isRemoteLocation, parseRemoteLocation, remoteList, remoteRank, remoteStat } from "@platform/lib/remote";

/** The address a folder answers to in the remote routes: always with its
 *  trailing "/" (an explorer location never carries one). */
function asPrefix(loc: string): string {
  return loc.endsWith("/") ? loc : loc + "/";
}

/** The bucket key of a folder location: `s3://b/a/b` → `a/b/`, the root → "". */
function prefixKey(loc: string): string {
  return parseRemoteLocation(asPrefix(loc))?.key ?? "";
}

/** One page of a folder. A remote page carries a cursor while more follow
 *  — the listing's own Load more (useDirListing). */
export function listFolder(path: string, cursor?: string | null): Promise<ListResult> {
  if (!isRemoteLocation(path)) return listDir(path, cursor);
  return remoteList(asPrefix(path), { cursor }).then((r) => ({
    path,
    entries: r.entries.map((e) => ({ name: e.name, is_dir: e.is_dir, size: e.size, mtime: e.mtime })),
    truncated: r.cursor !== null,
    cursor: r.cursor,
  }));
}

/** `prefetchListDir` for a local path (the loading scaffold's dedupe); a
 *  remote folder is asked directly. */
export function prefetchFolder(path: string): Promise<ListResult> {
  return isRemoteLocation(path) ? listFolder(path) : prefetchListDir(path);
}

/** Whether `path` names something, and whether it is a folder — the typed-
 *  address check (useTypedPathAddress). */
export function statLocation(path: string, signal?: AbortSignal): Promise<{ path: string; is_dir: boolean }> {
  if (!isRemoteLocation(path)) return statPath(path, signal);
  return remoteStat(path, { signal }).then((st) => ({
    // A folder's address without its trailing "/", as every explorer
    // location is spelled (path-url.ts rootedFsPath).
    path: st.kind === "dir" && st.key ? st.url.replace(/\/+$/, "") : st.url,
    is_dir: st.kind === "dir",
  }));
}

/** The in-folder ranked search (useListingSearch). An S3 folder asks the
 *  bucket index scoped to it (`under`), which runs the local search's own
 *  query resolution server-side; keys come back relative to the folder, as
 *  `rel`. An index still loading answers `scanning`, which the box polls
 *  through exactly as it does a local scan (listing/index-source). */
export function rankFolder(
  path: string,
  query: string,
  opts: { signal?: AbortSignal; limit?: number; ranked?: boolean } = {},
): Promise<IndexRankResult> {
  if (!isRemoteLocation(path)) return indexRank(path, query, opts);
  const under = asPrefix(path);
  const key = prefixKey(path);
  return remoteRank(query, { ...opts, under }).then((res) => {
    const r = res as typeof res & { mode?: "substring" | "glob"; pattern?: string };
    return {
      covered: res.ready,
      reason: res.ready ? "" : "scanning",
      hits: res.hits
        .filter((h) => h.key.startsWith(key) && h.key.length > key.length)
        .map((h) => ({
          rel: h.key.slice(key.length).replace(/\/+$/, ""),
          is_dir: h.is_dir,
          size: h.size,
          mtime: h.mtime,
        })),
      truncated: res.truncated,
      total: res.total,
      base: path,
      mode: r.mode ?? "substring",
      pattern: r.pattern ?? query,
    };
  });
}

/** Asking the local index to scan a folder. The bucket index is not built
 *  by scanning, so an S3 folder never asks — answered as a refusal, which
 *  every caller already treats as "nothing started". */
export function requestScan(path: string): Promise<FolderScanRequest> {
  if (!isRemoteLocation(path)) return requestFolderScan(path);
  return Promise.resolve({ started: false, why: "refused", run_id: null, root: path });
}

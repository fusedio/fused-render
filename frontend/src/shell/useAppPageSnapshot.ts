// The app page's own `_snapshot` resolution — extracted out of AppPage.tsx's
// own body into a small, plain hook, mirroring the explorer's own
// `useSnapshotForFolder.ts` (apps/explorer/listing/): both exist so the
// resolve/gate behavior can be driven through a hook-level render harness
// (react-test-renderer, no DOM — see AppPage.test.tsx) instead of only
// through the whole page, which has no render-test precedent in this
// codebase and pulls in a document-dependent effect (the keyboard nav
// listener), base-ui's Tabs, and the Tasks page's own subtree.
//
// NOT a shared module singleton, deliberately: this hook's return value
// belongs to exactly ONE page instance for exactly one `dir` at a time. A
// singleton is what let two apps in one repo sharing a sha leak one view's
// resolution into another view's caller in the explorer's own earlier
// snapshot work (code review finding 1, round 2, this branch) — the fix
// there was "rewrite against your own resolution, never the singleton"; here
// there is no singleton to begin with, so every rewrite already is one.
import { useEffect, useState } from "react";
import { getGitSnapshot } from "@platform/lib/api";
import { replaceSearch } from "@platform/lib/router";
import { carries, isSha, type ResolvedSnapshot } from "@platform/lib/snapshot-param";
import { SNAPSHOT_PARAM } from "./AppVersionPicker";

/** `dir`'s own resolution of the URL's `_snapshot` sha, re-derived whenever
 *  the URL or `dir` changes. `urlVersion` is the caller's own re-run signal
 *  (AppPage's `useUrlVersion()`) — this hook holds no subscription of its
 *  own, the same `urlVersion`-as-plain-number contract
 *  `useSnapshotForFolder` takes. Returns `null` for "Live" AND for "still
 *  resolving" alike — a caller that needs to tell those apart has no reason
 *  to here (every rewrite is a no-op against `null` either way). */
export function useAppPageSnapshot(
  dir: string,
  urlVersion: number,
): ResolvedSnapshot | null {
  const [resolvedSnapshot, setResolvedSnapshot] =
    useState<ResolvedSnapshot | null>(null);
  useEffect(() => {
    const raw = new URLSearchParams(location.search).get(SNAPSHOT_PARAM);
    if (!isSha(raw)) {
      // "Live": whatever `resolvedSnapshot` happened to hold from a previous
      // `dir`/sha is stale the moment there is no sha on the URL to justify
      // it. Guarded on `prev` so a page that was already live does not churn
      // a fresh object identity into state on every unrelated URL write this
      // effect also wakes for.
      setResolvedSnapshot((prev) => (prev === null ? prev : null));
      return;
    }
    // Already resolved THIS sha against an app folder that actually ENCLOSES
    // this page's own `dir`: nothing to do. Both halves matter (code review
    // finding 3, round 2) — a resolution for the right sha but a DIFFERENT
    // (stale, previous-`dir`) app folder must still be treated as
    // unresolved, or a `dir` switch that happens to land on the same sha as
    // the PREVIOUS app folder's own selection would keep rewriting every
    // read against that previous app forever.
    if (
      resolvedSnapshot &&
      resolvedSnapshot.sha === raw &&
      carries(resolvedSnapshot.app_dir, dir)
    ) {
      return;
    }
    let alive = true;
    getGitSnapshot(dir, raw)
      .then((r) => {
        if (!alive) return;
        setResolvedSnapshot({ sha: raw, dir: r.dir, app_dir: r.app_dir });
      })
      .catch((err: unknown) => {
        if (!alive) return;
        // Only a CONFIRMED 404 (git_snapshot.py's own status choice: no app
        // folder encloses this path, or the sha/entry did not exist at that
        // revision) is grounds to give up and fall back to Live. A transient
        // failure (a dropped connection, a 500) must not read identically to
        // "there is genuinely no snapshot" — everything stays on whatever it
        // last showed rather than silently going live out from under the
        // still-selected sha sitting on the URL.
        const status = (err as { status?: number } | null | undefined)?.status;
        if (status !== 404) return;
        setResolvedSnapshot(null);
        const params = new URLSearchParams(location.search);
        params.delete(SNAPSHOT_PARAM);
        const q = params.toString();
        replaceSearch(location.pathname + (q ? "?" + q : ""));
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- urlVersion is a
    // re-run signal (a fresh `_snapshot` on the URL), not a value read here
  }, [dir, urlVersion]);
  return resolvedSnapshot;
}

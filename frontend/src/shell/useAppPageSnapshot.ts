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
//
// RETURNS a small object, not a bare `ResolvedSnapshot | null` (code review
// finding 4, app page version dropdown pass): the previous shape returned
// `null` for "live" AND for "still resolving" alike, on the premise that "a
// caller that needs to tell those apart has no reason to here (every rewrite
// is a no-op against null either way)". That premise was wrong — a caller
// that only rewrites reads is safe either way, but AppPage.tsx's Overview
// frame does not merely rewrite a read: it decides WHETHER TO MOUNT A FRAME
// AT ALL, and on first paint of a `/apps/<dir>?_snapshot=<sha>` URL a `null`
// read as "live" mounted and RAN the live app before the resolve landed and
// swapped the src out from under it — side effects, a daemon start, a D301
// open record, all against the wrong era. `pending` makes that window an
// honest third state a caller can refuse to render against, the same
// distinction Preview.tsx's own `snapshotPending`/`srcFor` already draw for
// the explorer (`if (snapshotPending) return null`).
import { useEffect, useState } from "react";
import { getGitSnapshot } from "@platform/lib/api";
import { replaceSearch } from "@platform/lib/router";
import { carries, isSha, type ResolvedSnapshot } from "@platform/lib/snapshot-param";
import { SNAPSHOT_PARAM } from "./AppVersionPicker";

export interface AppPageSnapshotState {
  /** The URL's raw `_snapshot` claim right now, or null if there is none.
   *  Kept in sync with the URL regardless of resolve outcome (mirroring
   *  Preview.tsx's own `snapshotSha`) — forward this onto every content frame
   *  alongside `snap` (`platform/lib/snapshot-param.ts::snapshotFrameSrc`
   *  takes both): the runtime reads `_snapshot` off its OWN frame's query
   *  (static/runtime.js), so a param that rides only on the shell's address
   *  bar is invisible inside a frame. */
  sha: string | null;
  /** THIS page's own resolution of `sha` against `dir`'s app folder — null
   *  while `sha` is null (live) AND while `sha` is set but not yet (or not
   *  successfully) resolved (`pending` below distinguishes the two). Rewrite
   *  every read against this, never a shared singleton. */
  snap: ResolvedSnapshot | null;
  /** true exactly when `sha` names something this hook has not yet resolved
   *  into a `snap` that actually encloses `dir` — see this interface's own
   *  header comment for why a caller must treat this differently from
   *  "live" rather than folding it into `snap === null`. */
  pending: boolean;
  /** true when the most recent resolve attempt for the URL's CURRENT `sha`
   *  failed with something other than a confirmed 404 (a dropped connection,
   *  a 500) — code review finding 1, second round. `pending` alone cannot
   *  tell a caller this: it stays true for the entire time `snap` is
   *  unresolved, which is also true of the ordinary in-flight case a
   *  skeleton is the right thing to show for. Unlike a 404 (which this hook
   *  resolves on its own by falling back to Live), nothing about a transient
   *  failure resolves itself — the request simply never gets retried (the
   *  effect only re-runs on `dir`/`urlVersion`), so a caller must surface
   *  this as an error the user can act on rather than leave an indefinite
   *  skeleton up forever. Reset to false the moment `sha` changes, `sha`
   *  goes live, a later attempt succeeds, or the URL lands back on a sha
   *  this hook had already resolved before the error (code review finding 1,
   *  third round — the skip-check that short-circuits an already-resolved
   *  sha must not leave a stale error from a DIFFERENT sha's failure
   *  standing). */
  error: boolean;
  /** Ask the hook to re-attempt resolving the URL's current `sha`. A no-op
   *  while `sha` is null (nothing to retry). This is the user's actual
   *  escape from `error` besides picking a different commit in the version
   *  picker — which nothing about the pending skeleton alone told them they
   *  needed to do. */
  retry: () => void;
}

/** `dir`'s own resolution of the URL's `_snapshot` sha, re-derived whenever
 *  the URL or `dir` changes. `urlVersion` is the caller's own re-run signal
 *  (AppPage's `useUrlVersion()`) — this hook holds no subscription of its
 *  own, the same `urlVersion`-as-plain-number contract `useSnapshotForFolder`
 *  takes. */
export function useAppPageSnapshot(
  dir: string,
  urlVersion: number,
): AppPageSnapshotState {
  const [resolvedSnapshot, setResolvedSnapshot] =
    useState<ResolvedSnapshot | null>(null);
  const [error, setError] = useState(false);
  // Bumped by `retry()` — the effect's own extra re-run signal for "try the
  // SAME sha again", since neither `dir` nor `urlVersion` changes just
  // because a fetch failed transiently.
  const [retryToken, setRetryToken] = useState(0);
  // The URL's raw claim, read fresh on every render rather than held as its
  // own piece of state: `urlVersion` (bumped on every `fused:urlchange`,
  // including this hook's own `replaceSearch` calls below and a sibling
  // writer's) is what guarantees a render happens whenever this could have
  // changed — the same posture AppPage.tsx's own `tab` (`appPageTabFromSearch
  // (location.search)`) already takes. Read ONCE per render and closed over
  // by the effect below, rather than re-read a second time inside it, so the
  // two agree on the same value for the length of one render pass.
  const raw = new URLSearchParams(location.search).get(SNAPSHOT_PARAM);
  const sha = isSha(raw) ? raw : null;

  useEffect(() => {
    if (!sha) {
      // "Live": whatever `resolvedSnapshot` happened to hold from a previous
      // `dir`/sha is stale the moment there is no sha on the URL to justify
      // it. Guarded on `prev` so a page that was already live does not churn
      // a fresh object identity into state on every unrelated URL write this
      // effect also wakes for.
      setResolvedSnapshot((prev) => (prev === null ? prev : null));
      setError((prev) => (prev ? false : prev));
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
      resolvedSnapshot.sha === sha &&
      carries(resolvedSnapshot.app_dir, dir)
    ) {
      // A stale `error` from a DIFFERENT sha selected in between (picked,
      // failed non-404, then the user picked THIS already-resolved sha
      // again) must not keep painting the tab as errored — this sha is
      // genuinely resolved and usable. Guarded so a render that already had
      // no error does not churn a fresh boolean into state for no reason
      // (same idiom as the "live" branch above).
      setError((prev) => (prev ? false : prev));
      return;
    }
    let alive = true;
    // A fresh attempt (a new sha, or a re-run this hook's own `retry()`
    // triggered) starts clean — a stale `error` from a PREVIOUS sha's
    // failure must not keep painting an error banner over a request that
    // has not even settled yet.
    setError(false);
    getGitSnapshot(dir, sha)
      .then((r) => {
        if (!alive) return;
        // `entry` (finding 3): the snapshot's OWN entry page, resolved by
        // the server against the EXTRACTED tree — not the live entry
        // `getAppEntry(dir)` reports, which names whatever HEAD's app_entry
        // rule picked. Carried through so AppPage.tsx's Overview can render
        // THAT commit's entry even when it was renamed since (see
        // `ResolvedSnapshot.entry`'s own comment on why a directory-prefix
        // rewrite of the live entry could never have produced it).
        setResolvedSnapshot({ sha, dir: r.dir, app_dir: r.app_dir, entry: r.entry });
      })
      .catch((err: unknown) => {
        if (!alive) return;
        // Only a CONFIRMED 404 (git_snapshot.py's own status choice: no app
        // folder encloses this path, or the sha/entry did not exist at that
        // revision) is grounds to give up and fall back to Live. A transient
        // failure (a dropped connection, a 500) must not read identically to
        // "there is genuinely no snapshot" — everything stays PENDING rather
        // than silently going live out from under the still-selected sha
        // sitting on the URL (the derived `pending` below stays true for as
        // long as `resolvedSnapshot` does not match the URL's current `sha`,
        // which a transient failure leaves untouched either way).
        const status = (err as { status?: number } | null | undefined)?.status;
        if (status !== 404) {
          // Finding 1 (round 2): this used to just `return`, leaving
          // `resolvedSnapshot` (and so `pending`) exactly as they were —
          // forever, since nothing re-runs this effect on its own. A caller
          // gating a frame/fetch on `pending` alone (every one of Overview,
          // Files, API does, per the earlier pending-window findings) then
          // has no way to distinguish "still resolving" from "will never
          // resolve without help" and is stuck showing a skeleton
          // indefinitely. `error` is the caller-visible escape hatch: it
          // does not clear `pending` (there is genuinely still no `snap` to
          // rewrite reads against) — it is a caller's cue to swap the
          // skeleton for an error + retry affordance instead.
          setError(true);
          return;
        }
        // The URL write runs BEFORE the state write, deliberately: `sha`
        // above is read fresh from `location.search` on every render, so a
        // re-render this `setResolvedSnapshot` triggers must not fire while
        // the URL still names the sha this catch is in the middle of giving
        // up on — that would derive `pending: true` for one more render off
        // a `resolvedSnapshot: null` write that was actually the live
        // fallback landing, not a still-open resolve.
        const params = new URLSearchParams(location.search);
        params.delete(SNAPSHOT_PARAM);
        const q = params.toString();
        replaceSearch(location.pathname + (q ? "?" + q : ""));
        setResolvedSnapshot(null);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- urlVersion is a
    // re-run signal (a fresh `_snapshot` on the URL), not a value read here;
    // `sha`/`resolvedSnapshot` are read fresh from the closure on every run.
    // `retryToken` is the same kind of re-run signal, for `retry()` below.
  }, [dir, urlVersion, retryToken]);

  // The one place both halves of "resolved" are checked together — the same
  // guard the effect's own skip-check applies, restated here because a
  // caller must never see a `snap` for a sha the URL has since moved past, or
  // for an app folder a previous `dir` selected (finding 3's shape, applied
  // to the RETURN value rather than only to the refetch decision).
  const resolved =
    sha !== null &&
    resolvedSnapshot !== null &&
    resolvedSnapshot.sha === sha &&
    carries(resolvedSnapshot.app_dir, dir)
      ? resolvedSnapshot
      : null;
  return {
    sha,
    snap: resolved,
    pending: sha !== null && resolved === null,
    error,
    retry: () => {
      if (sha) setRetryToken((t) => t + 1);
    },
  };
}

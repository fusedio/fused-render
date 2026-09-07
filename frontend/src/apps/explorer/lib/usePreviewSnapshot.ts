// Preview.tsx's own `_snapshot` resolution, extracted into a small, plain
// hook — mirroring shell/useAppPageSnapshot.ts, which solved the exact same
// problem (code review, round 3, finding 8) for the app page's Overview/
// Files/API tabs: a NON-404 resolve failure used to leave the URL's `sha`
// claimed forever with nothing to show for it and no way stated to the
// caller that this would never resolve on its own (the effect only re-runs
// on `fsPath`/`urlVersion`, neither of which changes just because a fetch
// failed). `error`/`retry` here are that hook's same fix, applied here.
//
// Extracted (not left inline in Preview.tsx, which has no render-test
// precedent anywhere in this codebase — a 2000+ line component with no
// harness) so this resolve/error/retry state machine can be driven through
// the same hook harness (listing/hook-harness.ts — react-test-renderer, no
// DOM) `useSnapshotForFolder.ts`'s own tests already use, rather than only
// through the whole component.
import { useCallback, useEffect, useState } from "react";
import { getGitSnapshot } from "@platform/lib/api";
import {
  carries,
  getResolvedSnapshot,
  isSha,
  setResolvedSnapshot,
  type ResolvedSnapshot,
} from "@platform/lib/snapshot-param";
import { clearShellSnapshot } from "./snapshot-clear";

export interface PreviewSnapshotState {
  /** The URL's raw `_snapshot` claim right now, or null. Kept in sync with
   *  the URL regardless of resolve outcome — forward this onto every content
   *  frame alongside `snap` (`snapshotFrameSrc` takes both). */
  sha: string | null;
  /** THIS pane's own resolution of `sha` against `fsPath`'s app folder —
   *  null while `sha` is null (live) AND while `sha` is set but not yet (or
   *  not successfully) resolved (`pending` distinguishes the two). */
  snap: ResolvedSnapshot | null;
  /** true exactly when `sha` names something not yet resolved into a `snap`
   *  that actually encloses `fsPath` (two apps in one repo share shas). */
  pending: boolean;
  /** true when the most recent resolve attempt for the URL's CURRENT `sha`
   *  failed with something other than a confirmed 404 (a dropped
   *  connection, a 500) — round 3, finding 8. `pending` alone cannot tell a
   *  caller this apart from an ordinary in-flight resolve, and nothing
   *  re-tries on its own, so a caller must surface this as an error the
   *  user can act on (retry, or back to live) rather than an indefinite
   *  skeleton/blank pane. */
  error: boolean;
  /** Re-attempt resolving the URL's current `sha`. A no-op while `sha` is
   *  null. */
  retry(): void;
  /** Clear the resolution: drops `_snapshot` from the URL, the shared
   *  singleton, AND tells the git sidebar (`clearShellSnapshot` — round 3,
   *  findings 2 and 7), not just this pane's own state. */
  backToLive(): void;
  /** Record a resolution this pane's OWN picker (`window._fusedSnapshotSelected`)
   *  already fetched and already wrote the singleton/URL for — this hook
   *  does not duplicate that fetch, only the local bookkeeping a caller
   *  would otherwise have to keep in step with it by hand. */
  applySelected(snap: ResolvedSnapshot): void;
}

/** `fsPath`'s own resolution of the URL's `_snapshot` sha, re-derived
 *  whenever the URL or `fsPath` changes. `urlVersion` is the caller's own
 *  re-run signal (Preview.tsx's `useUrlVersion()`) — this hook holds no
 *  subscription of its own, the same contract `useSnapshotForFolder` and
 *  `useAppPageSnapshot` both already take. */
export function usePreviewSnapshot(
  fsPath: string,
  urlVersion: number
): PreviewSnapshotState {
  const [snapshotSha, setSnapshotSha] = useState<string | null>(() => {
    const raw = new URLSearchParams(location.search).get("_snapshot");
    return isSha(raw) ? raw : null;
  });
  const [resolvedSnapshotState, setResolvedSnapshotState] =
    useState<ResolvedSnapshot | null>(() => getResolvedSnapshot());
  const [error, setError] = useState(false);
  // Bumped by `retry()` — the effect's own extra re-run signal for "try the
  // SAME sha again", since neither `fsPath` nor `urlVersion` changes just
  // because a fetch failed transiently.
  const [retryToken, setRetryToken] = useState(0);

  useEffect(() => {
    const raw = new URLSearchParams(location.search).get("_snapshot");
    if (!isSha(raw)) {
      setSnapshotSha(null);
      setError((prev) => (prev ? false : prev));
      return;
    }
    // Already resolved THIS sha against an app folder that actually
    // ENCLOSES `fsPath`: nothing to do. Both halves matter (two apps in one
    // repo share shas) — a resolution for the right sha but a DIFFERENT
    // (stale, previous-file) app folder must still be treated as
    // unresolved.
    if (
      resolvedSnapshotState &&
      resolvedSnapshotState.sha === raw &&
      carries(resolvedSnapshotState.app_dir, fsPath)
    ) {
      setSnapshotSha(raw);
      setError((prev) => (prev ? false : prev));
      return;
    }
    setSnapshotSha(raw);
    // A fresh attempt (a new sha, or a re-run `retry()` triggered) starts
    // clean — a stale `error` from a PREVIOUS sha's failure must not keep
    // painting an error banner over a request that has not even settled yet.
    setError(false);
    let alive = true;
    getGitSnapshot(fsPath, raw)
      .then((r) => {
        if (!alive) return;
        const snap = { sha: raw, dir: r.dir, app_dir: r.app_dir };
        setResolvedSnapshot(snap);
        setResolvedSnapshotState(snap);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        // Only a DEFINITIVE 404 (no app folder encloses this path) is
        // grounds to give up and fall back to live. A TRANSIENT failure (a
        // dropped connection, a 500, the server mid-restart) must not read
        // identically to "there is genuinely no snapshot here".
        const status = (err as { status?: number } | null | undefined)?.status;
        if (status !== 404) {
          // Round 3, finding 8: this used to just `return`, leaving `snap`
          // (and so `pending`) exactly as they were — forever, since
          // nothing re-runs this effect on its own. `error` is the
          // caller-visible escape hatch: it does not clear `pending` (there
          // is genuinely still no `snap` to rewrite reads against) — it is
          // a cue to swap the pending state for an error + retry
          // affordance instead.
          setError(true);
          return;
        }
        // A confirmed 404 for THIS pane. If the SAME sha has already
        // resolved successfully somewhere ELSE in this shell — a companion
        // pane on a DIFFERENT app folder, since two apps in one repo share
        // shas — the sha itself is still perfectly valid; only THIS pane
        // has nothing to show for it, so only this pane's own state gives
        // up, leaving the shared `_snapshot` URL (and the singleton) alone
        // for whichever pane is legitimately resolving it.
        if (getResolvedSnapshot()?.sha === raw) {
          setSnapshotSha(null);
          return;
        }
        // Round 3, finding 2: this used to clear the singleton/URL by hand
        // (never telling the git sidebar) instead of going through the same
        // `backToLive` this hook's own callers use — `clearShellSnapshot`
        // is that one shared implementation now.
        clearShellSnapshot();
        setSnapshotSha(null);
        setResolvedSnapshotState(null);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- urlVersion is a
    // re-run signal, not a value read here; `retryToken` is the same kind of
    // re-run signal, for `retry()` below.
  }, [fsPath, urlVersion, retryToken]);

  const snap =
    snapshotSha !== null &&
    resolvedSnapshotState !== null &&
    resolvedSnapshotState.sha === snapshotSha &&
    carries(resolvedSnapshotState.app_dir, fsPath)
      ? resolvedSnapshotState
      : null;

  const backToLive = useCallback(() => {
    clearShellSnapshot();
    setResolvedSnapshotState(null);
    setSnapshotSha(null);
    setError(false);
  }, []);

  const applySelected = useCallback((s: ResolvedSnapshot) => {
    setResolvedSnapshotState(s);
    setSnapshotSha(s.sha);
    setError(false);
  }, []);

  const retry = useCallback(() => {
    // A no-op while `sha` is null: nothing to retry, and bumping the token
    // would just wake the effect for no reason.
    if (snapshotSha) setRetryToken((t) => t + 1);
  }, [snapshotSha]);

  return {
    sha: snapshotSha,
    snap,
    pending: snapshotSha !== null && snap === null,
    error,
    retry,
    backToLive,
    applySelected,
  };
}

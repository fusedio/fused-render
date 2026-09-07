// The folder listing's own git-snapshot resolution — extracted out of
// Listing.tsx so it can be driven through the same hook harness
// (listing/hook-harness.ts) `useDirListing`'s own tests already use, rather
// than only through the 2100-line `Listing` component (no render-test
// precedent anywhere in this codebase, per that file's own test comment).
//
// Own resolution, independent of Preview.tsx's: this hook backs a DIRECTORY
// view, which Preview.tsx never renders (it hosts a template iframe over a
// FILE). Both independently populate the SAME module-level singleton
// (platform/lib/snapshot-param.ts), so whichever resolves first is what the
// other sees too — a harmless redundant fetch when both are mounted (a split
// pane showing this folder's preview), never a wrong answer.
//
// `urlVersion` is a CALLER-SUPPLIED number (Listing.tsx passes
// `useUrlVersion()`), not something this hook reads itself — code review
// finding B5. A commit selection (Preview.tsx's `_fusedSnapshotSelected`) and
// a "back to live" are both a plain `replaceSearch`, which deliberately does
// NOT dispatch `fused:navigate` (only `fused:urlchange` — main.tsx wraps
// `history.replaceState` for exactly this). An effect keyed on `[fsPath]`
// alone never re-ran for a selection made while `fsPath` itself stayed put:
// a directory open in `_listing` mode with the git companion, a commit
// clicked in it, and the folder kept listing live with no banner until an
// UNRELATED navigation happened to fire the effect again. Taking
// `urlVersion` as a parameter (rather than calling `useUrlVersion()` inside
// this hook) keeps the hook itself trivially testable by `rerender`ing with
// a bumped number, with no need to simulate a real `window` event dispatch
// in a test harness that has neither a DOM nor `main.tsx`'s own wrapping of
// `history.replaceState`.
import { useEffect, useState } from "react";
import { getGitSnapshot } from "@platform/lib/api";
import { replaceSearch } from "@platform/lib/router";
import { writeQueryParam } from "@apps/explorer/lib/preview-side";
import {
  carries,
  getResolvedSnapshot,
  isSha,
  setResolvedSnapshot,
  type ResolvedSnapshot,
} from "@platform/lib/snapshot-param";

export function useSnapshotForFolder(
  fsPath: string,
  urlVersion: number
): { resolvedSnapshot: ResolvedSnapshot | null; backToLive: () => void } {
  const [resolvedSnapshot, setLocalResolvedSnapshot] =
    useState<ResolvedSnapshot | null>(() => getResolvedSnapshot());

  useEffect(() => {
    const raw = new URLSearchParams(location.search).get("_snapshot");
    if (!isSha(raw)) {
      setResolvedSnapshot(null);
      setLocalResolvedSnapshot(null);
      return;
    }
    // `urlVersion` bumps on EVERY history write, most of which have nothing
    // to do with `_snapshot` (a sort param, `_side`, `_mode`) — a resolution
    // already sitting on this exact sha needs no redundant round trip,
    // PROVIDED it was resolved against an app folder that actually encloses
    // THIS folder (code review finding [3], round 2: matching only on `sha`
    // let a resolution for a DIFFERENT app — two apps in one repo share
    // shas — survive a hop between them and ship the wrong `dir` forever).
    if (
      resolvedSnapshot &&
      resolvedSnapshot.sha === raw &&
      carries(resolvedSnapshot.app_dir, fsPath)
    ) {
      return;
    }
    let alive = true;
    getGitSnapshot(fsPath, raw)
      .then((r) => {
        if (!alive) return;
        const snap = { sha: raw, dir: r.dir, app_dir: r.app_dir };
        setResolvedSnapshot(snap);
        setLocalResolvedSnapshot(snap);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        // Only a DEFINITIVE 404 (no app folder encloses this path) is
        // grounds to give up — a TRANSIENT failure (a dropped connection, a
        // 500, the server mid-restart) must not read identically to "there
        // is genuinely no snapshot here" (code review finding [2], round 2):
        // stay pending instead, same as Preview.tsx's own re-sync effect.
        const status = (err as { status?: number } | null | undefined)?.status;
        if (status !== 404) return;
        // A confirmed 404 for THIS folder. If the SAME sha has already
        // resolved successfully somewhere ELSE in this shell — a companion
        // Preview pane on a DIFFERENT app folder, since two apps in one
        // repo share shas — the sha itself is still perfectly valid; only
        // THIS folder has nothing to show for it, so only this hook's own
        // local state gives up (this folder lists live), leaving the shared
        // `_snapshot` URL and the singleton alone for whichever pane is
        // legitimately using it. The previous shape cleared the SHELL's own
        // URL unconditionally on any one mount's failure, tearing the
        // snapshot down for every other pane.
        if (getResolvedSnapshot()?.sha === raw) {
          setLocalResolvedSnapshot(null);
          return;
        }
        setResolvedSnapshot(null);
        setLocalResolvedSnapshot(null);
        const search = writeQueryParam(location.search.replace(/^\?/, ""), "_snapshot", null);
        replaceSearch(location.pathname + (search ? "?" + search : ""));
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- urlVersion is a
    // re-run signal, not a value this effect reads
  }, [fsPath, urlVersion]);

  // The one way back the banner offers: clear the resolution and drop
  // `_snapshot` from the URL, same shape Preview.tsx's own "back to live"
  // (triggered by the git sidebar's own control) uses.
  const backToLive = () => {
    setResolvedSnapshot(null);
    setLocalResolvedSnapshot(null);
    const search = writeQueryParam(location.search.replace(/^\?/, ""), "_snapshot", null);
    replaceSearch(location.pathname + (search ? "?" + search : ""));
  };

  return { resolvedSnapshot, backToLive };
}

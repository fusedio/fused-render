// The app page's own "v<n>" arithmetic, lifted out of AppVersionPicker.tsx so
// a second surface (AppPage.tsx's Export control) can label a commit exactly
// the way the picker's closed face already does, rather than recomputing the
// same rule a second way and risking the two disagreeing about what "v7"
// means for the same sha.
//
// LIVES HERE, in platform/lib/ rather than shell/ (where it started): a third
// surface (apps/explorer/Preview.tsx's own Export App button) needs the same
// label, and shell imports apps/, never the reverse (see
// platform/lib/snapshot-param.ts's own comment on the same platform/apps
// boundary) — a module both sides of that boundary need lives in platform/,
// the same call this codebase already made for snapshot-param.ts.
//
// PURE arithmetic, no fetch of its own: `commits`/`total` are whatever the
// caller already loaded (`GET /api/git/commits`'s own `commits`/`total`,
// AppVersionPicker's own state shape) — see that component's header comment
// for why `total` (the server's full count) rather than `commits.length` (capped
// by the fetch's own `limit`) is what the newest row's number is derived from.
import { useEffect, useState } from "react";
import { shortSha } from "@platform/lib/snapshot-param";
import { getGitCommits, type GitCommit } from "@platform/lib/api";

/** `v<n>` for a commit found in `commits` (the newest is `v<total>`, each one
 *  after it counting down by one — see AppVersionPicker's header comment),
 *  the short sha for a selected commit older than the fetched window, or
 *  "Live" when `sha` is null. */
export function versionLabel(
  sha: string | null,
  commits: GitCommit[] | null,
  total: number,
): string {
  if (!sha) return "Live";
  const activeIndex = commits?.findIndex((c) => c.sha === sha) ?? -1;
  return activeIndex >= 0 ? `v${total - activeIndex}` : shortSha(sha);
}

/** The same label, as its own small fetch — for a caller (AppPage's Export
 *  control) that is not the picker itself and so holds none of its commit
 *  list already. Skips the network entirely while `sha` is null: "Live"
 *  needs no commit list to say. A commit older than AppVersionPicker's own
 *  fetch window (or one this fetch has not resolved yet) reads as its short
 *  sha until the list lands, exactly as the picker's own closed face does
 *  during the same window. */
export function useAppVersionLabel(dir: string, sha: string | null): string {
  const [commits, setCommits] = useState<GitCommit[] | null>(null);
  const [total, setTotal] = useState(0);
  useEffect(() => {
    if (!sha) {
      setCommits(null);
      setTotal(0);
      return;
    }
    let live = true;
    getGitCommits(dir)
      .then((r) => {
        if (!live) return;
        setCommits(r.commits);
        setTotal(r.total);
      })
      .catch(() => {
        if (live) {
          setCommits([]);
          setTotal(0);
        }
      });
    return () => {
      live = false;
    };
  }, [dir, sha]);
  return versionLabel(sha, commits, total);
}

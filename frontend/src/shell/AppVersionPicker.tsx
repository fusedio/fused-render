// The app page's version picker: "Live" or a recent commit for the app
// folder, in the header beside the tab strip. Selecting one writes
// `SNAPSHOT_PARAM` (`_snapshot`) onto the page's own URL — the shell's
// existing whole-page snapshot param, carried across every tab hop for free
// (appPageUrl's own `search` carry, no second carry rule needed here) — and
// "Live" clears it.
//
// Offered ONLY when `GET /api/git/app-folder` confirms an app folder actually
// encloses `dir`: the same fail-closed probe templates/git/template.html's own
// preview eye uses (D701 / review finding B4), so this control never appears
// somewhere `/api/git/snapshot` could never resolve for, regardless of which
// commit gets picked. Starts hidden (`probe === "checking"`) rather than
// optimistically shown, for the same fail-closed reason.
//
// Deliberately dumb about what a selection MEANS: this component only reads
// and writes the `_snapshot` sha. Resolving that sha into an extracted
// dir/entry/app_dir — what actually redraws Overview/Files/API — is AppPage's
// own job, kept in AppPage's OWN state and rewritten against ITS OWN
// resolution, never a shared module singleton (code review finding 1, round
// 2, on this branch's earlier snapshot work: two apps in one repo share shas,
// and a singleton written by whichever view resolves last can hold another
// view's resolution by the time a caller reads it).
import { useEffect, useState } from "react";
import { getGitAppFolder, getGitCommits, type GitCommit } from "@platform/lib/api";
import { useUrlVersion } from "@platform/lib/hooks";
import { replaceSearch } from "@platform/lib/router";
import { isSha, shortSha } from "@platform/lib/snapshot-param";

/** The shell URL param a selection writes — shared with AppPage's own resolve
 *  effect (task 3), which reads exactly this param off the same URL. */
export const SNAPSHOT_PARAM = "_snapshot";

/** The value the picker's own `<select>` uses for "Live" — never a real sha
 *  (too short to match `isSha`), so it can share one element with real shas
 *  rather than needing a second control. */
const LIVE = "";

type Probe = "checking" | "no-app" | "ok";

export default function AppVersionPicker({ dir }: { dir: string }) {
  const [probe, setProbe] = useState<Probe>("checking");
  useEffect(() => {
    let live = true;
    setProbe("checking");
    getGitAppFolder(dir)
      .then((r) => live && setProbe(r.ok ? "ok" : "no-app"))
      .catch(() => live && setProbe("no-app"));
    return () => {
      live = false;
    };
  }, [dir]);

  // `null` = still loading (the select shows only "Live" meanwhile, never a
  // half-built list); `[]` once a fetch settles either with truly zero
  // commits or a failed request — a failed commits fetch must leave the page
  // live and pickable, not stuck in a permanent loading state.
  const [commits, setCommits] = useState<GitCommit[] | null>(null);
  useEffect(() => {
    if (probe !== "ok") return;
    let live = true;
    setCommits(null);
    getGitCommits(dir)
      .then((r) => live && setCommits(r.commits))
      .catch(() => live && setCommits([]));
    return () => {
      live = false;
    };
  }, [dir, probe]);

  // Re-read on every URL event (a tab hop, a "Live" written by this very
  // component, back/forward) so the shown selection always matches the URL's
  // own claim rather than this component's last write.
  useUrlVersion();
  const raw = new URLSearchParams(location.search).get(SNAPSHOT_PARAM);
  const sha = isSha(raw) ? raw : null;
  const active = sha ? (commits?.find((c) => c.sha === sha) ?? null) : null;

  const select = (next: string) => {
    const params = new URLSearchParams(location.search);
    if (next && isSha(next)) params.set(SNAPSHOT_PARAM, next);
    else params.delete(SNAPSHOT_PARAM);
    const q = params.toString();
    replaceSearch(location.pathname + (q ? "?" + q : ""));
  };

  if (probe !== "ok") return null;

  return (
    <label
      className="app-version-picker"
      title={
        active
          ? `${active.sha} — ${active.subject}`
          : sha
            ? sha
            : "Live — the working tree"
      }
    >
      <span className="app-version-picker-eyebrow">Version</span>
      <select
        aria-label="App version"
        value={sha ?? LIVE}
        onChange={(e) => select(e.target.value)}
      >
        <option value={LIVE}>Live</option>
        {/* A selected sha absent from the loaded (or still-loading) list —
            a deep link, or a commit older than the fetched window — still
            gets its own option, so the select never silently snaps back to
            "Live" out from under a real selection. */}
        {sha && !active && (
          <option value={sha}>{shortSha(sha)}</option>
        )}
        {commits?.map((c) => (
          <option key={c.sha} value={c.sha}>
            {shortSha(c.sha)} — {c.subject}
          </option>
        ))}
      </select>
    </label>
  );
}

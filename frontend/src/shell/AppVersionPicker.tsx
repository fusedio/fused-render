// The app page's version picker: "Live" or a recent commit for the app
// folder, in the header beside the tab strip. Selecting one writes
// `SNAPSHOT_PARAM` (`_snapshot`) onto the page's own URL — the shell's
// existing whole-page snapshot param, carried across every tab hop for free
// (appPageUrl's own `search` carry, no second carry rule needed here) — and
// "Live" clears it.
//
// Rows read `v1`, `v2`, ... rather than a sha — the app page is for basic
// users (DECISIONS-app-snapshot-preview.md). `v<n>` is PURE client-side index
// arithmetic over the list this component already holds: the newest fetched
// commit is `v<total>` (the server's own count of ALL commits touching the
// app folder, not just what fit under the fetch's limit — see
// `GET /api/git/commits`'s own docstring), and each row after it counts down
// by one. The label is presentation only: `_snapshot` on the URL, and every
// `<option>`'s own `value`, stays the commit's hex sha — a v-number
// renumbers on a rebase or branch switch, so a link keyed on one would
// silently come to mean a different commit later. The short sha stays
// reachable as each row's own `title`.
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
import { ChevronDown } from "lucide-react";
import { getGitAppFolder, getGitCommits, type GitCommit } from "@platform/lib/api";
import { useUrlVersion } from "@platform/lib/hooks";
import { replaceSearch } from "@platform/lib/router";
import { isSha, shortSha } from "@platform/lib/snapshot-param";
import { versionLabel } from "./appVersionLabel";

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
  // live and pickable, not stuck in a permanent loading state. `total` is the
  // server's own count of ALL commits touching the app folder (not just this
  // fetch's `commits.length`, which a `limit` can cap) — it is what lets the
  // newest row read `v<total>` correctly regardless of the cap.
  const [commits, setCommits] = useState<GitCommit[] | null>(null);
  const [total, setTotal] = useState(0);
  useEffect(() => {
    if (probe !== "ok") return;
    let live = true;
    setCommits(null);
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
  }, [dir, probe]);

  // Re-read on every URL event (a tab hop, a "Live" written by this very
  // component, back/forward) so the shown selection always matches the URL's
  // own claim rather than this component's last write.
  useUrlVersion();
  const raw = new URLSearchParams(location.search).get(SNAPSHOT_PARAM);
  const sha = isSha(raw) ? raw : null;
  const active = sha ? (commits?.find((c) => c.sha === sha) ?? null) : null;
  // The closed face's own text (task 7): "Live" with nothing selected, else
  // v<n> for a commit the loaded list actually holds, else the short sha for
  // a deep link older than the fetched window — the same fallback the
  // option list already uses one line below, just read here for the FACE
  // rather than the row.
  const closedLabel = versionLabel(sha, commits, total);

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
      {/* A native `<select>` is necessarily its own closed face — an
          `<option>`'s text IS what the closed control shows. So the
          version-number-only closed face (task 7) is a separate visible
          `<span>` stacked over the real select, which stays fully
          interactive (keyboard, screen reader, mobile picker) but
          transparent. The options keep their full "v<n> — subject" text;
          this is presentation only, exactly as the module's own comment
          above already says of the v-number itself. */}
      <span className="app-version-picker-face">
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
            // A deep link older than the fetched window: this component has no
            // idea of its ordinal (that would need a second, unbounded fetch,
            // which this component deliberately never makes — see the plan's
            // "keep it cosmetic" amendment), so it falls back to the sha rather
            // than guessing a version number.
            <option value={sha} title={sha}>{shortSha(sha)}</option>
          )}
          {commits?.map((c, i) => (
            <option key={c.sha} value={c.sha} title={c.sha}>
              v{total - i} — {c.subject}
            </option>
          ))}
        </select>
        {/* aria-hidden: the select above already carries the accessible name
            (`aria-label="App version"`) and its own option text is what a
            screen reader or keyboard user actually reads/picks — this span
            is a sighted-mouse-user affordance only. */}
        <span className="app-version-picker-face-label" aria-hidden="true">
          {closedLabel}
        </span>
        {/* FINDING 6: with no native select visibly painted (its own text is
            transparent — see the comment above), this pill had NO
            affordance at all saying "this opens a menu" — it read as plain
            text. A sibling of the label rather than a child of it, so the
            label's own text content (asserted elsewhere as exactly the
            v-number/sha) stays pure text. Decorative only, same reasoning
            as the label itself: the real select carries the accessible
            name. */}
        <ChevronDown className="app-version-picker-caret" aria-hidden="true" size={12} />
      </span>
    </label>
  );
}

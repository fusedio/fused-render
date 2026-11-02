// THE RAIL'S GIT FACTS (ClaudeRail `git`): what the Git section under the
// chat rings draws — the branch, how far it stands from the remote's default
// branch, the last three commits that touched the page's folder or file —
// read off `GET /api/git/rail` (routers/git_rail.py) for the page's path.
//
// Read on mount and whenever the path changes; re-read a beat after the
// tasks pulse moves (a chat that just committed shows its commit without a
// reload — the pulse is the one feed every rail already follows) and when the
// tab comes back into view. `check` is the "Check for updates" seat: one
// fetch of the default branch (`POST /api/git/rail/check`), then the fresh
// counts. `repo` is null where the path is in no repository (a canvas clone,
// a plain folder): the section is not drawn.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getRailGit, postRailGitCheck, type RailGitRepo } from "@platform/lib/api";
import { useTasksPulseRows } from "@shell/tasksPulse";

/** How long after the pulse moves the rail re-reads: a commit lands a moment
 *  after the row's status does, and a burst of rows is one read. */
const PULSE_SETTLE_MS = 2500;

export interface RailGit {
  /** `undefined` until the first read answers; `null` = not in a repo. */
  repo: RailGitRepo | null | undefined;
  checking: boolean;
  /** The last check reached the remote (null until one has run). */
  checked: boolean | null;
  check: () => void;
  reload: () => void;
}

export function useRailGit(path: string | null): RailGit {
  const [repo, setRepo] = useState<RailGitRepo | null | undefined>(undefined);
  const [checking, setChecking] = useState(false);
  const [checked, setChecked] = useState<boolean | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  // One read in flight at a time; a later request supersedes an earlier one.
  const seq = useRef(0);
  const reload = useCallback(() => {
    if (!path) return;
    const mine = ++seq.current;
    getRailGit(path).then(
      (r) => {
        if (alive.current && mine === seq.current) setRepo(r.repo);
      },
      () => {
        /* a failed read keeps what is on screen */
      },
    );
  }, [path]);
  useEffect(() => {
    setRepo(undefined);
    setChecked(null);
    reload();
  }, [reload]);
  // After a task's STATUS moves (header) — a run that ended may have
  // committed. The rows' key+status fingerprint, not the rows themselves:
  // a streaming turn moves `last_active` every tick, and a git log per tick
  // would be the exact cost this hook must not add.
  const rows = useTasksPulseRows();
  const statusKey = useMemo(() => rows.map((t) => t.key + ":" + t.status).join("\n"), [rows]);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const t = window.setTimeout(reload, PULSE_SETTLE_MS);
    return () => window.clearTimeout(t);
  }, [statusKey, reload]);
  // …and when the tab comes back.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") reload();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [reload]);
  const check = useCallback(() => {
    if (!path || checking) return;
    setChecking(true);
    const mine = ++seq.current;
    postRailGitCheck(path).then(
      (r) => {
        if (!alive.current) return;
        setChecking(false);
        if (mine === seq.current) {
          setRepo(r.repo);
          setChecked(r.checked);
        }
      },
      () => {
        if (!alive.current) return;
        setChecking(false);
        setChecked(false);
      },
    );
  }, [path, checking]);
  return { repo, checking, checked, check, reload };
}

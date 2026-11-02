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
  // A CHECK OUTRANKS A RELOAD: its answer is the fresh fetch, and a reload
  // the pulse fired meanwhile must neither overwrite it nor make it skip
  // (`checkingRef`): no reload runs while a check is out, and the check's
  // answer always lands.
  const seq = useRef(0);
  const checkingRef = useRef(false);
  const pathRef = useRef(path);
  pathRef.current = path;
  const reload = useCallback(() => {
    if (!path || checkingRef.current) return;
    const mine = ++seq.current;
    getRailGit(path).then(
      (r) => {
        if (alive.current && mine === seq.current && !checkingRef.current) setRepo(r.repo);
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
  // committed. ONLY the tasks in this repository (the rows whose project or
  // target lies under the root the first read named): a task churning in
  // another project is no reason to fork git here. Keyed on key+status, not
  // the rows themselves: a streaming turn moves `last_active` every tick,
  // and a git log per tick would be the exact cost this hook must not add.
  const rows = useTasksPulseRows();
  const root = repo?.root ?? null;
  const statusKey = useMemo(() => {
    if (!root) return "";
    const base = root.replace(/\/+$/, "");
    const under = (p: string | undefined) => !!p && (p === base || p.startsWith(base + "/"));
    return rows
      .filter((t) => under(t.project) || under(t.target))
      .map((t) => t.key + ":" + t.status)
      .join("\n");
  }, [rows, root]);
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
    if (!path || checkingRef.current) return;
    checkingRef.current = true;
    setChecking(true);
    ++seq.current;
    const asked = path;
    postRailGitCheck(path).then(
      (r) => {
        checkingRef.current = false;
        if (!alive.current) return;
        setChecking(false);
        // The page moved on to another path meanwhile: not ours to show.
        if (pathRef.current !== asked) return;
        setRepo(r.repo);
        setChecked(r.checked);
      },
      () => {
        checkingRef.current = false;
        if (!alive.current) return;
        setChecking(false);
        if (pathRef.current === asked) setChecked(false);
      },
    );
  }, [path]);
  return { repo, checking, checked, check, reload };
}

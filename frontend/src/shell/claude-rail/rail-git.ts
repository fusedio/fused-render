// THE RAIL'S GIT FACTS (ClaudeRail `git`): what the Git section under the
// chat rings draws — the branch, how far it stands from the remote's default
// branch, the last commits that touched the page's folder or file —
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

/** How many commits a read asks for: a full window A card's worth for the
 *  Git door (owner, 2026-11-10: "fill it with commits"); the flag-off rail
 *  draws the first three. */
export const RAIL_GIT_COMMITS = 50;

export interface RailGit {
  /** `undefined` until the first read answers (or with no path); `null` =
   *  not in a repo. A path change keeps the last answer until the next. */
  repo: RailGitRepo | null | undefined;
  checking: boolean;
  /** The last check reached the remote (null until one has run). */
  checked: boolean | null;
  check: () => void;
  reload: () => void;
}

// THE LAST ANSWERS, FOR A NEW RAIL'S FIRST FRAME (QA, 2026-11-10): the
// explorer swaps Listing ↔ Preview on a folder → file hop, so a NEW rail
// mounts — and with nothing to draw until its read answered, the Git door
// and its hairline went for a frame and a pinned Git card closed. A new
// rail starts from the last answer for its path, else the repository whose
// root holds it (the innermost); its own read then swaps that for the truth.
// BOUNDED (CodeRabbit on #2236): the newest `ANSWERS_MAX` paths; a path
// answered again moves to the newest end, so the least recent goes first.
const ANSWERS_MAX = 200;
const answers = new Map<string, RailGitRepo | null>();
const trim = (p: string) => p.replace(/\/+$/, "") || "/";
function remember(path: string, repo: RailGitRepo | null): void {
  const key = trim(path);
  answers.delete(key);
  answers.set(key, repo);
  while (answers.size > ANSWERS_MAX) {
    const oldest = answers.keys().next().value;
    if (oldest === undefined) break;
    answers.delete(oldest);
  }
}

/** Test-only: an answer, as a read would leave it. */
export const rememberRailGitForTests = remember;

/** How many paths the cache holds (tests). */
export function railGitCacheSize(): number {
  return answers.size;
}

/** What a new rail for `path` draws until its own read answers. */
export function cachedRailGit(path: string | null): RailGitRepo | null | undefined {
  if (!path) return undefined;
  const p = trim(path);
  if (answers.has(p)) return answers.get(p);
  let best: RailGitRepo | undefined;
  for (const r of answers.values()) {
    if (!r) continue;
    const root = trim(r.root);
    if ((p === root || p.startsWith(root + "/")) && (!best || root.length > trim(best.root).length)) best = r;
  }
  return best;
}

/** Test-only. */
export function resetRailGitCacheForTests(): void {
  answers.clear();
}

export function useRailGit(path: string | null): RailGit {
  const [repo, setRepo] = useState<RailGitRepo | null | undefined>(() => cachedRailGit(path));
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
  // Which check is the live one: a path switch mid-check abandons it (the
  // effect below), and its late answer must then neither land nor clear the
  // flag a newer check holds (Bugbot on #2069).
  const checkToken = useRef(0);
  const pathRef = useRef(path);
  pathRef.current = path;
  const reload = useCallback(() => {
    if (!path || checkingRef.current) return;
    const mine = ++seq.current;
    getRailGit(path, RAIL_GIT_COMMITS).then(
      (r) => {
        remember(path, r.repo);
        if (alive.current && mine === seq.current && !checkingRef.current) setRepo(r.repo);
      },
      () => {
        /* a failed read keeps what is on screen */
      },
    );
  }, [path]);
  useEffect(() => {
    // A check still out is the OLD path's: let it go, so the new path's
    // read is not held back by it.
    checkToken.current += 1;
    checkingRef.current = false;
    setChecking(false);
    setChecked(null);
    // THE LAST REPOSITORY STAYS DRAWN while the new path's read is out
    // (owner, 2026-11-10: the explorer's folder → file hop in one repository
    // flickered the Git door, its hairline and its card): the answer swaps
    // it, and only an answer of "no repository" takes it away. No path: no
    // repository at all.
    if (!path) setRepo(undefined);
    reload();
  }, [reload, path]);
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
    const token = ++checkToken.current;
    const mine = () => alive.current && token === checkToken.current && pathRef.current === asked;
    postRailGitCheck(path, RAIL_GIT_COMMITS).then(
      (r) => {
        remember(asked, r.repo);
        // Abandoned (the page moved on): not ours to show, nor to end.
        if (!mine()) return;
        checkingRef.current = false;
        setChecking(false);
        setRepo(r.repo);
        setChecked(r.checked);
      },
      () => {
        if (!mine()) return;
        checkingRef.current = false;
        setChecking(false);
        setChecked(false);
      },
    );
  }, [path]);
  return { repo, checking, checked, check, reload };
}

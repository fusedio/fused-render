// The GitHub contribution loop's client half (docs/CONTRIB_LOOP.md §6.4,
// §12): the shapes `/api/contrib/*` answers with, one wrapper per route, the
// job poller, and `useAppContrib` — the app page's one subscription to a
// folder's copy status.
//
// REFUSALS ARE 200s. Every route answers `{ok, reason?, message?, …}` and a
// refusal (`ok:false`) is an ordinary answer carrying a sentence the user can
// read — so the shared `getJson`/`postJson` transport never throws on one,
// and every caller branches on `ok`. What DOES throw (a 5xx, a dropped
// connection) never shows its own text: `plainMessage` turns it into the one
// generic sentence the server's job registry also uses
// (fused_render/contrib/jobs.py GENERIC_MESSAGE), because a contrib route's
// raw error is exactly where git's stderr or a path would leak (§0.1).
//
// MERGE JOBS ARE NOT JOBS. `/propose/send`, `/remix/publish` and the inbox
// accept are jobs.py jobs, polled at `/api/contrib/job` (`pollJob` below).
// "Merge with Claude" is a merge_job (§6.3): `/merge/start` hands back its id
// and its state is read from `copy.merge_job` on the next `/api/contrib/app`
// answer — which is why `useAppContrib` polls fast while one runs.
import { useCallback, useEffect, useRef, useState } from "react";
import { getJson, postJson } from "@platform/lib/api";
import { pauseWhileHidden } from "@platform/lib/pause-while-hidden";
import { useContribFlags, type ContribFlags } from "@platform/lib/contrib-flags";

export type { ContribFlags };

// ---- shapes (§6.4, §9, §12) --------------------------------------------------

export type ContribEventKind =
  | "updated"
  | "updated-kept"
  | "merged-back"
  | "declined"
  | "merge-accepted"
  | "discarded"
  | "remixed"
  | "original-brought-in";

export interface ContribEvent {
  kind: ContribEventKind;
  /** Epoch seconds. */
  at: number;
  who: string | null;
}

export type MergePurpose = "needs-merge" | "return" | "own-sync" | "original";

export interface MergeJobView {
  id: string;
  /** `landing`: Keep the merge was pressed and the result is being moved
   *  into the real folder — still in flight, drawn as running. */
  status: "running" | "landing" | "ready" | "failed" | "accepted" | "discarded";
  /** Claude's 2–4 plain sentences about what it combined (ready). */
  summary: string;
  files: string[];
  purpose: MergePurpose;
  /** Epoch seconds, when the server sends it — the elapsed clock falls back
   *  to the first time this page saw the job running. */
  started_at?: number;
  /** A plain refusal sentence when `failed`. */
  error?: string | null;
}

export type CopyState =
  | "following"
  | "changes"
  | "proposed"
  | "needs-merge"
  | "merged"
  | "declined"
  | "remix"
  | "own";

export interface CopyStatus {
  kind: "own" | "github" | "scratch";
  state: CopyState;
  /** "alice/x": the repo this copy follows (github) or publishes to (own). */
  repo: string | null;
  /** The author's GitHub login — the name every sentence uses. */
  author: string | null;
  remix_of: string | null;
  /** Edits the author's latest does not have. */
  changes: number;
  /** Uncommitted tracked edits on top of those. */
  dirty: boolean;
  /** Edits made after the proposal was last sent. */
  unproposed: number;
  needs_merge: boolean;
  /** Whether "Merge with Claude" can resolve the overlap. False for the
   *  overlaps Claude cannot merge (a file of the user's the author's update
   *  would take over; an own copy whose GitHub history was rewritten): the
   *  page then shows `needs_merge_message` and no button. Absent = true. */
  merge_button?: boolean;
  /** The plain sentence for this overlap, when the server has a specific
   *  one ("alice's update adds a file you already have here (.env). Rename
   *  yours to get the update."). */
  needs_merge_message?: string | null;
  merge_job: MergeJobView | null;
  /** Newest unacknowledged event under 24 h old. */
  event: ContribEvent | null;
  /** A5: the remix's original moved and has not been brought in. */
  original_update: boolean;
  /** Collaborator with push rights; null = unknown. */
  can_push: boolean | null;
  /** Author updates waiting to come in. */
  behind?: number;
  /** False when the automatic-update preference is off: the author's
   *  updates then wait for the user's "Bring them in". */
  auto_sync?: boolean;
  /** False: classified without a GitHub identity (signed out at install),
   *  so whether the app is the user's own is not known yet — signing in
   *  lets fused check once and may make it "Your app". */
  owner_checked?: boolean;
  /** Own copy: a plain sentence when sending the user's edits to GitHub
   *  failed (there is no activity-dock failure row for these apps). Shown
   *  as is. */
  sync_error?: string | null;
  /** git_upstream's failure code behind `sync_error` ("auth", "rejected",
   *  "git-failed", …) — for branching, never for display. */
  sync_error_code?: string | null;
}

export interface ProposalView {
  number: number;
  url: string;
  title: string;
  state: "waiting" | "commented" | "merged" | "declined";
  comment?: { author: string; body: string; at: string };
  needs_merge: boolean;
  /** Committed edits made since the last send (uncommitted ones are
   *  `copy.dirty`, counted on top by the page). */
  unproposed: number;
  /** Open, on a GitHub-link copy: Update proposal is offered. */
  can_update?: boolean;
  /** Any open proposal, a remix's included. */
  can_withdraw?: boolean;
  /** What this copy runs differs from what was last sent — the one signal
   *  for offering Update proposal (WS-B). */
  stale?: boolean;
}

export interface ReviewRisk {
  kind: string;
  detail: string;
  file?: string | null;
}

export interface Review {
  verdict: "fine" | "look";
  summary: string;
  risks: ReviewRisk[];
  static_hits?: unknown[];
  model?: string;
  head_sha?: string;
  at?: number;
}

export interface TryCopy {
  status: string;
  path: string;
  url: string;
}

export interface InboxItem {
  number: number;
  url: string;
  title: string;
  author: string;
  /** Sent from Fused (false = opened on github.com directly). */
  from_fused: boolean;
  summary: string;
  /** Who wrote `summary`: "review" = Claude's safety review on THIS machine;
   *  anything else (the sender's own text, their Claude's draft) is the
   *  sender's words and is labelled so — never passed off as Claude's. */
  summary_source?: string;
  /** The proposal's own description, as the sender wrote it. Untrusted. */
  description?: string;
  review: Review | null;
  /** "none": no review started yet — brief, the page's read starts one. */
  review_status: "none" | "pending" | "running" | "ready" | "failed";
  try: TryCopy | null;
  head_sha: string;
  /** ISO timestamp from GitHub. */
  created_at: string;
}

export interface Contributor {
  login: string | null;
  name: string;
  commits: number;
}

export interface AppContrib {
  flags: ContribFlags;
  copy: CopyStatus;
  proposal: ProposalView | null;
  /** `loading`: the list has never been read from GitHub yet (a cold
   *  cache) — not the same as an empty inbox. `unavailable`: it could not be
   *  read at all. */
  inbox: { items: InboxItem[]; count: number; loading?: boolean; unavailable?: boolean } | null;
  contributors: Contributor[] | null;
  /** `checking`: this machine has not asked GitHub yet — "signed out" is
   *  not known, so nothing says "Sign in" on its strength (the sign-in
   *  step checks for itself). */
  github: { signed_in: boolean; login: string | null; checking?: boolean };
  share_url: string | null;
}

/** What `GET /api/contrib/app` actually answers: the AppContrib above with
 *  `ok:true`, `{ok:false, reason}` when the phase is off or the folder is not
 *  an app, or `pending:true` (copy null) while a first open is still being
 *  classified in the background — poll again. */
export type AppContribAnswer = Omit<Partial<AppContrib>, "copy"> & {
  ok?: boolean;
  reason?: string;
  pending?: boolean;
  copy?: CopyStatus | null;
};

/** Every POST's answer. Extra fields ride beside these per route. */
export interface ContribAnswer {
  ok: boolean;
  reason?: string;
  message?: string;
}

export interface JobStarted extends ContribAnswer {
  job?: string;
}

export interface ContribJob<R = Record<string, unknown>> {
  id?: string;
  status: "running" | "done" | "failed";
  message?: string | null;
  /** Why it failed, when the reason is one the page acts on
   *  ("signed-out" → run the sign-in step and retry). */
  reason?: string | null;
  result?: R | null;
  /** Optional progress key for a multi-step job (`/propose/send`:
   *  "copy" → "send" → "open"); absent = no step reported. */
  step?: string | null;
}

export interface ProposalDraft {
  title: string;
  description: string;
}

export interface RemixDraft {
  name: string;
  credit: string;
}

export interface SentProposal {
  number?: number;
  url?: string;
}

export interface PublishedRemix {
  repo?: string;
  share_url?: string;
  url?: string;
}

// ---- errors ------------------------------------------------------------------

export const GENERIC_MESSAGE = "Something went wrong. Try again.";

/** The sentence to show for a refusal or a failure — the server's own plain
 *  `message` for an `ok:false` answer, the generic sentence for anything that
 *  threw (never the thrown text). */
export function plainMessage(answer: unknown): string {
  if (answer && typeof answer === "object" && !(answer instanceof Error)) {
    const m = (answer as { message?: unknown }).message;
    if (typeof m === "string" && m.trim()) return m;
  }
  return GENERIC_MESSAGE;
}

// ---- reads -------------------------------------------------------------------

export function getAppContrib(path: string, opts?: { signal?: AbortSignal }): Promise<AppContribAnswer> {
  return getJson<AppContribAnswer>(`/api/contrib/app?path=${encodeURIComponent(path)}`, opts);
}

export function getContribJob<R>(id: string, opts?: { signal?: AbortSignal }): Promise<ContribJob<R>> {
  return getJson<ContribJob<R>>(`/api/contrib/job?id=${encodeURIComponent(id)}`, opts);
}

// ---- mutations (§12.3) -------------------------------------------------------
//
// Each resolves with the server's answer, refusals included; a transport
// failure resolves as `{ok:false}` with no message, so `plainMessage` gives the
// generic sentence and no caller needs a try/catch of its own.

async function post<T extends ContribAnswer>(route: string, body: Record<string, unknown>): Promise<T> {
  try {
    return await postJson<T>(`/api/contrib/${route}`, body);
  } catch {
    return { ok: false } as T;
  }
}

export const ackContrib = (path: string, kind: "event" | "settled") =>
  post<ContribAnswer>("ack", { path, kind });
export const startProposalDraft = (path: string) => post<JobStarted>("propose/draft", { path });
export const sendProposal = (path: string, v: { title: string; description: string; note: string }) =>
  post<JobStarted>("propose/send", { path, ...v });
export const updateProposal = (path: string) => post<JobStarted>("propose/update", { path });
export const withdrawProposal = (path: string) => post<ContribAnswer>("propose/withdraw", { path });
export const discardChanges = (path: string, withdraw: boolean) =>
  post<ContribAnswer>("discard", { path, withdraw });
export const pushToMain = (path: string) => post<ContribAnswer>("push-main", { path });
/** Bring the author's waiting updates in now (auto-update off). */
export const updateCopy = (path: string) => post<ContribAnswer>("update-copy", { path });
export const startMerge = (path: string) => post<JobStarted>("merge/start", { path });
export const acceptMerge = (path: string, job: string) => post<ContribAnswer>("merge/accept", { path, job });
export const discardMerge = (path: string, job: string) => post<ContribAnswer>("merge/discard", { path, job });
export const refreshContrib = (path: string) => post<ContribAnswer>("refresh", { path });
export const reviewProposal = (path: string, number: number) =>
  post<JobStarted>("inbox/review", { path, number });
export const tryProposal = (path: string, number: number, confirm: boolean) =>
  post<ContribAnswer & Partial<TryCopy>>("inbox/try", { path, number, confirm });
export const removeTry = (path: string, number: number) => post<ContribAnswer>("inbox/try/remove", { path, number });
/** `confirm`: the user accepted a "needs a look" proposal knowingly — the
 *  server refuses one without it (job fails with reason "confirm"). */
export const acceptProposal = (path: string, number: number, confirm = false) =>
  post<JobStarted>("inbox/accept", { path, number, confirm });
export const declineProposal = (path: string, number: number, note: string) =>
  post<ContribAnswer>("inbox/decline", { path, number, note });
export const startRemixDraft = (path: string) => post<JobStarted>("remix/draft", { path });
export const publishRemix = (path: string, v: { name: string; credit: string }) =>
  post<JobStarted>("remix/publish", { path, ...v });
export const bringInOriginal = (path: string) => post<JobStarted>("remix/bring-in", { path });

/** Retry sending an own app's edits to GitHub (`copy.sync_error`): the same
 *  background sync the app's own changes trigger (POST /api/git-upstream
 *  `action:"sync"`, which the contrib engine intercepts). Answers at once —
 *  the sync is queued — so the caller re-reads a moment later. */
export async function retrySync(path: string): Promise<ContribAnswer> {
  try {
    return await postJson<ContribAnswer>("/api/git-upstream", { action: "sync", path, trigger: "retry" });
  } catch {
    return { ok: false };
  }
}

/** A success toast's text: the server's own `message` when the answer (or a
 *  job's result) carries one, else the page's sentence. */
export function okMessage(answer: unknown, fallback: string): string {
  if (answer && typeof answer === "object") {
    const a = answer as { message?: unknown; result?: { message?: unknown } | null };
    if (typeof a.message === "string" && a.message.trim()) return a.message;
    const m = a.result?.message;
    if (typeof m === "string" && m.trim()) return m;
  }
  return fallback;
}

/** The signal of a controller that aborts when the calling component
 *  unmounts — for the job polls a sheet or card starts, so an unmounted page
 *  stops polling. A GETTER, read at the moment a poll starts: StrictMode's
 *  mount-unmount-mount replaces the controller after the first render, so a
 *  signal captured during render could already be aborted. */
export function useUnmountSignal(): () => AbortSignal {
  const ref = useRef<AbortController | null>(null);
  if (ref.current === null) ref.current = new AbortController();
  useEffect(() => {
    // StrictMode mounts twice: a controller aborted by the first cleanup is
    // replaced so the real mount gets a live one.
    if (ref.current?.signal.aborted) ref.current = new AbortController();
    const c = ref.current!;
    return () => c.abort();
  }, []);
  return useCallback(() => ref.current!.signal, []);
}

// ---- job polling -------------------------------------------------------------

const JOB_POLL_MS = 1200;

/** Poll a jobs.py job until it ends. Resolves with the final job — `failed`
 *  carries the plain `message`; a job the server no longer knows (a restart)
 *  resolves as failed with the generic sentence. `onTick` sees every running
 *  answer (for a step display). Rejects only with an AbortError when
 *  `signal` aborts. A transient read failure is retried, not fatal. */
export async function pollJob<R = Record<string, unknown>>(
  id: string,
  opts: { signal?: AbortSignal; onTick?: (job: ContribJob<R>) => void } = {},
): Promise<ContribJob<R>> {
  const { signal, onTick } = opts;
  let misses = 0;
  for (;;) {
    if (signal?.aborted) throw new DOMException("aborted", "AbortError");
    let job: ContribJob<R> | null = null;
    try {
      job = await getContribJob<R>(id, { signal });
      misses = 0;
    } catch (e) {
      if ((e as Error).name === "AbortError") throw e;
      // A 404 is a job this server never ran or has forgotten; three misses
      // in a row of anything else is a server that is not coming back soon.
      const status = (e as { status?: number }).status;
      if (status === 404 || ++misses >= 3) return { status: "failed", message: GENERIC_MESSAGE };
    }
    if (job) {
      if (job.status !== "running") return job;
      onTick?.(job);
    }
    await new Promise<void>((resolve, reject) => {
      const onAbort = () => {
        window.clearTimeout(t);
        reject(new DOMException("aborted", "AbortError"));
      };
      // Removed on the ordinary path too: one listener per tick would
      // otherwise pile up on a long-lived signal for the job's whole life.
      const t = window.setTimeout(() => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      }, JOB_POLL_MS);
      signal?.addEventListener("abort", onAbort, { once: true });
    });
  }
}

/** Start a job route and wait for it: one call for the dialog that only
 *  needs the outcome. `{ok:false, message, reason?}` for a refusal at start
 *  (reason e.g. "signed-out") or a failed job; `{ok:true, result}` when done.
 *  An aborted wait (the component unmounted) answers `reason:"aborted"` —
 *  callers drop it — rather than rejecting. The JOB keeps running server-side
 *  either way: abort only stops this page watching it. */
export async function runJob<R = Record<string, unknown>>(
  start: Promise<JobStarted>,
  opts: { signal?: AbortSignal; onTick?: (job: ContribJob<R>) => void } = {},
): Promise<{ ok: true; result: R | null } | { ok: false; message: string; reason?: string }> {
  const started = await start;
  if (!started.ok || !started.job) {
    return { ok: false, message: plainMessage(started), reason: started.reason };
  }
  let job: ContribJob<R>;
  try {
    job = await pollJob<R>(started.job, opts);
  } catch {
    return { ok: false, message: "", reason: "aborted" };
  }
  if (job.status === "done") return { ok: true, result: job.result ?? null };
  return { ok: false, message: plainMessage(job), reason: job.reason ?? undefined };
}

// ---- the subscription --------------------------------------------------------

const REST_POLL_MS = 60_000;
/** While something visible is moving on the server (Claude merging, a safety
 *  review reading, a first open still being classified) — every GET runs a
 *  handful of local git calls, so no faster than this (WS-D's floor). */
const BUSY_POLL_MS = 5_000;
/** `GET /app` answers from cache and only KICKS a background refresh, so the
 *  answer to a focus is usually one step behind GitHub. One more read this
 *  long after a focus picks up what that refresh found. */
const FOCUS_FOLLOWUP_MS = 5_000;
/** A busy state that never settles (a stuck review, a server that keeps
 *  saying "pending") must not poll at 5 s forever: after this many fast
 *  ticks the poll backs off to BACKOFF_POLL_MS until the state changes. */
const BUSY_TICKS_BEFORE_BACKOFF = 12;
const BACKOFF_POLL_MS = 30_000;

/** Is anything on the server mid-flight that the page is showing progress
 *  for? Drives the fast poll. */
export function contribBusy(c: AppContrib | null): boolean {
  if (!c) return false;
  const m = c.copy.merge_job?.status;
  if (m === "running" || m === "landing") return true;
  if (c.inbox?.loading) return true;
  return !!c.inbox?.items.some(
    (i) => i.review_status === "none" || i.review_status === "pending" || i.review_status === "running",
  );
}

function anyFlag(f: ContribFlags): boolean {
  return f.sources || f.changes || f.inbox || f.loop || f.remix;
}

export interface AppContribState {
  /** The last answer for THIS folder, or null (not asked yet / every flag
   *  off / the first read failed). */
  data: AppContrib | null;
  /** True once the first read for this folder has settled either way. */
  settled: boolean;
  /** True once the SERVER has answered for this folder (an AppContrib, or a
   *  plain `ok:false`) — unlike `settled`, a failed read never sets it. What
   *  a decision that rewrites the URL waits for. */
  answered: boolean;
  /** Re-read now. Resolves when the answer is painted. */
  refresh: () => Promise<void>;
}

/** The app page's copy status for `dir`: read on mount, on window focus (plus
 *  one follow-up), every 60 s while the page is visible — every 5 s while a
 *  merge or a review is running or the folder is still being classified —
 *  and whenever a caller says `refresh()`
 *  (after every mutation). Nothing is read while every contrib flag is off,
 *  so the page with the feature off makes exactly the requests it always did. */
export function useAppContrib(dir: string | null): AppContribState {
  const flags = useContribFlags();
  const enabled = !!dir && flags !== null && anyFlag(flags);
  const [data, setData] = useState<AppContrib | null>(null);
  const [settled, setSettled] = useState(false);
  const [pending, setPending] = useState(false);
  const [answered, setAnswered] = useState(false);
  // Generation token, AppPage's iconGenRef pattern: a folder switch (or a
  // newer read) makes every older in-flight answer stale, so app A's card can
  // never paint over app B's.
  const genRef = useRef(0);
  const dirRef = useRef(dir);
  dirRef.current = dir;

  const read = useCallback(async () => {
    const target = dirRef.current;
    if (!target) return;
    const gen = ++genRef.current;
    try {
      const next = await getAppContrib(target);
      if (genRef.current !== gen || dirRef.current !== target) return;
      if (next.ok === false) {
        // Phase off, or not an app: nothing to show.
        setPending(false);
        setData(null);
        setAnswered(true);
      } else if (next.pending || !next.copy) {
        // Still classifying a first open — poll again soon, show nothing yet,
        // and stay UNSETTLED: a `?_tab=proposals` link must not fall back to
        // the Overview on an answer that is not one yet.
        setPending(true);
        return;
      } else {
        setPending(false);
        setData(next as AppContrib);
        setAnswered(true);
      }
    } catch {
      // A failed read is not an answer: keep what is on screen.
    }
    if (genRef.current === gen) setSettled(true);
  }, []);

  // Folder change (or the flags turning on/off): start over.
  useEffect(() => {
    genRef.current += 1;
    setData(null);
    setPending(false);
    setAnswered(false);
    setSettled(!enabled);
    if (enabled) void read();
  }, [dir, enabled, read]);

  const busy = pending || contribBusy(data);
  useEffect(() => {
    if (!enabled) return;
    // A timeout chain, not an interval: the gap changes mid-stream when a
    // busy state outlives BUSY_TICKS_BEFORE_BACKOFF fast ticks.
    let fast = 0;
    let timer: number | undefined;
    let stopped = false;
    const gap = () => (!busy ? REST_POLL_MS : fast < BUSY_TICKS_BEFORE_BACKOFF ? BUSY_POLL_MS : BACKOFF_POLL_MS);
    const tick = pauseWhileHidden(() => {
      if (stopped) return;
      if (busy) fast += 1;
      void read().finally(() => {
        if (!stopped) timer = window.setTimeout(tick, gap());
      });
    });
    timer = window.setTimeout(tick, gap());
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      tick.cancel();
    };
  }, [enabled, busy, read]);

  useEffect(() => {
    if (!enabled) return;
    let followup: number | undefined;
    const onFocus = () => {
      void read();
      window.clearTimeout(followup);
      followup = window.setTimeout(() => void read(), FOCUS_FOLLOWUP_MS);
    };
    window.addEventListener("focus", onFocus);
    return () => {
      window.removeEventListener("focus", onFocus);
      window.clearTimeout(followup);
    };
  }, [enabled, read]);

  const refresh = useCallback(() => (enabled ? read() : Promise.resolve()), [enabled, read]);
  // Not settled while the flags themselves are still being read: a
  // `?_tab=proposals` link must keep its tab through that window too.
  return {
    data: enabled ? data : null,
    settled: flags !== null && settled,
    // Every flag off is a real answer too: there is no inbox to offer.
    answered: flags !== null && (!enabled || answered),
    refresh,
  };
}

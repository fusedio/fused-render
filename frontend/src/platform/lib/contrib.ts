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
import type { SetupRecord } from "@platform/lib/github-setup";
import { pauseWhileHidden } from "@platform/lib/pause-while-hidden";
import { useContribFlags, type ContribFlags } from "@platform/lib/contrib-flags";
import { useMarketplaceFeature } from "@platform/lib/marketplace-flag";

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

/** v3 (CONTRIB_LOOP.md §17.4): a GitHub-link copy merges `update` (the
 *  author's latest) or `proposal` (the PR head); v2's `return` is gone. */
export type MergePurpose = "needs-merge" | "update" | "proposal" | "own-sync" | "original";

export interface MergeJobView {
  id: string;
  /** `landing`: Keep the merge was pressed and the result is being moved
   *  into the real folder — still in flight, drawn as running. `choice`:
   *  nothing to combine line by line (one side deleted or replaced a file
   *  the other changed) — the panel's sentence names it and its actions
   *  (Keep mine / Use theirs) are the way out; no review is drawn. */
  status: "running" | "landing" | "ready" | "choice" | "failed" | "accepted" | "discarded";
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

/** `engine.status`'s state (§17.2) for a GitHub-link copy; `remix`/`own`
 *  for an own app. */
export type CopyState =
  | "following"
  | "changes"
  | "proposed"
  | "needs-merge"
  | "merged"
  | "declined"
  | "blocked"
  | "other-branch"
  | "merging"
  | "remix"
  | "own"
  // stage B, marketplace copies (§18.4)
  | "delisted"
  | "blocked-release"
  | "soft-blocked";

/** A block-list entry on this copy (§18.4 `record.block`, or a GitHub-link
 *  copy's `block_warn`). Names a release Fused found to be bad. */
export interface CopyBlock {
  level: "hard" | "soft";
  reason: string;
  what_to_do: string;
  /** ISO, when Fused blocked it. */
  at?: string | null;
  commit?: string | null;
  /** The release a hard block rolled the copy back to; null = none left. */
  rolled_back_to?: string | null;
  /** That release's date, already in words ("Oct 26") — `blocks.view`. */
  rolled_back_date?: string | null;
  /** …or as an ISO time, from a server that sends that instead. */
  rolled_back_to_at?: string | null;
  /** The roll-back met the user's own changes: Merge with Claude resolves it. */
  conflict?: boolean;
  /** Every release is blocked: nothing to roll back to. */
  no_target?: boolean;
  /** The user said "open anyway" to a soft block (`blocks.view`)… */
  confirmed?: boolean;
  /** …or when, as epoch seconds. */
  confirmed_at?: number | null;
}

/** A soft block the user has not said yes to yet: opening asks first. */
export function softBlockWaiting(b: CopyBlock | null | undefined): boolean {
  return !!b && b.level === "soft" && !b.confirmed && !b.confirmed_at;
}

/** The reviewed release a marketplace copy runs (§18.4 `record.approved`). */
export interface ApprovedRelease {
  commit: string | null;
  /** ISO date of the release. */
  at: string | null;
  version?: string | null;
  whats_new?: string | null;
}

export interface CopyStatus {
  kind: "own" | "github" | "marketplace" | "scratch";
  /** Marketplace copy: its row's slug and the reviewed release it runs. */
  slug?: string | null;
  approved?: ApprovedRelease | null;
  /** Marketplace copy: "delisted" once the row left the index. */
  listing_status?: "live" | "delisted" | null;
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
  /** The author asked for the proposal to be brought up to date (§16.6). */
  asked_update?: boolean;
  asked?: { author: string; body: string; at: string } | null;
  /** Published directly by a collaborator: reads as merged, no banner. */
  direct?: boolean;
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
  /** Epoch seconds the running review started (for its elapsed clock). */
  review_started_at?: number | null;
  try: TryCopy | null;
  head_sha: string;
  /** ISO timestamp from GitHub. */
  created_at: string;
  /** False: the proposal no longer fits on the app as it is now (GitHub's
   *  own check, cached). Null/absent = not known. */
  fits?: boolean | null;
  /** Merge with Claude for a proposal that no longer fits: its review is
   *  drawn on the item (Keep / Discard via /merge/accept|discard). */
  merge_job?: MergeJobView | null;
}

export interface Contributor {
  login: string | null;
  name: string;
  commits: number;
}

// ---- v2: the Source panel (§16.9) --------------------------------------------
//
// The server decides EVERYTHING the panel says and offers: one sentence, the
// files it is about, and the buttons in order. The page renders what it is
// sent and nothing more — no button is ever derived on this side.

export interface SourcePanel {
  kind: "github" | "marketplace" | "own" | "remix" | "scratch" | "pending" | null;
  /** Marketplace copy: a release Fused blocked is (or was) in this copy. */
  block?: CopyBlock | null;
  /** GitHub-link copy: it runs a version Fused blocked for the Marketplace —
   *  a warning only, nothing was changed (§18.4). */
  block_warn?: CopyBlock | null;
  /** "alice/x". */
  repo: string | null;
  /** The name every sentence uses (the author's login, or the user's own). */
  who: string | null;
  /** Epoch seconds of the last successful check against GitHub; null = never. */
  last_checked: number | null;
  /** For branching only (tone, the settled dismiss); the words are
   *  `sentence`, whole. */
  state: string;
  sentence: string;
  /** App-relative paths the sentence is about. */
  files: string[];
  unproposed: number;
  unpublished: number;
  github_new: number;
  /** App-relative files the Propose dialog offers unticked (never sent
   *  unless the user ticks them); filled only while `propose` is offered. */
  untracked?: string[];
  /** Claude's merge for this copy (github update/proposal, own-sync) — the
   *  same object as `copy.merge_job`; the panel draws its review. */
  merge?: MergeJobView | null;
}

export interface ContribAction {
  id: string;
  label: string;
  primary: boolean;
  /** A sentence to confirm with before running; absent = run on the click. */
  confirm?: string | null;
  danger?: boolean;
  /** The sheet (or flow) the page opens for this button instead of
   *  posting at once: "propose", "remix", "sign-in". */
  dialog?: string | null;
}

export interface ActivityLine {
  id: string;
  /** Epoch seconds. */
  at: number;
  /** "update", "switch" (a trust-level switch, Undo = the opposite
   *  switch), "rollback" (a block rolled the copy back; never Undo), … */
  kind: "update" | "switch" | "rollback" | (string & {});
  text: string;
  /** Something moved away and can come back: offers Undo. */
  undo: boolean;
}

export interface AppContrib {
  flags: ContribFlags;
  copy: CopyStatus;
  proposal: ProposalView | null;
  /** `loading`: the list has never been read from GitHub yet (a cold
   *  cache) — not the same as an empty inbox. `unavailable`: it could not be
   *  read at all. */
  inbox: {
    items: InboxItem[];
    count: number;
    loading?: boolean;
    unavailable?: boolean;
    /** Epoch seconds the list was read from GitHub, when known. */
    listed_at?: number | null;
  } | null;
  contributors: Contributor[] | null;
  /** `checking`: this machine has not asked GitHub yet — "signed out" is
   *  not known, so nothing says "Sign in" on its strength (the sign-in
   *  step checks for itself). */
  github: { signed_in: boolean; login: string | null; checking?: boolean };
  share_url: string | null;
  /** v2 (§16.9). `panel` null (or absent, an older server) = show nothing. */
  panel?: SourcePanel | null;
  actions?: ContribAction[];
  activity?: ActivityLine[];
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
export const acceptMerge = (path: string, job: string) => post<ContribAnswer>("merge/accept", { path, job });
export const discardMerge = (path: string, job: string) => post<ContribAnswer>("merge/discard", { path, job });
export const reviewProposal = (path: string, number: number) =>
  post<JobStarted>("inbox/review", { path, number });
/** `head_sha`: the head of the review the user read (Accept and Try are
 *  pinned to a head). The server refuses with reason "review-needed" and a
 *  plain sentence when the proposal moved since — a yes is for the code on
 *  screen, never for whatever the sender pushed after it. */
export const tryProposal = (path: string, number: number, confirm: boolean, head_sha: string) =>
  post<ContribAnswer & Partial<TryCopy>>("inbox/try", { path, number, confirm, head_sha });
export const removeTry = (path: string, number: number) => post<ContribAnswer>("inbox/try/remove", { path, number });
/** `confirm`: the user accepted a "needs a look" proposal knowingly — the
 *  server refuses one without it (job fails with reason "confirm").
 *  `head_sha`: as for `tryProposal` — the yes is pinned to the head whose
 *  review was on screen. */
export const acceptProposal = (path: string, number: number, confirm: boolean, head_sha: string) =>
  post<JobStarted>("inbox/accept", { path, number, confirm, head_sha });
export const declineProposal = (path: string, number: number, note: string) =>
  post<ContribAnswer>("inbox/decline", { path, number, note });
/** A proposal that no longer fits (`fits: false`): ask its sender to bring
 *  it up to date, with an optional note (§16.6). */
export const askToUpdate = (path: string, number: number, note: string) =>
  post<ContribAnswer>("inbox/ask-update", { path, number, note });
/** …or have Claude merge it onto the app's latest here (§16.6) → a job. */
export const startInboxMerge = (path: string, number: number) => post<JobStarted>("inbox/merge", { path, number });
export const startRemixDraft = (path: string) => post<JobStarted>("remix/draft", { path });

/** v2's one action route (§16.9): every button the server sent, by its id.
 *  `confirm`: the user said yes to the action's confirm sentence. Answers
 *  `{ok, job?}` — a long action hands back a jobs.py job to poll — or a
 *  refusal; `reason:"confirm"` asks for the confirm first. `merge: true`
 *  marks a merge job (Merge with Claude): its progress and review ride on
 *  `copy.merge_job`, not on the job poll. */
export const runAction = (
  path: string,
  action: string,
  opts: { confirm?: boolean; args?: Record<string, unknown> } = {},
) =>
  post<JobStarted & { merge?: boolean }>("action", {
    path,
    action,
    ...(opts.confirm ? { confirm: true } : {}),
    ...(opts.args ? { args: opts.args } : {}),
  });
/** Undo / Restore one activity line (§16.9). */
export const restoreActivity = (path: string, id: string) => post<ContribAnswer>("restore", { path, id });

// ---- stage B: the author's Marketplace listing (§18.6) -----------------------

/** The Source panel's buttons that belong to the listing: rendered by the
 *  ListingStrip, posted to `/api/contrib/listing-action`. */
export const LISTING_ACTION_IDS: ReadonlySet<string> = new Set([
  "publish-market",
  "publish-update",
  "confirm-powers",
  "fix-with-claude",
  "check-listing",
  "remove-market",
]);

export type ListingReviewStatus = "in-review" | "live" | "needs-confirmation" | "failed";

export interface ListingPower {
  kind: string;
  detail: string;
}

export interface Listing {
  listed: boolean;
  slug: string | null;
  /** The app's own fused-app-id (its entry page's), which a listing is
   *  named after; null when the app has none yet (App Doctor adds one). */
  app_id?: string | null;
  /** False: listed under an older id than `app_id` (see `oldIdNote`). */
  slug_is_app_id?: boolean | null;
  /** The app's display name the sentences use. */
  name?: string | null;
  approved: { commit: string | null; at: string | null } | null;
  pr: { number: number; url: string; head_sha: string; state: string } | null;
  review: {
    status: ListingReviewStatus | null;
    reasons: string[];
    new_powers: ListingPower[];
    whats_new: string | null;
  } | null;
  update_available: boolean;
  /** App Doctor's sharing checks are clear — publishing needs it. */
  doctor_clear: boolean;
  /** The app has edits not on GitHub yet — publishing needs them pushed. */
  unpushed: boolean;
}

/** GET /api/contrib/listing. `sentence` is the server's status line
 *  (`actions_listing.sentence`); `actions` its buttons when it sends them
 *  (else the page takes the listing ids from `/api/contrib/app`'s). */
export interface ListingAnswer extends Partial<ContribAnswer> {
  listing?: Listing | null;
  /** `actions_listing.decide`'s state: "pending", "unknown", "signed-out",
   *  "not-listed", "in-review", "needs-confirmation", "failed", "live". */
  state?: string | null;
  sentence?: string | null;
  actions?: ContribAction[];
  /** The listing is being re-read from GitHub: poll this job, then GET
   *  again (§16.14's named wait). */
  job?: string | null;
  /** The live release's version, when the ladder's copy of this answer
   *  carries one (§19.2). */
  version?: string | null;
}

export interface ListingCategory {
  id: string;
  label: string;
}

/** GET /api/contrib/listing-draft: the Publish dialog's prefill. */
export interface ListingDraft extends Partial<ContribAnswer> {
  /** The live row's slug when listed — NOT the listing id to show. */
  slug?: string | null;
  /** The listing id: the entry page's fused-app-id; null = none yet. */
  app_id?: string | null;
  slug_is_app_id?: boolean | null;
  name?: string;
  description?: string;
  category?: string;
  categories?: (ListingCategory | string)[];
  tags?: string[];
  icon?: string | null;
  preview?: string | null;
}

/** What `publish-market` sends as its `args`. No slug: the listing is named
 *  after the app's fused-app-id, which the server derives. */
export interface ListingDetails {
  name: string;
  description: string;
  category: string;
  tags: string[];
  icon: string | null;
  preview: string | null;
}

export function getListing(path: string, opts: { fresh?: boolean; signal?: AbortSignal } = {}): Promise<ListingAnswer> {
  return getJson<ListingAnswer>(
    `/api/contrib/listing?path=${encodeURIComponent(path)}${opts.fresh ? "&fresh=1" : ""}`,
    { signal: opts.signal },
  ).catch(() => ({ ok: false }) as ListingAnswer);
}

export function getListingDraft(path: string): Promise<ListingDraft> {
  return getJson<ListingDraft>(`/api/contrib/listing-draft?path=${encodeURIComponent(path)}`).catch(
    () => ({ ok: false }) as ListingDraft,
  );
}

/** An app listed under an older id than its own fused-app-id: the one line
 *  saying how to move it (a refusal "old-slug" says the same in the
 *  server's words). Null when there is nothing to say. */
export function oldIdNote(
  v: { slug?: string | null; app_id?: string | null; slug_is_app_id?: boolean | null } | null | undefined,
): string | null {
  if (!v || v.slug_is_app_id !== false || !v.slug || !v.app_id) return null;
  return `Listed under the old id ${v.slug}. Remove it, then publish again to move it to ${v.app_id}.`;
}

/** A publish refusal that App Doctor fixes: its checks are not clear
 *  ("doctor"), or the app has no fused-app-id yet ("app-id") — the page
 *  offers Open App Doctor beside the server's sentence. */
export function opensDoctor(reason: string | null | undefined): boolean {
  return reason === "doctor" || reason === "app-id";
}

/** The listing's one action route — same body and answer as `runAction`.
 *  `fix-with-claude` answers `open`: where the new Claude session is. */
export const runListingAction = (
  path: string,
  action: string,
  opts: { confirm?: boolean; args?: Record<string, unknown> } = {},
) =>
  post<JobStarted & { open?: string }>("listing-action", {
    path,
    action,
    ...(opts.confirm ? { confirm: true } : {}),
    ...(opts.args ? { args: opts.args } : {}),
  });

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
  const moving = (s: string | undefined) => s === "running" || s === "landing";
  if (moving(c.copy?.merge_job?.status) || moving(c.panel?.merge?.status)) return true;
  if (c.panel?.state === "pending") return true;
  if (c.inbox?.loading) return true;
  return !!c.inbox?.items.some(
    (i) =>
      i.review_status === "none" ||
      i.review_status === "pending" ||
      i.review_status === "running" ||
      moving(i.merge_job?.status),
  );
}

function anyFlag(f: ContribFlags): boolean {
  return f.sources || f.changes || f.inbox || f.loop || f.remix;
}

/** Whether `useAppContrib` reads at all: any contrib switch, the Marketplace
 *  switch, or `force` — a folder the caller already knows is a Marketplace
 *  copy. Such a copy's block is enforced whatever the switches say (§18.4),
 *  so the page must learn whether opening it asks first even with every
 *  switch off. */
export function contribReadEnabled(
  dir: string | null,
  flags: ContribFlags | null,
  market: boolean | null,
  force: boolean,
): boolean {
  return !!dir && ((flags !== null && anyFlag(flags)) || market === true || force);
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
export function useAppContrib(dir: string | null, opts: { marketplace?: boolean } = {}): AppContribState {
  const flags = useContribFlags();
  // Stage B (§18.8): a Marketplace copy's panel, buttons and block strips
  // answer with the Marketplace switch on too, Community flags or not; the
  // server answers `{ok:false}` for any other copy then, and nothing shows.
  // `marketplace`: the caller knows this folder is a Marketplace copy (the
  // ladder's `kind`, or the open gate's 423) — read whatever the switches
  // say, since its block asks before it opens whatever they say.
  const market = useMarketplaceFeature();
  const force = !!opts.marketplace;
  const enabled = contribReadEnabled(dir, flags, market, force);
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
  const known = flags !== null || market === true || force;
  return {
    data: enabled ? data : null,
    settled: known && settled,
    // Every flag off is a real answer too: there is no inbox to offer.
    answered: known && (!enabled || answered),
    refresh,
  };
}

// ---- the ladder (§19.1/§19.2) -------------------------------------------------

/** GET /api/contrib/ladder: where an app the user owns stands — rung 1 on
 *  this Mac, 2 on GitHub, 3 in the Marketplace. `mine:false` (not an app, or
 *  a copy of someone else's) skips the ladder; the other-side view reads
 *  `/api/contrib/app` instead. Answers whatever the Community switches say:
 *  the switches ride in `flags`. */
export interface LadderAnswer {
  ok: boolean;
  reason?: string;
  message?: string;
  /** Null when `mine` is false (no ladder for someone else's app). */
  rung: 1 | 2 | 3 | null;
  kind: string | null;
  mine: boolean;
  /** Who owns the repo is still being asked: read again shortly. */
  pending?: boolean;
  remix_of?: string | null;
  github: {
    installed: boolean;
    signed_in: boolean;
    /** This machine has not asked GitHub who is signed in yet. */
    checking?: boolean;
    login: string | null;
    repo: string | null;
    /** Null while unknown (asked in the background). */
    visibility: "public" | "private" | null;
    unpushed: number;
    link: string | null;
    /** Rung 1: the sentence Put it on GitHub would refuse with (nested in a
     *  bigger project, a remote already there), else null. */
    blocked?: string | null;
  };
  /** The §18.7 listing answer, or null. */
  listing: ListingAnswer | null;
  /** `clear` null: Doctor could not say (not on GitHub yet, or it failed). */
  doctor: { clear: boolean | null; failing: number | null };
  /** `github_link`: an older server's copy of the install-link switch, now
   *  folded into `sources` — not read. */
  flags: { sources: boolean; marketplace: boolean; github_link?: boolean };
}

export function getLadder(dir: string, opts?: { fresh?: boolean }): Promise<LadderAnswer> {
  return getJson<LadderAnswer>(
    `/api/contrib/ladder?dir=${encodeURIComponent(dir)}${opts?.fresh ? "&fresh=1" : ""}`,
  ).catch(() => ({ ok: false, reason: "failed" }) as LadderAnswer);
}

const LADDER_PENDING_MS = 5_000;
const LADDER_PENDING_TRIES = 12;

export interface LadderState {
  /** The last answer for THIS folder (`ok:false` included), or null while
   *  the first read is out. */
  data: LadderAnswer | null;
  /** Re-read now (fresh). Resolves when the answer is painted. */
  refresh: () => Promise<void>;
}

/** The ladder for `dir`: read on mount, on window focus and whenever a caller
 *  says `refresh()` (after every action that can move a rung). Never cached
 *  across folders — a folder switch starts over (§19.1: read live). */
export function useLadder(dir: string | null): LadderState {
  const [data, setData] = useState<LadderAnswer | null>(null);
  const genRef = useRef(0);
  const dirRef = useRef(dir);
  dirRef.current = dir;

  const read = useCallback(async (fresh = false) => {
    const target = dirRef.current;
    if (!target) return;
    const gen = ++genRef.current;
    const next = await getLadder(target, { fresh });
    if (genRef.current !== gen || dirRef.current !== target) return;
    setData(next);
  }, []);

  useEffect(() => {
    genRef.current += 1;
    setData(null);
    if (dir) void read();
  }, [dir, read]);

  useEffect(() => {
    if (!dir) return;
    const onFocus = () => void read();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [dir, read]);

  // Ownership still being asked (`pending`): read again shortly, a bounded
  // number of times — the answer moves on by itself on the server.
  const pending = !!data?.pending;
  const triesRef = useRef(0);
  useEffect(() => {
    triesRef.current = 0;
  }, [dir]);
  useEffect(() => {
    if (!pending || triesRef.current >= LADDER_PENDING_TRIES) return;
    const t = window.setTimeout(() => {
      triesRef.current += 1;
      void read();
    }, LADDER_PENDING_MS);
    return () => window.clearTimeout(t);
  }, [pending, data, read]);

  // A fresh read may start the listing's GitHub check (`listing.job`) and
  // answer before it ends: wait for that job, then read once more — its
  // outcome can move the rung (§19.2: the server never waits on GitHub).
  const listingJob = data?.listing?.job ?? null;
  useEffect(() => {
    if (!listingJob) return;
    const ctl = new AbortController();
    pollJob(listingJob, { signal: ctl.signal })
      .then(() => {
        if (!ctl.signal.aborted) void read();
      })
      .catch(() => {
        /* unmounted, or the job could not be read: the next focus re-reads */
      });
    return () => ctl.abort();
  }, [listingJob, read]);

  const refresh = useCallback(() => read(true), [read]);
  return { data, refresh };
}

/** The ladder's listing as a page reads it: the bare `Listing`, the server's
 *  status sentence, and the version rung 3 names ("Live · v1.2.0") when the
 *  answer carries one (`approved.version`, else a top-level `version`).
 *  Tolerates a bare `Listing` in place of the §18.7 answer. */
export function ladderListing(answer: LadderAnswer["listing"] | undefined): {
  listing: Listing | null;
  sentence: string | null;
  version: string | null;
} {
  if (!answer || typeof answer !== "object") return { listing: null, sentence: null, version: null };
  const wrapped = "listing" in answer || "sentence" in answer || "state" in answer;
  const listing = (wrapped ? answer.listing : (answer as unknown as Listing)) ?? null;
  const str = (v: unknown) => (typeof v === "string" && v.trim() ? v : null);
  const version =
    str((listing?.approved as { version?: unknown } | null | undefined)?.version) ??
    str((listing as { version?: unknown } | null)?.version) ??
    str((answer as { version?: unknown }).version);
  return { listing, sentence: wrapped ? str(answer.sentence) : null, version };
}

/** POST /api/contrib/publish-github (§19.2): rung 1 → 2 in one call — `git
 *  init`, identity and a first commit when needed, then github_setup's
 *  publish. Answers the publish record; poll `GET /api/github/publish`
 *  (github-setup.ts) until it settles. NOT through `post()` above: a refusal
 *  is a 409 whose `error` sentence ("lives inside a bigger project", "already
 *  has a remote") is shown as-is, so it must throw with that message rather
 *  than be swallowed into the generic one. */
export function publishGithub(
  dir: string,
  body: { name: string; visibility: "public" | "private" },
): Promise<SetupRecord> {
  return postJson<SetupRecord>("/api/contrib/publish-github", { dir, ...body });
}

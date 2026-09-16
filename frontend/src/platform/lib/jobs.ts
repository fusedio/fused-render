// Reading the background-job registry (fused_render/jobs.py) — the model the
// download manager draws (SPEC §36, D244).
//
// A page reports long-running work through `fused.trackJob()` in the injected
// runtime; the record lives on the server so it outlives the reporting
// document, and the shell reads it back here. Everything the manager decides
// from a record — how far along it is, what to call it, whether it can still be
// cancelled — lives in this module as a pure function with a test, rather than
// inline in the component, because those are the parts that are wrong in ways a
// screenshot doesn't show (a bar at 0% because `total` was absent rather than
// zero; a row that says "stalled" for work that finished).
import { getJson, postJson } from "@platform/lib/api";

// "waiting" — the two NON-terminal states are "running" and "waiting". Work
// has stopped and is not coming back on its own: it is sitting on a QUESTION
// only the user can answer (today, the sole producer is
// `envinstall._mirror_into_jobs`'s `needs_build` branch: uv's "Install
// anyway" compile prompt). Not "running" — nothing is actually in flight, so
// a bar or a spinner would lie. Not terminal either: none of "done" / "error"
// / "cancelled" means "stopped, waiting on you" (see `fused_render/jobs.py`'s
// own state-machine comment for the fuller reasoning).
export type JobState = "running" | "waiting" | "done" | "error" | "cancelled";
export type JobKind = "download" | "task";
// Who is running the work, which decides what ✕ can do (SPEC BG-4). "page" —
// only the page knows what stopping means, so cancel is a request it honours.
// "server" — this app owns the process and really stops it.
export type JobOwner = "page" | "server";

// Which of the four notification tiers a row belongs to (SPEC
// actionable-notifications). `tier` governs RETENTION, and, for "silent"
// alone, whether the row pops a card in the floating column at all
// (`popupJobs`/`popupTick`, `platform/ui/JobPopupCard.tsx`) — every other
// tier still pops on its way to terminal, `transient` included; what a
// non-silent tier decides is what happens AFTER that pop:
//   "attention"  — kept in the panel until dismissed, and drawn there
//                  without the panel having to be opened.
//   "trail"      — kept in the panel until dismissed.
//   "transient"  — kept nowhere; its card is the only trace it leaves.
//   "silent"     — kept nowhere AND pops nothing: a producer declares this
//                  when finishing is not news (a resident model load/unload
//                  is the shipped example — the running row was already
//                  visible, and turning "done" says nothing new). Silence
//                  is a property of SUCCESS only — `effectiveTier` still
//                  promotes an `error`/`cancelled` row to "attention"
//                  regardless of the declared tier, so a silent job that
//                  fails is always news.
// "trail" is the default on the server (`fused_render/jobs.py`'s `Job.tier`)
// on purpose: a producer that sets nothing behaves exactly like every row
// did before this field existed.
export type JobTier = "attention" | "trail" | "transient" | "silent";

export interface Job {
  id: string;
  title: string;
  detail: string;
  // The model running this row, as a dimmed suffix on the TITLE — never
  // folded into title or detail, because detail is a worker's progress
  // ticks' own line and a model name concatenated there would get
  // overwritten by the next tick. "" means no model to show (a download, a
  // scheduled run, a page's own `fused.trackJob()`) and JobRow renders
  // nothing for it, not an empty element.
  model: string;
  kind: JobKind;
  state: JobState;
  // null means "no number to show": an indeterminate bar, not zero progress.
  done: number | null;
  total: number | null;
  // Whether `total` prices the WHOLE download or only the phase currently in
  // flight (SPEC AI-5n, D498). "phase" — the default every plain reporter has
  // always sent without knowing it, correct as-is for a single-repo download.
  // "download" — an explicit claim only a multi-phase reporter
  // (`worker_base.download_plan`) is entitled to make. `shared/modelSize.ts`
  // is the one place this decides anything: a "download" total may win
  // outright over the catalog's constant; a "phase" total may only ever
  // raise it (never-understate).
  total_scope: "download" | "phase";
  // Whether `total` is a genuine count or a stand-in guess (today only the
  // index-scan bridge sets this — the last scan's row count, standing in for
  // a walk still in progress). Rendered as a qualifier next to the AMOUNT,
  // not folded into `jobAmount` itself — see that call site in
  // DownloadManager.tsx — so no other job kind grows an "(estimated)" suffix
  // just by existing (D733).
  total_estimated: boolean;
  unit: string; // "bytes" | "s" | "" — decides how done/total are formatted
  message: string; // the error text when state is "error"; the question's caption when state is "waiting"
  // Where clicking this row goes, once it lands in Notifications — an
  // absolute fs path (the .html that raised it, or a server producer's own
  // repo root/output folder) OR one of a handful of shell routes a few
  // server producers name directly (router.ts's `navigateToJobPage` is the
  // one place that tells the two shapes apart and turns either into a real
  // navigation). Empty for a job no destination has been given yet.
  page: string;
  // A short, human-readable label naming WHAT RAISED this job — "Playground",
  // "Local models", "Benchmark", "Explorer", "Claude setup", "GitHub",
  // "Scheduler", "App install". Deliberately NOT `page` and never derived
  // from it: `page` answers "where does clicking this row go", `origin`
  // answers "who asked for this" — a Playground render's `page` is its own
  // output file, while its `origin` stays "Playground", and the two move
  // independently (a scheduled run's `page` can point at its own output
  // while its `origin` stays "Scheduler"). "" when no producer named one —
  // JobRow renders no caption at all for it, never an empty placeholder.
  origin: string;
  owner: JobOwner;
  cancellable: boolean;
  cancel_requested: boolean;
  started_at: number;
  updated_at: number;
  finished_at: number | null;
  // Server-computed: running, but nothing has reported in a while. The reporter
  // is gone (its page was closed); the work may well be carrying on.
  stalled: boolean;
  // The id of another row this row is blocked on, or "" for the ordinary
  // case — set server-side while an image/video render waits on a shared
  // model load (`fused_render/ai/supervisor.py` `_wait_ready`'s merge). See
  // `mergedRows` below for what the manager does with it.
  waiting_for: string;
  // Which of the four tiers this row belongs to (see `JobTier` above) —
  // chosen by the PRODUCER, server-side only. Sticky across ticks on one id
  // like every other field: `job_id_for(model)` (`ai/supervisor.py`) is
  // shared by a resident load, a weights-only download and an unload, so
  // each of those reports restates its own tier explicitly rather than
  // relying on what an earlier report on that id left behind. Read this
  // through `effectiveTier` below, not directly — a terminal row's actual
  // tier can differ from what its producer declared.
  tier: JobTier;
  // §3 (SPEC-quiet-notifications.md): the client-side grouping key. Mirrors
  // `fused_render/jobs.py`'s `Job.group` exactly, including its default:
  // defaulted server-side, ONCE, at creation, from the id's own
  // `sys:<name>:` prefix when it has one, else the whole id. The
  // whole-id fallback is what makes an ungrouped job's own id its own
  // group of exactly one member — by construction, not by a client-side
  // special case — which is why "a lone job renders and behaves exactly as
  // today" holds without this file ever having to check "is this job even
  // grouped at all". See `groupJobs` below for the client-side grouping
  // this field feeds.
  group: string;
}

export interface JobsSnapshot {
  jobs: Job[];
  // The SERVER's clock at the moment of the read. Ages are measured against
  // this, never against the browser's Date.now(): the two disagree after a tab
  // throttle or a suspend, and the visible symptom is a job that finished "in
  // 3 seconds' time".
  now: number;
}

export function fetchJobs(signal?: AbortSignal): Promise<JobsSnapshot> {
  return getJson<JobsSnapshot>("/api/jobs", { signal });
}

export function cancelJob(id: string): Promise<Job> {
  return postJson<Job>(`/api/jobs/${encodeURIComponent(id)}/cancel`, {});
}

export function dismissJob(id: string): Promise<{ dismissed: string }> {
  return postJson<{ dismissed: string }>(`/api/jobs/${encodeURIComponent(id)}/dismiss`, {});
}

export function clearFinishedJobs(): Promise<{ cleared: number }> {
  return postJson<{ cleared: number }>("/api/jobs/clear", {});
}

// The cross-document nudge runtime.js writes when it reports (see its
// JOB_PING_KEY comment). Keep the two spellings in step — tests/test_jobs_api.py
// pins them together.
export const JOB_PING_KEY = "fused-render:jobs-ping";

export function isRunning(job: Job): boolean {
  return job.state === "running";
}

/**
 * A job that has stopped and is not coming back — `done`, `error` and
 * `cancelled` are one question, because Notifications draws all three the
 * same way and Activity loses all three the same tick they land there.
 */
export function isTerminal(job: Job): boolean {
  return job.state === "done" || job.state === "error" || job.state === "cancelled";
}

/** Every job that has finished, one way or another — what Notifications draws
 *  alongside its repo rows. */
export function terminalJobs(jobs: Job[]): Job[] {
  return jobs.filter(isTerminal);
}

/** What the Jobs section draws: work still in progress, i.e. not terminal.
 *  `waiting` stays in (a row parked on a question the user has not answered
 *  yet is not finished — losing it here would make it vanish everywhere,
 *  since it is not terminal either and so `terminalJobs` would not pick it
 *  up); every terminal state leaves in the same tick it reaches
 *  Notifications. */
export function inFlightJobs(jobs: Job[]): Job[] {
  return jobs.filter((j) => !isTerminal(j));
}

/** Server-owned jobs (a pull, a load) keyed by the model id the caller's own
 *  card matches on (`title`, which the supervisor sets to the model id, so a
 *  card never re-derives it) — for a card to ask "is there a job for me" and
 *  get back an ANSWER, not just a fact about history.
 *
 *  D663 keeps a finished job's row until it is dismissed rather than
 *  sweeping it a few seconds after its first read, so a map built from every
 *  `server` row regardless of state stayed non-null for a model for the rest
 *  of the session once its pull or load finished — every consumer that
 *  gated on PRESENCE (`RepoCard.tsx`'s `!!job` disables and
 *  "Downloading…"/"Loading…" labels, `PlaygroundTab.tsx`'s `jobForSelected`)
 *  read that as "still busy" forever, active again the moment anything else
 *  on the page was, which is exactly when this page polls (`isBusy`,
 *  `aiRuntime.ts`). Filtering here — the one place this map is built —
 *  means presence in it means what it always should have: an active job,
 *  matching this file's own `inFlightJobs` for the identical reason (Part A
 *  item 1 / C3 fix). */
export function activeJobByModel(jobs: Job[]): Map<string, Job> {
  return new Map(
    jobs.filter((j) => j.owner === "server" && !isTerminal(j)).map((j) => [j.title, j]),
  );
}

/** The jobs list after a Clear — every row Clear would NOT take, i.e. every
 *  `running` row, stalled included. Used to optimistically patch the local
 *  list the instant the server confirms a clear, without waiting for the
 *  next poll. */
export function jobsAfterClear(jobs: Job[]): Job[] {
  return jobs.filter(isRunning);
}

// A scheduled message's job row, by id (fused_render/schedule.py `_JOB_PREFIX`).
export const SCHEDULE_JOB_PREFIX = "sys:schedule:";

// A model load's own row, by id (fused_render/ai/supervisor.py `job_id_for`).
export const AI_MODEL_JOB_PREFIX = "sys:ai-model:";

/** The tier a reader should actually treat this row as — DERIVED, never
 *  stored. `job.tier` is what the producer declared; this is what the row
 *  means right now.
 *
 *  The one override: a terminal job in `error` or `cancelled` is always
 *  `attention`, regardless of what its producer declared. A failed run is
 *  news even for a producer that otherwise declares itself `transient` (a
 *  scheduled run, an index scan, a text generation) or `silent` (a resident
 *  model load/unload) — the thing that makes those tiers correct on SUCCESS
 *  (nothing survives it, or nothing about finishing is news) is exactly what
 *  is no longer true on a failure: the user did not get what they asked for,
 *  which is always worth a look. A `done` row, or a still-running one, is
 *  unaffected and reads its stored tier as-is.
 *
 *  Mirrors `effective_tier` in `fused_render/jobs.py` — keep the two in
 *  step. */
export function effectiveTier(job: Job): JobTier {
  if (job.state === "error" || job.state === "cancelled") return "attention";
  return job.tier;
}

/** Which jobs get a row of their own in Activity: every job the registry
 *  knows about, except:
 *  - a scheduled message's own row, in ANY state (D661: "a task is not
 *    something I even want in the activity" — an explicit product decision,
 *    not a consequence of its declared tier, so it is checked by id prefix
 *    rather than by `effectiveTier` alone).
 *  - a TERMINAL job whose `effectiveTier` is "transient" or "silent" — a
 *    finished index scan, a finished text generation, a resident model
 *    load/unload, none of which leave anything to act on.
 *  A transient/silent row that is still `running` is otherwise unaffected —
 *  `tier` only ever governs RETENTION (and, for "silent", popping) of a
 *  TERMINAL row (`JobTier` above), so a running index scan, text generation
 *  or model load still gets a row here regardless of its declared tier,
 *  exactly what Activity's Cancel control needs to reach. Reading
 *  `effectiveTier` rather than the stored `tier` matters here: a producer
 *  that declared itself transient/silent but ended in `error`/`cancelled`
 *  still gets a row, because the override already turned it into
 *  `attention`. */
/** D-A / §2b (SPEC-quiet-notifications.md): a successful job whose own page
 *  the user is already looking at is not news — it goes to the folded
 *  "Recent" section (§4) instead of popping or holding a seat in "Needs
 *  you"/"Worth keeping". Suppression is client-side, at DISPLAY time only —
 *  the server learns nothing about presence and stays authoritative about
 *  the row's existence (a dismiss, a real re-check, still works normally).
 *
 *  Gated on `state === "done"` specifically, not `isTerminal`: `error` and
 *  `cancelled` are promoted to "attention" by `effectiveTier` before this
 *  check ever runs (verified by reading `effectiveTier` above, not assumed),
 *  so a failure already fails the `!== "attention"` test and is never
 *  suppressed — this condition is written the long way on purpose so that
 *  stays visibly true rather than relying on it as an accident of `done`-only
 *  gating. */
export function isRecentOnly(job: Job, isOpenAnywhere: (source: string) => boolean): boolean {
  if (job.state !== "done") return false;
  if (effectiveTier(job) === "attention") return false;
  return isOpenAnywhere(job.page);
}

// ------------------------------------------------------------------ §3 grouping
//
// SPEC-quiet-notifications.md §3. `Job.group` (above) is defaulted server-side
// to a group of exactly one (the job's own id) for anything with no
// `sys:<name>:` family prefix, which is what makes "a lone job renders and
// behaves exactly as today" true by construction — the functions below never
// special-case group size 1, and a regression test pins that it doesn't need
// to.

/** Finding 3 (code review, quiet-notifications): the server's `group` field
 *  is a FAMILY key (`sys:ai-image:<id>` -> `sys:ai-image`), and terminal rows
 *  are kept until dismissed (D663) - so grouping purely by `(page, group)`
 *  folds a page's ENTIRE history for that family into one group. Once a
 *  second model download has ever existed on a page, every future download
 *  in that family joins the same permanent group: it never pops on its own
 *  completion again (`popupJobs` excludes any multi-member group), an old
 *  failure pins the group in "Needs you" forever, and the subline counts
 *  jobs from hours ago ("17 of 18 done").
 *
 *  THE FIX: a group must mean one BURST of work, not one family of work.
 *  Within a `(page, group)` family, jobs are further split into CLUSTERS by
 *  activity gap - sort the family by `started_at`, walk in order, and start
 *  a new cluster whenever a job's `started_at` is more than `GROUP_GAP_MS`
 *  past the latest `finished_at ?? started_at` seen so far in the CURRENT
 *  cluster. The final grouping key is `(page, family, cluster)`, not just
 *  `(page, family)` - so a burst from hours ago can never absorb a job that
 *  starts today, no matter how many bursts came before it in the same
 *  family. See `clusterFamily` below for the walk itself.
 *
 *  DO NOT "simplify" this back to `(page, group)` - that is precisely the
 *  bug this fix exists to close (findings 3/3a/3b/3c, code review
 *  2026-09-16). See DECISIONS-quiet-notifications.md for the full writeup. */
export const GROUP_GAP_MS = 2 * 60 * 1000;

/** One group of jobs sharing the same `(page, group)` FAMILY *and* burst
 *  cluster - the unit a group row is judged and rendered as. `jobs`
 *  preserves the snapshot's own arrival order. `key` is the full
 *  cluster-scoped identity (family plus which burst) - use it, not
 *  `${page} ${group}` alone, anywhere a stable per-burst identity is needed
 *  (React list keys, `groupPopupTick`'s own tracked-group keys): two
 *  different bursts of the same family share `page`/`group` but must never
 *  be treated as the same group. */
export interface JobGroup {
  page: string;
  group: string;
  key: string;
  jobs: Job[];
}

function familyKey(job: Job): string {
  return `${job.page} ${job.group}`;
}

/** Split one `(page, group)` family into burst clusters - see `GROUP_GAP_MS`
 *  above for the rule. Returns each member's cluster index, computed off a
 *  copy sorted by `started_at` (the family's own arrival order, preserved by
 *  the caller, is not necessarily start order - a shorter job can be
 *  reported after a longer one that started first). */
function clusterFamily(members: readonly Job[]): Map<string, number> {
  const sorted = [...members].sort((a, b) => a.started_at - b.started_at);
  const clusterOf = new Map<string, number>();
  let clusterIdx = -1;
  let latestActivity = -Infinity; // latest finished_at ?? started_at seen so far, THIS cluster only
  for (const j of sorted) {
    if (clusterIdx === -1 || j.started_at - latestActivity > GROUP_GAP_MS) {
      clusterIdx += 1;
      latestActivity = -Infinity;
    }
    clusterOf.set(j.id, clusterIdx);
    const activity = j.finished_at ?? j.started_at;
    if (activity > latestActivity) latestActivity = activity;
  }
  return clusterOf;
}

/** Group a job snapshot by `(page, group, cluster)`, preserving first-seen
 *  order - see `GROUP_GAP_MS`'s doc comment above for why a family alone is
 *  not the unit.
 *
 *  THIS RUNS BEFORE CLASSIFICATION, ON PURPOSE - the resolved design
 *  question this branch inherited from an earlier handoff. `jobRows`/
 *  `recentJobs` judge a GROUP's fate as a whole (every member must satisfy
 *  the suppression condition, or the whole group stays visible), not each
 *  member independently and then folded after the fact: classifying members
 *  first and grouping after can double-count a group across "Worth keeping"
 *  and "Recent" (a group split by presence lands partly in each), or drop it
 *  from both (neither half's own filter recognizes the other half kept it
 *  alive). Grouping first and then asking "does this whole group satisfy the
 *  condition" is the only order that keeps a group in exactly one place. */
export function groupJobs(jobs: readonly Job[]): JobGroup[] {
  const families = new Map<string, Job[]>();
  for (const j of jobs) {
    const fk = familyKey(j);
    let arr = families.get(fk);
    if (!arr) {
      arr = [];
      families.set(fk, arr);
    }
    arr.push(j);
  }
  const clustersByFamily = new Map<string, Map<string, number>>();

  const byKey = new Map<string, JobGroup>();
  const order: JobGroup[] = [];
  for (const j of jobs) {
    const fk = familyKey(j);
    let clusterOf = clustersByFamily.get(fk);
    if (!clusterOf) {
      clusterOf = clusterFamily(families.get(fk) ?? []);
      clustersByFamily.set(fk, clusterOf);
    }
    const cluster = clusterOf.get(j.id) ?? 0;
    const key = `${fk}#${cluster}`;
    let g = byKey.get(key);
    if (!g) {
      g = { page: j.page, group: j.group, key, jobs: [] };
      byKey.set(key, g);
      order.push(g);
    }
    g.jobs.push(j);
  }
  return order;
}

/** A group is fully terminal only once EVERY member is — one member still
 *  running or waiting keeps the whole group "in flight", however many of
 *  its siblings have already finished. */
export function isGroupTerminal(members: readonly Job[]): boolean {
  return members.every(isTerminal);
}

/** §2b composed with §3: a group is suppressed only when it is fully
 *  terminal AND every member individually satisfies `isRecentOnly` — spec's
 *  own words, "a group row is suppressed under §2b when every member
 *  satisfies the suppression condition. One failing member keeps the whole
 *  group visible." A member still running fails `isRecentOnly` on its own
 *  (it isn't `state === "done"`), so a group with any in-flight member is
 *  never suppressed by this — consistent with "in flight" never being quiet. */
export function isGroupRecentOnly(
  members: readonly Job[],
  isOpenAnywhere: (source: string) => boolean,
): boolean {
  return isGroupTerminal(members) && members.every((j) => isRecentOnly(j, isOpenAnywhere));
}

/** The tier a GROUP reads as, extending `effectiveTier`'s per-job rule: one
 *  member in `error`/`cancelled` (i.e. `effectiveTier(j) === "attention"`)
 *  promotes the whole row, the same way a single failing job is always news
 *  regardless of what it declared. Absent any attention member, the group
 *  takes the "loudest" tier present among the rest — `trail` over
 *  `transient` over `silent` — so a group mixing a kept-tier member with a
 *  quieter one still earns the lasting row its kept member would have gotten
 *  alone. */
export function groupEffectiveTier(members: readonly Job[]): JobTier {
  if (members.some((j) => effectiveTier(j) === "attention")) return "attention";
  if (members.some((j) => effectiveTier(j) === "trail")) return "trail";
  if (members.some((j) => effectiveTier(j) === "transient")) return "transient";
  return "silent";
}

/** Index every job in a snapshot by the `JobGroup` it belongs to — the one
 *  lookup `jobRows`/`recentJobs`/the popup pipeline all need to judge a
 *  member's fate by its GROUP's verdict rather than its own. */
function indexGroups(jobs: readonly Job[]): Map<string, JobGroup> {
  const byId = new Map<string, JobGroup>();
  for (const g of groupJobs(jobs)) {
    for (const j of g.jobs) byId.set(j.id, g);
  }
  return byId;
}

export function jobRows(jobs: Job[], isOpenAnywhere?: (source: string) => boolean): Job[] {
  const groupById = indexGroups(jobs);
  return jobs.filter((j) => {
    if (j.id.startsWith(SCHEDULE_JOB_PREFIX)) return false;
    if (!isTerminal(j)) return true;
    if (effectiveTier(j) === "transient" || effectiveTier(j) === "silent") return false;
    if (isOpenAnywhere) {
      const g = groupById.get(j.id);
      if (g && isGroupRecentOnly(g.jobs, isOpenAnywhere)) return false;
    }
    return true;
  });
}

/** §4 (SPEC-quiet-notifications.md): the exact complement of `jobRows`'s own
 *  presence-based exclusion — every job `jobRows` drops BECAUSE
 *  `isRecentOnly` said so, and nothing else. A schedule job (D661) or a
 *  transient/silent terminal job is excluded from `jobRows` for reasons that
 *  have nothing to do with presence, so this function excludes them too,
 *  rather than letting them leak into Recent the moment their page happens
 *  to be open. Together, `jobRows(jobs, isOpenAnywhere)` and
 *  `recentJobs(jobs, isOpenAnywhere)` partition every terminal job that
 *  isn't schedule/transient/silent into exactly one of "still shown" or
 *  "folded into Recent" — never both, never neither (pinned by
 *  `jobs.test.ts`'s partition test).
 *
 *  A row landing here is not gone: `isRecentOnly` fires client-side, at read
 *  time, from a live fact about where the user currently is — the very next
 *  read (a page navigation away, the presence entry going stale) moves the
 *  SAME job straight into `jobRows`'s output instead, with nothing
 *  server-side to reconcile. */
export function recentJobs(jobs: Job[], isOpenAnywhere: (source: string) => boolean): Job[] {
  const groupById = indexGroups(jobs);
  return jobs.filter((j) => {
    if (j.id.startsWith(SCHEDULE_JOB_PREFIX)) return false;
    if (!isTerminal(j)) return false;
    if (effectiveTier(j) === "transient" || effectiveTier(j) === "silent") return false;
    const g = groupById.get(j.id);
    return g ? isGroupRecentOnly(g.jobs, isOpenAnywhere) : isRecentOnly(j, isOpenAnywhere);
  });
}

export function mergedRows(jobs: Job[]): Job[] {
  const hidden = new Set(
    jobs.filter((j) => j.waiting_for && isRunning(j)).map((j) => j.waiting_for),
  );
  return jobs.filter((j) => !hidden.has(j.id));
}

/** What actually reaches Notifications from a full job snapshot —
 *  `ActivityDock.tsx`'s `onJobsReported`, pulled out here so the composition
 *  itself has a test rather than only a rendered component to poke at.
 *
 *  `mergedRows` runs FIRST, on the FULL snapshot, exactly like
 *  `DownloadManagerView`'s own `jobs` computation
 *  (`inFlightJobs(jobRows(mergedRows(reported)))`) — it needs the
 *  REFERENCING row (the waiter, `waiting_for`-tagged) still present to
 *  decide whether the row it names is hidden. Skipping it here let a render
 *  waiting on a shared model load draw ONE row in Activity while its two
 *  underlying jobs drew TWO in Notifications: the load goes terminal a tick
 *  before the waiter notices and clears its own `waiting_for`
 *  (`_wait_ready`'s poll loop), so a poll landing in that gap saw the load
 *  as terminal while Activity's own merged view still had it hidden. */
export function terminalNotifications(
  jobs: Job[],
  isOpenAnywhere?: (source: string) => boolean,
): Job[] {
  return terminalJobs(jobRows(mergedRows(jobs), isOpenAnywhere));
}

/** §4's own `terminalNotifications` — same `mergedRows`-first composition,
 *  for the same reason (a load/waiter pair must agree on terminal-ness
 *  before either is judged), but feeding `recentJobs` instead of `jobRows`:
 *  this is what `ActivityDock.tsx` hands `RepoUpdatesDock` as its Recent
 *  section, the complement of what `terminalNotifications` hands it as
 *  "Needs you"/"Worth keeping". */
export function recentNotifications(
  jobs: Job[],
  isOpenAnywhere: (source: string) => boolean,
): Job[] {
  return recentJobs(mergedRows(jobs), isOpenAnywhere);
}

// ------------------------------------------------------------------ popups
//
// The floating pop-up card (SPEC actionable-notifications, user: "when
// getting notifications, ensure the latest notification always pops up and
// auto disappears under 3 seconds. they still stay in the list"). `tier`
// narrowed to mean retention only (see `JobTier` above) is what makes this
// possible for `attention`/`trail`/`transient`: every terminal job in one of
// those three is news worth a card, whether or not it earns a lasting row.
// `silent` is the one exception — its whole point is to pop NOTHING on a
// successful finish (a resident model load/unload: the running row already
// said as much, so "done" is not news).

/** Every terminal job that should pop a card — deliberately NOT `jobRows`
 *  filtered by `effectiveTier`, since that filter is exactly what would drop
 *  a `transient` job's pop. `mergedRows` still runs first, for the same
 *  reason `terminalNotifications` runs it first: a render waiting on a
 *  shared model load must not pop the load's own id as a second card the
 *  instant it goes terminal, one poll ahead of the waiter noticing and
 *  clearing its own `waiting_for`. The `sys:schedule:*` exclusion (D661) is
 *  independent of tier and applies here exactly as it does in `jobRows` —
 *  a scheduled message's run is not a job anyone asked to watch.
 *
 *  The `silent` exclusion below reads the STORED `job.tier`, gated on
 *  `state === "done"` specifically — NOT `effectiveTier(j) !== "silent"`.
 *  A manager process can die mid-report and leave a row stuck `error` while
 *  its last-written tier is still `silent` (the supervisor's own reporting
 *  thread is the producer of that report; nothing guarantees its failure
 *  path gets to restate tier before it dies) — that row must still pop,
 *  because silence is a property of SUCCESS only, and a failure is always
 *  news. Reading `effectiveTier` here would already promote that row to
 *  `attention` and let it through correctly by accident, but it would also
 *  hide the actual rule being applied: this filter cares about the
 *  producer's OWN claim on a clean finish, not the derived display tier.
 *
 *  A MULTI-MEMBER GROUP'S OWN MEMBERS ARE EXCLUDED HERE (D-C,
 *  SPEC-quiet-notifications.md §3) — their popping is handled by
 *  `groupPopupTick` below instead, on the group's OWN rule (pop on start,
 *  pop on failure, never on ordinary or full completion), not on this
 *  function's per-job terminal rule. Without this exclusion a group member
 *  would pop here on every ordinary completion, exactly the "bent version of
 *  the terminal-only path" this build was told not to ship. A group of one
 *  is unaffected — it is excluded from nothing extra, so it keeps today's
 *  pop-on-every-terminal-event behavior exactly. */
export function popupJobs(jobs: Job[], isOpenAnywhere?: (source: string) => boolean): Job[] {
  const groupById = indexGroups(jobs);
  return terminalJobs(mergedRows(jobs))
    .filter((j) => !j.id.startsWith(SCHEDULE_JOB_PREFIX))
    .filter((j) => !(j.tier === "silent" && j.state === "done"))
    .filter((j) => !(isOpenAnywhere && isRecentOnly(j, isOpenAnywhere)))
    .filter((j) => {
      const g = groupById.get(j.id);
      return !g || g.jobs.length === 1;
    });
}

/** One popup tick's candidate key — a terminal EVENT, not a job id.
 *  `job_id_for(model)` (`fused_render/ai/supervisor.py`) mints one id for a
 *  resident model's load, its weights-only download and its unload, so the
 *  same id can go terminal more than once across the popup's lifetime; keying
 *  "have I popped this?" on the bare id would pop the first of those events
 *  and then silently swallow every later one landing on the same id while it
 *  is still in `seen`. `finished_at` changes on every genuine terminal event
 *  on that id, so the pair is what actually identifies "this particular
 *  finish", not "this job slot". */
function popupKey(job: Job): string {
  return `${job.id}:${job.finished_at ?? ""}`;
}

/** One popup tick's worth of decision: which job (if any) should pop this
 *  time, and the `seen` set to carry into the next call.
 *
 *  THE FIRST-TICK BACKLOG PROBLEM: a poller's very first read after a page
 *  load or refresh sees every already-terminal job at once — naively popping
 *  on "this job is terminal and I haven't popped it yet" would replay the
 *  whole backlog as a burst of cards the instant the page opens. `isFirstTick`
 *  is the caller's own flag for "this is the very first call this poller has
 *  ever made" (a ref initialized to `true` and flipped to `false` right
 *  after); on that call every current candidate is seeded into the returned
 *  `seen` set with no popup. This is the frontend twin of `_seen_running` in
 *  `fused_render/server/routers/index.py` — same shape, same reason: a fact
 *  this process never watched happen is not news to it.
 *
 *  LATEST WINS; NO STACKING — "the latest notification always pops up", not
 *  a queue of them. When more than one id is new in the same tick, the one
 *  that pops is whichever has the newest `finished_at`, not whichever
 *  `list_jobs` happened to return last. `list_jobs` sorts by
 *  `(started_at, id)` (`fused_render/jobs.py`), so its own tail is only the
 *  job that STARTED last — a short render that finishes behind an
 *  already-running model load becomes a fresh candidate in the same tick as
 *  the load's own completion, and the load, having started second, would win
 *  the old order-based pick even though the render is what actually just
 *  finished and is what the user is waiting on.
 *
 *  `seen` is REBUILT from this tick's candidate keys every call, exactly like
 *  `trackSeenIds` above, rather than only ever grown — a `popupKey` is a
 *  one-shot fact about a single terminal event, so once that event's key is
 *  no longer among the current candidates (dismissed, cleared, swept, or
 *  superseded by the same id's NEXT terminal event) it simply falls out and
 *  never needs forgetting on purpose. */
export function popupTick(
  jobs: Job[],
  seen: ReadonlySet<string>,
  isFirstTick: boolean,
  isOpenAnywhere?: (source: string) => boolean,
): { seen: Set<string>; popped: Job | null } {
  const next = new Set<string>();
  let popped: Job | null = null;
  // Finding 2 (code review 2026-09-16): candidates are gathered WITHOUT the
  // presence filter here (`popupJobs(jobs)`, not `popupJobs(jobs,
  // isOpenAnywhere)`) so every terminal candidate's key lands in `next`,
  // suppressed or not. The old code called `popupJobs(jobs, isOpenAnywhere)`
  // directly, so a presence-suppressed job's key was NEVER added to `seen` --
  // it simply never appeared in the loop. The instant the user navigated
  // away and `isOpenAnywhere` flipped false for that page, the very same
  // already-finished job looked brand new (its key still absent from
  // `seen`) and popped a stale card. Presence suppression is instead applied
  // per-candidate below, only to gate POPPING -- the key is still recorded
  // either way, so a later presence flip can never manufacture a "new" event
  // out of an old one.
  for (const j of popupJobs(jobs)) {
    const key = popupKey(j);
    next.add(key);
    if (isOpenAnywhere && isRecentOnly(j, isOpenAnywhere)) continue;
    if (isFirstTick || seen.has(key)) continue;
    if (popped === null || (j.finished_at ?? 0) > (popped.finished_at ?? 0)) popped = j;
  }
  return { seen: next, popped };
}

/** Carried tick-to-tick state for `groupPopupTick`, the same shape of
 *  "rebuilt every call" ref `popupTick`'s own `seen` set is. */
export interface GroupPopupState {
  /** Group keys (`(page, group)`) that had at least one RUNNING member as of
   *  the last tick — the "0 to some" edge for D-C's start rule is detected
   *  by a key's absence here followed by its presence now. Self-resetting:
   *  once a group goes fully idle/terminal its key simply falls out, so its
   *  NEXT run from zero is a fresh start, not a permanently-spent one. */
  runningGroups: ReadonlySet<string>;
  /** `popupKey`-shaped keys of members already popped for failing — same
   *  one-shot-terminal-event identity `popupTick` uses, restricted to
   *  members of MULTI-member groups (a single-member group's own failure
   *  already pops via `popupTick`/`popupJobs` unchanged). */
  failedSeen: ReadonlySet<string>;
}

export const EMPTY_GROUP_POPUP_STATE: GroupPopupState = {
  runningGroups: new Set(),
  failedSeen: new Set(),
};

function groupKeyOf(g: JobGroup): string {
  // Finding 3's own cluster-scoped identity, not the bare family -- two
  // different bursts of the same family must never share a tracked-group key.
  return g.key;
}

/** D-C's own pop rule (SPEC-quiet-notifications.md §3), for MULTI-member
 *  groups only — a group of one is handled entirely by `popupTick` above and
 *  never reaches here (see `groupJobs(jobs).filter(...length > 1)` below).
 *
 *  Two, and only two, events pop a group's card:
 *   1. START — the group goes from no running members to some. This is a
 *      genuinely new kind of event `popupTick`'s terminal-only path cannot
 *      express at all (a running job is never a `popupJobs` candidate), not
 *      a bent reuse of it — the trap this build was explicitly told to
 *      avoid.
 *   2. FAILURE — any member enters `error`/`cancelled` (reads via
 *      `effectiveTier(member) === "attention"`, the same promotion rule
 *      every other tier decision in this file uses).
 *  Ordinary completion (one member finishing cleanly) and full completion
 *  (the group going fully terminal) pop NOTHING — an unattended multi-file
 *  operation stays quiet exactly the way a single successful job already
 *  does, until something needs the user's attention.
 *
 *  Not gated by presence/`isOpenAnywhere` on purpose: a START is never
 *  terminal, so `isGroupRecentOnly` (which requires full-terminal) is always
 *  false for it regardless of where the user is — "in flight" is never
 *  quiet. A FAILURE is `effectiveTier === "attention"`, which `isRecentOnly`
 *  itself already always excludes from suppression — so there is no
 *  presence check this function could apply that would ever change either
 *  outcome.
 *
 *  LATEST WINS, same tie-break shape as `popupTick`: when a start and a
 *  failure are both new in the same tick (in the same or different groups),
 *  the one with the later moment — a start's `started_at`, a failure's
 *  `finished_at` — wins, since that is genuinely the more recent event. */
export function groupPopupTick(
  jobs: Job[],
  state: GroupPopupState,
  isFirstTick: boolean,
): { state: GroupPopupState; popped: Job | null } {
  const groups = groupJobs(jobs).filter((g) => g.jobs.length > 1);
  const nextRunning = new Set<string>();
  const nextFailedSeen = new Set<string>();
  let popped: Job | null = null;
  let poppedAt = -Infinity;

  for (const g of groups) {
    const key = groupKeyOf(g);
    const runningMembers = g.jobs.filter(isRunning);
    if (runningMembers.length > 0) {
      nextRunning.add(key);
      if (!isFirstTick && !state.runningGroups.has(key)) {
        const rep = runningMembers.reduce((a, b) =>
          (b.started_at ?? 0) > (a.started_at ?? 0) ? b : a,
        );
        const at = rep.started_at ?? 0;
        if (at > poppedAt) {
          popped = rep;
          poppedAt = at;
        }
      }
    }

    for (const member of g.jobs) {
      if (effectiveTier(member) !== "attention") continue;
      const mkey = popupKey(member);
      nextFailedSeen.add(mkey);
      if (isFirstTick || state.failedSeen.has(mkey)) continue;
      const at = member.finished_at ?? 0;
      if (at > poppedAt) {
        popped = member;
        poppedAt = at;
      }
    }
  }

  return { state: { runningGroups: nextRunning, failedSeen: nextFailedSeen }, popped };
}

// A REAL, server-side dismissal that happened somewhere its own `onPatch`
// cannot reach the shell's own terminal-jobs list — concretely
// `platform/ui/JobPopupCard.tsx`, whose reused `JobRow` really does call
// `dismissFn` (a whole-row click opens and dismisses, same as the panel's own
// row) but whose `onPatch` only closes THAT card, never touching
// `App.tsx`'s `terminalJobs` (the state `shell/RepoUpdatesDock.tsx` reads).
// Left unpatched there, the Notifications panel kept showing the row until
// its next poll — and a second ✕ press in the meantime could fail against an
// id the server had already deleted.
//
// The module-level notify/subscribe shape `platform/lib/index-freshness.ts`
// (`noteIndexLifecycle`/`subscribeIndexLifecycle`) and
// `shell/onboarding/progress.ts` (`noteProgressMayHaveMoved`) already use for
// exactly this kind of thing: the event's source (`JobPopupCard`, several
// components below `App`) and its one real consumer (`App`, which owns
// `terminalJobs`) have no other connection worth threading a prop through.
const dismissListeners = new Set<(id: string) => void>();

/** Record that `id` was just dismissed for real (its server-side row is
 *  gone) from somewhere that cannot patch the shell's own terminal-jobs list
 *  itself. */
export function noteJobDismissed(id: string): void {
  for (const fn of dismissListeners) fn(id);
}

/** Subscribe to real job dismissals `noteJobDismissed` reports. Returns an
 *  unsubscribe function. */
export function subscribeJobDismissed(fn: (id: string) => void): () => void {
  dismissListeners.add(fn);
  return () => void dismissListeners.delete(fn);
}

// How long the popup card stays fully visible before its exit animation
// starts. 2500ms, not the user's own literal "3 seconds": the card shares
// `lib/toast`'s TOAST_EXIT_MS (150ms) exit transition, so total on-screen
// time is 2500 + 150 = 2650ms — comfortably under the "under 3 seconds" the
// user asked for rather than landing right on the edge of it.
export const JOB_POPUP_VISIBLE_MS = 2500;

// Fraction complete in 0..1, or null when there is nothing honest to draw.
// Terminal jobs (done/error/cancelled) draw no bar at all — `Bar` in
// DownloadManager.tsx returns null outright for those states, so this
// function's return value is only ever consulted for a running job.
//
// `total` of 0 is null, not 1: a reporter that has not learned the size yet
// sends 0, and painting that as a full bar says the opposite of the truth. A
// `done` past `total` is clamped rather than dropped — an over-count is a
// reporter rounding, and a bar past its own end is worse than a full one.
export function jobFraction(job: Job): number | null {
  // `== null`, not `=== null` (D577): covers `undefined` as well, so a payload
  // missing these keys yields null rather than `undefined / undefined` ->
  // `NaN` -> a literal `NaN%` painted into the bar. Not user-reachable today
  // (fused_render/jobs.py serializes explicit nulls — `done: float | None =
  // None`), but the loose check costs nothing and removes the trap.
  if (job.total == null || job.total <= 0 || job.done == null) return null;
  return Math.max(0, Math.min(1, job.done / job.total));
}

// A byte count as the manager shows it: 3 significant-ish digits, binary
// units, no more precision than the number deserves. Deliberately local rather
// than lib/format's file-size helper — this one has to render a partial count
// against a total in the SAME unit ("1.2 / 8.1 GB"), which a standalone
// formatter can't do without picking two different units.
const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"];

export function byteScale(max: number): { div: number; unit: string } {
  let div = 1;
  let i = 0;
  while (i < BYTE_UNITS.length - 1 && max >= div * 1024) {
    div *= 1024;
    i += 1;
  }
  return { div, unit: BYTE_UNITS[i] };
}

function num(value: number, div: number): string {
  const scaled = value / div;
  if (div === 1) return String(Math.round(scaled));
  return scaled >= 100 ? scaled.toFixed(0) : scaled >= 10 ? scaled.toFixed(1) : scaled.toFixed(2);
}

// Seconds as a clock: "0:09", "12:00", "1:30:00". The hours field appears only
// when there are hours, so a short clip is not dressed up as a long one.
function clock(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  const s = whole % 60;
  const m = Math.floor(whole / 60) % 60;
  const h = Math.floor(whole / 3600);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

// "1.2 / 8.1 GB", "412 MB", "12:00 / 1:30:00", "3 / 12" — or "" when there is
// no number at all. Bytes scale both sides by the LARGER of the two so the pair
// reads as one measurement instead of "1200 MB / 8.1 GB".
//
// SECONDS get the clock, and that is not decoration. A transcription reports
// seconds of audio (SPEC AI-10a) and every non-byte unit used to fall through
// to a bare pair, so a 90-minute recording read "720 / 5400" — a number a user
// takes for segments or steps. A unit that is only ever right by accident is
// worse than one that is absent, since the bare pair looks deliberate.
export function jobAmount(job: Job): string {
  const { total, unit } = job;
  // A reporter's own `done` can pass a stale `total` (an index rescan whose
  // tree grew since the last scan set `total_estimated` — the ONLY producer
  // of this today) — `jobFraction` already clamps the BAR at full rather
  // than past it or backwards. The printed number has to agree, or the row
  // says two different things at once ("700,000 / 672,424 files" beside a
  // bar already pinned at 100%). Clamping the numerator to the total — not
  // dropping the denominator — was chosen because the total is still the
  // honest fact worth showing (D733): it says what the walk expected, and a
  // clamped "672,424 / 672,424" reads as "caught up to the estimate", which
  // is closer to the truth than either a bare unclamped count or a total
  // that vanishes the moment it is exceeded.
  const rawDone = job.done;
  const done =
    rawDone !== null && total !== null && total > 0 && rawDone > total ? total : rawDone;
  if (done === null && total === null) return "";
  if (unit === "s") {
    if (done === null) return "";
    return total === null || total <= 0
      ? clock(done)
      : `${clock(done)} / ${clock(total)}`;
  }
  // Any other unit is a plain COUNT: locale thousands separators (never a
  // hard-coded comma — `toLocaleString()` is the one formatter in this file
  // that has to agree with the Preferences panel's own count, which renders
  // in the browser's locale and so groups digits Indian-style, not
  // Western-style, once the count passes a lakh) plus the unit word itself,
  // e.g. "10,856 files" or "10,856 / 672,424 files". `unit: "files"`
  // (index scans, D724) and `unit: "tokens"` (text generation,
  // `ai/supervisor.py`'s `text_row_fields`) are the two real callers today;
  // both used to fall through to a bare, unformatted number (the defect
  // this branch fixes) and both read strictly better with a word attached.
  if (unit !== "bytes") {
    if (done === null) return "";
    const count = (n: number) => Math.round(n).toLocaleString();
    const suffix = unit ? ` ${unit}` : "";
    return total === null || total <= 0
      ? `${count(done)}${suffix}`
      : `${count(done)} / ${count(total)}${suffix}`;
  }
  const scale = byteScale(Math.max(done ?? 0, total ?? 0));
  if (total === null || total <= 0) {
    return done === null ? "" : `${num(done, scale.div)} ${scale.unit}`;
  }
  if (done === null) return `${num(total, scale.div)} ${scale.unit}`;
  return `${num(done, scale.div)} / ${num(total, scale.div)} ${scale.unit}`;
}

// The one line under the title. In priority order, because the states overlap:
// an error's message beats everything (it is the thing to act on), a cancel
// that has been asked for but not yet honored has to say so or the ✕ reads as
// broken, and a stalled row must not keep showing a stale detail as if it were
// live.
export function jobStatusLine(job: Job): string {
  if (job.state === "error") return job.message || "Failed";
  if (job.state === "cancelled") return job.detail || "Cancelled";
  if (job.state === "done") return job.detail || "Done";
  // A question on the page, not a failure and not progress — the caption
  // names what it is waiting on (e.g. "waiting for your approval to compile
  // <pkg>"). Checked ahead of `stalled`/`cancel_requested` below: both of
  // those describe a REPORTER that has gone quiet or been asked to stop, and
  // a "waiting" row's reporter already exited on purpose the moment it wrote
  // this state (see `fused_render/jobs.py`'s own comment on `WAITING`).
  if (job.state === "waiting") return job.message || "Waiting for you";
  // Stalled outranks a pending cancel, and says so explicitly when both hold.
  // "Cancelling…" claims something is working on the request; if the reporter
  // died before honoring it, that claim would stand for the whole ten-minute
  // stale-drop window while nothing at all was happening.
  if (job.stalled) {
    // WHOSE reporter went quiet decides what to say. A page-owned row means a
    // tab was closed. A server-owned one (a model download, SPEC §40) means the
    // app's own worker stopped reporting — and telling someone their page was
    // closed when no page was involved sends them to look in the wrong place.
    const why =
      job.owner === "server"
        ? "the process running it stopped reporting"
        : "the page that started it was closed";
    return job.cancel_requested
      ? `Cancel requested, but nothing is reporting any more — ${why}`
      : `No longer reporting — ${why}`;
  }
  if (job.cancel_requested) return "Cancelling…";
  // NOT `job.detail || jobDetail(job)` here: a running job can carry a real
  // progress AMOUNT (jobAmount, a sibling fact this function does not see)
  // with no phase text at all — a bare download row is exactly that case.
  // Falling back to `jobDetail` from inside this function would win over
  // that amount and stamp "Task · started 3m ago" onto a row that already
  // had something true to say. The last-resort fallback belongs at the call
  // site instead, once status AND amount are both known to be empty
  // (DownloadManager.tsx's `statusLine`, `repoStatusText`'s job branch).
  //
  // `message` joins `detail` here (was `detail` alone) because a reporter's
  // PHASE — a fact distinct from `detail`'s "where"/"what" — has always
  // lived in `message` for a running job (`_report`'s error/waiting-only
  // convention meant it was simply never populated while running, until the
  // index-scan bridge started putting its run's phase there — "writing
  // index" / "writing signatures", D724 — with no code path that ever
  // rendered it: `message` was read only for `error`/`waiting` above). Every
  // other reporter still sends `message: ""` while running (or nothing at
  // all), so this is additive for them — `[]` still degrades to `detail`
  // alone.
  return [job.message, job.detail].filter(Boolean).join(" · ");
}

/** A COARSE duration, in the largest unit that still says something true:
 *  seconds under a minute, whole minutes under an hour, then `2h 5m`. Shared
 *  by the engine rows (whose poll is every 10s, so counting seconds would be
 *  wrong between ticks more often than right) and `jobDetail` below (whose
 *  input is a job's own `started_at`, at the same coarseness). */
export function engineDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest === 0 ? `${h}h` : `${h}h ${rest}m`;
}

const JOB_KIND_TEXT: Record<JobKind, string> = { download: "Download", task: "Task" };

/**
 * `jobStatusLine`'s last resort (D665) — no card may render a single line of
 * text (title alone, no status line beneath it). Engine rows already
 * guarantee a `.dl-status` line (`engineDetail`); a `running` job with no
 * server detail, no byte/step amount and no local action failure fell
 * through to a bare title, which is the gap this closes.
 *
 * Built only from facts every job always carries, so it can never itself be
 * empty: what kind of work this is, and how long it has been going. `stalled`
 * is folded in because a job with nothing else to say and no reporter left is
 * exactly the case most likely to need this fallback at all.
 *
 * `nowS` (C4 fix) is the SERVER's clock (`JobsSnapshot.now`), the same rule
 * `JobsSnapshot.now`'s own doc states and every other age in this file
 * follows — never the browser's `Date.now()`. `job.started_at` is a server
 * timestamp; measuring it against the browser's clock instead reads wrong
 * by however far the two have drifted, which after a tab throttle or a
 * laptop sleep is not a rounding error — it is the same "finished in 3
 * seconds' time" symptom `JobsSnapshot.now` exists to prevent, just for a
 * duration instead of an age.
 */
export function jobDetail(job: Job, nowS: number): string {
  const kind = JOB_KIND_TEXT[job.kind] ?? "Job";
  const started = `started ${engineDuration(nowS - job.started_at)} ago`;
  return job.stalled ? `${kind} · ${started} · not reporting` : `${kind} · ${started}`;
}

// Poll cadence. Fast while anything is live — a progress bar that steps once a
// second reads as stuck — and slow otherwise, where the only thing a poll can
// discover is a job started with no ping behind it: one reported from another
// same-origin document (a page in another browser tab), or one a server-side
// process reports on its own with no browser ever POSTing anything (a
// scheduled message's timer tick, `schedule.py`'s `_report` — runs no JS, so
// writes no ping). A row a page's own JS causes — the env-install path
// included, even though the row itself is created server-side inside
// `envinstall.start()` — is pinged the moment the triggering POST resolves.
// This floor is what covers the cases a ping can't reach.
export const POLL_ACTIVE_MS = 1000;
export const POLL_IDLE_MS = 5000;

// How long to keep the ACTIVE cadence going after the last running job
// disappears. jobs.py sweeps a finished row after FINISHED_TTL_S (currently
// 3s) — a short TTL only actually shortens what the user sees if the client
// is still polling fast enough to catch the row landing AND catch it being
// swept. Dropping straight to POLL_IDLE_MS (5s) the instant nothing is
// running would mean a row could be missed on arrival, or sit for a ragged
// 0-5s after it dies depending on poll phase, instead of the clean ~3s the
// server now promises.
//
// GRACE_MS must comfortably outlive FINISHED_TTL_S plus a poll interval —
// this is the other half of that relationship, so a future change to
// FINISHED_TTL_S (fused_render/jobs.py) should come back here and check the
// margin still holds, and vice versa: shrinking GRACE_MS below
// FINISHED_TTL_S + POLL_ACTIVE_MS reopens the same lag this constant exists
// to close.
export const GRACE_MS = 6000;

/**
 * Poll cadence given the current jobs and how long ago a job was last seen
 * running. Pure — no clock of its own — so the caller (DownloadManager's
 * `useJobs`) is the one that owns `Date.now()` and remembers when it last
 * saw a running job; this function just decides what the elapsed time means.
 */
export function pollInterval(jobs: Job[], sinceLastRunningMs: number): number {
  if (jobs.some(isRunning)) return POLL_ACTIVE_MS;
  if (sinceLastRunningMs < GRACE_MS) return POLL_ACTIVE_MS;
  return POLL_IDLE_MS;
}

// ------------------------------------------------------------- auto-expand
//
// Shared by BOTH notification cards (DownloadManager.tsx's jobs/downloads
// card and shell/RepoUpdatesDock.tsx's repo-updates card, D562 follow-up —
// user call: "we can make the notifications 'un collapse' when a new one
// comes"). Lives here, not in repo-updates-lib.ts, because platform/ may not
// import shell/ (frontend/scripts/check-boundaries.mjs) — a helper both
// sides use has to live on the platform side, and shell is free to import
// it back.
//
// Pure and generic over the id: a job id for the jobs card, a repo root for
// the repo-updates card. `seen` in, `seen` out — the caller (a ref, one per
// card) owns the mutable state across renders/polls; this function only
// decides what one snapshot means against it.
//
// An id merely CHANGING (progress ticking, running -> done, ahead/behind
// moving) is not new — it was already in `seen` from an earlier snapshot and
// stays there, so `hasNew` stays false and the card does not re-open under a
// user who just folded it. An id that DISAPPEARS (cleared, dismissed,
// forgotten, the server no longer reporting it) falls out of the returned
// set — it is only ever repopulated from `currentIds` — so a genuinely
// re-arriving id later reads as new again, exactly like a first arrival.
export function trackSeenIds(
  currentIds: Iterable<string>,
  seen: ReadonlySet<string>
): { seen: Set<string>; hasNew: boolean } {
  const next = new Set<string>();
  let hasNew = false;
  for (const id of currentIds) {
    next.add(id);
    if (!seen.has(id)) hasNew = true;
  }
  return { seen: next, hasNew };
}

// ---- What the status-bar chip says about the work (statusbar redesign) -----
//
// The bar has room for ONE word per chip. For a single running job that word
// is what the job itself says it is doing, read off the LEADING -ing VERB of,
// in order: its `detail` (the live phase a reporter writes — "Denoising",
// "Decoding" — the same word the page shows under its own progress bar, so
// the bar and the page never disagree), then its `title` ("Erasing text using
// flux" → "Erasing"; an image prompt has no verb and yields nothing). Anything
// else falls back to the kind: a download is "Downloading", every other task
// is "Working", and a job parked on a question is "Waiting".
function leadingVerb(text: string): string | null {
  const first = text.trim().split(/\s+/)[0] ?? "";
  const word = first.replace(/[^\p{L}\p{N}-]/gu, "");
  if (!/ing$/i.test(word) || word.length < 5 || word.length > 14) return null;
  return word[0].toUpperCase() + word.slice(1);
}

export function jobTypeLabel(job: Job): string {
  if (job.state === "waiting") return "Waiting";
  const detail = job.detail || "";
  // The one non-verb status a reporter writes: a Claude call parked behind
  // another ("Queued — another Claude call is in flight", server/ai.py).
  if (/^queued\b/i.test(detail.trim())) return "Queued";
  const verb = leadingVerb(detail) ?? leadingVerb(job.title);
  if (verb) return verb;
  if (job.kind === "download") return "Downloading";
  return "Working";
}

// One progress figure for the whole bar: the mean fraction of the RUNNING
// jobs that report one. `undefined` = nothing running, draw no line at all;
// `null` = running but nobody has a total yet, draw the indeterminate sweep.
// A mean, not Σdone/Σtotal, because units differ per job (bytes vs seconds
// vs steps) and summing them is meaningless.
export function aggregateProgress(jobs: readonly Job[]): number | null | undefined {
  const running = jobs.filter(isRunning);
  if (running.length === 0) return undefined;
  const known = running.map(jobFraction).filter((f): f is number => f !== null);
  if (known.length === 0) return null;
  return known.reduce((a, b) => a + b, 0) / known.length;
}

// §5 (SPEC-quiet-notifications.md) "interactive turns and needs-input" — the
// decision table, split from the hook that drives it (useTaskStatusNotify.ts)
// the same way schedule-toast.ts/scheduleEvents.ts split rules from polling —
// so the rules can be tested without a DOM.
//
// Derives from the EXISTING task-status poll (tasksPulse.ts's
// useTasksPulseRows) rather than adding a server channel, per the spec's own
// "Sources" instruction — `/api/tasks`+`/api/tasks/changes`
// (fused_render/server/routers/tasks.py `_status()`) already computes
// `needs_attention` as its rule 0 and `in_progress`/`blocked`/`done` for
// everything else. This file only watches for the three transitions the
// spec's table names:
//   - in_progress -> done            ("Task finished")
//   - in_progress -> blocked         ("Task failed")
//   - anything    -> needs_attention ("Task needs your input")
//
// NEEDS_ATTENTION IS ALREADY RETAINED (Akshil, 2026-09-03; tasks-lib.ts's
// `attentionRows`, wired into RepoUpdatesDock.tsx line ~1185) — a dedicated,
// always-current Notifications-panel row for every task parked on a
// question, with its own dismiss/undismiss lifecycle (RepoUpdatesDock.test.tsx,
// "the waiting-task row"). Routing this transition through notify()'s
// ordinary `tone: "error"` shape would ALWAYS-retain (notifications.ts's
// `isRetained`) a SECOND, separately-dismissible row for the exact same
// fact — a visible duplicate, not a second source of truth. So this one
// transition raises a plain notify() with no tone/tier (resolves to
// "transient": pops, does NOT retain, per notifications.ts's `resolveTier`/
// `isRetained`) — the one thing `attentionRows` cannot do on its own, an
// announcement at the MOMENT a task parks. `attentionRows` keeps covering
// "is still parked" for as long as that remains true.
//
// `in_progress -> blocked` ("Task failed") has no pre-existing row anywhere
// in the Notifications panel, so it keeps the ordinary `tone: "error"` shape
// (pop + retain, with a `page` so the row is clickable, per
// SPEC-actionable-notifications.md's "every row goes somewhere").
//
// `in_progress -> done` ("Task finished") IS RETAINED AND CLICKABLE — a
// REVERSAL of this file's own earlier position (until 2026-09-16 this
// comment argued a plain "it's over" confirmation "is not something to hunt
// for again" and the branch below returned no `page` at all, which
// `lib/notifications.ts`'s `isRetained` resolves to a transient popup that
// is never kept). The user, from a screenshot: "the user does want to open
// the app along with claude template to go back" — a finished run is
// exactly the moment someone wants to jump back into it, so losing the row
// the instant the popup's ~2.5s expire was the bug, not a feature. `page:
// taskDestination(task)` gives it the same destination
// `in_progress -> blocked` already carries, which alone makes
// `isRetained` keep it (`Boolean(input.action || input.page)`).
//
// NEVER PRESENCE-SUPPRESSED (2026-09-17, second reversal): this branch used
// to also carry `source: taskSource(task)`, which let a finished task's
// POPUP get swallowed by `isPopupSuppressed` (jobs.ts) whenever the run's own
// chat/project was already on screen — "you're already looking at it" for a
// job that is still running. A finished Claude task is different: the run
// has ENDED, so "already looking at the chat" no longer means "already knows
// it's done" the way it does for an in-progress job's own page. Dropping
// `source` here means a finished task always pops, same as `in_progress ->
// blocked` already does. This also drops the `recent: true` flag from the
// now-removed "Recent" section (SPEC-quiet-notifications.md §4, reversed the
// same day — see DECISIONS-quiet-notifications.md): the row simply lands as
// an ordinary retained row in the one unified list.
import type { TaskPulseTask } from "@platform/lib/api";
import type { NotificationInput } from "@platform/lib/notifications";
import { labelForSource } from "@platform/lib/format";
import { taskColumn } from "@shell/tasks-lib";
import { taskHref } from "@shell/tasks-lib";
import { folderHref } from "@shell/schedule-lib";
import { recentFsPath } from "@apps/explorer/lib/recents";

/** Where a click on this task's own notification should land — the same
 *  fallback chain `attentionRows` already uses (a live session's chat, else
 *  the folder, else the Tasks page itself). */
export function taskDestination(task: TaskPulseTask): string {
  return taskHref(task) ?? folderHref(task) ?? "/tasks";
}

/** The "who made this" caption for a task's own notification — SAME
 *  project-first order `folderHref` (schedule-lib.ts) and `attentionRows`
 *  (tasks-lib.ts:4872) already settled on, not target-first: a task made
 *  from inside an app targets that app's own ENTRY PAGE
 *  (".../Transcripto/index.html"), so target-first here reproduces the
 *  exact "index" caption bug `attentionRows`'s own comment names. `project`
 *  names the containing app/folder; `target` is only a fallback for a task
 *  with no project at all. */
function taskCaption(task: TaskPulseTask): string {
  return labelForSource(task.project || task.target);
}

/** A task's own title, with a leading repeat of its own caption stripped —
 *  "Transcripto YouTube transcriber" next to a "Transcripto" eyebrow reads as
 *  the same word twice; "YouTube transcriber" under a "Transcripto" eyebrow
 *  reads as two different pieces of information. Deliberately narrow: only
 *  strips an actual PREFIX match (case-insensitive, followed by whitespace
 *  or punctuation), never touches a title that doesn't happen to start with
 *  its own caption — most tasks aren't named "<project> <description>", and
 *  this must never mangle those into something shorter and wrong. Falls back
 *  to the untouched title whenever there is no caption, no match, or the
 *  match would consume the whole title. */
function titleWithoutCaption(title: string, caption: string): string {
  if (!caption) return title;
  const escaped = caption.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const stripped = title.replace(new RegExp(`^${escaped}[\\s:-]+`, "i"), "");
  return stripped || title;
}

/**
 * The notify() input for ONE task's status transition, or `null` when this
 * pair of statuses is not one the spec names — including no change at all.
 *
 * `previous === undefined` is a task's FIRST SIGHTING — either on this
 * document's very first poll tick (every row already on screen, including
 * 200 already-`done` ones) or a task whose key this document simply hadn't
 * polled before. Those two cases must NOT be treated alike (2026-09-17 fix):
 * a task already parked, already blocked, or already done at first sighting
 * is not news BY DEFAULT — `attentionRows` (and the Tasks page itself)
 * already show it, and treating a page load as a wave of transitions would
 * flood a fresh tab with popups for every row already on screen. But a run
 * that STARTS and FINISHES between two polls is ALSO a first sighting of its
 * key (the pulse is keyed on `session_id`, one row per run — see
 * SPEC-quiet-notifications.md's tasksPulse.ts note), and treating every first
 * sighting as silent is exactly why "a finished task usually raises no
 * notification at all" was reported as a bug: the 10s/30s poll routinely
 * never sees a short run `in_progress` at all.
 *
 * `watchStartS` (unix seconds — same unit as `happened_at`) is the moment
 * THIS document started watching, fixed once by the caller (the hook) at its
 * own first tick. It is the only fact that can tell "backfill" (happened
 * before I started looking) from "news" (happened after): a key arriving
 * already in a TERMINAL column (`done`/`blocked`) whose own `happened_at` —
 * "the newest thing that actually happened" on the row, tasks.py's own
 * phrase, never a future due time — is AFTER `watchStartS` counts as that
 * terminal transition, exactly as if this document had caught it mid-flight.
 * A terminal row whose `happened_at` predates `watchStartS` is backfill and
 * stays silent, which is also what makes a document's own first tick quiet
 * by construction: the hook sets `watchStartS` to "now" at that very tick, so
 * nothing already on the very first answer can have a LATER `happened_at`.
 *
 * `needs_attention` is deliberately NOT covered by this backfill/news split —
 * only `done`/`blocked` are, per the spec's own table. A task already parked
 * on a question at first sighting stays silent here regardless of timing;
 * `attentionRows` is its permanent, always-current row.
 */
export function notificationForTransition(
  previous: string | undefined,
  task: TaskPulseTask,
  watchStartS: number,
  // TERMINAL-SESSION SCOPING (2026-09-18): the reported bug — a plain
  // `claude` session started by hand in a terminal, nothing to do with
  // fused-render at all, raising a fused-render "Finished" notice — because
  // this hook watches EVERY session in the machine-wide `~/.claude/projects`
  // pool, not only ones started from our own template. `task.entrypoint` is
  // the one signal a transcript carries for this: "cli" for an interactive
  // terminal, "sdk-cli" for a headless/programmatic spawn (what
  // templates/claude/agent.py's own spawn produces) — see `TaskPulseTask`'s
  // own doc comment on why this is a PROXY, not proof, and can only ever be
  // "fairly sure", never exact.
  //
  // Threaded in as a plain argument, not read off a module here, because
  // this function is deliberately pure (the header comment above: "so the
  // rules can be tested without a DOM") — the caller (useTaskStatusNotify.ts)
  // owns subscribing to the preference.
  //
  // Defaults to `false` (the pref's own shipping default) purely so the many
  // existing 3-argument call sites in task-status-notify.test.ts keep
  // compiling; none of them set `entrypoint: "cli"`, so the gate below never
  // fires for them regardless of this default.
  notifyTerminalSessions = false,
  // ALREADY-OPEN POPUP SUPPRESSION (F8, 2026-09-18): "we never want to show
  // notifications for tasks when the claude template / app is already
  // opened" — a screenshot showed the exact session's own chat on screen,
  // task-finished popup still firing. Injected (not imported) for the same
  // reason `notifyTerminalSessions` above is: this function stays pure and
  // DOM-free; `useTaskStatusNotify.ts` supplies the real presence check
  // (`snapshotIsOpenAnywhere`, platform/lib/presence.ts), batched once per
  // poll tick rather than once per task. Takes an already-normalized fs
  // path/route (see `taskDestination`'s call site below, which runs
  // `taskDestination(task)` through `recentFsPath` before calling this —
  // `taskDestination` returns an `/explorer/view/...` HREF with a query
  // string, which is not the shape presence entries store; a bare
  // `isOpenAnywhere(taskDestination(task))` call silently never matches
  // anything). Defaults to "nothing is open" so the many existing
  // lower-arity call sites in task-status-notify.test.ts keep compiling
  // unchanged.
  isDestinationOpen: (page: string) => boolean = () => false,
): NotificationInput | null {
  const column = taskColumn(task);
  if (previous === undefined) {
    if ((column === "done" || column === "blocked") && (task.happened_at ?? 0) > watchStartS) {
      previous = "in_progress";
    } else {
      return null;
    }
  }
  if (column === previous) return null;

  if (column === "needs_attention") {
    return { title: `${task.title || "A task"} needs your input` };
  }
  if (previous === "in_progress" && column === "blocked") {
    return {
      title: `${task.title || "A task"} failed`,
      tone: "error",
      page: taskDestination(task),
    };
  }
  if (previous === "in_progress" && column === "done") {
    // TERMINAL-SESSION GATE (2026-09-18): only an EXACT "cli" counts as
    // "started outside our own template" — a missing/unknown entrypoint
    // (an older transcript, or a head read that hasn't resolved yet) fails
    // OPEN and still notifies, exactly as it always has. Narrower than "not
    // sdk-cli", deliberately: the whole point of the terminal-sessions
    // preference is that "sdk-cli" is a proxy, not proof, so treating
    // anything-but-sdk-cli as interactive would silence sessions this
    // signal was never confident about in the first place.
    if (task.entrypoint === "cli" && !notifyTerminalSessions) return null;
    // THIRD REVERSAL, 2026-09-17 — the code-review-round fix (see
    // DECISIONS-quiet-notifications.md's "notification card regression"
    // entry) restoring what the SECOND reversal above accidentally deleted.
    // Dropping `source` (rightly — see the header comment above) also
    // silently dropped the ONLY thing that fed the card's caption, because
    // `notifications.ts`'s `toStored` computed the caption from `source`
    // alone: no `source`, no caption, no matter how good `origin`
    // resolved. The card the user actually saw was a single bold line —
    // "why do you always want to make the notification smaller? ... I don't
    // want a single line of text" — with no creator context at all.
    // `notifications.ts` now has a separate `origin` field for exactly this:
    // "caption this row" without "suppress it when its page is open" (the
    // two `source` used to conflate). Set `origin` here, never `source` —
    // the whole point of the second reversal above stands.
    const caption = taskCaption(task);
    const destination = taskDestination(task);
    // ALREADY-OPEN GATE (F8): only suppress the POPUP (`quiet: true` below),
    // never the retained row — the user's own correction: "by show I mean
    // popup ... if that is not simple enough to do just dont have a
    // notification" (popup-only turned out simple, so that's what shipped).
    // Excludes the bare `/tasks` fallback deliberately: `taskDestination`
    // falls all the way through to `/tasks` for a task with no session AND
    // no folder, and suppressing on THAT would go quiet for every such task
    // whenever anyone merely has the Tasks page open — a page with no
    // relation to this specific task at all.
    const quiet = destination !== "/tasks" && isDestinationOpen(recentFsPath(destination));
    return {
      title: titleWithoutCaption(task.title || "A task", caption),
      // "Finished" moves OUT of the title and into `detail` (`.dl-model`,
      // MessageRowView's `secondary`) rather than staying baked into the
      // title string or landing in `status`: `status` is already spoken for
      // by MessageRowView's own "Happened N times" repeat-count line
      // (notifications.ts's `count`/`family` collapse), which must keep
      // working for a task that finishes more than once. `detail` is empty
      // for this call site otherwise, so it costs nothing and reads as a
      // real second line ("YouTube transcriber" / "Finished"), not a
      // repeated word wedged into the title.
      detail: "Finished",
      tone: "info",
      origin: caption || undefined,
      page: destination,
      quiet,
      // FAMILY-BY-FOLDER, NOT BY TITLE (2026-09-18 fix, user: "these 2
      // fused-render notifications should have been grouped together as
      // count"). `notifications.ts`'s default family is caption+TITLE, which
      // is right for most messages (two unrelated notices from the same
      // folder must stay separate rows) but wrong for two DIFFERENT finished
      // tasks in the SAME folder ("hi", "New session") — different titles,
      // same folder, and the user's own framing is "grouped ... as count",
      // i.e. one row per folder, not one row per exact task name. Only set
      // when there is a caption at all — a captionless finished task (no
      // project, no target) has no folder identity to group by, so it falls
      // back to `messageFamily`'s ordinary page/title chain untouched. See
      // `familyKey`'s own doc comment on `NotificationInput` for the
      // accepted trade-off (the newest finished task's title wins; `count`
      // carries the rest).
      familyKey: caption ? `task-finished:${caption}` : undefined,
    };
  }
  return null;
}

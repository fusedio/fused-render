// Tasks page — every task on this machine, in one place, three ways.
//
// A TASK IS A CLAUDE SESSION. Same thing, one name. A task owns a THREAD, and
// the thread's MESSAGES are every prompt sent into it — typed in a chat, typed
// in the template's chat, or fired by the scheduler. The three sources differ
// only in how the message arrived; the thread does not care. That is the whole
// model, and it is why this page no longer merges two feeds client-side the way
// it used to: `GET /api/tasks` returns the merge, already titled, already
// counted, already ordered. See
// SPEC.md's SCH section and DECISIONS.md D322.
//
// List and Board show tasks. So does the Calendar — the chip IS a task, not a
// message. What the time axis adds is placement, not a different unit: one chip
// per task per day, anchored at that task's earliest message that day, the rest
// nested behind a `+N`. All three views therefore answer the same question with
// the same noun, which is the point.
//
// Their markup lives in shell/ScheduleTaskViews.tsx (List and Board) and
// shell/ScheduleCalendar.tsx; this file owns the page: the poll, the toggle,
// the filters, the modal.
//
// Scheduling happens in two places, deliberately: the chat composer's Send now
// pill (templates/claude/template.html) when a chat is already open — it knows
// the folder and holds the message — and this page's New task modal when the
// starting point is the calendar ("what should run Monday 9am?"), where no chat
// exists yet to borrow from. A calendar slot click opens the modal with that
// time filled in.
//
// The composer also has a THIRD affordance that lands here: its Schedule button
// links to `/tasks?new=1&target=…`, for the case the pill cannot serve — a
// task that wants a title, a description or a repeat rule. It is a handoff, not
// a second form: the chat sends the folder it is bound to and nothing else, and
// the effect below opens the modal on it.
//
// Backend: fused_render/schedule.py (the store and the loop that fires it),
// fused_render/tasks_store.py + server/routers/tasks.py (task identity, titles,
// unread), server/routers/schedule.py (this page's calls). The app does the
// sending itself rather than handing the job to cron, so a scheduled turn runs
// with the same environment, credentials and file-access consent as one the
// user typed — see that module's docstring for why that matters more than it
// sounds.
//
// The honest cost of that choice is the one thing this page must never hide:
// **nothing fires while the app is closed.** Work that came due meanwhile is
// not lost — it QUEUES and runs when the app next opens. The queue lives in the
// dock, bottom right, which shows what is running and what is past due and
// waiting, and is where either can be cancelled before it goes. It deliberately
// does NOT show work scheduled for later: "queued" means about to run, and a
// list that also held next Tuesday would answer a different question.
//
// Section layout and per-action busy/error state follow shell/Mounts.tsx.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  getConfig,
  getSchedule,
  getScheduleQueue,
} from "@platform/lib/api";
import type {
  ScheduledMessage,
  ScheduleResult,
  Task,
} from "@platform/lib/api";
import { useRefreshOnReturn } from "@platform/lib/hooks";
import { draftChatUrl } from "@apps/claude";
import {
  chatKeySession,
  fetchDrafts,
  NEW_CHAT_PREFIX,
  newChatFile,
} from "@platform/lib/drafts";
import type { ChatDraft } from "@platform/lib/drafts";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import ScheduleCalendar, {
  ICON_VIEW_BOARD,
  ICON_VIEW_CALENDAR,
  ICON_VIEW_CARDS,
  ICON_VIEW_LIST,
} from "./ScheduleCalendar";
import NewJobModal, { seededDraftForm, splitDraft } from "./NewJobModal";
import type { DraftSeed } from "./NewJobModal";
import {
  EMPTY_FILTERS,
  TaskBoard,
  TaskFilterControls,
  TaskList,
  filterTasks,
  filtersForView,
  projectOptions,
} from "./ScheduleTaskViews";
import type { TaskFilters } from "./ScheduleTaskViews";
import {
  readListing,
  readTasksRows,
  refreshListing,
  subscribeListing,
  TASKS_POKE_EVENT,
  useTasksFeeder,
} from "./tasksPulse";
import {
  TASK_VIEWS,
  applyQueueOverrides,
  expireQueueOverrides,
  isChatDraftTask,
  NO_QUEUE_OVERRIDES,
  provisionalTasks,
  viewFromSearch,
  viewUrl,
  withQueueOverride,
} from "./tasks-lib";
import type { QueueOverride, QueueOverrides, TaskView } from "./tasks-lib";
import { TaskCards } from "./TaskCards";
import { TasksSkeleton } from "./TasksSkeleton";
import { useMissingFolders } from "./useMissingFolders";
import { TOOLBAR_MERGE_LEVEL, useToolbarFit } from "./row-fit";
import { useTaskPeekEnabled } from "./task-peek-flag";
import { TaskPeek, useTaskPeekHost, useTaskPeekLayout } from "./TaskPeek";
import {
  closePeek,
  frameClickCloses,
  openPeek,
  peekGutter,
  refreshPeekBaseline,
} from "./task-peek-store";
import { isUnderDir } from "./current-apps-lib";

/** The app page's Tasks tab (shell/AppPage.tsx, D488) mounts this SAME page
 *  narrowed to one folder: every task whose project is `project` or sits inside
 *  it. The scope is applied before the toolbar filters, so those still work
 *  within it; nothing else about the page changes — same views, same modal,
 *  same poll. The unscoped `/tasks` route passes nothing. */
export interface TasksScope {
  /** The app folder, canonical forward-slash — the value `Task.project` carries. */
  project: string;
  /** That folder's entry page when it has one, already resolved by the app page
   *  (AppPage asks `getAppEntry` to frame the Overview). PREFILLS a new task's
   *  target and nothing else — never the filter, which stays on `project` so
   *  the tab keeps listing every task in the folder. */
  entry?: string | null;
}

// How often the page re-reads the SCHEDULE and the QUEUE. A `pending` message
// becomes `sent` on the server's own tick (30s), so anything much slower than
// this shows a message as still-waiting for a while after it went out.
//
// The TASKS feed is no longer on this clock: it moved to the shared listing feed
// (`tasksPulse.subscribeListing`), whose floor refresh is this same 20s — one
// `/api/tasks` for the document rather than one per surface that wants the rows.
const POLL_MS = 20000;

// Which view is up, remembered across visits — a person who plans on the
// calendar plans on the calendar every time. List is the default now (Akshil,
// 2026-08-17): the page's first question turned out to be "what is running",
// not "when", and the calendar is the drill-down for the scheduled subset.
//
// SECOND to the URL, since 2026-08-18. `?view=` (tasks-lib.viewFromSearch) is
// what a link carries and therefore what wins; this key is the fallback for a
// bare `/tasks`, which is how the page is opened from the sidebar. Both are
// kept in step, so switching the view in one tab still greets the next visit
// the same way.
//
// Shared with the app page's Tasks tab (AppPage.tsx, D488), which mounts this
// same component scoped to one folder: picking Board there is remembered here
// too. One page, one memory — a per-app key would make the same control forget
// on every other app.
const VIEW_KEY = "fused-render:scheduled-view";

// How far ahead a deep link's prefilled time lands. The form's own default is
// +1h, which is a planning answer; a link says "I want to set this up now", so
// the time it opens on should be near-now and only then adjusted. Two minutes
// rather than one because the when-field is minute-precision, and a value
// inside the CURRENT minute opens the form on a time already behind the clock.
const NEW_LINK_LEAD_MS = 120_000;

/**
 * WHAT A DEEP LINK HANDED THIS ONE OPENING — the chat composer's Schedule
 * button, and nothing else on the page.
 *
 * ONE OBJECT, SEEDED BY `openForm`, because the bug was that it used to be six
 * loose `useState`s cleared one at a time in the modal's `onClose` — and
 * `attachments` was never on that list (Akshil, 2026-09-12). A composer hop
 * carrying an image therefore left the chips standing in page state, and the
 * NEXT "+ New task" — and every one after it, for the life of the page — opened
 * holding a picture from a chat the reader had walked away from. A
 * clear-on-close is a list somebody has to keep in step with the fields; a
 * seed-on-open cannot fall out of step, because there is no second place that
 * says what a hop is made of.
 */
interface ChatHop {
  /** The chat record this card is EDITING — a session id, or `new:<file>`.
   *  `""` for every opening that did not come out of a chat. */
  key: string;
  /** Where "Back to chat" lands, verbatim from the hop's `?from=`. */
  from: string;
}

/** The opening that came from nowhere: every way into the form but the hop.
 *  A module constant so `openForm`'s default argument is one stable value. */
const NO_HOP: ChatHop = { key: "", from: "" };

/**
 * ONE CHAT RECORD, AS THE NEW TASK CARD OPENS ON IT (design "one record", §1).
 *
 * The hop no longer carries a sentence, a tray or a session in its URL — it
 * carries the KEY, and this turns what is stored under that key into the seed
 * the card already knows how to mount on (`DraftSeed`, the shape a reopened
 * task draft uses). One seeding rule for both kinds of draft, which is what
 * makes "the modal opens on the record" a true sentence rather than two
 * near-identical paths.
 *
 * THE WORDS COME OUT OF `text`, SPLIT, and they go back into `text`, JOINED
 * (contract §1: the words live in `text`, never in `form.description`; the
 * modal splits and joins with `splitDraft`/`joinDraft`). So the composer's box
 * and the card's two prose fields are two views of one string, and a hop out
 * followed by Back to chat is lossless. Nothing here reads `form.title` — the
 * title IS the first line, which is also what the listing row prints.
 *
 * `session_id` is the KEY when the key is a session: a hop out of a chat that
 * has already run is a message into that thread, and that is what the Schedule
 * payload's `sessionId` is built from. `new:<file>` has no thread to continue,
 * and its `<file>` is the folder the card falls back to when the record names
 * no target of its own.
 *
 * `at` IS WHAT A SESSION KEY CANNOT SAY (Akshil, 2026-09-16). A `new:<file>` key
 * spells its folder; a session id spells only the thread, so this used to fall
 * through to `""` and the card opened on the reader's HOME — and its first
 * autosave then wrote that home path onto the conversation's own record as the
 * target. Every door that knows the folder now states it: the hop's `?target=`
 * (the composer's own `file`), or a draft row's `project`. The stored form still
 * outranks it, because a form that names a target is a choice somebody made.
 */
export function chatHopSeed(
  key: string,
  record: ChatDraft | null,
  at = "",
): DraftSeed | null {
  if (!record) return null;
  const split = splitDraft(record.text);
  const form = (record.form ?? {}) as Record<string, unknown>;
  const newChat = key.startsWith(NEW_CHAT_PREFIX);
  return {
    id: "",
    form: {
      ...form,
      title: split.title,
      description: split.description,
      attachments: record.attachments ?? [],
      target: (typeof form.target === "string" && form.target)
        || (newChat ? newChatFile(key) : "") || at || "",
      session_id: newChat ? "" : key,
    },
  };
}

/**
 * WHAT TIME A FORM THAT ALREADY EXISTS OPENS ON — its own, or none (Bugbot, PR
 * #1126, 2026-09-12).
 *
 * `NEW_LINK_LEAD_MS` is an answer to a question a FRESH link asks: a card that
 * has never been saved opens on now+2m because a deep link means "set this up",
 * and a near-now time is the one to adjust from. A form that has been saved
 * already answered that question, and the answer is in it — `when`, which the
 * store keeps as null until somebody actually opens the when-row and picks one
 * (NewJobModal `draftBody`). Null there means IMMEDIATE: a task to run, not a
 * task to plan.
 *
 * Handing the lead date to such a form overwrote that. `creating` being a Date
 * is what makes the card `planning`, `planning` is what makes `timePicked` open
 * true, and `timePicked` is what puts `when` in the next autosave — so merely
 * reopening an immediate draft rewrote it as one scheduled for two minutes'
 * time, and Schedule sent it that way. So: the stored time when there is one,
 * and nothing at all when there is not. Only the opening with no stored form
 * behind it keeps the lead (see both call sites).
 *
 * An unreadable `when` reads as none here. The card seeds its own field from
 * the string verbatim (`seededDraftForm`), so nothing is lost by this one
 * declining to guess a Date out of it.
 */
export function reopenTime(seed: DraftSeed | null): Date | null {
  const when = (seed?.form as { when?: unknown } | null | undefined)?.when;
  if (typeof when !== "string" || !when) return null;
  const at = new Date(when);
  return Number.isNaN(at.getTime()) ? null : at;
}

export default function Scheduled({ scope }: { scope?: TasksScope } = {}) {
  // THIS PAGE IS THE POLLER while it is open. The sidebar's Tasks entry reads the
  // same rows (shell/tasksPulse) and would otherwise run a timer of its own
  // alongside this one — two calls to /api/tasks for one answer, at two
  // cadences. Holding a feeder for the page's lifetime says "take my answers,
  // make no calls", so the shared store stands down until this unmounts.
  useTasksFeeder();
  const [state, setState] = useState<ScheduleResult | null>(null);
  // NOT `[]`, because this page remounts on every navigation and /api/tasks is
  // 2.9s on the first call of a server process — so a bare `[]` meant List →
  // Home → List went back to a skeleton over a listing this session had already
  // read, and a cold first visit showed nothing while the sidebar beside it
  // already knew every task's name (see .claude-design/tasks-cold-load.md).
  //
  // Two seeds, best first: the last full listing of this JS session, else rows
  // upcast from the pulse store the sidebar has been filling all along. Both
  // are replaced whole by the poll below — this is what the page paints WHILE
  // that call is in the air, not a cache it trusts.
  const [tasks, setTasks] = useState<Task[]>(
    () => readListing() ?? provisionalTasks(readTasksRows()),
  );
  const [tasksFailed, setTasksFailed] = useState(false);
  // Has ANY listing been on this page yet — a seed above, a poll's answer, or a
  // poll's failure? Until one has, `tasks` being `[]` means "not asked yet",
  // not "none", and the view below must not say "No tasks yet" over it: a
  // reload straight onto /tasks (or the app launching onto it) showed that
  // empty state for the whole cold listing — up to seconds after a server
  // start — then filled in, which reads as the page having lost the tasks and
  // found them again (Akshil, 2026-09-09). A skeleton is the honest state.
  //
  // A remembered listing counts even when it is EMPTY: a machine with no tasks
  // asked once and got `[]`, and a remount must show the empty state it earned,
  // not a skeleton over it (Bugbot, #1079). Provisional rows count only when
  // there are some — an empty pulse store is exactly "not asked yet".
  const [tasksLoaded, setTasksLoaded] = useState(
    () => readListing() !== null || tasks.length > 0,
  );
  // THE CLAIMS A QUEUE VERB MAKES, until the server speaks about the same key
  // (tasks-lib.applyQueueOverrides). They live on the PAGE and not in the view
  // that raised them for one reason: a claim exists to outrun the poll, and a
  // view is remounted by every navigation — a store inside one would be undone
  // by the answer it was written to beat. They are retired by the listing feed's
  // subscription below, which is the one place the server's answer arrives.
  const [queueOverrides, setQueueOverrides] = useState<QueueOverrides>(NO_QUEUE_OVERRIDES);
  const noteQueued = useCallback((override: QueueOverride) => {
    setQueueOverrides((cur) => withQueueOverride(cur, override));
  }, []);
  const [queued, setQueued] = useState<ScheduledMessage[]>([]);
  const [running, setRunning] = useState<ScheduledMessage[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  // localStorage can THROW (private mode, locked-down webviews), and this read
  // runs during first render — unguarded it took the whole page down for a
  // preference. Storage failing costs the memory, never the page.
  const [view, setView] = useState<TaskView>(() => {
    let saved: TaskView = "list";
    try {
      const stored = localStorage.getItem(VIEW_KEY);
      // Against TASK_VIEWS rather than a hand-written list of the non-default
      // ones: this store is untrusted input (an older build wrote it, a person
      // edited it), and the one place that decides what a view NAME is has to be
      // the same place `?view=` reads (tasks-lib.TASK_VIEWS).
      if (stored && (TASK_VIEWS as string[]).includes(stored)) saved = stored as TaskView;
    } catch {
      // A blocked store just means no remembered view; the URL may still say.
    }
    // The URL outranks the memory, and the memory is what a bare `/tasks`
    // falls back to. Read once, in the initialiser: the page remounts on every
    // navigation (App.tsx keys it on the nav epoch), so a back button onto
    // `?view=board` comes through here rather than needing a subscription.
    return viewFromSearch(location.search, saved);
  });
  // null = closed; a Date = open, prefilled (from a calendar slot click);
  // "blank" = open from the New task button, prefilled with "in an hour".
  const [creating, setCreating] = useState<Date | "blank" | null>(null);
  // A task being changed reopens the same modal prefilled. Editing an
  // OCCURRENCE means editing its rule: the template is what gets edited, and
  // the resolver below is what makes a click on any run land there.
  const [editing, setEditing] = useState<ScheduledMessage | null>(null);
  // WHICH OPENING THIS IS. Bumped every time the form is opened, and part of the
  // modal's `key` below, so no two openings can ever share a React identity.
  //
  // The form reads `editing` in `useState` initialisers — they run ONCE, on
  // mount — so an opening that reuses the previous one's mount inherits every
  // value the user left behind. The key was `editing ? "edit:<id>" : "new"`,
  // which is not an identity but a MODE: two different new-task openings, and
  // two clicks on two different calendar slots, are the same string. That is how
  // a fresh card came up with Repeat ticked on "Weekly on Monday, 5 times" and a
  // date in three weeks — settings from a form the user had opened earlier and
  // never chosen here (QA, 2026-08-18). A stale recurrence is not a cosmetic
  // slip: pressing Save on it schedules a repeating task nobody asked for.
  //
  // A counter rather than more fields in the key, because the bug is not about
  // WHAT the openings differ in — it is that "this is a new opening" was never
  // stated at all, and any key built out of the form's inputs collides again the
  // moment two openings happen to share them.
  // WHICH CHAT RECORD THIS OPENING IS EDITING, and where it came from — see
  // `ChatHop` above. Set by `openForm` and by nothing else, which is what keeps
  // one chat's handoff from seeding the next card the reader opens.
  const [hop, setHop] = useState<ChatHop>(NO_HOP);
  const [openSeq, setOpenSeq] = useState(0);
  // The single door into the form, so "clean slate" is one rule in one place: a
  // new opening is a new mount, and opening a NEW task drops whatever was being
  // edited (leaving it set kept the card in Edit mode under a "+ New task"
  // press).
  //
  // …and it is where the deep link's values are SPENT: every opening states its
  // own hop, and the ones that had none say so by saying nothing (`NO_HOP`, the
  // default). That is the whole of fix for the attachments that used to follow
  // the reader from one New task card to the next — see `ChatHop` (Akshil,
  // 2026-09-12).
  const openForm = (
    at: Date | "blank" | null,
    entry: ScheduledMessage | null,
    seed: ChatHop = NO_HOP,
    draft: DraftSeed | null = null,
  ) => {
    // Every opening ABANDONS any chat-draft fetch still in flight (Bugbot on
    // PR #1126, 2026-09-12): a press that lands here through any door — New
    // task, a calendar slot, an Edit, a draft row, or a resolved hop — is the
    // opening the reader meant, and an older `openChatDraft` answer arriving
    // afterwards must not paint over it. See `chatDraftGen` below.
    chatDraftGen.current++;
    setOpenSeq((n) => n + 1);
    setHop(seed);
    setEditing(entry);
    setDraftSeed(draft);
    setCreating(at);
  };
  // An unfinished New task form this card already saved, re-opened: its id and
  // the stored form, which is everything `NewJobModal` needs to come up on it
  // and to go on autosaving under the SAME id.
  //
  // TWO DOORS REACH IT, and they hand it over the same way. A draft ROW carries
  // the form on the row itself (`/api/tasks` emits it), so `openDraft` seeds
  // from the row with no second fetch. And a SESSION-BOUND draft has no row at
  // all — the conversation's row wears the chip instead (routers/tasks.py
  // `_bound_chips`) — so the composer's own Schedule hop is the way back to it,
  // and the effect below looks it up by session before opening (Akshil,
  // 2026-09-12).
  const [draftSeed, setDraftSeed] = useState<DraftSeed | null>(null);
  /** THE GENERATION OF THE PRESS THAT OWNS THE MODAL. Two openings still fetch
   *  before they can seed — the `?new=1&draft=` hop and the Draft chip's press —
   *  and an answer whose number is no longer current is dropped rather than
   *  painted over a card the reader has since opened another way (Bugbot on
   *  PR #1126). Every door through `openForm` takes a number, so this is the
   *  whole rule and not a per-arm one. */
  const chatDraftGen = useRef(0);
  /**
   * THE CARD, OPENED ON ONE CHAT RECORD — the only way this page ever opens one
   * (Akshil, 2026-09-16).
   *
   * Three doors reach it and they hand over the same three things: the KEY, the
   * route "Back to chat" lands on, and the FOLDER the card falls back to when
   * the record names no target. The `?new=1&draft=` hop states all three in its
   * URL; a draft row and a bound-form line read them off the row. One read, one
   * seeding rule (`chatHopSeed`), no merge to get wrong — and, since every door
   * lands here rather than one of them navigating to a composer, pressing a
   * draft row on THIS page does not throw the page's filters and scroll away to
   * open a card that was always going to be drawn over it.
   *
   * A FAILED LOOKUP IS "UNKNOWN", NOT "NONE" (`fetchDrafts` answers null for a
   * blip). The card opens anyway, on the key it was given — it is the SAME
   * record either way, so an uninformed card costs a moment of empty fields and
   * never a second draft.
   *
   * A SECOND PRESS IS THE SAME PRESS: nothing is minted, and the generation
   * below drops the answer to an opening the reader has since replaced.
   */
  const openChatRecord = (key: string, from: string, at: string) => {
    const hopTo: ChatHop = { key, from };
    const lead = new Date(Date.now() + NEW_LINK_LEAD_MS);
    const gen = ++chatDraftGen.current;
    void fetchDrafts().then((all) => {
      if (gen !== chatDraftGen.current) return;
      // THE FOUND FORM'S OWN TIME, and no time at all when it had none — see
      // `reopenTime`. The lead date belongs only to the fallthrough, where the
      // lookup found nothing and the card is being opened fresh.
      const found = all && chatHopSeed(key, all.chat[key] ?? null, at);
      openForm(found ? reopenTime(found) : lead, null, hopTo, found);
    }, () => {
      if (gen !== chatDraftGen.current) return;
      openForm(lead, null, hopTo);
    });
  };
  const openChatDraft = (task: Task) => {
    openChatRecord(task.key, draftChatUrl(task), task.project || task.file || "");
  };
  /**
   * A DRAFT ROW'S PRESS, ON THIS PAGE — THE NEW TASK CARD, BOTH KINDS (Akshil,
   * 2026-09-16).
   *
   * The ROW still decides which record, because the two kinds are filed
   * differently — a chat draft under its own key, a task draft under an id —
   * but they now open the same card, which is what makes a draft row one thing
   * to learn instead of two. Neither mints anything; a task draft carries its
   * stored form on the row, so that arm reads nothing at all.
   */
  const openDraft = (task: Task) => {
    if (isChatDraftTask(task)) {
      openChatDraft(task);
      return;
    }
    if (!task.draft_id) return;
    // A reopened draft carries its own everything (the row's stored `form`), so
    // whatever a deep link said earlier in this page's life is not about it —
    // `NO_HOP`, through the same door every other opening takes.
    //
    // …AND NO TIME, which is `reopenTime`'s rule stated the short way (Bugbot,
    // PR #1126, 2026-09-12): the card reads its own `when` out of the stored
    // form, and a form that stored none is an immediate task that must stay one.
    openForm(null, null, NO_HOP, { id: task.draft_id, form: task.form ?? null });
  };
  /**
   * THE BOUND FORM, OPENED FROM THE THREAD LINE THAT QUOTES IT (Bugbot, PR
   * #1126, 2026-09-12).
   *
   * The leading line of an expanded thread prints `task.draft.preview`, which is
   * this conversation's unsent words — and since the bound form arrived, those
   * words are sometimes in a New task card instead of in the composer. Pressing
   * it went to the chat either way, and after a hop the chat holds nothing: the
   * reader pressed their own sentence and landed somewhere it was not. The
   * server says which (`draft.kind`), and this is the other press.
   *
   * IT IS THE HOP DOOR, not a second way in — and now that is literally true:
   * a session-bound form IS the session's chat record (contract §1, "one record,
   * two doors"), so this opens the card on `chat[<session>]` through exactly the
   * function the Schedule hop seeds from. One lookup, one seeding rule, one
   * card, and no second draft to reconcile with the first.
   *
   * Same generation guard as every other opening that fetches first, and the
   * same fallthrough: a lookup that fails opens the card anyway rather than
   * dying under the cursor.
   */
  const openBoundDraft = (task: Task) => {
    const session = (task.session_id ?? "").trim();
    if (!session) return;
    openChatRecord(session, draftChatUrl(task), task.project || task.file || "");
  };
  // Search, status and project, client-side only — nothing here is worth a URL
  // or a localStorage row: a filter is how you read the page this minute.
  const [filters, setFilters] = useState<TaskFilters>(EMPTY_FILTERS);
  // Only so a folder chip's tooltip can say "~/Desktop/fused" rather than the
  // full /Users/... path. Missing home just means untouched paths.
  const [home, setHome] = useState("");
  useEffect(() => {
    getConfig().then((c) => setHome(c.home), () => {});
  }, []);

  // `?new=1&draft=<chat key>&target=<folder>&from=<route>` — the hop
  // (`apps/claude/sched/scheduled.schedulerUrl`), pressed by the chat
  // composer's Schedule button AND by every draft row anywhere. The whole
  // handoff is those three values: WHICH record to open, WHICH folder it is
  // about, and where to go back to. The words, the tray and the session used to
  // ride the URL as `?message=`, `?attachments=` and `?session_id=`, which is
  // three copies of a thing the server already holds — see design "one record",
  // §1. `target` is not a fourth copy: it is the one fact a session KEY cannot
  // state, and without it the card opened on the reader's home folder.
  //
  // The params are CONSUMED, not just read: cleared with replaceState so a
  // reload (or Back to here from wherever the user went next) is the plain
  // Tasks page rather than a modal that reopens forever. replaceState, not
  // push, for the same reason — the deep-linked URL is not a place worth
  // keeping in the history.
  //
  // `?edit=<entry id>` — the chat's blocked-composer banner sends the user here
  // to reschedule or stop the message that is blocking it. It cannot be handled
  // in the effect below, because the entry it names lives in a fetch that has
  // not answered yet on first render; it is held here and resolved once the
  // schedule lands.
  const [editId, setEditId] = useState<string | null>(null);
  useEffect(() => {
    const q = new URLSearchParams(location.search);
    // `?edit=` TRAVELS ON ITS OWN TOO (Akshil, 2026-09-16): a scheduled-later row
    // in a chat's Recent list presses it, and that press carries no `new=1`
    // because there is no draft record behind it — only an entry to change or
    // stop. Read before the hop's guard, and consumed below with the rest.
    setEditId(q.get("edit"));
    if (q.get("new") !== "1") {
      if (!q.get("edit")) return;
      q.delete("edit");
      const left = q.toString();
      history.replaceState(history.state, "", location.pathname + (left ? `?${left}` : ""));
      return;
    }
    const key = q.get("draft") ?? "";
    const at = new Date(Date.now() + NEW_LINK_LEAD_MS);
    // A `?new=1` WITH NO KEY is the app page's own "+ New task" link: there is
    // no chat behind it and nothing to read, so the card opens on the lead date
    // in this tick, exactly as it always has.
    if (!key) {
      // …THOUGH IT MAY STILL SAY WHERE IT CAME FROM. A Schedule pressed in an
      // EMPTY never-sent composer has no record to hand over — a draft with no
      // words is a row saying nothing — but it does know the folder the chat is
      // mounted on and the route back to it, and a blank card that opened on
      // the reader's home with no way back would be the press half working
      // (Akshil, 2026-09-16).
      const from = q.get("from") ?? "";
      const at0 = q.get("target") ?? "";
      // `id: ""` IS "NO DRAFT", NOT A DRAFT CALLED "" (Bugbot 4028344040). The
      // seed is here to carry the FOLDER and nothing else, and the card reads an
      // empty id as a form nobody has minted — so a settings-only change on this
      // blank card still writes no Untitled row. See `NewJobModal`'s `draftId`.
      openForm(
        at,
        null,
        from ? { key: "", from } : NO_HOP,
        at0 ? { id: "", form: { target: at0 } } : null,
      );
    } else {
      // ONE READ, AND THE CARD OPENS ON WHAT IT ANSWERS. The record holds the
      // words, the tray and whatever settings a previous hop left on it, so
      // there is one seeding rule (`chatHopSeed`) and no merge to get wrong.
      //
      // …AND A RECORD THAT ALREADY CARRIES A TIME IS A REOPEN, so it opens on
      // that time rather than on the lead date (`reopenTime`, Bugbot PR #1126):
      // `now+2m` here turned every reopened immediate draft into a scheduled
      // one.
      //
      // A FAILED LOOKUP IS "UNKNOWN", NOT "NONE" (`fetchDrafts` answers null for
      // a blip). The card opens anyway, on the key it was given — it is the
      // SAME record either way, so an uninformed card costs a moment of empty
      // fields and never a second draft. That is the difference one record
      // makes: the failure mode used to be an eviction.
      openChatRecord(key, q.get("from") ?? "", q.get("target") ?? "");
    }
    q.delete("new");
    q.delete("draft");
    q.delete("target");
    q.delete("from");
    q.delete("edit");
    const rest = q.toString();
    history.replaceState(history.state, "", location.pathname + (rest ? `?${rest}` : ""));
  }, []);

  /**
   * `?draft=<id>` — AN UNFINISHED NEW TASK FORM, PRESSED SOMEWHERE ELSE
   * (Akshil, 2026-09-14).
   *
   * This is how a TASK draft opens from anywhere — its own row on this page,
   * and the chat landing's Recent list, whose press builds exactly this URL
   * (`apps/claude/ui/list-rows.draftHref`).
   *
   * `?new=1` OWNS THE `draft` PARAM WHEN IT IS THERE, and this arm stands down
   * for it: the hop's `?new=1&draft=<chat key>` names a CHAT record, the two
   * keys are both bare strings, and a card cannot be opened as both. One arm
   * per shape, decided by `new`.
   *
   * IT IS `openDraft`'S OWN ARM, reached by a param rather than by a row — same
   * id, same stored form, same `NO_HOP` and the same "no time" rule
   * (`reopenTime`: a reopened draft reads its `when` out of the form it stored,
   * and an immediate task must stay one) — EXCEPT when the press came out of a
   * composer, which says so with `&hop=1` and opens on the lead date like every
   * other hop (see below). The form comes off `GET /api/drafts`
   * rather than off a row, because the listing has not answered on first render
   * and this opening must not wait for 800 rows to decide which card to be.
   *
   * A lookup that fails opens the card on the id anyway: the server folds a
   * write naming an existing id into that draft (`drafts.py put_task`), so an
   * uninformed card costs a moment of stale fields and never a second draft.
   * Same generation as every other door that fetches first, so a second press
   * through any of them owns the modal.
   *
   * The param is CONSUMED, exactly as `?new=1` is: a reload that reopened the
   * card for ever is a URL worth nothing to go back to.
   */
  useEffect(() => {
    const q = new URLSearchParams(location.search);
    if (q.get("new") === "1") return; // the hop's own param — see above
    const id = q.get("draft");
    if (!id) return;
    // `&from=` — THE WAY BACK, WHEN THERE IS ONE (Akshil, 2026-09-16). A draft
    // ROW presses this URL bare: it was opened from a list, and there is no
    // conversation behind it to return to. The chat composer's Schedule now
    // presses it too — a never-sent chat mints a TASK draft per press rather
    // than one chat record per folder (`apps/claude/sched/scheduled.taskDraftUrl`)
    // — and that press DID come out of a chat, so it names the route home and
    // the card draws "Back to chat" for it. No `key`: this card is editing a
    // task draft, not a chat record.
    const from = q.get("from") ?? "";
    const hopTo: ChatHop = from ? { key: "", from } : NO_HOP;
    // `&hop=1` — A PRESS OUT OF A COMPOSER, NOT A ROW (Bugbot 4028344051).
    //
    // A draft row is a REOPEN and takes the time the draft stored, which for an
    // immediate draft is none (`reopenTime`). A Schedule press is the same
    // gesture the session hop's `?new=1` makes and must land the same way: on
    // now+2m, with the card planning, so the when-row is open and the Schedule
    // button is labelled for a scheduled run. Without it the card opened with no
    // lead time and folded, and its confirm named a time the task would not
    // wait for — it ran at once.
    //
    // A RECORD THAT ALREADY CARRIES A TIME STILL OUTRANKS THE LEAD, exactly as
    // `openChatRecord` has it: a hop the reader made, went back from and made
    // again opens on the time they picked.
    const hopped = q.get("hop") === "1";
    const lead = new Date(Date.now() + NEW_LINK_LEAD_MS);
    const openAt = (seed: DraftSeed | null): Date | null =>
      hopped ? reopenTime(seed) ?? lead : null;
    const gen = ++chatDraftGen.current;
    void fetchDrafts().then(
      (all) => {
        if (gen !== chatDraftGen.current) return;
        // Spread rather than handed over: `TaskDraft` is an interface and
        // `DraftSeed.form` is an index-signature bag, and only a fresh object
        // literal crosses that gap.
        const stored = all?.task[id];
        const seed: DraftSeed = { id, form: stored ? { ...stored } : null };
        openForm(openAt(seed), null, hopTo, seed);
      },
      () => {
        if (gen !== chatDraftGen.current) return;
        openForm(openAt(null), null, hopTo, { id, form: null });
      },
    );
    q.delete("draft");
    q.delete("hop");
    q.delete("from");
    const rest = q.toString();
    history.replaceState(history.state, "", location.pathname + (rest ? `?${rest}` : ""));
  }, []);

  // Three feeds, one poll, INDEPENDENT failures — each is allowed to fail
  // without taking the others down, because each answers a different question
  // and two thirds of an answer beats an error page.
  //
  // The schedule is the only one whose failure is worth a banner: it carries
  // the permission modes the form needs, so without it the page cannot even
  // offer to create anything. Tasks failing costs the rows (a quiet line says
  // so). The queue failing costs the Queued strip, and says nothing at all —
  // an empty queue and an unreadable one look the same to a user, and the
  // common case by far is that there is simply nothing waiting.
  //
  // TWO OF THEM HERE NOW, not three: the ROWS moved to the shared listing feed
  // (`tasksPulse.subscribeListing`, subscribed below), which runs one
  // `/api/tasks` and one change long-poll for the whole document — so this page
  // and every chat card on it read the same listing off the same socket, and the
  // sidebar's dot, which that feed publishes, cannot disagree with the rows under
  // it. This pair keeps its own clock because it is its own pair of endpoints.
  const reloadFeeds = () => {
    getSchedule().then(
      (r) => {
        setState(r);
        setLoadError(null);
      },
      (e: Error) => setLoadError(e.message),
    );
    getScheduleQueue().then(
      (r) => {
        setQueued(r.queued ?? []);
        setRunning(r.running ?? []);
      },
      () => {
        setQueued([]);
        setRunning([]);
      },
    );
  };
  /** "Re-read everything on this page NOW" — what a created task, a returned-to
   *  tab or a finished run asks for. The rows answer through the feed's own
   *  refresh, which is collapsed to one read for the document. */
  const reload = () => {
    reloadFeeds();
    refreshListing();
  };
  useEffect(reloadFeeds, []);
  useRefreshOnReturn(reload);
  useEffect(() => {
    // The ROWS are deliberately not on this timer: the feed carries the same 20s
    // floor (LISTING_FLOOR_MS), and asking here as well would be two full listing
    // reads every twenty seconds for one answer.
    const id = window.setInterval(reloadFeeds, POLL_MS);
    return () => window.clearInterval(id);
  }, []);
  // The corner card knows a run ended about a second after it does; this page's
  // own clock is 20s. pokeTasks forwards that knowledge here as a window event —
  // it has already refreshed the listing itself, so what this answers for is the
  // schedule and the queue, and the row flips the moment the popover does rather
  // than up to a poll later.
  useEffect(() => {
    window.addEventListener(TASKS_POKE_EVENT, reloadFeeds);
    return () => window.removeEventListener(TASKS_POKE_EVENT, reloadFeeds);
  }, []);
  // THE ROWS, LIVE — one feed for the document (`tasksPulse.subscribeListing`).
  //
  // The fast lane is still `/api/tasks/changes`, long-polling the server's change
  // watcher (tasks_watch.py) so a `claude` typed into a terminal in some folder is
  // a row here within a second rather than up to a poll later; only the rows that
  // moved come back and are folded into place, and a 20s floor read stays as the
  // truth underneath. What changed is WHOSE loop it is: this page used to run one
  // and every ClaudeChat mount on it ran another, so the cards wall with twelve
  // chats open held thirteen sockets on a 25-second wait against a browser cap of
  // six, and every other request on the page queued behind them. The feed also
  // publishes to the sidebar and remembers the listing for the next mount, which
  // is what this effect used to do by hand.
  useEffect(
    () =>
      subscribeListing((ev) => {
        setTasks(ev.rows);
        setTasksFailed(ev.failed);
        setTasksLoaded(true);
        // THE SERVER HAS SPOKEN about every key it named, so every claim about
        // one of them is over — right or wrong (tasks-lib.expireQueueOverrides).
        // A full listing speaks about every key it holds; a delta about exactly
        // the rows and `gone` keys it carries — which is the fast half of the
        // same rule, since every queue verb rings the watcher and the delta it
        // rings usually lands within milliseconds of the press that made the
        // claim. A FAILED read has said nothing, and retires nothing.
        if (ev.failed) return;
        const spoken = ev.delta
          ? [...ev.delta.rows.map((t) => t.key), ...ev.delta.gone]
          : ev.rows.map((t) => t.key);
        setQueueOverrides((cur) => expireQueueOverrides(cur, spoken));
      }),
    [],
  );

  // A folder chip pressed on a row or a card: filter the page to that
  // project, pressing the pinned one again clears it. It REPLACES the project
  // selection rather than adding to it — the gesture means "show me this
  // folder", and a press that quietly widened an existing selection would be
  // the opposite of what it looks like. Everything else about the filters is
  // left alone, so a status or a search already on stays on. A TOGGLE, because
  // the chip stays on screen wearing the state: pressing the folder you are
  // already filtered to is the obvious way to let it go. ONE handler for the
  // List and the Cards wall (Akshil, 2026-09-05: the card's chip must filter
  // like the List's), so the two cannot drift.
  const pickProject = (project: string) =>
    setFilters((f) => ({
      ...f,
      projects: f.projects.length === 1 && f.projects[0] === project ? [] : [project],
    }));

  // The Draft chip pressed on a row or a card: keep only the rows carrying
  // unsent words, and press it again to let them all back (design.md, Round 2).
  // A plain toggle of one boolean, unlike the folder's replace-not-widen rule,
  // because there is only one of it — and everything else about the filters is
  // left alone for the same reason: a search or a project already on stays on,
  // and this narrows what they left. ONE handler for the List and the Board,
  // exactly like `pickProject`, so the two views cannot drift.
  const pickDraft = () => setFilters((f) => ({ ...f, draft: !f.draft }));

  const pickView = (v: TaskView) => {
    setView(v);
    // Into the URL, so the view is a thing you can link to and reload onto.
    // replaceState, not push: see tasks-lib.viewUrl — the toggle is a way of
    // reading this page, not a place to come back to. The path is taken from
    // `location` rather than hardcoded so this cannot be the thing that has to
    // be remembered on the next rename.
    try {
      history.replaceState(history.state, "", viewUrl(location.pathname, location.search, v));
    } catch {
      // Some embeddings refuse history writes; the switch itself still happens.
    }
    try {
      localStorage.setItem(VIEW_KEY, v);
    } catch {
      // A blocked store forgets the choice; the switch itself still happens.
    }
  };

  const entries = state?.entries ?? [];

  // Every folder that has a task, for the project filter. Derived from the
  // tasks themselves rather than from a separate call: the set of projects IS
  // "the folders these tasks are in", and any other source could disagree.
  // The app page's scope, applied FIRST: `tasks` above stays the whole machine
  // (it is what publishTasks hands the sidebar), and everything the page shows
  // or offers to filter is derived from this narrowed set instead.
  // The server's rows with the standing queue claims painted over them. FIRST,
  // ahead of the scope and the filters, so a row a claim moves into Queued is
  // filtered and counted as queued by everything downstream — the Status facet
  // included. `publishTasks` above deliberately hands the sidebar the UNPAINTED
  // rows: a claim is this page's optimism about a press made on this page, and
  // the rail is not the place to carry it.
  const painted = useMemo(
    () => applyQueueOverrides(tasks, queueOverrides),
    [tasks, queueOverrides],
  );
  const inScope = useMemo(
    () => (scope ? painted.filter((t) => isUnderDir(t.project, scope.project)) : painted),
    [painted, scope],
  );
  const projects = useMemo(() => projectOptions(inScope), [inScope]);
  // The Archive facet does not apply on the Calendar (see
  // tasks-lib.filtersForView): a hidden selection must never silently filter
  // that view's grid to nothing, so the query it runs drops "archived" from
  // the status list while the STORED `filters` — and therefore the popover's
  // tick and the badge on List/Board — stay exactly as the user left them.
  const shown = useMemo(
    () => filterTasks(inScope, filtersForView(filters, view)),
    [inScope, filters, view],
  );
  // Which of the shown tasks' folders the disk no longer has — asked once per
  // folder, so a row can say "Folder missing" instead of opening an Explorer
  // that can only answer with a stat error (useMissingFolders).
  const missing = useMissingFolders(shown);
  // THE SIDE PEEK, and it belongs to THIS page only: `useTaskPeekHost` is what
  // arms `openPeek` for the four views, adopts a `?peek=` deep link and follows
  // Back. Scoped (the app page's Tasks tab) it stays disarmed, so every press
  // there navigates exactly as it did — the same rule that leaves the sidebar's
  // task list and the notifications alone
  // (.claude-design/task-side-peek/design.md).
  // THE FLAG (task-peek-flag.ts, `task_peek_enabled`): default ON since
  // 2026-09-17, and OFF — the switch, or the first frames before the prefs read
  // lands — means this page is the page it has always been: no panel, no
  // `?peek=`, no measured fit, no walk attributes. Read here and handed down,
  // so there is one answer for the whole page.
  const peekOn = useTaskPeekEnabled();
  // The toolbar folds its words before it clips them (shell/row-fit.ts) — and
  // only while the feature is on, since the ladder arrived with it.
  const [toolbar, toolbarRef] = useToolbarFit(peekOn);
  const peekable = !scope && peekOn;
  useTaskPeekHost(peekable);
  const peek = useTaskPeekLayout(peekable);
  // SCROLL, DON'T FOLD (Akshil, 2026-09-15). `data-floored` used to switch on
  // at the middle pane's floor only, and the row ladder folded marks on the way
  // down to it. With the floor at a flat 500 that meant hiding meta across the
  // whole 1094→500 range — so the switch is now `tight` (frame narrower than
  // the column): under it the list's content is held at the widest row's need
  // and the pane scrolls sideways. Floored is a subset of tight (500 < any
  // baseline), so nothing the floor did is lost.
  const scrolls = peek.open && peek.tight;
  // THE MIDDLE PANE'S BASELINE (design.md, Widths v2). What is kept here is the
  // WATCH; the measurement itself is the store's (`measureTasksBaseline`), for
  // a reason worth stating where a reader would come looking for it: this
  // effect is passive, and `useTaskPeekHost`'s adoption of a `?peek=` deep link
  // is a LAYOUT effect — it runs first, so a link-opened visit would freeze a
  // baseline this observer had never had a chance to take. The store reads the
  // page itself when it is asked for a number it does not have, and this watch
  // is only the cheap path for the ordinary case.
  const frameRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!peekable) return;
    const frame = frameRef.current;
    if (!frame) return;
    const read = () => refreshPeekBaseline();
    read();
    const ro = new ResizeObserver(read);
    ro.observe(frame);
    // The page's own sections arrive after the first fetch, so the element the
    // measurement needs may not exist on the first tick.
    const mo = new MutationObserver(read);
    mo.observe(frame, { childList: true, subtree: true });
    return () => {
      ro.disconnect();
      mo.disconnect();
    };
  }, [peekable]);
  // ONE sentence for "there is nothing here", handed to all four views, so a
  // reader flipping List → Board → Cards → Calendar over the same empty set
  // reads the same words in the same place (Akshil, 2026-09-09). Which sentence
  // is the page's call, not a view's: only the page knows whether the set is
  // empty because the machine has no tasks, this app has none, or the filters
  // matched none.
  const emptyLabel =
    inScope.length === 0
      ? scope
        ? "No tasks for this app yet."
        : "No tasks yet. Everything Claude runs for you shows up here."
      : "Nothing matches these filters.";

  // PUT THE CARD AWAY — the modal's ✕, and the source chip's press, which opens
  // the task it names and cannot do that under an open card. One function, so
  // "closing keeps the draft" (design.md, Decisions) has one meaning: the
  // modal's own autosave has already flushed on unmount either way.
  const closeCard = () => {
    setCreating(null);
    setEditing(null);
    setDraftSeed(null);
  };

  /**
   * WHICH TASK THE OPEN CARD CAME OUT OF (design.md B, Option 1) — null for
   * every other opening.
   *
   * Nothing stores a "source task": scheduling from a task travels as its
   * SESSION, either on the hop (`?new=1&session_id=…`, the composer's Schedule
   * button) or on the draft that hop saved (`session_id` in the stored form,
   * which is what survives closing and reopening the card). Both name the same
   * conversation, and the listing this page already holds is what turns it back
   * into a row — no second fetch, and nothing new on the wire.
   *
   * `tasks` and not `inScope`: the source is a fact about this card, not about
   * what the app page is filtered to, and a chip that vanished inside an app
   * would be saying the task does not exist.
   */
  const sourceTask = useMemo(() => {
    if (editing) return null;
    // The hop's key IS the session when the chat has one (`new:<file>` is the
    // shape that has none), and the seed restates it for a card reopened from a
    // stored record.
    const session = chatKeySession(hop.key)
      || seededDraftForm(draftSeed).sessionId || "";
    if (!session) return null;
    return tasks.find((t) => t.session_id === session) ?? null;
  }, [editing, hop.key, draftSeed, tasks]);

  /**
   * …and where its chip goes: THE SIDE PEEK, which is the one door this page
   * owns. It does not navigate — deliberately, and the page holds none of the
   * tools for it (tasks-lib.test.ts pins that) — so where there is no peek to
   * open, the chip stays a statement rather than becoming a dead control: the
   * modal draws a button only for a card that hands it an `onOpen`.
   *
   * The card goes away first. The peek slides in BESIDE this page, which is
   * currently behind a modal, so opening one under the card would look like the
   * press did nothing. The draft survives that (`closeCard`), and its row is
   * one press away.
   */
  const openSourceTask = (task: Task) => {
    closeCard();
    openPeek(task.key);
  };

  // Editing is addressed by ENTRY id, not by task: a task is a thread, and a
  // thread has nothing to edit — only a message that has not gone out yet does.
  // An occurrence resolves to its template, because changing "tomorrow's run"
  // of a repeating task means changing the rule.
  const editEntry = (entryId: string) => {
    const entry = entries.find((e) => e.id === entryId);
    if (!entry) return;
    const template = entry.template_id
      ? entries.find((e) => e.id === entry.template_id)
      : null;
    openForm(null, template ?? entry);
  };

  // Resolve `?edit=<entry id>` once the schedule has actually arrived. The chat
  // sends the user here from its blocked-composer banner, and it names the very
  // message that is blocking them — so landing on a prefilled NEW task form
  // instead of that message would quietly create a second one and leave the
  // block in place.
  //
  // Resolved inline rather than through `editEntry` so the effect can depend on
  // exactly what it reads. Cleared either way: an id that no longer resolves —
  // the run fired, or was cancelled elsewhere while the page loaded — drops back
  // to the plain page rather than retrying for ever.
  useEffect(() => {
    if (!editId) return;
    const all = state?.entries;
    if (!all) return;
    const entry = all.find((e) => e.id === editId);
    const template = entry?.template_id
      ? all.find((e) => e.id === entry.template_id)
      : null;
    if (entry) {
      openForm(null, template ?? entry);
    }
    setEditId(null);
  }, [editId, state]);

  // THE FRAME, and only a name for it while the peek is off: the page is
  // exactly what it was, and the flex row below is added only on `/tasks`.
  const page = (
    // `schedule-page` is not decoration: it is what lets the card sections opt
    // out of the 760px content column `.prefs-page > *` imposes, while the prose
    // inside them stays at that measure. See styles/schedule.css.
    <div className="prefs-page schedule-page">
      {/* Title only — no description, no mechanics paragraph: the page says
          what it is by shape, and the line under it was buying nothing but
          vertical space the views wanted. The app-must-be-running caveat lives
          where a person meets its consequence — the Queued strip. */}
      {/* Scoped, the app page's own header names the app and the tab already
          says "Tasks"; a second heading would be the page saying its name twice. */}
      {!scope && (
        <header className="schedule-header">
          <h1>Tasks</h1>
        </header>
      )}

      {loadError && <ErrorBanner>Failed to load tasks: {loadError}</ErrorBanner>}
      {!state && !loadError && <SkeletonLines rows={2} label="Loading tasks" />}

      {state && (
        <section className="prefs-section schedule-main">
          {/* `data-fit` — how many of the toolbar's labels have had to fold for
              the row to fit the width it has. Measured, never a breakpoint
              (shell/row-fit.ts). */}
          <div
            className="schedule-toolbar"
            ref={toolbarRef}
            {...(peekOn ? { "data-fit": toolbar.level } : {})}
          >
            {/* The view toggle leads, at the far left of every view — it is the
                one control that must never change address, and anchoring it to
                the start of the row is what guarantees that regardless of what
                sits beside it. List first and default: the page's question is
                "what is running", and the calendar is the drill-down for the
                scheduled subset of it. */}
            {/* Icon + label on each half, added 2026-08-18. The three words are
                short and near-identical in weight, so the row read as a block of
                text you had to actually read; a list, a set of columns and a
                calendar are shapes you recognise before you read anything. The
                labels stay — an icon-only switcher for a control this central
                would be recognition traded for guessing (design-principles §4)
                — and the marks are lucide's, at the same 14px every other glyph
                on this page uses (ScheduleCalendar's `icon`). */}
            {/* `schedule-view-seg` and the per-button `data-view` are the Tasks
                tour's anchors (platform/lib/tours/tasks.ts): three other
                controls in the app wear `.schedule-form-seg` (the calendar's
                range, the modal's Ends), so the shared class cannot name this
                one. Styling still hangs off `.schedule-form-seg`. */}
            <div className="schedule-form-seg schedule-view-seg" role="radiogroup" aria-label="View">
              <button type="button"
                      data-view="list"
                      title="List"
                      className={"btn btn-secondary schedule-view-btn" + (view === "list" ? " is-active" : "")}
                      aria-pressed={view === "list"}
                      onClick={() => pickView("list")}>
                {ICON_VIEW_LIST}
                <span className="schedule-fit-lbl">List</span>
              </button>
              <button type="button"
                      data-view="board"
                      title="Board"
                      className={"btn btn-secondary schedule-view-btn" + (view === "board" ? " is-active" : "")}
                      aria-pressed={view === "board"}
                      onClick={() => pickView("board")}>
                {ICON_VIEW_BOARD}
                <span className="schedule-fit-lbl">Board</span>
              </button>
              {/* Before the calendar (Akshil, 2026-09-03): the first three
                  answer "what is there" and "what is happening right now",
                  and the calendar is the drill-down for the scheduled subset —
                  the same argument that made List the default. */}
              <button type="button"
                      data-view="cards"
                      title="Cards"
                      className={"btn btn-secondary schedule-view-btn" + (view === "cards" ? " is-active" : "")}
                      aria-pressed={view === "cards"}
                      onClick={() => pickView("cards")}>
                {ICON_VIEW_CARDS}
                <span className="schedule-fit-lbl">Cards</span>
              </button>
              <button type="button"
                      data-view="calendar"
                      title="Calendar"
                      className={"btn btn-secondary schedule-view-btn" + (view === "calendar" ? " is-active" : "")}
                      aria-pressed={view === "calendar"}
                      onClick={() => pickView("calendar")}>
                {ICON_VIEW_CALENDAR}
                <span className="schedule-fit-lbl">Calendar</span>
              </button>
            </div>
            {/* Search, Status and Project, on ALL THREE views (2026-08-18). They
                used to be hidden on the calendar, on the argument that it
                answers "when" and a week with tasks filtered out of it is a week
                that lies. That reading did not survive contact: the filters are
                not a claim about what exists, they are how you read the page
                this minute — the same three lenses, and a person who has just
                narrowed the List to one project and switched to Calendar meant
                to keep looking at that project, not to be handed everything
                back. Views are lenses on one dataset (design-principles §1), and
                a control that vanishes when you change lens makes them read as
                three different pages.

                They sit AFTER the toggle, which owns the row's only auto margin,
                so nothing here can move either end of the bar. */}
            <TaskFilterControls
              filters={filters}
              projects={projects}
              home={home}
              onChange={setFilters}
              hideArchiveStatus={view === "calendar"}
              // The ladder's Project rung: one trigger instead of two, same rows
              // inside it, each under its own heading (shell/row-fit.ts
              // TOOLBAR_DROPS). Project loses its own control here and stays
              // REACHABLE, which is the difference between folding a filter and
              // taking it away.
              merged={toolbar.level >= TOOLBAR_MERGE_LEVEL}
            />
            <button type="button" className="btn btn-primary schedule-new"
                    onClick={() => openForm("blank", null)}>
              +<span className="schedule-fit-lbl"> New task</span>
            </button>
          </div>

          {/* No chip row under the toolbar: each filter menu already carries its
              own count on its trigger (Status ①, Project ①), so a second row
              restating the same thing was duplication, not reassurance — and it
              only ever appeared for one of the two filters, which made the page
              look like it had lost the other. Clearing is where setting is: in
              the menu. */}
          {tasksFailed && (
            // One quiet line, not a banner: the form still works and only the
            // tasks are missing. Shown on the calendar too since 2026-08-18 —
            // its chips come from the same feed, so an empty week and an
            // unreadable one looked identical there.
            <p className="schedule-tv-note">Tasks could not be loaded.</p>
          )}

          {!tasksLoaded ? (
            // The view's own ghost, under the toolbar the schedule already let
            // us draw: the page has its final shape from the first paint, and
            // the reader can tell which view they are on before a row lands.
            <TasksSkeleton view={view} />
          ) : view === "calendar" ? (
            <ScheduleCalendar
              emptyLabel={emptyLabel}
              // The FILTERED set, same as the other two views get: the toolbar's
              // three controls are live here now, and a filter that is shown but
              // does nothing is worse than one that is hidden.
              tasks={shown}
              entries={entries}
              queued={queued}
              running={running}
              onReload={reload}
              onCreateAt={(t) => openForm(t, null)}
              onEditEntry={editEntry}
            />
          ) : view === "board" ? (
            <TaskBoard
              tasks={shown}
              home={home}
              onReload={reload}
              onQueued={noteQueued}
              // A draft card's press re-opens the form it was saved from —
              // the same gesture, and the same callback, as the List row's.
              onOpenDraft={openDraft}
              // …and the Draft chip as a TAG here too: the filter is the List's
              // and the Board's, on the one shared `TaskFilters`, so switching
              // view keeps it and keeps the chip that turns it off.
              onPickDraft={pickDraft}
              draftOn={filters.draft}
              missing={missing}
              emptyLabel={emptyLabel}
            />
          ) : view === "cards" ? (
            <TaskCards
              emptyLabel={emptyLabel}
              // The FILTERED set, like every other view: Cards only ORDERS it
              // (tasks-lib.cardsForTasks — every lane, Archive last), and a
              // Project, Status or Search the reader set on another view is a
              // lens they meant to keep — the same argument that put the
              // toolbar on the calendar.
              tasks={shown}
              home={home}
              onReload={reload}
              // The folder chip in a card's head is the List row's filter tag
              // (Akshil, 2026-09-05): same handler, same pinned state.
              onPickProject={pickProject}
              pinnedProjects={filters.projects}
              // …and the Draft chip is a TAG here too (Akshil, 2026-09-12), the
              // same handler and the same shared `TaskFilters` the List and the
              // Board press: three views, one filter, one gesture to set it and
              // one to let it go.
              onPickDraft={pickDraft}
              draftOn={filters.draft}
              missing={missing}
            />
          ) : (
            <TaskList
              tasks={shown}
              home={home}
              missing={missing}
              // The rows NEVER fold their marks while the panel is up
              // (Akshil, 2026-09-15): the moment the frame is narrower than the
              // column, the list scrolls sideways to whatever the widest row
              // needs instead of hiding anything. The same switch the frame
              // writes as `data-floored` below, so the stylesheet and the fit
              // ladder can never disagree.
              floored={scrolls}
              // A failed poll empties `tasks` too, and the List cannot tell that
              // apart from a filter that matched nothing — but it must, because
              // one is a reason to forget where the reader was and the other is
              // a reason to hold onto it. See `stale` in TaskList.
              stale={tasksFailed}
              onEditEntry={editEntry}
              // …and the draft row's press, which opens the same card on the
              // form the row is carrying rather than on a stored entry.
              onOpenDraft={openDraft}
              // …and the thread's leading draft line, when the words it is
              // quoting are in the form bound to that conversation rather than
              // in its composer — the chat holds nothing of those, so that
              // press has to reopen the card instead (Bugbot, PR #1126).
              onOpenBoundDraft={openBoundDraft}
              // The folder chip as a TAG: pressing one narrows the page to that
              // project, pressing the pinned one again clears it. It REPLACES
              // the project selection rather than adding to it — the gesture
              // means "show me this folder", and a press that quietly widened
              // an existing selection would be the opposite of what it looks
              // like. Everything else about the filters is left alone, so a
              // status or a search already on stays on.
              onPickProject={pickProject}
              // Which project the page is pinned to — so the chip survives the
              // filter that makes every row agree, and shows that it is on.
              pinnedProjects={filters.projects}
              // The Draft chip, the same way: pressing one keeps only the rows
              // carrying unsent words, pressing it again lets them all back.
              onPickDraft={pickDraft}
              draftOn={filters.draft}
              // Cancelling a message changes server state. The 20s poll would
              // catch it anyway, so this is about the row not looking stuck for
              // twenty seconds, not about correctness.
              onReload={reload}
              // A Skip pressed on a row paints the row before the poll agrees —
              // the same claim the Board's drag makes, held by the page so it
              // survives the view the press was made in (see `queueOverrides`).
              onQueued={noteQueued}
              emptyLabel={emptyLabel}
            />
          )}
        </section>
      )}

      {(creating !== null || editing || draftSeed) && state && (
        <NewJobModal
          // Keyed on WHICH OPENING this is, and on what is being edited, because
          // the form reads `editing` in `useState` initialisers — they run once,
          // on mount.
          //
          // The entry half is what the `?edit=<id>` deep link needs: it cannot
          // avoid arriving in two steps — it opens the modal immediately and can
          // only resolve the entry once the schedule fetch answers — so without
          // it the card mounted on `editing = null` and then sat there with both
          // fields blank under an "Edit task" heading.
          //
          // `openSeq` is the other half and the one that makes this an IDENTITY
          // rather than a mode: `"new"` was the same string for every new-task
          // opening and for every calendar slot, so React reused the mount and
          // the card came up wearing the last form's answers — a Repeat rule the
          // user never chose, one Save away from a real repeating task. See
          // `openSeq` above.
          // …and a DRAFT is a third identity, for the same reason: its fields are
          // read in `useState` initialisers, so re-opening one over a card that
          // was showing another must be a fresh mount.
          key={`${
            hop.key
              ? `chat:${hop.key}`
              : draftSeed
                ? `draft:${draftSeed.id}`
                : editing
                  ? `edit:${editing.id}`
                  : "new"
          }#${openSeq}`}
          initialTime={creating instanceof Date ? creating : null}
          // Scoped, a new task is a task FOR THIS APP: the entry page is
          // prefilled so the modal opens ready to type, because a task made
          // from inside an app is nearly always about the page, not the folder
          // around it. The folder is the fallback when the app has no entry.
          // Prefill only — the field shows exactly what will be saved, and
          // deleting the filename back to the folder is the user's to make. A
          // deep link's own target still wins: it named a path on purpose.
          initialTarget={scope?.entry ?? scope?.project ?? null}
          // The stored record, handed back whole — a task draft's own form, or
          // a chat record turned into one by `chatHopSeed`. Null on every
          // opening that had nothing stored behind it.
          initialDraft={draftSeed}
          // THE CHAT RECORD THIS CARD IS EDITING (design "one record", §1).
          // Not a key to supersede and delete: the card autosaves back onto this
          // very record, so the hop and the composer are two doors onto one
          // stored thing. `""` on every opening that did not come from a chat.
          chatKey={hop.key}
          chatBack={hop.from}
          // SCOPED, THE PATH IS NOT A QUESTION (design.md §2): a task made from
          // inside an app runs against that app, so the field states the target
          // instead of asking for it. The unscoped `/tasks` page is untouched —
          // there the folder is the first thing the card has to ask.
          lockTarget={!!scope}
          // WHICH TASK THIS CARD CAME OUT OF, and the way to it — see
          // `sourceTask` above. The modal draws the chip; where a task opens
          // stays this page's answer, because it is the one holding the peek.
          sourceTask={
            sourceTask
              ? {
                taskId: sourceTask.task_id,
                // Pressable only where there is a peek to open — see
                // `openSourceTask`.
                onOpen: peekable ? () => openSourceTask(sourceTask) : null,
              }
              : null
          }
          editing={editing}
          // IS THIS CARD BEING USED TO PLAN? Three ways it is: the reader is on
          // the calendar (where "when" is the question the view itself asks),
          // the opening carried a time (a slot click), or an existing task is
          // being changed. From the List or the Board it is not, and the
          // when-row folds into More options — a task typed there is one to run
          // now, and the row was what everybody skipped past (Akshil,
          // 2026-08-23).
          planning={view === "calendar" || creating instanceof Date || !!editing}
          permissionModes={state.permission_modes}
          // Newest-first fallback recents: past entries arrive newest first,
          // and the modal dedupes against what localStorage already knows.
          recentTargets={entries.map((e) => e.target)}
          // NOTHING TO CLEAR HERE ANY MORE, and that is the fix rather than an
          // omission (Akshil, 2026-09-12). The hop used to be six values undone
          // one by one on close — with `attachments` missing from the list, so a
          // composer handoff's picture rode into every New task card opened
          // afterwards. The opening states its own hop now (`openForm`), so a
          // close has nothing to forget and no list to keep in step.
          // CLOSING A DRAFT KEEPS IT (design.md, Decisions): the card is put
          // away, the draft stays — as its own row, or as the `Draft` chip on
          // the conversation it is bound to — and the modal's own autosave has
          // already flushed on unmount. See `closeCard`, which the source chip
          // takes too.
          onClose={closeCard}
          onCreated={reload}
        />
      )}
    </div>
  );

  if (!peekable) return page;

  // THE PAIR (design.md, Layout model): one flex row holding the frame and the
  // peek as DOM siblings, the peek lifted out of flow over the row's right edge
  // so its slide never reflows the frame under it. ONE number drives both
  // halves of the 200ms — the frame's width is `100% − <what the peek takes>`,
  // and the peek's own transform runs off the same value.
  //
  // In COVER mode the frame takes nothing off its width (rule 4): there is no
  // usable frame left at that size, so the panel is laid over it whole rather
  // than squeezing the view to a sliver.
  const taken = peek.open && !peek.cover ? peek.width : 0;
  // THE FLOOR, handed to the stylesheet as a length (design.md, Widths v2).
  // `peek.floor` is a FRAME width — three quarters of a baseline that counts
  // the page's gutters — and what the views need is the width of the content
  // inside those gutters, so the gutters come back off here rather than being
  // guessed at in CSS.
  const contentFloor = Math.max(0, Math.round(peek.floor - peekGutter()));
  return (
    <div className="tasks-peek-host">
      <div
        ref={frameRef}
        className={"tasks-frame" + (peek.instant ? " is-instant" : "")}
        // `data-floored` is the switch and `--tasks-floor` the number: below the
        // floor the views stop reflowing and scroll sideways inside the frame
        // instead (styles/task-peek.css). The toolbar is deliberately NOT under
        // it — it stays one line at every width and folds its own way.
        data-floored={scrolls ? "1" : undefined}
        // …and `data-tight` a little earlier: once the frame is narrower than
        // the column plus its gutters there are no centred margins left to give
        // and the page's side padding is just two dark bands (design.md, Polish
        // batch 3). Written off the same baseline the floor is.
        data-tight={peek.open && peek.tight ? "1" : undefined}
        style={
          {
            width: `calc(100% - ${taken}px)`,
            "--tasks-floor": `${contentFloor}px`,
          } as CSSProperties
        }
        // CLICKING BLANK FRAME CLOSES (design.md, Close triggers — and Akshil's
        // decision to keep Notion's behaviour). Everything that is a control or
        // an item does its own thing: rows and cards carry the walk's own
        // attribute, the toolbar's chips are buttons, and a menu or a dialog
        // portalled over the page is neither. What is left is page background.
        onClick={(e) => {
          if (!peek.open) return;
          if (frameClickCloses(e.target as Element | null)) closePeek();
        }}
      >
        {page}
      </div>
      {/* THE UNFILTERED SET, not `shown`: a filter is a lens on the page, not a
          statement about which conversation may be open, and narrowing the list
          under an open panel must not close it (or, worse, make its task look
          deleted to `settlePeek`). `loaded` is what turns "no such task" from a
          wait into an answer. */}
      <TaskPeek
        tasks={inScope}
        loaded={tasksLoaded}
        home={home}
        missing={missing}
        onReload={reload}
      />
    </div>
  );
}

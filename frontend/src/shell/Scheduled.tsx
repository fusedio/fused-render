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
import { useEffect, useMemo, useRef, useState } from "react";
import {
  getConfig,
  getSchedule,
  getScheduleQueue,
  getTaskChanges,
  getTasks,
} from "@platform/lib/api";
import type {
  ScheduledMessage,
  ScheduleResult,
  Task,
} from "@platform/lib/api";
import { useRefreshOnReturn } from "@platform/lib/hooks";
// The chat's own handoff module, not a second reading of its URL shape: the
// param is written by `schedulerUrl` and there must be exactly one parser for it
// (owner E2E R1, F4 (2026-09-10)). A leaf module with no imports of its own, so
// this costs the shell chunk nothing but the function.
import {
  parseAttachmentsParam,
  type SchedAttachment,
} from "@apps/claude/ui/sched-draft";
import { chatDraftKey, fetchChatDraft } from "@platform/lib/drafts";
import { chatPaneUrl } from "./schedule-lib";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import ScheduleCalendar, {
  ICON_VIEW_BOARD,
  ICON_VIEW_CALENDAR,
  ICON_VIEW_CARDS,
  ICON_VIEW_LIST,
} from "./ScheduleCalendar";
import NewJobModal from "./NewJobModal";
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
  forgetListing,
  publishTasks,
  readListing,
  readTasksRows,
  rememberListing,
  TASKS_POKE_EVENT,
  useTasksFeeder,
} from "./tasksPulse";
import {
  TASK_VIEWS,
  isChatDraftTask,
  mergeTaskChanges,
  provisionalTasks,
  viewFromSearch,
  viewUrl,
} from "./tasks-lib";
import type { TaskView } from "./tasks-lib";
import { TaskCards } from "./TaskCards";
import { TasksSkeleton } from "./TasksSkeleton";
import { useMissingFolders } from "./useMissingFolders";
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

// How often the page re-reads itself. A `pending` message becomes `sent` on the
// server's own tick (30s), so anything much slower than this shows a message as
// still-waiting for a while after it went out.
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
interface HopSeed {
  target: string | null;
  message: string | null;
  session: string | null;
  back: string | null;
  attachments: SchedAttachment[];
  /** Which chat draft this form supersedes — see the effect that reads it. */
  chatKey: string | null;
}

/** The opening that came from nowhere: every way into the form but the hop.
 *  A module constant so `openForm`'s default argument is one stable value. */
const NO_HOP: HopSeed = {
  target: null, message: null, session: null, back: null,
  attachments: [], chatKey: null,
};

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
  // The rows as the changes loop below last saw them, and the server
  // generation they answer to. Refs, not state: the loop is one long-lived
  // effect and must read the newest value without re-subscribing on every
  // poll. Mirrored from `tasks` by the effect under it.
  const tasksRef = useRef<Task[]>([]);
  const generationRef = useRef(-1);
  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);
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
  // WHAT THE OPENING CARRIED IN FROM A DEEP LINK — see `HopSeed` above. Set by
  // `openForm` and by nothing else, which is what keeps one chat's handoff from
  // seeding the next card the reader opens.
  const [hop, setHop] = useState<HopSeed>(NO_HOP);
  const [openSeq, setOpenSeq] = useState(0);
  // The single door into the form, so "clean slate" is one rule in one place: a
  // new opening is a new mount, and opening a NEW task drops whatever was being
  // edited (leaving it set kept the card in Edit mode under a "+ New task"
  // press).
  //
  // …and it is where the deep link's values are SPENT: every opening states its
  // own hop, and the ones that had none say so by saying nothing (`NO_HOP`, the
  // default). That is the whole of fix for the attachments that used to follow
  // the reader from one New task card to the next — see `HopSeed` (Akshil,
  // 2026-09-12).
  const openForm = (
    at: Date | "blank" | null,
    entry: ScheduledMessage | null,
    seed: HopSeed = NO_HOP,
  ) => {
    setOpenSeq((n) => n + 1);
    setHop(seed);
    setEditing(entry);
    setDraftRow(null);
    setCreating(at);
  };
  // An unfinished New task form, re-opened from its own row (design.md, "Reopen
  // path": editing a draft row is the only way back to it). It travels as the
  // ROW rather than as its id, because the row already carries the saved form —
  // `/api/tasks` emits it — so the card can seed from it on the first paint with
  // no second fetch, exactly as an Edit seeds from its entry.
  const [draftRow, setDraftRow] = useState<Task | null>(null);
  /**
   * A NEVER-SENT CHAT'S ROW, OPENED AS A NEW TASK (Akshil, 2026-09-12: "a row
   * without a session is a draft and always opens the New Task modal").
   *
   * It builds the very `HopSeed` the chat composer's Schedule button builds —
   * the folder, the words, the tray's chips, the way back, and the chat draft
   * this card supersedes — and spends it through `openForm` exactly as the
   * `?new=1` effect below does. Same door, same seeding, same aftermath: the
   * modal mints a task draft at once (`hopSeeded`), the first save names
   * `from_chat_key` so the server deletes the `new:<file>` draft in the same
   * request, and "Back to chat" (`backChatHref`) hands the words back to the
   * composer they came from. Nothing here is a second implementation of that
   * handoff; it is the same one, reached from a row instead of a button.
   *
   * IT HAS TO FETCH FIRST, and that is the one cost. The row carries only
   * `draft.preview` — the first LINE — and the card's two prose fields are the
   * whole message split across them (`splitDraft`), so opening on the preview
   * would quietly drop every line after the first. The card seeds in `useState`
   * initialisers, which run once at mount, so there is no patching it
   * afterwards: the text has to be in hand BEFORE the modal opens. An
   * unreadable answer falls back to the preview rather than refusing the press
   * — a truncated draft is worth more than a dead row — and `fetchChatDraft`
   * already answers null instead of throwing.
   */
  const openChatDraft = (task: Task) => {
    // The chat's own `file` FIRST, for platform/lib/drafts.chatDraftKey's
    // reason: the draft is keyed on that exact string, and the way back has to
    // mount the pane on the same one or the composer seeds from a key nothing
    // wrote. `target` is the fallback for a server that sends the row without
    // it.
    const at = task.file || task.target || "";
    void fetchChatDraft(task.key).then((stored) => {
      const seed: HopSeed = {
        target: at || null,
        message: stored?.text ?? task.draft?.preview ?? null,
        // NO SESSION, and that is what this row IS: a conversation that has
        // never been sent has no id to continue, which is the whole meaning of
        // a `new:<file>` key.
        session: null,
        back: at ? chatPaneUrl(at) : null,
        attachments: stored?.attachments ?? [],
        // The row's key IS the chat draft's key (`new:<file>`), which is what
        // lets the card's first save move the draft rather than duplicate it
        // (design.md, Round 2: "A draft moves, never duplicates").
        chatKey: task.key,
      };
      openForm(new Date(Date.now() + NEW_LINK_LEAD_MS), null, seed);
    });
  };
  const openDraft = (task: Task) => {
    // A NEVER-SENT CHAT OPENS THIS MODAL TOO (Akshil, 2026-09-12). The rule is
    // the ROW's, not the draft's kind: a row with no session is a draft and
    // always opens the New task modal; a row with a session always opens the
    // chat. The build before this one sent the two draft kinds to two different
    // places and tried to warn about it in the chip's wording ("Draft reply"),
    // which is a label apologising for a press — the press was the thing to fix.
    //
    // What it is seeded from is where the two kinds still differ, and that is
    // the only thing `isChatDraftTask` decides now. A task draft has a stored
    // form to re-open (`draftRow`, below); a chat draft has none, so its words
    // travel as a HOP — the identical object the composer's own Schedule button
    // builds, so this press and that button land on the same card, mint the
    // same task draft, delete the same chat draft (`from_chat_key`) and offer
    // the same way back. Asked FIRST, because it is the row that has no
    // `draft_id` to fall through to.
    if (isChatDraftTask(task)) {
      openChatDraft(task);
      return;
    }
    if (!task.draft_id) return;
    setOpenSeq((n) => n + 1);
    // A reopened draft carries its own everything (the row's stored `form`), so
    // whatever a deep link said earlier in this page's life is not about it.
    setHop(NO_HOP);
    setEditing(null);
    setCreating(null);
    setDraftRow(task);
  };
  // `hop` (above) is what a deep link named — the folder, the composer's words,
  // the session it was typed in, the way back, the tray's chips, and the chat
  // draft this form supersedes. `NO_HOP` for every other way of opening the
  // form, and it is the OPENING that says so, not a clear-up afterwards.
  //
  // The chips are the half worth naming twice: they were already copied into
  // the task-shots dir by the composer's Schedule button, so what arrives here
  // is exactly the shape a saved entry's `attachments` has, and the card seeds
  // from them the same way an Edit does (owner E2E R1, F4 (2026-09-10)).
  //
  // WHICH CHAT DRAFT THIS FORM SUPERSEDES (design.md, Round 2: "A draft moves,
  // never duplicates").
  //
  // The Schedule hop carries a composer's words here, and the composer's own
  // autosave has already stored them as a chat draft. The task draft this card
  // is about to mint is THE SAME WORDS, so the first save names the key they
  // came from and the server deletes that one in the same request — one draft,
  // one row, one TASK number, with no window where the List shows the sentence
  // twice.
  //
  // The key is the hop's own: its `session_id` when the chat had one, else
  // `new:<target>` — and `target` IS the chat's `file` (sched-draft.schedulerUrl
  // writes `link.file` into it), which is the whole reason no new param was
  // needed. platform/lib/drafts.chatDraftKey carries the rule that keeps the
  // four spellings of that path in step.
  // Search, status and project, client-side only — nothing here is worth a URL
  // or a localStorage row: a filter is how you read the page this minute.
  const [filters, setFilters] = useState<TaskFilters>(EMPTY_FILTERS);
  // Only so a folder chip's tooltip can say "~/Desktop/fused" rather than the
  // full /Users/... path. Missing home just means untouched paths.
  const [home, setHome] = useState("");
  useEffect(() => {
    getConfig().then((c) => setHome(c.home), () => {});
  }, []);

  // `?new=1&target=…` — the chat composer's Schedule button
  // (templates/claude/template.html openScheduler). The chat knows the folder
  // and nothing else, so the params carry only that, and this turns them into
  // an already-open form: landing on a page with a button still to press would
  // make one control read as two.
  //
  // The params are CONSUMED, not just read: cleared with replaceState so a
  // reload (or Back to here from wherever the user went next) is the plain
  // Tasks page rather than a modal that reopens forever. replaceState, not
  // push, for the same reason — the deep-linked URL is not a place worth
  // keeping in the history.
  // `?edit=<entry id>` — the chat's blocked-composer banner sends the user here
  // to reschedule or stop the message that is blocking it. It cannot be handled
  // in the effect below, because the entry it names lives in a fetch that has
  // not answered yet on first render; it is held here and resolved once the
  // schedule lands.
  const [editId, setEditId] = useState<string | null>(null);
  useEffect(() => {
    const q = new URLSearchParams(location.search);
    if (q.get("new") !== "1") return;
    setEditId(q.get("edit"));
    // The chat's whole handoff, read in one go and handed to the opening it is
    // about: the typed draft fills the card's two prose fields — its first line
    // names the task and the rest is the description (NewJobModal splitDraft) —
    // the open conversation the session a ONE-OFF will continue, and the chat's
    // URL the way back — the form's round trip.
    const seed: HopSeed = {
      target: q.get("target"),
      message: q.get("message"),
      session: q.get("session_id"),
      back: q.get("back"),
      // Defensive by contract, not by suspicion: this is a URL a user can edit
      // and a param an older build may not have written — `parseAttachmentsParam`
      // answers [] for anything it cannot read, because a parse error here would
      // cost the folder, the draft and the session as well as the files.
      attachments: parseAttachmentsParam(q.get("attachments")),
      // Only for a hop that came FROM a chat, which is what `message` says: the
      // other `?new=1&target=…` links (the app page's "+ New task") carry no
      // composer and have no draft of anybody's to supersede. `message` may be
      // empty and still present — an empty composer stored no draft, so the key
      // is simply one the server finds nothing under.
      chatKey: q.get("message") === null
        ? null
        : chatDraftKey(q.get("session_id"), q.get("target")),
    };
    openForm(new Date(Date.now() + NEW_LINK_LEAD_MS), null, seed);
    q.delete("new");
    q.delete("target");
    q.delete("message");
    q.delete("session_id");
    q.delete("back");
    q.delete("edit");
    q.delete("attachments");
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
  const reload = () => {
    getSchedule().then(
      (r) => {
        setState(r);
        setLoadError(null);
      },
      (e: Error) => setLoadError(e.message),
    );
    getTasks().then(
      (r) => {
        // A full listing that left before a delta landed is OLDER than what
        // is on screen; applying it would roll the rows back and the
        // generation with them (bugbot #892). The next poll catches up.
        if (typeof r.generation === "number" && r.generation < generationRef.current) return;
        setTasks(r.tasks ?? []);
        setTasksFailed(false);
        setTasksLoaded(true);
        if (typeof r.generation === "number") generationRef.current = r.generation;
        // The sidebar's Tasks entry reads the same rows (shell/tasksPulse): the
        // dot and the counts beside the label are this answer, not a second poll
        // of their own — two polls would show a dot the page disagrees with for
        // twenty seconds at a time. Publishing also restarts that module's own
        // timer, so while this page is open nothing else calls /api/tasks.
        publishTasks(r.tasks ?? []);
        // And keep it for the next mount: this page is remounted on every
        // navigation, and the seed above is what saves the trip back from
        // paying for the listing twice.
        rememberListing(r.tasks ?? []);
      },
      () => {
        setTasks([]);
        setTasksFailed(true);
        setTasksLoaded(true);
        forgetListing();
      },
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
  useEffect(reload, []);
  useRefreshOnReturn(reload);
  useEffect(() => {
    const id = window.setInterval(reload, POLL_MS);
    return () => window.clearInterval(id);
  }, []);
  // The corner card knows a run ended about a second after it does; this page's
  // own clock is 20s. pokeTasks forwards that knowledge here as a window event —
  // the feeder above means the shared store may not fetch on our behalf — and
  // the page re-reads all three feeds, so the row flips the moment the popover
  // does rather than up to a poll later.
  useEffect(() => {
    window.addEventListener(TASKS_POKE_EVENT, reload);
    return () => window.removeEventListener(TASKS_POKE_EVENT, reload);
  }, []);
  // The fast lane. /api/tasks/changes long-polls the server's change watcher
  // (tasks_watch.py) and answers the moment a session starts, resumes, takes
  // a prompt or grows — so a `claude` typed into a terminal in some folder is
  // a row here within a second, not up to a poll later. Only the rows that
  // moved come back and are folded into place (mergeTaskChanges); the 20s
  // full reload above stays as the truth underneath. Hidden tabs sit the loop
  // out — useRefreshOnReturn reloads on the way back — and a failed call
  // backs off rather than hammering a server that is restarting.
  useEffect(() => {
    let stopped = false;
    let controller: AbortController | null = null;
    const sleep = (ms: number) =>
      new Promise<void>((resolve) => {
        window.setTimeout(resolve, ms);
      });
    const untilVisible = () =>
      new Promise<void>((resolve) => {
        const onChange = () => {
          if (document.visibilityState !== "visible") return;
          document.removeEventListener("visibilitychange", onChange);
          resolve();
        };
        document.addEventListener("visibilitychange", onChange);
      });
    const run = async () => {
      while (!stopped) {
        if (document.visibilityState !== "visible") {
          await untilVisible();
          continue;
        }
        if (generationRef.current < 0) {
          // No full listing has answered yet; nothing to merge into.
          await sleep(500);
          continue;
        }
        controller = new AbortController();
        try {
          const r = await getTaskChanges(generationRef.current, 25, controller.signal);
          if (stopped) return;
          if (r.full) {
            // "Reload everything" includes a server that restarted and counts
            // from zero again: forget our generation FIRST, or the stale-listing
            // guard in reload() would refuse the very listing that catches us
            // up, forever (bugbot #892).
            generationRef.current = -1;
            reload();
            await sleep(1000);
            continue;
          }
          generationRef.current = r.generation;
          const rows = r.rows ?? [];
          const gone = r.gone ?? [];
          if (rows.length || gone.length) {
            const merged = mergeTaskChanges(tasksRef.current, rows, gone);
            tasksRef.current = merged;
            setTasks(merged);
            publishTasks(merged);
            // The merge is now the freshest full listing there is, so it — not
            // the poll's older answer — is what a remount should seed from.
            rememberListing(merged);
          }
        } catch {
          if (stopped) return;
          await sleep(3000);
        }
      }
    };
    void run();
    return () => {
      stopped = true;
      controller?.abort();
    };
  }, []);

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
  const inScope = useMemo(
    () => (scope ? tasks.filter((t) => isUnderDir(t.project, scope.project)) : tasks),
    [tasks, scope],
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

  return (
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
          <div className="schedule-toolbar">
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
                      className={"btn btn-secondary schedule-view-btn" + (view === "list" ? " is-active" : "")}
                      aria-pressed={view === "list"}
                      onClick={() => pickView("list")}>
                {ICON_VIEW_LIST}
                List
              </button>
              <button type="button"
                      data-view="board"
                      className={"btn btn-secondary schedule-view-btn" + (view === "board" ? " is-active" : "")}
                      aria-pressed={view === "board"}
                      onClick={() => pickView("board")}>
                {ICON_VIEW_BOARD}
                Board
              </button>
              {/* Before the calendar (Akshil, 2026-09-03): the first three
                  answer "what is there" and "what is happening right now",
                  and the calendar is the drill-down for the scheduled subset —
                  the same argument that made List the default. */}
              <button type="button"
                      data-view="cards"
                      className={"btn btn-secondary schedule-view-btn" + (view === "cards" ? " is-active" : "")}
                      aria-pressed={view === "cards"}
                      onClick={() => pickView("cards")}>
                {ICON_VIEW_CARDS}
                Cards
              </button>
              <button type="button"
                      data-view="calendar"
                      className={"btn btn-secondary schedule-view-btn" + (view === "calendar" ? " is-active" : "")}
                      aria-pressed={view === "calendar"}
                      onClick={() => pickView("calendar")}>
                {ICON_VIEW_CALENDAR}
                Calendar
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
            />
            <button type="button" className="btn btn-primary schedule-new"
                    onClick={() => openForm("blank", null)}>
              + New task
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
              missing={missing}
            />
          ) : (
            <TaskList
              tasks={shown}
              home={home}
              missing={missing}
              // A failed poll empties `tasks` too, and the List cannot tell that
              // apart from a filter that matched nothing — but it must, because
              // one is a reason to forget where the reader was and the other is
              // a reason to hold onto it. See `stale` in TaskList.
              stale={tasksFailed}
              onEditEntry={editEntry}
              // …and the draft row's press, which opens the same card on the
              // form the row is carrying rather than on a stored entry.
              onOpenDraft={openDraft}
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
              emptyLabel={emptyLabel}
            />
          )}
        </section>
      )}

      {(creating !== null || editing || draftRow) && state && (
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
            draftRow ? `draft:${draftRow.draft_id}` : editing ? `edit:${editing.id}` : "new"
          }#${openSeq}`}
          initialTime={creating instanceof Date ? creating : null}
          // Scoped, a new task is a task FOR THIS APP: the entry page is
          // prefilled so the modal opens ready to type, because a task made
          // from inside an app is nearly always about the page, not the folder
          // around it. The folder is the fallback when the app has no entry.
          // Prefill only — the field shows exactly what will be saved, and
          // deleting the filename back to the folder is the user's to make. A
          // deep link's own target still wins: it named a path on purpose.
          initialTarget={hop.target ?? scope?.entry ?? scope?.project ?? null}
          initialMessage={hop.message}
          initialAttachments={hop.attachments}
          // The saved form, handed back whole. Null on every other opening.
          initialDraft={
            draftRow?.draft_id
              ? { id: draftRow.draft_id, form: draftRow.form ?? null }
              : null
          }
          chatSessionId={hop.session}
          chatBack={hop.back}
          // The chat draft this card's own first save supersedes — see
          // `HopSeed` above. Null on every opening that did not come from a
          // composer, which is every opening but the Schedule hop.
          fromChatKey={hop.chatKey}
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
          onClose={() => {
            setCreating(null);
            setEditing(null);
            // CLOSING A DRAFT KEEPS IT (design.md, Decisions): the card is put
            // away, the row stays on the list, and the modal's own autosave has
            // already flushed on unmount.
            setDraftRow(null);
          }}
          onCreated={reload}
        />
      )}
    </div>
  );
}

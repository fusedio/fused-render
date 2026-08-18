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
// links to `/scheduled?new=1&target=…`, for the case the pill cannot serve — a
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
import { useEffect, useMemo, useState } from "react";
import {
  getConfig,
  getSchedule,
  getScheduleQueue,
  getTasks,
} from "@platform/lib/api";
import type {
  ScheduledMessage,
  ScheduleResult,
  Task,
} from "@platform/lib/api";
import { useRefreshOnReturn } from "@platform/lib/hooks";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import ScheduleCalendar from "./ScheduleCalendar";
import NewJobModal from "./NewJobModal";
import {
  EMPTY_FILTERS,
  TaskBoard,
  TaskFilterControls,
  TaskList,
  filterTasks,
  projectOptions,
} from "./ScheduleTaskViews";
import type { TaskFilters } from "./ScheduleTaskViews";

// How often the page re-reads itself. A `pending` message becomes `sent` on the
// server's own tick (30s), so anything much slower than this shows a message as
// still-waiting for a while after it went out.
const POLL_MS = 20000;

// Which view is up, remembered across visits — a person who plans on the
// calendar plans on the calendar every time. List is the default now (Akshil,
// 2026-08-17): the page's first question turned out to be "what is running",
// not "when", and the calendar is the drill-down for the scheduled subset.
const VIEW_KEY = "fused-render:scheduled-view";

// How far ahead a deep link's prefilled time lands. The form's own default is
// +1h, which is a planning answer; a link says "I want to set this up now", so
// the time it opens on should be near-now and only then adjusted. Two minutes
// rather than one because the when-field is minute-precision, and a value
// inside the CURRENT minute opens the form on a time already behind the clock.
const NEW_LINK_LEAD_MS = 120_000;

type View = "list" | "board" | "calendar";

export default function Scheduled() {
  const [state, setState] = useState<ScheduleResult | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [tasksFailed, setTasksFailed] = useState(false);
  const [queued, setQueued] = useState<ScheduledMessage[]>([]);
  const [running, setRunning] = useState<ScheduledMessage[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  // localStorage can THROW (private mode, locked-down webviews), and this read
  // runs during first render — unguarded it took the whole page down for a
  // preference. Storage failing costs the memory, never the page.
  const [view, setView] = useState<View>(() => {
    try {
      const saved = localStorage.getItem(VIEW_KEY);
      return saved === "board" || saved === "calendar" ? saved : "list";
    } catch {
      return "list";
    }
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
  const [openSeq, setOpenSeq] = useState(0);
  // The single door into the form, so "clean slate" is one rule in one place: a
  // new opening is a new mount, and opening a NEW task drops whatever was being
  // edited (leaving it set kept the card in Edit mode under a "+ New task"
  // press).
  const openForm = (at: Date | "blank" | null, entry: ScheduledMessage | null) => {
    setOpenSeq((n) => n + 1);
    setEditing(entry);
    setCreating(at);
  };
  // What a deep link named (see the effect below); all null for every other
  // way of opening the form.
  const [newTarget, setNewTarget] = useState<string | null>(null);
  const [newMessage, setNewMessage] = useState<string | null>(null);
  const [newSession, setNewSession] = useState<string | null>(null);
  const [newBack, setNewBack] = useState<string | null>(null);
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
    setNewTarget(q.get("target"));
    // The chat's whole handoff: the typed draft fills the card's two prose
    // fields — its first line names the task and the rest is the description
    // (NewJobModal splitDraft) — the
    // open conversation the session a ONE-OFF will continue, and the chat's URL
    // the way back — the form's round trip.
    setNewMessage(q.get("message"));
    setNewSession(q.get("session_id"));
    setNewBack(q.get("back"));
    openForm(new Date(Date.now() + NEW_LINK_LEAD_MS), null);
    q.delete("new");
    q.delete("target");
    q.delete("message");
    q.delete("session_id");
    q.delete("back");
    q.delete("edit");
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
        setTasks(r.tasks ?? []);
        setTasksFailed(false);
      },
      () => {
        setTasks([]);
        setTasksFailed(true);
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

  const pickView = (v: View) => {
    setView(v);
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
  const projects = useMemo(() => projectOptions(tasks), [tasks]);
  const shown = useMemo(() => filterTasks(tasks, filters), [tasks, filters]);

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
      <header className="schedule-header">
        <h1>Tasks</h1>
      </header>

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
            <div className="schedule-form-seg" role="radiogroup" aria-label="View">
              <button type="button"
                      className={"btn btn-secondary" + (view === "list" ? " is-active" : "")}
                      aria-pressed={view === "list"}
                      onClick={() => pickView("list")}>
                List
              </button>
              <button type="button"
                      className={"btn btn-secondary" + (view === "board" ? " is-active" : "")}
                      aria-pressed={view === "board"}
                      onClick={() => pickView("board")}>
                Board
              </button>
              <button type="button"
                      className={"btn btn-secondary" + (view === "calendar" ? " is-active" : "")}
                      aria-pressed={view === "calendar"}
                      onClick={() => pickView("calendar")}>
                Calendar
              </button>
            </div>
            {/* Search, Status and Project belong to the two task views: the
                calendar answers "when", and a week with tasks filtered out of
                it is a week that lies. They sit AFTER the toggle, so hiding
                them here cannot move it. */}
            {view !== "calendar" && (
              <TaskFilterControls
                filters={filters}
                projects={projects}
                home={home}
                onChange={setFilters}
              />
            )}
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
          {view !== "calendar" && tasksFailed && (
            // One quiet line, not a banner: the form and the calendar still
            // work, and only the rows are missing.
            <p className="schedule-tv-note">Tasks could not be loaded.</p>
          )}

          {view === "calendar" ? (
            <ScheduleCalendar
              tasks={tasks}
              entries={entries}
              queued={queued}
              running={running}
              onReload={reload}
              onCreateAt={(t) => openForm(t, null)}
              onEditEntry={editEntry}
            />
          ) : view === "board" ? (
            <TaskBoard tasks={shown} home={home} onReload={reload} />
          ) : (
            <TaskList
              tasks={shown}
              home={home}
              onEditEntry={editEntry}
              // Cancelling a message changes server state. The 20s poll would
              // catch it anyway, so this is about the row not looking stuck for
              // twenty seconds, not about correctness.
              onReload={reload}
              emptyLabel={
                tasks.length === 0
                  ? "No tasks yet. Everything Claude runs for you shows up here."
                  : "Nothing matches these filters."
              }
            />
          )}
        </section>
      )}

      {(creating !== null || editing) && state && (
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
          key={`${editing ? `edit:${editing.id}` : "new"}#${openSeq}`}
          initialTime={creating instanceof Date ? creating : null}
          initialTarget={newTarget}
          initialMessage={newMessage}
          chatSessionId={newSession}
          chatBack={newBack}
          editing={editing}
          permissionModes={state.permission_modes}
          // Newest-first fallback recents: past entries arrive newest first,
          // and the modal dedupes against what localStorage already knows.
          recentTargets={entries.map((e) => e.target)}
          // `newTarget` is cleared with the rest: it described the ONE form the
          // deep link opened, and left standing it would prefill the next
          // "+ New task" with a folder the user arrived from some time ago.
          onClose={() => {
            setCreating(null);
            setEditing(null);
            setNewTarget(null);
            setNewMessage(null);
            setNewSession(null);
            setNewBack(null);
          }}
          onCreated={reload}
        />
      )}
    </div>
  );
}

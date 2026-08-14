// Scheduled messages page — everything scheduled across every folder, in one
// place: a week calendar (the "when" view, default) and the card list (the
// "what happened" view), one toggle apart. Both render the same entries from
// the same poll; neither owns data the other lacks.
//
// Scheduling happens in two places, deliberately: the chat composer's Send now
// pill (templates/claude/template.html) when a chat is already open — it knows
// the folder and holds the message — and this page's New job modal when the
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
// server/routers/schedule.py (this page's calls). The app does the sending
// itself rather than handing the job to cron, so a scheduled turn runs with the
// same environment, credentials and file-access consent as one the user typed —
// see that module's docstring for why that matters more than it sounds.
//
// The honest cost of that choice is the one thing this page must never hide:
// **nothing fires while the app is closed.** A one-shot that comes due with the
// app shut fires when it next starts, within the server's catch-up bound; a
// recurring run is skipped outright (the next one is already coming). Both
// facts are stated on the page, because a scheduling UI that implies a
// guarantee it does not have is worse than no scheduling UI.
//
// Section layout and per-action busy/error state follow shell/Mounts.tsx.
import { useEffect, useState } from "react";
import { cancelScheduledMessage, getSchedule } from "@platform/lib/api";
import type { ScheduledMessage, ScheduleResult } from "@platform/lib/api";
import { useRefreshOnReturn } from "@platform/lib/hooks";
import { navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import ScheduleCalendar, {
  ICON_CANCEL,
  ICON_EDIT,
  ICON_INBOX,
} from "./ScheduleCalendar";
import NewJobModal from "./NewJobModal";
import {
  BOARD_COLUMNS,
  boardColumn,
  canUnskip,
  describeRepeats,
  formatDue,
  isLive,
  relativeDue,
  stateLabel,
  stateTone,
} from "./schedule-lib";
import { restoreScheduledMessage } from "@platform/lib/api";

// How often the list re-reads itself. A `pending` entry becomes `sent` on the
// server's own tick (30s), so anything much slower than this shows a message as
// still-waiting for a while after it went out.
const POLL_MS = 20000;

// Which view is up, remembered across visits — a person who plans on the
// calendar plans on the calendar every time.
const VIEW_KEY = "fused-render:scheduled-view";

// How far ahead a deep link's prefilled time lands. The form's own default is
// +1h, which is a planning answer; a link says "I want to set this up now", so
// the time it opens on should be near-now and only then adjusted.
//
// Not zero, and not one minute: the form's when-field is minute-precision and
// it refuses a due time at or before `Date.now()` outright, so a value inside
// the CURRENT minute opens the modal already invalid — "Choose a time in the
// future" printed under a time the user never picked. Two minutes always rounds
// into the next one.
const NEW_LINK_LEAD_MS = 120_000;

function hoursText(seconds: number): string {
  const hours = Math.round(seconds / 3600);
  if (hours >= 48) return `${Math.round(hours / 24)} days`;
  if (hours >= 1) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${Math.max(1, Math.round(seconds / 60))} minutes`;
}

// The Board view: the Inbox board's shape (fixed-width columns, dot + count
// heads, compact cards) over the scheduler's own facts. No drag, deliberately:
// the Inbox's columns are triage labels a person may move; these are states
// the loop decides, and dragging a Missed card to Ran would be fiction.
function ScheduleBoard({
  entries,
  onEdit,
}: {
  entries: ScheduledMessage[];
  onEdit: (entry: ScheduledMessage) => void;
}) {
  return (
    <div className="schedule-board">
      {BOARD_COLUMNS.map((col) => {
        const cards = entries.filter((e) => boardColumn(e) === col.key);
        return (
          <div className="schedule-board-col" key={col.key}>
            <div className={`schedule-board-head schedule-board-head--${col.key}`}>
              <span><span className="schedule-board-dot" /> {col.label}</span>
              <span className="schedule-board-count">{cards.length}</span>
            </div>
            <div className="schedule-board-cards">
              {cards.length === 0 && <p className="schedule-card-why">Empty</p>}
              {cards.map((e) => {
                const editable = e.state === "pending" || e.state === "recurring";
                return (
                  <button
                    type="button"
                    key={e.id}
                    className="schedule-board-card"
                    title={e.message}
                    onClick={() => {
                      if (editable) onEdit(e);
                      else if (e.claude_session_id)
                        navigateUrl(`/sessions?peek=${encodeURIComponent(e.claude_session_id)}`);
                    }}
                  >
                    <span className="schedule-board-card-name">{e.message}</span>
                    <span className="schedule-board-card-meta">
                      <span className={`schedule-state schedule-state--${stateTone(e)}`}>
                        {stateLabel(e)}
                      </span>
                      <span className="schedule-board-card-time">
                        {e.state === "recurring"
                          ? describeRepeats(e.repeats || "")
                          : relativeDue(isLive(e) || !e.fired ? e.due : e.fired)}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function EntryCard({
  entry,
  unskippable,
  onCancelled,
  onEdit,
}: {
  entry: ScheduledMessage;
  // Decided by the parent (schedule-lib.canUnskip) — the card cannot see the
  // template, and offering Unskip wider than the server honours is a 404.
  unskippable: boolean;
  onCancelled: () => void;
  onEdit: (entry: ScheduledMessage) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = async (call: (id: string) => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await call(entry.id);
      onCancelled();
    } catch (e) {
      // The 404 case is a real race, not a bug: the loop may have sent this
      // message while the user was reaching for Cancel. Show what the server
      // said and reload, so the card corrects itself.
      setError((e as Error).message);
      onCancelled();
    } finally {
      setBusy(false);
    }
  };
  const cancel = () => act(cancelScheduledMessage);
  const unskip = () => act(restoreScheduledMessage);

  // A waiting message is described by when it is DUE; one that has already acted,
  // by when it actually went out — not the same instant when the app caught up on
  // it after being closed, which is why the title carries both.
  const waiting = isLive(entry) || !entry.fired;
  const stamp = waiting ? entry.due : entry.fired;
  const stampTitle = waiting
    ? formatDue(entry.due)
    : `Due ${formatDue(entry.due)} · ran ${formatDue(entry.fired)}`;

  return (
    <div className={`schedule-card schedule-card--${stateTone(entry)}`}>
      <div className="schedule-card-head">
        <span className={`schedule-state schedule-state--${stateTone(entry)}`}>
          {stateLabel(entry)}
        </span>
        <span className="schedule-card-when" title={stampTitle}>
          {/* A recurring rule has no single "when" — its rule is the when. */}
          {entry.state === "recurring" ? describeRepeats(entry.repeats || "") : relativeDue(stamp)}
        </span>
      </div>

      {/* The prompt is the card's subject — what the reader scans to find the one
          they mean — so it gets the body colour and the room. Clamped rather than
          scrolled: a card grid wants even heights, and the full text is one hover
          away in the title. */}
      <p className="schedule-card-message" title={entry.message}>
        {entry.message}
      </p>

      {/* Only what departs from the default earns a line: continuing a
          session, or a non-auto mode. The default case says nothing. */}
      <p className="schedule-card-meta">
        <code title={entry.target}>{entry.target}</code>
        {(entry.session_id || entry.permission_mode !== "auto") && (
          <span>
            {entry.session_id ? "Continues an existing chat" : ""}
            {entry.session_id && entry.permission_mode !== "auto" ? " · " : ""}
            {entry.permission_mode !== "auto" ? `${entry.permission_mode} permissions` : ""}
          </span>
        )}
      </p>

      {entry.error && <p className="schedule-card-why">{entry.error}</p>}
      {error && <ErrorBanner>{error}</ErrorBanner>}

      {/* Actions last and pinned to the foot, so cards of different text lengths
          still line their buttons up across a row. */}
      <div className="schedule-card-actions">
        {(entry.state === "pending" || entry.state === "recurring") && (
          <>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={() => onEdit(entry)}
            >
              {ICON_EDIT} Edit
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={cancel}
            >
              {ICON_CANCEL}{" "}
              {busy ? "Cancelling…" : entry.state === "recurring" ? "Cancel schedule" : "Cancel"}
            </button>
          </>
        )}
        {unskippable && (
          <button
            type="button"
            className="btn btn-secondary"
            disabled={busy}
            onClick={unskip}
          >
            {busy ? "Working…" : "Unskip"}
          </button>
        )}
        {/* The transcript is the only place that knows what the turn actually DID —
            this page can say a message ran, never what came of it. The Inbox
            addresses a session by exactly this id, so once the watcher has captured
            it the card can hand the reader straight to the conversation.
            Not gated on the sessions mount being ready: the /sessions route already
            shows its own honest "Preparing…" state, and threading that readiness in
            here would buy a disabled link instead of a slightly slow one. */}
        {entry.claude_session_id && (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() =>
              navigateUrl(`/sessions?peek=${encodeURIComponent(entry.claude_session_id!)}`)
            }
          >
            {ICON_INBOX} Open in Inbox
          </button>
        )}
      </div>
    </div>
  );
}

export default function Scheduled() {
  const [state, setState] = useState<ScheduleResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // localStorage can THROW (private mode, locked-down webviews), and this read
  // runs during first render — unguarded it took the whole page down for a
  // preference. Same posture as the Inbox's layout memory: storage failing
  // costs the memory, never the page.
  const [view, setView] = useState<"calendar" | "list" | "board">(() => {
    try {
      const saved = localStorage.getItem(VIEW_KEY);
      return saved === "list" || saved === "board" ? saved : "calendar";
    } catch {
      return "calendar";
    }
  });
  // null = closed; a Date = open, prefilled (from a calendar slot click);
  // "blank" = open from the New job button, prefilled with "in an hour".
  const [creating, setCreating] = useState<Date | "blank" | null>(null);
  // A job being changed reopens the same modal prefilled. Editing an
  // OCCURRENCE means editing its rule: the template is what gets edited, and
  // the resolver below is what makes a click on any chip land there.
  const [editing, setEditing] = useState<ScheduledMessage | null>(null);
  // The folder a deep link named (see the effect below); null for every other
  // way of opening the form.
  const [newTarget, setNewTarget] = useState<string | null>(null);

  // `?new=1&target=…` — the chat composer's Schedule button
  // (templates/claude/template.html openScheduler). The chat knows the folder
  // and nothing else, so the params carry only that, and this turns them into
  // an already-open form: landing on a page with a button still to press would
  // make one control read as two.
  //
  // The params are CONSUMED, not just read: cleared with replaceState so a
  // reload (or Back to here from wherever the user went next) is the plain
  // Schedule page rather than a modal that reopens forever. replaceState, not
  // push, for the same reason — the deep-linked URL is not a place worth
  // keeping in the history.
  useEffect(() => {
    const q = new URLSearchParams(location.search);
    if (q.get("new") !== "1") return;
    setNewTarget(q.get("target"));
    setCreating(new Date(Date.now() + NEW_LINK_LEAD_MS));
    q.delete("new");
    q.delete("target");
    const rest = q.toString();
    history.replaceState(history.state, "", location.pathname + (rest ? `?${rest}` : ""));
  }, []);

  const reload = () => {
    getSchedule().then(
      (r) => {
        setState(r);
        setLoadError(null);
      },
      (e: Error) => setLoadError(e.message),
    );
  };
  useEffect(reload, []);
  useRefreshOnReturn(reload);
  useEffect(() => {
    const id = window.setInterval(reload, POLL_MS);
    return () => window.clearInterval(id);
  }, []);

  const pickView = (v: "calendar" | "list" | "board") => {
    setView(v);
    try {
      localStorage.setItem(VIEW_KEY, v);
    } catch {
      // A blocked store forgets the choice; the switch itself still happens.
    }
  };

  const entries = state?.entries ?? [];
  const live = entries.filter(isLive);
  const past = entries.filter((e) => !isLive(e));

  const edit = (entry: ScheduledMessage) => {
    const template = entry.template_id
      ? entries.find((e) => e.id === entry.template_id)
      : null;
    setEditing(template ?? entry);
    setCreating(null);
  };

  return (
    // `schedule-page` is not decoration: it is what lets the card sections opt
    // out of the 760px content column `.prefs-page > *` imposes, while the prose
    // inside them stays at that measure. See styles/schedule.css.
    <div className="prefs-page schedule-page">
      {/* Copy voice: Google Calendar's — short, neutral, no mechanics essays
          (Akshil, 2026-08-14, "wording is too crude"). What the first cut
          explained in paragraphs, the UI now mostly says by shape; the one
          honesty that must survive is the app-must-be-running caveat below. */}
      <header>
        <h1>Schedule</h1>
        <p className="deploy-muted">
          Tasks Claude runs for you, at the times you choose.
        </p>
      </header>

      {loadError && <ErrorBanner>Failed to load scheduled messages: {loadError}</ErrorBanner>}
      {!state && !loadError && <SkeletonLines rows={2} label="Loading scheduled messages" />}

      {state && (
        <section className="prefs-section schedule-main">
          <div className="schedule-toolbar">
            {/* Calendar first and default: this page's question is "when", and
                the list is the drill-down for "what exactly happened". */}
            <div className="schedule-form-seg" role="radiogroup" aria-label="View">
              <button type="button"
                      className={"btn btn-secondary" + (view === "calendar" ? " is-active" : "")}
                      aria-pressed={view === "calendar"}
                      onClick={() => pickView("calendar")}>
                Calendar
              </button>
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
            </div>
            <button type="button" className="btn btn-primary schedule-new"
                    onClick={() => setCreating("blank")}>
              + New task
            </button>
          </div>

          <p className="deploy-muted">
            {/* The one caveat that must stay on the page, in one line. The
                bound is a server setting, so the number comes from the server. */}
            Tasks run while fused-render is open. If it's closed at the time, a one-time
            task still runs up to {hoursText(state.max_late_seconds)} later; a repeating
            task waits for its next time.
          </p>

          {view === "calendar" ? (
            <ScheduleCalendar
              entries={entries}
              onCancelled={reload}
              onCreateAt={(t) => setCreating(t)}
              onEdit={edit}
            />
          ) : view === "board" ? (
            <ScheduleBoard entries={entries} onEdit={edit} />
          ) : (
            <>
              {live.length === 0 ? (
                <p className="deploy-muted">No upcoming tasks</p>
              ) : (
                <div className="schedule-cards">
                  {live.map((e) => (
                    <EntryCard key={e.id} entry={e} unskippable={canUnskip(e, entries)}
                               onCancelled={reload} onEdit={edit} />
                  ))}
                </div>
              )}
              {past.length > 0 && (
                <>
                  {/* Newest first, which is the server's ordering — the two groups
                      run in opposite directions on purpose (see
                      schedule.list_entries); do not re-sort what it gives. */}
                  <h2>Past</h2>
                  <div className="schedule-cards">
                    {past.map((e) => (
                      <EntryCard key={e.id} entry={e} unskippable={canUnskip(e, entries)}
                               onCancelled={reload} onEdit={edit} />
                    ))}
                  </div>
                </>
              )}
            </>
          )}
        </section>
      )}

      {(creating !== null || editing) && state && (
        <NewJobModal
          initialTime={creating instanceof Date ? creating : null}
          initialTarget={newTarget}
          editing={editing}
          permissionModes={state.permission_modes}
          // `newTarget` is cleared with the rest: it described the ONE form the
          // deep link opened, and left standing it would prefill the next
          // "+ New task" with a folder the user arrived from some time ago.
          onClose={() => { setCreating(null); setEditing(null); setNewTarget(null); }}
          onCreated={reload}
        />
      )}
    </div>
  );
}

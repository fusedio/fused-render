// Scheduled messages page — the durable list of prompts waiting to go to Claude,
// and how the ones already sent turned out.
//
// It does NOT schedule anything, deliberately. Scheduling lives in the claude
// template's composer (templates/claude/template.html, the "Send now" pill),
// because that row already knows the folder and already holds the message —
// asking for both again here was making the user do twice what they had done
// once. What is left is the part with nowhere else to live: everything scheduled
// across every folder, in one list, with a way to call it off.
//
// Backend: fused_render/schedule.py (the store and the loop that fires it),
// server/routers/schedule.py (this page's three calls). The app does the sending
// itself rather than handing the job to cron, so a scheduled turn runs with the
// same environment, credentials and file-access consent as one the user typed —
// see that module's docstring for why that matters more than it sounds.
//
// The honest cost of that choice is the one thing this page must never hide:
// **nothing fires while the app is closed.** A due time that passes with the app
// shut fires when it next starts, but only within the server's catch-up bound;
// past that the entry is `missed`. Both facts are stated on the page, because a
// scheduling UI that implies a guarantee it does not have is worse than no
// scheduling UI.
//
// Section layout and per-action busy/error state follow shell/Mounts.tsx.
import { useEffect, useState } from "react";
import { cancelScheduledMessage, getSchedule } from "@platform/lib/api";
import type { ScheduledMessage, ScheduleResult, ScheduledState } from "@platform/lib/api";
import { useRefreshOnReturn } from "@platform/lib/hooks";
import { navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";

// How often the list re-reads itself. A `pending` entry becomes `sent` on the
// server's own tick (30s), so anything much slower than this shows a message as
// still-waiting for a while after it went out.
const POLL_MS = 20000;

function formatDue(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

// Relative time for a due stamp, in both directions — a pending entry is "in
// 20m", a sent one "12m ago". The list is mostly read to answer "when?", so this
// carries more than the absolute stamp does (which is still shown as a title).
function relativeDue(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const secs = (t - Date.now()) / 1000;
  const ahead = secs >= 0;
  const s = Math.abs(secs);
  const say = (n: number, unit: string) =>
    ahead ? `in ${n}${unit}` : `${n}${unit} ago`;
  if (s < 60) return ahead ? "any moment" : "just now";
  if (s < 3600) return say(Math.round(s / 60), "m");
  if (s < 86400) return say(Math.round(s / 3600), "h");
  return say(Math.round(s / 86400), "d");
}

// Not finished with: waiting for its time, being sent, or sent with a turn still
// running. The third case is why this is not just a `state` check — a sent
// message whose session is still working is very much live.
const isLive = (e: ScheduledMessage) =>
  e.state === "pending" || e.state === "sending" || (e.state === "sent" && !e.turn);

const STATE_LABELS: Record<ScheduledState, string> = {
  pending: "Scheduled",
  sending: "Sending…",
  sent: "Sent",
  missed: "Missed",
  error: "Failed",
  cancelled: "Cancelled",
};

// `sent` only means the SESSION STARTED. How the turn then went is a second fact,
// and conflating them would report a dead turn as a clean send — so a sent row is
// labelled by its turn once the turn has one.
function stateLabel(entry: ScheduledMessage): string {
  if (entry.state === "sent") {
    if (entry.turn === "ok") return "Ran";
    if (entry.turn === "failed") return "Turn failed";
    if (entry.turn === "cancelled") return "Stopped";
    // Not "Running…": nothing is watching it any more, and saying otherwise is
    // the frozen-progress-bar lie the job registry's `stalled` state avoids.
    if (entry.turn === "unknown") return "Stopped reporting";
    return "Running…";
  }
  return STATE_LABELS[entry.state] ?? entry.state;
}

// Which CSS state class a row paints with. A failed turn reads as a failure even
// though `state` is the cheerful half of the pair.
function stateTone(entry: ScheduledMessage): string {
  if (entry.state === "sent" && (entry.turn === "failed" || entry.turn === "unknown"))
    return "error";
  if (entry.state === "sent" && !entry.turn) return "sending";
  return entry.state;
}

function hoursText(seconds: number): string {
  const hours = Math.round(seconds / 3600);
  if (hours >= 48) return `${Math.round(hours / 24)} days`;
  if (hours >= 1) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${Math.max(1, Math.round(seconds / 60))} minutes`;
}

function EntryCard({
  entry,
  onCancelled,
}: {
  entry: ScheduledMessage;
  onCancelled: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const cancel = async () => {
    setBusy(true);
    setError(null);
    try {
      await cancelScheduledMessage(entry.id);
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
          {relativeDue(stamp)}
        </span>
      </div>

      {/* The prompt is the card's subject — what the reader scans to find the one
          they mean — so it gets the body colour and the room. Clamped rather than
          scrolled: a card grid wants even heights, and the full text is one hover
          away in the title. */}
      <p className="schedule-card-message" title={entry.message}>
        {entry.message}
      </p>

      <p className="schedule-card-meta">
        <code title={entry.target}>{entry.target}</code>
        <span>
          {entry.session_id ? "continues an existing session" : "new session"}
          {entry.permission_mode !== "auto" ? ` · ${entry.permission_mode} permissions` : ""}
        </span>
      </p>

      {entry.error && <p className="schedule-card-why">{entry.error}</p>}
      {error && <ErrorBanner>{error}</ErrorBanner>}

      {/* Actions last and pinned to the foot, so cards of different text lengths
          still line their buttons up across a row. */}
      <div className="schedule-card-actions">
        {entry.state === "pending" && (
          <button
            type="button"
            className="btn btn-secondary"
            disabled={busy}
            onClick={cancel}
          >
            {busy ? "Cancelling…" : "Cancel"}
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
            Open in Inbox
          </button>
        )}
      </div>
    </div>
  );
}

export default function Scheduled() {
  const [state, setState] = useState<ScheduleResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
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

  const entries = state?.entries ?? [];
  const live = entries.filter(isLive);
  const past = entries.filter((e) => !isLive(e));

  return (
    <div className="prefs-page">
      <header>
        <h1>Scheduled messages</h1>
        <p className="deploy-muted">
          Messages waiting to be sent to Claude, and how the ones already sent turned out.
          When a message comes due, fused-render starts the session itself — in the same
          folder, with the same skills and permissions as a chat you open by hand.
        </p>
        <p className="deploy-muted">
          {/* Scheduling is NOT here, deliberately. The chat already knows the folder
              and already holds the message, so asking for both again on a settings
              page was work the user had done once already. */}
          To schedule one, open a chat on the folder or file you want it to run in and
          pick a time from the <strong>Send now</strong> menu in the composer.
        </p>
      </header>

      {loadError && <ErrorBanner>Failed to load scheduled messages: {loadError}</ErrorBanner>}
      {!state && !loadError && <SkeletonLines rows={2} label="Loading scheduled messages" />}

      {state && (
        <section className="prefs-section">
          <h2>Scheduled &amp; running</h2>
          <p className="deploy-muted">
            {/* The limitation, stated where it is relevant rather than buried. The
                bound is a server setting, so the number comes from the server. */}
            These send when they come due — as long as fused-render is running. If the app is
            closed at that moment, the message goes out the next time it starts, up to{" "}
            {hoursText(state.max_late_seconds)} late; after that it is marked missed rather
            than sent at a time you did not intend.
          </p>
          <p className="deploy-muted">
            {/* Where the ✕ lives, said once. A running turn is a job row, not a
                pending promise, so Cancel below is not the thing that stops it. */}
            A message that has already gone out shows up as a running job at the foot of the
            screen while its turn works — including when it is waiting on a permission
            prompt — and can be stopped from there.
          </p>
          {live.length === 0 ? (
            <p className="deploy-muted">Nothing scheduled.</p>
          ) : (
            <div className="schedule-cards">
              {live.map((e) => <EntryCard key={e.id} entry={e} onCancelled={reload} />)}
            </div>
          )}
        </section>
      )}

      {past.length > 0 && (
        <section className="prefs-section">
          <h2>Already handled</h2>
          <p className="deploy-muted">
            {/* Newest first, which is the server's ordering — the two groups run in
                opposite directions on purpose (see schedule.list_entries), so this
                section must not re-sort or reverse what it is given. */}
            Most recent first. Kept rather than cleared: a message that failed or was missed is
            exactly the one worth being able to read afterwards.
          </p>
          <div className="schedule-cards">
            {past.map((e) => (
              <EntryCard key={e.id} entry={e} onCancelled={reload} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

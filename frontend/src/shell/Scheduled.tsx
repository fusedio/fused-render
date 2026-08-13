// Scheduled messages page — a durable list of prompts to send Claude later,
// and the form that adds to it.
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
import {
  cancelScheduledMessage,
  getSchedule,
  scheduleMessage,
} from "@platform/lib/api";
import type { ScheduledMessage, ScheduleResult, ScheduledState } from "@platform/lib/api";
import { useRefreshOnReturn } from "@platform/lib/hooks";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Field, TextArea, TextInput } from "@platform/ui/field/fields";
import { SkeletonLines } from "@platform/ui/Skeleton";

// How often the list re-reads itself. A `pending` entry becomes `sent` on the
// server's own tick (30s), so anything much slower than this shows a message as
// still-waiting for a while after it went out.
const POLL_MS = 20000;

// The quick-pick offsets, in minutes. Deliberately short: these are the "later
// today" cases, where computing a wall-clock time by hand is the annoying part.
// Anything further out is what the date field is for.
const QUICK_PICKS: { label: string; minutes: number }[] = [
  { label: "In 1 hour", minutes: 60 },
  { label: "In 3 hours", minutes: 180 },
  { label: "Tomorrow morning", minutes: -1 }, // computed; see quickPickValue
];

// `datetime-local` wants "YYYY-MM-DDTHH:mm" in LOCAL time — which is also
// exactly what the server reads a naive timestamp as (schedule.parse_due), so
// the value the user sees is the value that gets scheduled, with no conversion
// on either side.
function toLocalInputValue(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

function quickPickValue(minutes: number): string {
  if (minutes >= 0) return toLocalInputValue(new Date(Date.now() + minutes * 60000));
  // Tomorrow at 9am local, the one absolute time worth a chip.
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(9, 0, 0, 0);
  return toLocalInputValue(d);
}

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

// Still waiting on the loop, so still cancellable and still worth a countdown.
const isLive = (e: ScheduledMessage) => e.state === "pending" || e.state === "sending";

const STATE_LABELS: Record<ScheduledState, string> = {
  pending: "Scheduled",
  sending: "Sending…",
  sent: "Sent",
  missed: "Missed",
  error: "Failed",
  cancelled: "Cancelled",
};

function hoursText(seconds: number): string {
  const hours = Math.round(seconds / 3600);
  if (hours >= 48) return `${Math.round(hours / 24)} days`;
  if (hours >= 1) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${Math.max(1, Math.round(seconds / 60))} minutes`;
}

function EntryRow({
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
      // said and reload, so the row corrects itself.
      setError((e as Error).message);
      onCancelled();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="prefs-section">
      <div className="schedule-row-head">
        <span className={`schedule-state schedule-state--${entry.state}`}>
          {STATE_LABELS[entry.state] ?? entry.state}
        </span>
        {/* A waiting message is described by when it is DUE; one that has already
            acted, by when it actually went out — which is not the same instant
            when the app caught up on it after being closed. */}
        {isLive(entry) || !entry.fired ? (
          <span title={formatDue(entry.due)}>{relativeDue(entry.due)}</span>
        ) : (
          <span title={`Due ${formatDue(entry.due)} · ran ${formatDue(entry.fired)}`}>
            {relativeDue(entry.fired)}
          </span>
        )}
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
      </div>
      <p className="schedule-message">{entry.message}</p>
      <p className="deploy-muted">
        <code>{entry.target}</code>
        {entry.session_id ? " · continues an existing session" : " · new session"}
        {entry.permission_mode !== "auto" ? ` · ${entry.permission_mode} permissions` : ""}
      </p>
      {entry.error && <p className="deploy-muted">{entry.error}</p>}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

export default function Scheduled() {
  const [state, setState] = useState<ScheduleResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [target, setTarget] = useState("");
  const [message, setMessage] = useState("");
  const [due, setDue] = useState(() => quickPickValue(60));
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

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

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setFormError(null);
    setNote(null);
    try {
      // `due` is the naive local string from the input; the server reads it as
      // local time, so it is sent through unchanged rather than converted here.
      const { entry } = await scheduleMessage({ target, message, due });
      setNote(`Scheduled for ${formatDue(entry.due)}.`);
      setMessage("");
      reload();
    } catch (err) {
      setFormError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const entries = state?.entries ?? [];
  const live = entries.filter(isLive);
  const past = entries.filter((e) => !isLive(e));

  return (
    <div className="prefs-page">
      <header>
        <h1>Scheduled messages</h1>
        <p className="deploy-muted">
          Send Claude a message later. When it comes due, fused-render starts the session
          itself — in the same folder, with the same skills and permissions as a chat you
          open by hand.
        </p>
      </header>

      <section className="prefs-section">
        <h2>Schedule a message</h2>
        <form className="schedule-form" onSubmit={submit}>
          <Field
            label="Folder or file"
            required
            hint="The session runs here, exactly as if you opened a chat on it."
          >
            <TextInput
              type="text"
              value={target}
              placeholder="~/Documents/Fused/local/my-app"
              onChange={(ev) => setTarget(ev.target.value)}
              required
            />
          </Field>
          <Field label="Message" required>
            <TextArea
              value={message}
              rows={4}
              placeholder="Update the changelog for everything that landed today."
              onChange={(ev) => setMessage(ev.target.value)}
              required
            />
          </Field>
          <Field label="When" required hint="Your local time.">
            <TextInput
              type="datetime-local"
              value={due}
              onChange={(ev) => setDue(ev.target.value)}
              required
            />
          </Field>
          <div className="schedule-quick">
            {QUICK_PICKS.map((q) => (
              <button
                key={q.label}
                type="button"
                className="btn btn-secondary"
                onClick={() => setDue(quickPickValue(q.minutes))}
              >
                {q.label}
              </button>
            ))}
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? "Scheduling…" : "Schedule"}
            </button>
          </div>
        </form>
        {formError && <ErrorBanner>{formError}</ErrorBanner>}
        {note && <p className="deploy-muted">{note}</p>}
        <p className="deploy-muted">
          A scheduled message runs unattended, so it uses automatic permissions: Claude's own
          classifier approves what it judges safe and parks anything else for you to answer in
          that folder's chat.
        </p>
      </section>

      {loadError && <ErrorBanner>Failed to load scheduled messages: {loadError}</ErrorBanner>}
      {!state && !loadError && <SkeletonLines rows={2} label="Loading scheduled messages" />}

      {state && (
        <section className="prefs-section">
          <h2>Waiting to send</h2>
          <p className="deploy-muted">
            {/* The limitation, stated where it is relevant rather than buried. The
                bound is a server setting, so the number comes from the server. */}
            These send when they come due — as long as fused-render is running. If the app is
            closed at that moment, the message goes out the next time it starts, up to{" "}
            {hoursText(state.max_late_seconds)} late; after that it is marked missed rather
            than sent at a time you did not intend.
          </p>
          {live.length === 0 ? (
            <p className="deploy-muted">Nothing scheduled.</p>
          ) : (
            live.map((e) => <EntryRow key={e.id} entry={e} onCancelled={reload} />)
          )}
        </section>
      )}

      {past.length > 0 && (
        <section className="prefs-section">
          <h2>Already handled</h2>
          <p className="deploy-muted">
            Kept rather than cleared: a message that failed or was missed is exactly the one
            worth being able to read afterwards.
          </p>
          {past.map((e) => (
            <EntryRow key={e.id} entry={e} onCancelled={reload} />
          ))}
        </section>
      )}
    </div>
  );
}

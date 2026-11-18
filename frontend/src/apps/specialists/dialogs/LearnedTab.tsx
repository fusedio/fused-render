// Settings › Learned (PRODUCT §6, §8; mock-up Learned.dc.html): every change of
// the recent updates — already in effect, each with Revert — the drafts to
// people waiting for the user, and Run now.
//
// While an update runs, the tab says "Updating… started 12 s ago" and asks
// GET …/nightly every few seconds; when the run ends it reads the changes again
// on its own. Revert sends a plain-words request into the main chat and the
// dialog stays open, saying "Revert asked in the chat" until the item reads
// `reverted` (or `changed`: something else touched that entry since, C4).
import { useCallback, useEffect, useRef, useState } from "react";
import {
  discardDraft,
  errorText,
  getLearned,
  listDrafts,
  revertChange,
  runNightly,
  sendDraft,
  type ChangeItem,
  type Draft,
  type LearnedRun,
} from "@platform/lib/specialists-api";
import { kindClass, draftLine } from "../components/SidePanel";
import { dayLabel } from "../components/FoldedSession";
import {
  agoText,
  getNightlyStatus,
  revertFallback,
  revertable,
  secondPerson,
  whenText,
  type NightlyStatus,
} from "../lib/api-extra";

/** How often a live run is asked about. */
export const NIGHTLY_POLL_MS = 3_000;
/** How often the changes are re-read while a revert is pending. */
export const REVERT_POLL_MS = 5_000;

/** Where an item came from, in words: "From 2 notes, a review and a breakout". */
export function fromLine(from: readonly string[] | undefined): string {
  if (!from?.length) return "";
  const n = from.filter((x) => x.startsWith("n-")).length;
  const r = from.filter((x) => x.startsWith("r-")).length;
  const b = from.filter((x) => x.startsWith("b-")).length;
  const parts: string[] = [];
  if (n) parts.push(n === 1 ? "a note" : `${n} notes`);
  if (r) parts.push(r === 1 ? "a review of a task" : `reviews of ${r} tasks`);
  if (b) parts.push(b === 1 ? "a breakout" : `${b} breakouts`);
  if (!parts.length) return "";
  const last = parts.pop()!;
  return "From " + (parts.length ? parts.join(", ") + " and " + last : last);
}

export interface LearnedApi {
  getLearned: typeof getLearned;
  listDrafts: typeof listDrafts;
  getNightly: (id: string) => Promise<NightlyStatus>;
  runNightly: typeof runNightly;
  revertChange: typeof revertChange;
  sendDraft: typeof sendDraft;
  discardDraft: typeof discardDraft;
}
const DEFAULT_API: LearnedApi = {
  getLearned,
  listDrafts,
  getNightly: getNightlyStatus,
  runNightly,
  revertChange,
  sendDraft,
  discardDraft,
};

/** One change: one line, the rest ("From …", the full text) behind More. */
function ItemRow({
  item,
  pending,
  busy,
  onRevert,
}: {
  item: ChangeItem;
  pending: boolean;
  busy: boolean;
  onRevert: () => void;
}) {
  const [open, setOpen] = useState(false);
  const reverted = item.state === "reverted";
  const changed = item.state === "changed";
  const text = secondPerson(item.text);
  const from = fromLine(item.from);
  return (
    <div className={"spx-run-item" + (open ? " is-open" : "")}>
      <span className={"spx-kind is-badge " + kindClass(item.kind)}>{item.kind}</span>
      <div className="spx-run-text">
        <span className={(reverted ? "spx-struck" : "spx-strong") + (open ? "" : " spx-oneline")} title={open ? undefined : text}>
          {text}
        </span>
        {open && from ? <span className="spx-muted spx-small">{from}</span> : null}
      </div>
      <button type="button" className="spx-linkbtn spx-more" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        {open ? "Less" : "More"}
      </button>
      {reverted ? (
        <span className="spx-warn spx-small spx-run-state">Reverted</span>
      ) : changed ? (
        <span className="spx-muted spx-small spx-run-state" title="This entry was changed again after the update, so it cannot be reverted on its own.">
          Changed since
        </span>
      ) : pending ? (
        <span className="spx-muted spx-small spx-run-state" role="status">
          Revert asked in the chat
        </span>
      ) : revertable(item.state) ? (
        <button type="button" className="btn btn-secondary" onClick={onRevert} disabled={busy} aria-label={`Revert: ${item.text}`}>
          Revert
        </button>
      ) : null}
    </div>
  );
}

export function LearnedTab({
  specialistId,
  sendToChat,
  sendAndClose = sendToChat,
  api = DEFAULT_API,
  now = () => Date.now(),
}: {
  specialistId: string;
  /** Send into the main chat, keeping the dialog open (Revert). */
  sendToChat: (text: string) => boolean;
  /** Send into the main chat and close the dialog (Review and send). */
  sendAndClose?: (text: string) => boolean;
  api?: LearnedApi;
  now?: () => number;
}) {
  const [runs, setRuns] = useState<LearnedRun[] | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [nightly, setNightly] = useState<NightlyStatus | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [requested, setRequested] = useState<ReadonlySet<string>>(new Set());
  const [, tick] = useState(0);
  const wasRunning = useRef(false);

  const loadChanges = useCallback(() => {
    api.getLearned(specialistId, 5).then((r) => setRuns(r.runs || []), (e) => setError(errorText(e)));
    api.listDrafts(specialistId).then((r) => setDrafts(r.drafts || []), () => {});
  }, [api, specialistId]);
  const loadNightly = useCallback(() => {
    api.getNightly(specialistId).then((r) => setNightly(r), () => {});
  }, [api, specialistId]);
  useEffect(() => {
    loadChanges();
    loadNightly();
  }, [loadChanges, loadNightly]);

  const running = !!nightly?.running;
  // A live run: ask again every few seconds, and tick the "started N s ago".
  useEffect(() => {
    if (!running) return;
    const poll = setInterval(loadNightly, NIGHTLY_POLL_MS);
    const clock = setInterval(() => tick((n) => n + 1), 1_000);
    return () => {
      clearInterval(poll);
      clearInterval(clock);
    };
  }, [running, loadNightly]);
  // …and when it ends, read what it changed.
  useEffect(() => {
    if (wasRunning.current && !running) {
      loadChanges();
      setNote("The update finished. Its changes are below.");
    }
    wasRunning.current = running;
  }, [running, loadChanges]);

  // A revert is pending: re-read until the item settles (reverted / changed).
  const pendingIds = (runs ?? []).flatMap((r) =>
    r.items.filter((i) => i.state === "reverting" || (requested.has(i.id) && revertable(i.state))).map((i) => i.id),
  );
  const anyPending = pendingIds.length > 0;
  useEffect(() => {
    if (!anyPending) return;
    const t = setInterval(loadChanges, REVERT_POLL_MS);
    return () => clearInterval(t);
  }, [anyPending, loadChanges]);

  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    setNote("");
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const revert = (run: string, item: ChangeItem) =>
    act(async () => {
      let r;
      try {
        r = await api.revertChange(specialistId, run, item.id);
      } catch (e) {
        // 409 "gone": the change is no longer there to undo. Say why, show the list as it is now.
        if ((e as { status?: number }).status === 409) loadChanges();
        throw e;
      }
      if (!sendToChat(r.message || revertFallback(item))) throw new Error("The chat is not ready yet. Try again in a moment.");
      setRequested((s) => new Set(s).add(item.id));
    });
  const review = (d: Draft) =>
    act(async () => {
      const r = await api.sendDraft(specialistId, d.id);
      if (!sendAndClose(r.message)) throw new Error("The chat is not ready yet. Try again in a moment.");
      loadChanges();
    });
  const discard = (d: Draft) =>
    act(async () => {
      await api.discardDraft(specialistId, d.id);
      loadChanges();
    });
  const runNow = () =>
    act(async () => {
      try {
        await api.runNightly(specialistId);
        setNightly((n) => ({ last_ok: n?.last_ok ?? null, last_row: n?.last_row ?? null, running: true, started: now() / 1000 }));
        loadNightly();
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (status === 409) {
          setNote("It cannot run right now: an update or a chat is already running in this specialist.");
          return;
        }
        throw e;
      }
    });

  const waiting = drafts.filter((d) => d.status === "waiting" || d.status === "sending" || !d.status);
  // A report whose bug was fixed, or that went stale: nothing to do, kept for reference.
  const closed = drafts.filter((d) => d.status === "closed");
  const started = nightly?.started || 0;

  return (
    <div className="spx-tab">
      <div className="spx-tab-row">
        <span className={"spx-small " + (running ? "spx-updating" : "spx-muted")} role="status">
          {running ? (
            <>
              <span className="spx-dot is-working" aria-hidden="true" />
              {started ? `Updating… started ${agoText(started, now())}` : "Updating…"}
            </>
          ) : (
            "Updates run once a day while the app is open and idle."
          )}
        </span>
        <button type="button" className="btn btn-secondary" onClick={() => void runNow()} disabled={busy || running}>
          Run now
        </button>
      </div>
      {note ? (
        <div className="spx-muted spx-small" role="status">
          {note}
        </div>
      ) : null}
      {error ? (
        <div className="spx-error" role="alert">
          {error}
        </div>
      ) : null}
      {runs === null ? <div className="spx-muted">Loading…</div> : null}
      {runs && !runs.length && !running ? (
        <div className="spx-empty-note">No updates yet. The first one runs on its own once a day, or press Run now.</div>
      ) : null}
      {(runs ?? []).map((run, ri) => (
        <section key={run.run} className="spx-run" aria-label={ri === 0 ? "Latest update" : `Update of ${dayLabel(run.at)}`}>
          <div className="spx-run-head">
            <h3>{ri === 0 ? "Latest update" : `Update of ${dayLabel(run.at)}`}</h3>
            <span className="spx-muted spx-small">
              {run.at ? `${whenText(run.at)} · ` : ""}
              {run.summary}
              {run.items.length ? " Everything below is already in effect." : ""}
            </span>
          </div>
          {run.items.length ? (
            <div className="spx-run-items">
              {run.items.map((item) => (
                <ItemRow
                  key={item.id}
                  item={item}
                  busy={busy}
                  pending={pendingIds.includes(item.id)}
                  onRevert={() => void revert(run.run, item)}
                />
              ))}
            </div>
          ) : (
            <div className="spx-muted spx-small">No changes.</div>
          )}
        </section>
      ))}
      <section className="spx-run" aria-label="Waiting for you">
        <div className="spx-run-head">
          <h3>Waiting for you</h3>
          <span className="spx-muted spx-small">A message to another person cannot be taken back, so it is never sent by itself.</span>
        </div>
        {waiting.length ? (
          waiting.map((d) => (
            <div key={d.id} className="spx-run-item is-draft">
              <span className="spx-kind is-badge is-draft">Draft</span>
              <div className="spx-run-text">
                <span className="spx-strong">{draftLine(d)}</span>
                {d.why ? <span className="spx-muted spx-small">{d.why}</span> : null}
              </div>
              <span className="spx-row-gap">
                <button type="button" className="btn btn-primary" onClick={() => void review(d)} disabled={busy}>
                  Review and send
                </button>
                <button type="button" className="btn btn-secondary" onClick={() => void discard(d)} disabled={busy}>
                  Discard
                </button>
              </span>
            </div>
          ))
        ) : (
          <div className="spx-muted spx-small">Nothing waiting.</div>
        )}
      </section>
      {closed.length ? (
        <section className="spx-run" aria-label="Closed reports">
          <h3 className="spx-muted">Closed reports</h3>
          <ul className="spx-plain-list">
            {closed.map((d) => (
              <li key={d.id} className="spx-muted spx-small">
                {draftLine(d)}
                {d.why ? ` · ${d.why}` : ""}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

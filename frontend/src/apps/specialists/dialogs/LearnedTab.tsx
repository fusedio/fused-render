// Settings › Learned (PRODUCT §6, §8; mock-up Learned.dc.html): every change of
// the recent nightly updates — already in effect, each with Revert — the
// drafts to people waiting for the user, and Run now.
import { useCallback, useEffect, useState } from "react";
import {
  discardDraft,
  errorText,
  getLearned,
  getNightly,
  listDrafts,
  revertChange,
  revertMessage,
  runNightly,
  sendDraft,
  type ChangeItem,
  type Draft,
  type LearnedRun,
} from "@platform/lib/specialists-api";
import { kindClass, draftLine } from "../components/SidePanel";
import { dayLabel } from "../components/FoldedSession";

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
  getNightly: typeof getNightly;
  runNightly: typeof runNightly;
  revertChange: typeof revertChange;
  sendDraft: typeof sendDraft;
  discardDraft: typeof discardDraft;
}
const DEFAULT_API: LearnedApi = { getLearned, listDrafts, getNightly, runNightly, revertChange, sendDraft, discardDraft };

export function LearnedTab({
  specialistId,
  sendToChat,
  api = DEFAULT_API,
}: {
  specialistId: string;
  sendToChat: (text: string) => boolean;
  api?: LearnedApi;
}) {
  const [runs, setRuns] = useState<LearnedRun[] | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.getLearned(specialistId, 5).then((r) => setRuns(r.runs || []), (e) => setError(errorText(e)));
    api.listDrafts(specialistId).then((r) => setDrafts(r.drafts || []), () => {});
    api.getNightly(specialistId).then((r) => setRunning(!!r.running), () => {});
  }, [api, specialistId]);
  useEffect(load, [load]);

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
      const r = await api.revertChange(specialistId, run, item.id);
      if (!sendToChat(r.message || revertMessage(item, run))) throw new Error("The chat is not ready yet. Try again in a moment.");
      load();
    });
  const review = (d: Draft) =>
    act(async () => {
      const r = await api.sendDraft(specialistId, d.id);
      if (!sendToChat(r.message)) throw new Error("The chat is not ready yet. Try again in a moment.");
      load();
    });
  const discard = (d: Draft) =>
    act(async () => {
      await api.discardDraft(specialistId, d.id);
      load();
    });
  const runNow = () =>
    act(async () => {
      try {
        await api.runNightly(specialistId);
        setRunning(true);
        setNote("The update is running. Its changes show here when it is done.");
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

  return (
    <div className="spx-tab">
      <div className="spx-tab-row">
        <span className="spx-muted spx-small">
          {running ? "An update is running now." : "Updates run once a day while the app is open and idle."}
        </span>
        <button type="button" className="btn btn-secondary" onClick={() => void runNow()} disabled={busy || running}>
          Run now
        </button>
      </div>
      {note ? (
        <div className="spx-muted" role="status">
          {note}
        </div>
      ) : null}
      {error ? (
        <div className="spx-error" role="alert">
          {error}
        </div>
      ) : null}
      {runs === null ? <div className="spx-muted">Loading…</div> : null}
      {runs && !runs.length ? <div className="spx-muted">No updates yet. The first one runs tonight, or press Run now.</div> : null}
      {(runs ?? []).map((run, ri) => (
        <section key={run.run} className="spx-run" aria-label={`Update of ${dayLabel(run.at)}`}>
          <div className="spx-run-head">
            <h3>{ri === 0 ? "Last night's update" : `Update of ${dayLabel(run.at)}`}</h3>
            <span className="spx-muted spx-small">
              {run.summary}
              {run.items.length ? " Everything below is already in effect." : ""}
            </span>
          </div>
          {run.items.length ? (
            <div className="spx-run-items">
              {run.items.map((item) => {
                const reverted = item.state === "reverted";
                const reverting = item.state === "reverting";
                return (
                  <div key={item.id} className="spx-run-item">
                    <span className={"spx-kind is-badge " + kindClass(item.kind)}>{item.kind}</span>
                    <div className="spx-run-text">
                      <span className={reverted ? "spx-struck" : "spx-strong"}>{item.text}</span>
                      {fromLine(item.from) ? <span className="spx-muted spx-small">{fromLine(item.from)}</span> : null}
                    </div>
                    {reverted ? (
                      <span className="spx-warn spx-small">Reverted</span>
                    ) : reverting ? (
                      <span className="spx-muted spx-small">Revert asked in the chat</span>
                    ) : (
                      <button
                        type="button"
                        className="btn btn-secondary"
                        onClick={() => void revert(run.run, item)}
                        disabled={busy}
                        aria-label={`Revert: ${item.text}`}
                      >
                        Revert
                      </button>
                    )}
                  </div>
                );
              })}
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

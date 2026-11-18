// THE RIGHT COLUMN (PRODUCT §8; mock-up Main.dc.html): Learned last night
// (already in effect; each item has Revert; See all), Waiting for you (drafts
// to people: Review and send, Discard), Inbox (count, View), and at the
// bottom a quiet "In your tasks". Breakouts are not listed or openable here.
//
// Revert and Review and send ask the server for the message (it records the
// request) and send it into the main chat, where the request and the answer
// show (U, V).
import { useCallback, useEffect, useState } from "react";
import { chatUrl } from "@platform/lib/queue";
import { navigateUrl } from "@platform/lib/router";
import {
  discardDraft,
  errorText,
  getPanel,
  revertChange,
  revertMessage,
  sendDraft,
  type ChangeItem,
  type Draft,
  type SpecialistPanel,
} from "@platform/lib/specialists-api";

export const PANEL_POLL_MS = 30_000;

export interface PanelApi {
  getPanel: typeof getPanel;
  revertChange: typeof revertChange;
  sendDraft: typeof sendDraft;
  discardDraft: typeof discardDraft;
}
const DEFAULT_API: PanelApi = { getPanel, revertChange, sendDraft, discardDraft };

/** The colour class of a change kind (Learned / Corrected / Retired / …). */
export function kindClass(kind: string): string {
  const k = kind.toLowerCase();
  if (k === "corrected") return "is-corrected";
  if (k === "retired" || k === "moved" || k === "changed") return "is-retired";
  return "is-learned";
}

/** "Bug report to Ana on Slack: sync stalls…" */
export function draftLine(d: Draft): string {
  const to = [d.to?.name, d.to?.service ? `on ${d.to.service}` : ""].filter(Boolean).join(" ");
  const what = d.subject || d.text.split("\n")[0] || "";
  return to ? `${what ? what + " · " : ""}to ${to}` : what;
}

export function SidePanel({
  specialistId,
  sendToChat,
  onOpenSettings,
  refreshKey = 0,
  api = DEFAULT_API,
}: {
  specialistId: string;
  /** Send a message into the main chat; false when the chat cannot take it. */
  sendToChat: (text: string) => boolean;
  onOpenSettings: (tab: "learned" | "memory") => void;
  /** Bumped by the page to re-read now. */
  refreshKey?: number;
  api?: PanelApi;
}) {
  const [panel, setPanel] = useState<SpecialistPanel | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [requested, setRequested] = useState<ReadonlySet<string>>(new Set());

  const load = useCallback(() => {
    api.getPanel(specialistId).then(
      (p) => {
        setPanel(p);
        setError("");
      },
      (err) => setError(errorText(err)),
    );
  }, [api, specialistId]);

  useEffect(() => {
    setPanel(null);
    setRequested(new Set());
    load();
    const t = setInterval(load, PANEL_POLL_MS);
    return () => clearInterval(t);
  }, [load, refreshKey]);

  const revert = async (item: ChangeItem) => {
    const run = panel?.learned?.run || "";
    setBusy(item.id);
    setError("");
    try {
      const r = await api.revertChange(specialistId, run, item.id);
      if (!sendToChat(r.message || revertMessage(item, run))) {
        setError("The chat is not ready yet. Try Revert again in a moment.");
        return;
      }
      setRequested((s) => new Set(s).add(item.id));
      load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  };
  const review = async (d: Draft) => {
    setBusy(d.id);
    setError("");
    try {
      const r = await api.sendDraft(specialistId, d.id);
      if (!sendToChat(r.message)) {
        setError("The chat is not ready yet. Try again in a moment.");
        return;
      }
      load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  };
  const discard = async (d: Draft) => {
    setBusy(d.id);
    setError("");
    try {
      await api.discardDraft(specialistId, d.id);
      load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  };

  const learned = panel?.learned;
  const waiting = (panel?.waiting ?? []).filter((d) => d.status === "waiting" || d.status === "sending" || !d.status);
  const helping = panel?.tasks?.helping_now ?? [];
  const guided = panel?.tasks?.guided_week ?? 0;
  const inbox = panel?.inbox?.count ?? 0;

  return (
    <aside className="spx-side" aria-label="What changed">
      {error ? (
        <div className="spx-error" role="alert">
          {error}
        </div>
      ) : null}
      <section className="spx-side-sec" aria-labelledby={`spx-learned-${specialistId}`}>
        <div className="spx-side-head">
          <h2 id={`spx-learned-${specialistId}`}>Learned last night</h2>
          {learned && learned.total > 0 ? (
            <button type="button" className="spx-linkbtn" onClick={() => onOpenSettings("learned")}>
              See all {learned.total}
            </button>
          ) : null}
        </div>
        {learned && learned.items.length ? (
          <>
            <div className="spx-muted spx-small">Already in effect. Revert anything that looks wrong.</div>
            {learned.items.map((item) => {
              const reverted = item.state === "reverted";
              const pending = item.state === "reverting" || requested.has(item.id);
              return (
                <div key={item.id} className="spx-item">
                  <span className={reverted ? "spx-struck" : ""}>{item.text}</span>
                  <div className="spx-item-foot">
                    <span className={"spx-kind " + kindClass(item.kind)}>{item.kind}</span>
                    {reverted ? (
                      <span className="spx-small spx-warn">Reverted</span>
                    ) : pending ? (
                      <span className="spx-small spx-muted">Revert asked in the chat</span>
                    ) : (
                      <button
                        type="button"
                        className="spx-pill"
                        onClick={() => void revert(item)}
                        disabled={busy !== null}
                        aria-label={`Revert: ${item.text}`}
                      >
                        Revert
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </>
        ) : (
          <div className="spx-muted spx-small">{panel ? "Nothing changed in the last update." : "Loading…"}</div>
        )}
      </section>

      {waiting.length ? (
        <section className="spx-side-sec" aria-labelledby={`spx-waiting-${specialistId}`}>
          <h2 id={`spx-waiting-${specialistId}`}>Waiting for you</h2>
          {waiting.map((d) => (
            <div key={d.id} className="spx-item is-draft">
              <span>{draftLine(d)}</span>
              <div className="spx-item-foot">
                <span className="spx-small spx-draft">Draft, not sent</span>
                <span className="spx-row-gap">
                  <button type="button" className="spx-pill is-primary" onClick={() => void review(d)} disabled={busy !== null}>
                    Review and send
                  </button>
                  <button type="button" className="spx-pill" onClick={() => void discard(d)} disabled={busy !== null}>
                    Discard
                  </button>
                </span>
              </div>
            </div>
          ))}
        </section>
      ) : null}

      <section className="spx-side-sec" aria-labelledby={`spx-inbox-${specialistId}`}>
        <h2 id={`spx-inbox-${specialistId}`}>Inbox</h2>
        <div className="spx-item-foot">
          <span className="spx-small">
            {inbox === 0 ? "No notes waiting" : `${inbox} ${inbox === 1 ? "note" : "notes"} waiting for tonight's update`}
          </span>
          <button type="button" className="spx-linkbtn" onClick={() => onOpenSettings("memory")}>
            View
          </button>
        </div>
      </section>

      <section className="spx-side-sec spx-tasks" aria-labelledby={`spx-tasks-${specialistId}`}>
        <h2 id={`spx-tasks-${specialistId}`} className="spx-tasks-title">
          In your tasks
        </h2>
        {helping.length ? (
          <span>
            Helping in {helping.length} {helping.length === 1 ? "task" : "tasks"} now
            {helping.map((t, i) => {
              const href = chatUrl(t.file, t.session_id);
              return (
                <span key={t.breakout_id || t.session_id}>
                  {i === 0 ? " · " : ", "}
                  <a
                    href={href}
                    onClick={(e) => {
                      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                      e.preventDefault();
                      void navigateUrl(href);
                    }}
                  >
                    {t.title || "a task"}
                  </a>
                </span>
              );
            })}
          </span>
        ) : (
          <span>Not helping in any task right now</span>
        )}
        <span>
          Gave guidance to {guided} {guided === 1 ? "task" : "tasks"} this week
        </span>
      </section>
    </aside>
  );
}

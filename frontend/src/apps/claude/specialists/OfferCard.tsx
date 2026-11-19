// THE OFFER CARD (docs/specialists.md §4 "The offer card…"): a task called
// `specialist_breakout`, the permission server raised a card for it, and this
// draws it as "Hand this to the <name> specialist?" with the brief and three
// answers.
//
//   Open breakout  decide allow with `answers` = the brief as JSON (the brief
//                  branch in agent `_decide`), then POST …/breakouts (origin
//                  offer, request_id) and open the card beside the task.
//   Edit brief     the three fields become editable; Open breakout sends the
//                  edited brief, which is what the specialist sees.
//   Keep trying    decide deny.
import { useRef, useState } from "react";
import { createBreakout, errorText, type Brief } from "@platform/lib/specialists-api";
import { chatAnchorOf, openSpecialistBreakout } from "@platform/lib/specialist-breakouts";
import type { CardActions } from "../ui/CardStack";
import type { PermissionRow } from "../protocol/types";
import { unansweredReasonOf } from "../protocol/unanswered";
import { resolveSpecialist, readSpecialistList } from "./useTaskEvents";

export interface OfferHost {
  parentSessionId: string;
  parentFile: string;
  parentTitle?: string;
  parentFolder?: string;
  runId?: string;
  /** Re-read the task's events (the breakout_open chip). */
  refresh?: () => void;
}

/** The brief the card was raised with (the `specialist_breakout` input fields). */
export function briefOf(row: PermissionRow): Brief {
  const input = (row.input || {}) as Record<string, unknown>;
  const s = (k: string) => (typeof input[k] === "string" ? (input[k] as string) : "");
  // An answered card shows what was decided, which may be an edited brief.
  const a = row.answers || {};
  return {
    problem: a.problem ?? s("problem"),
    tried: a.tried ?? s("tried"),
    look_at: a.look_at ?? s("look_at"),
  };
}

/** The `answers` payload `answerQuestion` turns into the decide body's JSON. */
export function briefAnswers(b: Brief): Record<string, string[]> {
  return { problem: [b.problem.trim()], tried: [b.tried.trim()], look_at: [b.look_at.trim()] };
}

export function OfferCard({
  row,
  actions,
  host,
  create = createBreakout,
  list = readSpecialistList,
}: {
  row: PermissionRow;
  actions: CardActions;
  host: OfferHost;
  // Seams for tests.
  create?: typeof createBreakout;
  list?: typeof readSpecialistList;
}) {
  const input = (row.input || {}) as Record<string, unknown>;
  const specKey = typeof input.specialist === "string" ? input.specialist : "";
  const name = (typeof input.name === "string" && input.name) || specKey || "the";
  const [brief, setBrief] = useState<Brief>(() => briefOf(row));
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const resolved = !!row.decision;
  const self = useRef<HTMLElement>(null);

  const open = async () => {
    const b = { problem: brief.problem.trim(), tried: brief.tried.trim(), look_at: brief.look_at.trim() };
    if (!b.problem) {
      setError("Say what the problem is first.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      // The decision first: the task is waiting on it, and it must hear
      // "handed off" whatever happens to the card below.
      if (!row.decision) await actions.answerQuestion(row.id, briefAnswers(b));
      const spec = resolveSpecialist(await list(), specKey, typeof input.name === "string" ? input.name : "");
      if (!spec) throw new Error(`The ${name} specialist no longer exists.`);
      const made = await create(spec.id, {
        parent: {
          session_id: host.parentSessionId,
          ...(host.runId ? { run_id: host.runId } : {}),
          file: host.parentFile,
          ...(host.parentTitle ? { title: host.parentTitle } : {}),
        },
        brief: b,
        origin: "offer",
        request_id: row.id,
      });
      openSpecialistBreakout({
        breakoutId: made.breakout_id,
        specialistId: spec.id,
        specialistName: made.name || spec.name,
        home: made.home,
        parentSessionId: host.parentSessionId,
        parentFile: host.parentFile,
        ...(host.parentTitle ? { parentTitle: host.parentTitle } : {}),
        seed: made.seed,
        startParams: made.start_params || {},
        brief: b,
        ...(host.parentFolder ? { parentFolder: host.parentFolder } : {}),
        ...(chatAnchorOf(self.current) ? { anchor: chatAnchorOf(self.current)! } : {}),
      });
      host.refresh?.();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const keepTrying = async () => {
    setBusy(true);
    setError("");
    try {
      await actions.decidePermission(row.id, "deny", "once");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const unanswered = unansweredReasonOf(row);
  const status = !resolved
    ? row.sendError || ""
    : row.decision === "allow"
      ? `Handed to the ${name} specialist. The breakout opens beside this task.`
      : unanswered
        ? "Nobody answered. The task keeps working on it."
        : "You chose to keep trying. The task keeps working on it.";

  const field = (key: keyof Brief, label: string) => (
    <>
      <span className="spx-offer-key" id={`${row.id}-${key}`}>
        {label}
      </span>
      {editing && !resolved ? (
        <textarea
          className="spx-offer-input"
          aria-labelledby={`${row.id}-${key}`}
          rows={key === "problem" ? 2 : 2}
          value={brief[key]}
          onChange={(e) => setBrief({ ...brief, [key]: e.target.value })}
        />
      ) : (
        <span>{brief[key] || "—"}</span>
      )}
    </>
  );

  return (
    <section ref={self} className={"spx-offer" + (resolved ? " is-resolved" : "")} aria-label={`Hand this to the ${name} specialist?`} data-perm-id={row.id}>
      <div className="spx-offer-who">
        <span className="spx-chip-dot" aria-hidden="true" />
        {name} specialist
      </div>
      <div className="spx-offer-title">Hand this to the {name} specialist?</div>
      <div className="spx-offer-brief">
        {field("problem", "Problem")}
        {field("tried", "Tried")}
        {field("look_at", "Look at")}
      </div>
      {!resolved ? (
        <>
          <div className="spx-offer-note">
            It opens in a card beside this task. The task keeps working on everything else in the meantime and leaves
            this problem alone.
          </div>
          <div className="spx-offer-actions">
            <button type="button" className="spx-btn is-primary" onClick={() => void open()} disabled={busy}>
              Open breakout
            </button>
            <button
              type="button"
              className="spx-btn"
              onClick={() => setEditing((v) => !v)}
              disabled={busy}
              aria-pressed={editing}
            >
              {editing ? "Done editing" : "Edit brief"}
            </button>
            <button type="button" className="spx-btn" onClick={() => void keepTrying()} disabled={busy}>
              Keep trying
            </button>
          </div>
        </>
      ) : null}
      {status ? (
        <div className={"spx-offer-status" + (row.sendError && !resolved ? " is-error" : "")} role="status">
          {status}
        </div>
      ) : null}
      {error ? (
        <div className="spx-offer-status is-error" role="alert">
          {error}
          {resolved && row.decision === "allow" ? (
            <button type="button" className="spx-btn" onClick={() => void open()} disabled={busy}>
              Try again
            </button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

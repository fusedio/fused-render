// THE BREAKOUT CONTROL inside a task (PRODUCT §3.3.3): "Open a breakout with"
// — "A copy of this chat" (today's breakout, unchanged, only where it is on)
// and one row per specialist. Choosing a specialist opens a brief the user
// types; Open breakout creates it (POST …/breakouts, origin hand) and opens
// the floating card beside the task.
import { useEffect, useId, useRef, useState } from "react";
import { createBreakout, errorText, type Brief, type SpecialistSummary } from "@platform/lib/specialists-api";
import { openSpecialistBreakout } from "@platform/lib/specialist-breakouts";

export interface BreakoutMenuHost {
  parentSessionId: string;
  parentFile: string;
  parentTitle?: string;
  runId?: string;
  refresh?: () => void;
}

export function BreakoutMenu({
  specialists,
  host,
  onCopyOfChat,
  create = createBreakout,
}: {
  specialists: readonly SpecialistSummary[];
  host: BreakoutMenuHost;
  /** Today's breakout (pick mode). Absent when the plain breakout is off. */
  onCopyOfChat?: () => void;
  // Seam for tests.
  create?: typeof createBreakout;
}) {
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<SpecialistSummary | null>(null);
  const [brief, setBrief] = useState<Brief>({ problem: "", tried: "", look_at: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const menuId = useId();
  const wrap = useRef<HTMLDivElement>(null);
  const firstItem = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    firstItem.current?.focus?.();
    const onDown = (e: MouseEvent) => {
      if (wrap.current && e.target instanceof Node && !wrap.current.contains(e.target)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!specialists.length && !onCopyOfChat) return null;

  const pick = (s: SpecialistSummary) => {
    setOpen(false);
    setTarget(s);
    setBrief({ problem: "", tried: "", look_at: "" });
    setError("");
  };
  const start = async () => {
    if (!target) return;
    const b = { problem: brief.problem.trim(), tried: brief.tried.trim(), look_at: brief.look_at.trim() };
    if (!b.problem) {
      setError("Say what the problem is first.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const made = await create(target.id, {
        parent: {
          session_id: host.parentSessionId,
          ...(host.runId ? { run_id: host.runId } : {}),
          file: host.parentFile,
          ...(host.parentTitle ? { title: host.parentTitle } : {}),
        },
        brief: b,
        origin: "hand",
      });
      openSpecialistBreakout({
        breakoutId: made.breakout_id,
        specialistId: target.id,
        specialistName: made.name || target.name,
        home: made.home,
        parentSessionId: host.parentSessionId,
        parentFile: host.parentFile,
        ...(host.parentTitle ? { parentTitle: host.parentTitle } : {}),
        seed: made.seed,
        startParams: made.start_params || {},
      });
      setTarget(null);
      host.refresh?.();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="spx-bmenu" ref={wrap}>
      {target ? (
        <form
          className="spx-brief"
          aria-label={`Brief for the ${target.name} specialist`}
          onSubmit={(e) => {
            e.preventDefault();
            void start();
          }}
        >
          <div className="spx-brief-title">Breakout with the {target.name} specialist</div>
          <label className="spx-brief-field">
            <span>Problem</span>
            <textarea rows={2} value={brief.problem} onChange={(e) => setBrief({ ...brief, problem: e.target.value })} />
          </label>
          <label className="spx-brief-field">
            <span>Tried</span>
            <textarea rows={2} value={brief.tried} onChange={(e) => setBrief({ ...brief, tried: e.target.value })} />
          </label>
          <label className="spx-brief-field">
            <span>Look at</span>
            <textarea rows={2} value={brief.look_at} onChange={(e) => setBrief({ ...brief, look_at: e.target.value })} />
          </label>
          {error ? (
            <div className="spx-offer-status is-error" role="alert">
              {error}
            </div>
          ) : null}
          <div className="spx-offer-actions">
            <button type="submit" className="spx-btn is-primary" disabled={busy}>
              Open breakout
            </button>
            <button type="button" className="spx-btn" onClick={() => setTarget(null)} disabled={busy}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
      <div className="spx-bmenu-row">
        <button
          type="button"
          className="spx-btn spx-bmenu-btn"
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={open ? menuId : undefined}
          onClick={() => setOpen((v) => !v)}
        >
          Breakout
        </button>
        {open ? (
          <div className="spx-bmenu-pop" role="menu" id={menuId} aria-label="Open a breakout with">
            <div className="spx-bmenu-head" aria-hidden="true">
              Open a breakout with
            </div>
            {onCopyOfChat ? (
              <button
                type="button"
                role="menuitem"
                className="spx-bmenu-item"
                ref={firstItem}
                onClick={() => {
                  setOpen(false);
                  onCopyOfChat();
                }}
              >
                A copy of this chat
              </button>
            ) : null}
            {specialists.map((s, i) => (
              <button
                key={s.id}
                type="button"
                role="menuitem"
                className="spx-bmenu-item"
                {...(i === 0 && !onCopyOfChat ? { ref: firstItem } : {})}
                onClick={() => pick(s)}
              >
                <span className="spx-chip-dot" aria-hidden="true" />
                {s.name} specialist
              </button>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

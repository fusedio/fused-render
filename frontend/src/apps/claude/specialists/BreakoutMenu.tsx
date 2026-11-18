// THE BREAKOUT CONTROL inside a task (PRODUCT §3.3.3): "Open a breakout with"
// — "A copy of this chat" (today's breakout, unchanged, only where it is on)
// and one row per specialist, the ones whose folder triggers match this task
// first under "Suggested". A menu: arrow keys move, Enter picks, Escape closes.
// Choosing a specialist opens a brief pre-filled from the task (the last
// request and the last error) that the user edits; Open breakout creates it
// (POST …/breakouts, origin hand) and opens the floating card beside the task.
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import {
  createBreakout,
  errorText,
  getSpecialist,
  type Brief,
  type SpecialistSummary,
  type Triggers,
} from "@platform/lib/specialists-api";
import { chatAnchorOf, openSpecialistBreakout } from "@platform/lib/specialist-breakouts";
import type { Turn } from "../protocol/controller-api";

export interface BreakoutMenuHost {
  parentSessionId: string;
  parentFile: string;
  parentTitle?: string;
  parentFolder?: string;
  runId?: string;
  refresh?: () => void;
}

const BRIEF_FIELD_MAX = 600;

const clip = (s: string, n: number) => {
  const flat = s.replace(/\s+\n/g, "\n").trim();
  return flat.length <= n ? flat : flat.slice(0, n - 1).trimEnd() + "…";
};

/** A brief pre-filled from the task: the problem is the last thing the user
 *  asked and the last error seen after it; Look at names what that error came
 *  from. Trimmed; the user edits it. */
export function briefFromTask(turns: readonly Turn[]): Brief {
  let request = "";
  let error = "";
  let where = "";
  for (let i = turns.length - 1; i >= 0; i--) {
    const t = turns[i]!;
    if (!error && t.role === "error" && t.text) error = t.text;
    if (!error && t.role === "assistant") {
      const segs = t.segments ?? [];
      // Only an error the turn ENDED on: one the task got past (a later tool
      // call ran fine) is not the problem (seen live: a recovered `python3 -c`
      // failure became "Last error: Exit code 1", Look at `python3 -I -c "`).
      const seg = [...segs].reverse().find((s) => s.kind === "tool");
      if (seg && seg.status === "error") {
        const lines = (seg.output || "").split("\n").map((l) => l.trim()).filter(Boolean);
        error = lines.find((l) => !/^exit code -?\d+$/i.test(l)) || lines[0] || "";
        const inp = seg.input || {};
        where = String(inp.file_path || inp.command || inp.path || inp.pattern || "").replace(/\s+/g, " ").trim();
      }
    }
    if (t.role === "user" && t.text?.trim()) {
      request = t.text;
      break;
    }
  }
  const problem = [request && clip(request, BRIEF_FIELD_MAX), error && `Last error: ${clip(error, 240)}`]
    .filter(Boolean)
    .join("\n");
  return { problem, tried: "", look_at: where ? clip(where, 240) : "" };
}

/** Does a folder trigger cover this task's folder? (`~/x` matches any home's
 *  `/…/x`; a trigger matches the folder itself and anything under it.) */
export function folderMatches(folder: string, triggers: readonly string[]): boolean {
  const f = folder.replace(/[\\/]+$/, "");
  if (!f) return false;
  for (const raw of triggers) {
    const t = (raw || "").trim().replace(/[\\/]+$/, "");
    if (!t) continue;
    if (t.startsWith("~")) {
      const tail = t.slice(1);
      const at = f.indexOf(tail);
      if (tail && at >= 0 && (f.length === at + tail.length || f[at + tail.length] === "/")) return true;
      continue;
    }
    if (f === t || f.startsWith(t + "/")) return true;
  }
  return false;
}

// The triggers live on the detail route; read once per specialist per page.
const triggerCache = new Map<string, Promise<Triggers | null>>();
function triggersOf(id: string, load: typeof getSpecialist): Promise<Triggers | null> {
  let p = triggerCache.get(id);
  if (!p) {
    p = load(id).then(
      (d) => d.triggers ?? null,
      () => null,
    );
    triggerCache.set(id, p);
  }
  return p;
}

export function BreakoutMenu({
  specialists,
  host,
  turns = [],
  folder = "",
  onCopyOfChat,
  create = createBreakout,
  detail = getSpecialist,
}: {
  specialists: readonly SpecialistSummary[];
  host: BreakoutMenuHost;
  /** The task's turns, for the pre-filled brief. */
  turns?: readonly Turn[];
  /** The task's folder, for "Suggested". */
  folder?: string;
  /** Today's breakout (pick mode). Absent when the plain breakout is off. */
  onCopyOfChat?: () => void;
  // Seams for tests.
  create?: typeof createBreakout;
  detail?: typeof getSpecialist;
}) {
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<SpecialistSummary | null>(null);
  const [brief, setBrief] = useState<Brief>({ problem: "", tried: "", look_at: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [suggested, setSuggested] = useState<ReadonlySet<string>>(() => new Set());
  const menuId = useId();
  const wrap = useRef<HTMLDivElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  // Which specialists' folder triggers cover this task.
  const ids = specialists.map((s) => s.id).join(",");
  useEffect(() => {
    if (!open || !folder) return;
    let live = true;
    void Promise.all(
      specialists.map(async (s) => {
        const t = s.triggers ?? (await triggersOf(s.id, detail));
        return t && folderMatches(folder, t.folders ?? []) ? s.id : null;
      }),
    ).then((hits) => {
      if (live) setSuggested(new Set(hits.filter((x): x is string => !!x)));
    });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, folder, ids, detail]);

  const ordered = useMemo(() => {
    const first = specialists.filter((s) => suggested.has(s.id));
    const rest = specialists.filter((s) => !suggested.has(s.id));
    return { first, rest };
  }, [specialists, suggested]);

  const items = () => Array.from(pop.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
  useEffect(() => {
    if (!open) return;
    items()[0]?.focus?.();
    const onDown = (e: MouseEvent) => {
      if (wrap.current && e.target instanceof Node && !wrap.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const onMenuKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const list = items();
    if (!list.length) return;
    const at = list.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      const next = at < 0 ? (step > 0 ? 0 : list.length - 1) : (at + step + list.length) % list.length;
      list[next]?.focus?.();
    } else if (e.key === "Home") {
      e.preventDefault();
      list[0]?.focus?.();
    } else if (e.key === "End") {
      e.preventDefault();
      list[list.length - 1]?.focus?.();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      trigger.current?.focus?.();
    } else if (e.key === "Enter" || e.key === " ") {
      if (at >= 0) {
        e.preventDefault();
        list[at]?.click?.();
      }
    }
  };

  if (!specialists.length && !onCopyOfChat) return null;

  const pick = (s: SpecialistSummary) => {
    setOpen(false);
    setTarget(s);
    setBrief(briefFromTask(turns));
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
      const anchor = chatAnchorOf(wrap.current);
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
        brief: b,
        ...(host.parentFolder ? { parentFolder: host.parentFolder } : {}),
        ...(anchor ? { anchor } : {}),
      });
      setTarget(null);
      host.refresh?.();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const row = (s: SpecialistSummary) => (
    <button key={s.id} type="button" role="menuitem" tabIndex={-1} className="spx-bmenu-item" onClick={() => pick(s)}>
      <span className="spx-chip-dot" aria-hidden="true" />
      {s.name} specialist
    </button>
  );

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
          onKeyDown={(e) => {
            if (e.key === "Escape" && !busy) {
              e.stopPropagation();
              setTarget(null);
            }
          }}
        >
          <div className="spx-brief-title">Breakout with the {target.name} specialist</div>
          <div className="spx-brief-hint">Filled in from the task. Edit it so the specialist starts from the right place.</div>
          <label className="spx-brief-field">
            <span>Problem</span>
            <textarea rows={3} value={brief.problem} onChange={(e) => setBrief({ ...brief, problem: e.target.value })} />
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
          ref={trigger}
          type="button"
          className="spx-btn spx-bmenu-btn"
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={open ? menuId : undefined}
          onClick={() => setOpen((v) => !v)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              setOpen(true);
            }
          }}
        >
          Breakout
        </button>
        {open ? (
          <div
            className="spx-bmenu-pop"
            role="menu"
            id={menuId}
            aria-label="Open a breakout with"
            ref={pop}
            onKeyDown={onMenuKey}
          >
            <div className="spx-bmenu-head" aria-hidden="true">
              Open a breakout with
            </div>
            {onCopyOfChat ? (
              <button
                type="button"
                role="menuitem"
                tabIndex={-1}
                className="spx-bmenu-item"
                onClick={() => {
                  setOpen(false);
                  onCopyOfChat();
                }}
              >
                A copy of this chat
              </button>
            ) : null}
            {ordered.first.length ? (
              <>
                <div className="spx-bmenu-group" aria-hidden="true">
                  Suggested
                </div>
                {ordered.first.map(row)}
                {ordered.rest.length ? (
                  <div className="spx-bmenu-group" aria-hidden="true">
                    Other specialists
                  </div>
                ) : null}
              </>
            ) : null}
            {ordered.rest.map(row)}
          </div>
        ) : null}
      </div>
    </div>
  );
}

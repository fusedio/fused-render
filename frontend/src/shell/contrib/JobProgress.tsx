// A long operation's progress, as the steps the user would name — "Making
// your copy on GitHub…", "Sending your changes…" — never the machinery's.
// Steps before the current one wear a check, the current one a spinner, the
// rest wait dimmed. A job that reports no `step` shows the first step as
// current: true for every multi-step job here (each starts with its first).
//
// `since` (ms) turns on an elapsed clock beside the current step, and a
// step's `hint` says who decides how long it takes (GitHub, Claude) — the
// waiting is fine when the person knows why (§16.13).
import { useEffect, useState } from "react";
import { Check, Loader2 } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { elapsed, type Step } from "./words";

/** "0:07" since `since` (ms), ticking once a second; "" without a start. */
export function useElapsed(since: number | null | undefined): string {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!since) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [since]);
  return since ? elapsed(now - since) : "";
}

export function JobProgress({ steps, current, since }: { steps: Step[]; current: string | null; since?: number | null }) {
  const at = Math.max(0, steps.findIndex((s) => s.key === current));
  const clock = useElapsed(since);
  return (
    <ol className="contrib-steps" aria-live="polite">
      {steps.map((s, i) => (
        <li
          key={s.key}
          className={cn("contrib-step", i < at && "is-done", i === at && "is-current")}
          aria-current={i === at ? "step" : undefined}
        >
          <span className="contrib-step-mark" aria-hidden="true">
            {i < at ? <Check /> : i === at ? <Loader2 className="animate-spin" /> : null}
          </span>
          <span className="contrib-step-text">
            {s.label}
            {i === at && clock && <span className="contrib-step-clock">{clock}</span>}
            {i === at && s.hint && <span className="contrib-step-hint">{s.hint}</span>}
          </span>
        </li>
      ))}
    </ol>
  );
}

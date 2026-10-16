// A long operation's progress, as the steps the user would name — "Making
// your copy on GitHub…", "Sending your changes…" — never the machinery's.
// Steps before the current one wear a check, the current one a spinner, the
// rest wait dimmed. A job that reports no `step` shows the first step as
// current: true for every multi-step job here (each starts with its first).
import { Check, Loader2 } from "lucide-react";
import { cn } from "@platform/lib/utils";

export function JobProgress({
  steps,
  current,
}: {
  steps: { key: string; label: string }[];
  current: string | null;
}) {
  const at = Math.max(0, steps.findIndex((s) => s.key === current));
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
          {s.label}
        </li>
      ))}
    </ol>
  );
}

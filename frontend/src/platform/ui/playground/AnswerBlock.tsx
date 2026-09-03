// Label above, card below — the shape every stage's result wears.
//
// `pg-answer-block` is KEPT on the block, as a hook and nothing else: the AI
// tour (platform/lib/tours/ai.ts) waits on `.pg-answer-block` to know the
// answer has landed, and both the idle slot and the filled card render it, so
// dropping the class would strand the tour on its last step. Every visual on
// this element is a utility; the class name carries no style of its own once
// ai-playground.css is gone.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

/** The tour's landmark. See above — a hook, not a style. */
export const TOUR_ANSWER_BLOCK = "pg-answer-block";

export function AnswerBlock({ className, ...props }: ComponentProps<"div">) {
  return (
    <div className={cn(TOUR_ANSWER_BLOCK, "flex flex-col gap-2", className)} {...props} />
  );
}

export function AnswerLabel({ className, ...props }: ComponentProps<"p">) {
  return (
    <p
      className={cn(
        "m-0 flex min-w-0 items-baseline gap-2 text-[12px] font-semibold text-[var(--fg-muted)]",
        className,
      )}
      {...props}
    />
  );
}

/** Which model produced what is under this label. Same weight and hue as the
 *  transcribe stage's metadata line rather than a new treatment — it is the
 *  same kind of fact, and must not read as louder than the label it qualifies.
 *  `min-w-0` plus the ellipsis because a repo id is long and must not push the
 *  label off its own row. */
export function AnswerProvenance({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "min-w-0 truncate text-[12px] font-normal text-[var(--fg-muted)] opacity-75 tabular-nums",
        className,
      )}
      {...props}
    />
  );
}

/** The one reply, rendered. The right padding leaves the first line clear of
 *  the copy button pinned in the corner. */
export function AnswerCard({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "relative rounded-[12px] border border-solid border-[var(--border)] bg-[var(--bg-alt)] py-[14px] pr-[44px] pl-4 text-[13.5px] leading-[1.6] [overflow-wrap:anywhere]",
        className,
      )}
      {...props}
    />
  );
}

/** Tokens, timings, tokens-per-second — the run's own figures under the card. */
export function TurnFoot({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "mt-1.5 flex items-baseline gap-3 text-[11.5px] text-[var(--fg-muted)] tabular-nums",
        className,
      )}
      {...props}
    />
  );
}

/** A reasoning model's scratchpad, collapsed. Native <details>, as everywhere
 *  else in this tab. */
export function ThinkBlock({ className, ...props }: ComponentProps<"details">) {
  return (
    <details
      className={cn("mb-2 text-[12px] text-[var(--fg-muted)] [&>summary]:cursor-pointer", className)}
      {...props}
    />
  );
}

export function ThinkBody({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn("mt-1 border-l-2 border-[var(--border)] pl-2.5 whitespace-pre-wrap", className)}
      {...props}
    />
  );
}

/** What the run is doing, said plainly. */
export function StageStatus({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("m-0 text-[12.5px] text-[var(--fg-muted)]", className)} {...props} />;
}

/** What went wrong. */
export function StageError({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("m-0 text-[12.5px] text-[var(--error)]", className)} {...props} />;
}

/** The URL asked for a capability this machine cannot run, and the stage below
 *  is the substitute. Quiet, not alarming: nothing failed. */
export function BlockedAsk({ className, ...props }: ComponentProps<"p">) {
  return (
    <p
      className={cn("mt-0 mr-0 mb-2.5 ml-0 text-[12.5px] leading-[1.45] text-[var(--fg-muted)]", className)}
      {...props}
    />
  );
}

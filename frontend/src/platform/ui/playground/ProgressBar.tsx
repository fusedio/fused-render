// A job's progress, as a 4px track. shadcn's `Progress` (base UI) is the whole
// widget — root, track, indicator — so the two halves are reached here through
// their `data-slot`s rather than through props the wrapper does not take.
//
// The fill eases over 0.4s because the poll that moves it lands about once a
// second: without it the bar teleports between readings. Reduced motion drops
// the easing, not the bar.
import { cn } from "@platform/lib/utils";
import { Progress } from "@platform/shadcn/ui/progress";

export function ProgressBar({
  /** 0–100. */
  value,
  className,
}: {
  value: number;
  className?: string;
}) {
  return (
    <Progress
      value={value}
      className={cn(
        "block w-[min(320px,100%)] gap-0",
        "[&_[data-slot=progress-track]]:h-1 [&_[data-slot=progress-track]]:overflow-hidden [&_[data-slot=progress-track]]:rounded-[2px] [&_[data-slot=progress-track]]:bg-[rgba(var(--tint),0.1)]",
        "[&_[data-slot=progress-indicator]]:bg-[var(--accent)] [&_[data-slot=progress-indicator]]:transition-[width] [&_[data-slot=progress-indicator]]:duration-[.4s] [&_[data-slot=progress-indicator]]:ease-[ease]",
        "motion-reduce:[&_[data-slot=progress-indicator]]:transition-none",
        className,
      )}
    />
  );
}

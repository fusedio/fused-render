// The live level: twelve bars, lit from the left by the microphone's own RMS.
// The lit bar is TALLER as well as brighter, so the meter reads at a glance and
// still reads with the colour taken away.
import type { ComponentProps } from "react";

import { cn } from "@platform/lib/utils";

export function LevelMeter({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn("inline-flex h-5 items-center gap-[3px]", className)}
      aria-hidden="true"
      {...props}
    />
  );
}

export function levelMeterBarClass(lit: boolean, className?: string): string {
  return cn(
    "w-1 rounded-[2px]",
    lit ? "h-4 bg-[var(--success-bright)]" : "h-2 bg-[rgba(var(--tint),0.15)]",
    className,
  );
}

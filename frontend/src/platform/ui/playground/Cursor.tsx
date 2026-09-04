// The generating caret: one accent block, blinking on a two-step 1s beat, drawn
// as a pseudo-element so it can sit at the end of streamed text without being a
// node the markdown renderer has to know about.
import { cn } from "@platform/lib/utils";

export function Cursor({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "after:animate-pg-blink after:text-[var(--accent)] after:content-['▍']",
        "motion-reduce:after:animate-none",
        className,
      )}
      {...props}
    />
  );
}

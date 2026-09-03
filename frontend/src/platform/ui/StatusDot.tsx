// THE status-bar indicator, shared by every chip (D590): outlined when the
// section holds nothing, filled when it holds something. Announced in words,
// never as a count — the caller passes a worded label and `role="img"` makes
// it one thing to a screen reader. Colour is `currentColor`, so the chip's own
// state (muted idle, destructive failure) tints it with no second rule.
import { cn } from "@platform/lib/utils";

export default function StatusDot({ on, label }: { on: boolean; label: string }) {
  return (
    <span
      data-slot="status-bar-dot"
      role="img"
      aria-label={label}
      className={cn("inline-block size-[7px] shrink-0 rounded-full border border-current", on && "bg-current")}
    />
  );
}

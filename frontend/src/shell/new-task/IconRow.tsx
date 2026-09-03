// A fact ABOUT the task, under the writing surface: a 14px glyph in a fixed
// gutter, the control(s) beside it. Every quiet row on the card (folder, when,
// permissions) hangs from the same gutter so the controls line up on one edge.
// `sub` rows carry no glyph and start on that edge — the lines that belong to
// the row above them (a note, a nested control).
import type { ReactNode } from "react";
import { cn } from "@platform/lib/utils";

export function IconRow({
  icon,
  children,
  className,
}: {
  icon?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("grid grid-cols-[1rem_minmax(0,1fr)] items-start gap-x-2.5", className)}>
      <span
        aria-hidden="true"
        className="flex h-7 items-center justify-center text-muted-foreground [&_svg]:size-3.5"
      >
        {icon}
      </span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

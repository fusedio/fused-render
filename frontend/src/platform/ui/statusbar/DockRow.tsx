// The one row shape every status-bar panel draws: a head line (title, optional
// figures, one action, a trailing ✕), then optional lines under it (model id,
// figures, progress bar, status sentence). Job, engine, model, repo, pairing and
// queue rows all compose these.
import type { ComponentProps } from "react";
import { X } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";

export function DockRow({ className, dimmed, ...props }: ComponentProps<"div"> & { dimmed?: boolean }) {
  return (
    <div
      data-slot="dock-row"
      className={cn("min-w-[min(238px,calc(100vw-34px))] px-2.5 py-2 text-xs", dimmed && "opacity-60", className)}
      {...props}
    />
  );
}

export function DockRowHead({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex items-center gap-2", className)} {...props} />;
}

/** The row's title. `token` = a single unbreakable token (model id, folder
 *  name): one line, ellipsised. Otherwise a prompt-shaped title: two lines,
 *  clamped. */
export function DockTitle({ token, className, ...props }: ComponentProps<"span"> & { token?: boolean }) {
  return (
    <span
      className={cn(
        "flex-1 font-medium",
        token ? "min-w-0 truncate" : "min-w-[15ch] line-clamp-2 [overflow-wrap:anywhere]",
        className,
      )}
      {...props}
    />
  );
}

/** A muted line under the head (status sentence, model id, figures). */
export function DockLine({ className, clamp = 3, ...props }: ComponentProps<"div"> & { clamp?: 1 | 2 | 3 }) {
  return (
    <div
      className={cn(
        "mt-1 text-xs leading-snug text-muted-foreground break-words",
        clamp === 1 && "truncate",
        clamp === 2 && "line-clamp-2",
        clamp === 3 && "line-clamp-3",
        className,
      )}
      {...props}
    />
  );
}

/** The row's own text action — Unload / Stop / Cancel / Update. */
export function DockAction({ className, ...props }: ComponentProps<typeof Button>) {
  return <Button variant="outline" size="xs" className={cn("shrink-0 h-6", className)} {...props} />;
}

/** The trailing dismiss ✕, pinned to the row's right edge. */
export function DockDismiss({ className, ...props }: ComponentProps<typeof Button>) {
  return (
    <Button variant="ghost" size="icon-xs" className={cn("ml-auto shrink-0 text-muted-foreground hover:text-foreground", className)} {...props}>
      <X />
    </Button>
  );
}

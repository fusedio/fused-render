// One status-bar item: a ghost button (the chip — label + the one filled/
// outlined circle) whose panel is a shadcn Popover anchored above it. The three
// docks (Models, Activity, Notifications) compose this; they keep owning WHEN
// the panel is open (autoExpand / exclusiveSection wiring), this only draws it.
//
// The popover is controlled. `onOpenChange` hands the dock the reason so a
// chip press keeps its unified toggle semantics (may write the preference)
// while an outside press / Escape stays a transient close (never writes).
import type { ReactNode } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { cn } from "@platform/lib/utils";
import StatusDot from "@platform/ui/StatusDot";

export type SectionCloseReason = "trigger" | "dismiss";

export function StatusBarSection({
  label,
  on,
  dotLabel,
  idle,
  failure,
  open,
  title,
  hasRows,
  onToggle,
  onDismiss,
  children,
}: {
  label: string;
  /** Filled circle = the section holds something. */
  on: boolean;
  /** Worded accessible name for the circle ("jobs running"). */
  dotLabel: string;
  /** Nothing in the section: muted chip text. */
  idle: boolean;
  /** Something failed in here: destructive chip text. */
  failure?: boolean;
  open: boolean;
  title: string;
  /** A panel with rows gets the shared fixed width; an empty one hugs its sentence. */
  hasRows: boolean;
  /** The chip itself was pressed. */
  onToggle: () => void;
  /** Outside press / Escape / focus-out — a transient close. */
  onDismiss: () => void;
  children: ReactNode;
}) {
  return (
    <Popover
      open={open}
      onOpenChange={(next, details) => {
        if (details.reason === "trigger-press") onToggle();
        else if (!next) onDismiss();
      }}
    >
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="xs"
            className={cn(
              // Quiet text on the bar, not a control sitting on it: no box of
              // its own, the bar's own xs/muted type, and a square footprint
              // that fills the strip's height. `appearance-none border-0
              // bg-transparent` is load-bearing — the token sheet ships without
              // Tailwind's preflight, so a button with no background of its own
              // paints the platform widget (grey fill, outset border).
              "h-full min-w-0 appearance-none gap-1.5 rounded-none border-0 bg-transparent px-1.5 text-xs font-normal hover:text-foreground",
              idle && "text-muted-foreground",
              failure && "text-destructive",
            )}
            title={title}
          />
        }
      >
        <span className="truncate">{label}</span>
        <StatusDot on={on} label={dotLabel} />
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="end"
        sideOffset={6}
        className={cn(
          "p-0 gap-0 rounded-lg shadow-sm overflow-hidden tabular-nums",
          hasRows ? "w-[min(340px,calc(100vw-32px))]" : "w-auto max-w-[min(340px,calc(100vw-32px))]",
        )}
      >
        {children}
      </PopoverContent>
    </Popover>
  );
}

/** The panel's own empty sentence ("No models loaded"). */
export function DockEmpty({ children }: { children: ReactNode }) {
  return (
    <div className="min-w-[min(238px,calc(100vw-34px))] px-3 py-2.5 text-center text-xs text-muted-foreground">
      {children}
    </div>
  );
}

/** The scrolling row list; rows draw their own top hairline. */
export function DockRows({ children }: { children: ReactNode }) {
  return (
    <div data-slot="dock-rows" className="max-h-[min(46vh,340px)] overflow-y-auto overflow-x-hidden scrollbar-auto-hide divide-y divide-border">
      {children}
    </div>
  );
}

/** Footer for bulk actions (Clear / Cancel queued), right-aligned under the list. */
export function DockFooter({ children }: { children: ReactNode }) {
  return <div className="flex items-center justify-end gap-2 border-t border-border px-2.5 py-1.5">{children}</div>;
}

/** A labelled group inside one panel (Running / Background tasks). */
export function DockSection({ heading, children }: { heading?: string; children: ReactNode }) {
  return (
    <div className="border-t border-border first:border-t-0">
      {heading && (
        <div className="px-2.5 pt-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{heading}</div>
      )}
      {children}
    </div>
  );
}

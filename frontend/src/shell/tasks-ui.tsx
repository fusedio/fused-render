// The Tasks page's local composites — the marks the List, the Board and the
// Calendar all wear, built once here out of the flow composites and the shadcn
// primitives so the three views cannot drift apart. Domain-flavoured (a
// BoardColumn, a task id, a folder chip), which is why they live beside the
// views rather than in platform/ui/flow.
import { useState } from "react";
import type { ComponentProps, ReactNode } from "react";
import { Check } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { StatusIcon as FlowStatusIcon } from "@platform/ui/flow/StatusIcon";
import { Identifier, Muted } from "@platform/ui/flow/Typography";
import { bucketOf } from "@platform/ui/status-colors";
import type { StatusBucket } from "@platform/ui/status-colors";
import { BOARD_COLUMNS } from "./schedule-lib";
import type { BoardColumn } from "./schedule-lib";
import { UNREAD_LABEL, taskUnreadLabel } from "./tasks-lib";

const STATUS_LABELS: Record<BoardColumn, string> = Object.fromEntries(
  BOARD_COLUMNS.map((c) => [c.key, c.label]),
) as Record<BoardColumn, string>;

/** A task's lifecycle column → the status-colors bucket. `failed` repaints red
 *  without moving the task out of its column. The map is status-colors.ts's;
 *  this only chooses which key to ask it with. */
export function columnBucket(status: BoardColumn, failed?: boolean): StatusBucket {
  return failed ? "red" : bucketOf(status);
}

/**
 * The ring — the ONE mark a unit of work wears on all three views. Hue is the
 * status (red when failed), shape is the read-state: filled while something in
 * here is unread, hollow once it has been looked at. `count` is what the fill
 * stands for on a CONTAINER (a task row over its thread, a lane header over
 * its cards) and becomes the tooltip; a leaf passes `unread` alone. `pulse`
 * marks a turn in flight.
 */
export function StatusIcon({
  status,
  failed,
  label,
  unread,
  count,
  pulse,
  className,
}: {
  status: BoardColumn;
  failed?: boolean;
  label?: string;
  unread?: boolean;
  count?: number;
  pulse?: boolean;
  className?: string;
}) {
  const text = label ?? (failed ? "Failed" : (STATUS_LABELS[status] ?? status));
  const many = taskUnreadLabel(count ?? 0);
  const said = many ?? (unread ? UNREAD_LABEL.toLowerCase() : null);
  return (
    <span className="schedule-ring inline-flex shrink-0" title={many ?? text}>
      <FlowStatusIcon
        bucket={columnBucket(status, failed)}
        filled={unread}
        pulse={pulse}
        label={said ? `${text}, ${said}` : text}
        className={className}
      />
    </span>
  );
}

/** TASK-002 / MSG-003 — designed identifiers, read and searched for, so mono. */
export function IdChip({ id, className }: { id: string; className?: string }) {
  return <Identifier className={cn("shrink-0 whitespace-nowrap", className)}>{id}</Identifier>;
}

/** The one correction a lane cannot say about a run ("Stopped"). A bordered
 *  pill so it reads as its own object beside whatever else is on the line. */
export function OutcomePill({ text, title }: { text: string; title: string }) {
  return (
    <Badge variant="outline" className="h-[18px] px-1.5 text-[11px] font-medium shrink-0" title={title}>
      {text}
    </Badge>
  );
}

/**
 * The folder a task's work happens in — the folder's own name, the full path
 * as the tooltip. With `onPick` it is a TAG: pressed, it filters the page to
 * this folder (and says so while it is on); pressed again, it lets go.
 * `stopPropagation` so a press never counts as a press on the row it sits in.
 */
export function FolderChip({
  name,
  title,
  onPick,
  active = false,
}: {
  name: string;
  title?: string;
  onPick?: () => void;
  active?: boolean;
}) {
  if (!name) return null;
  const cls = "h-5 max-w-[180px] px-2 text-xs font-normal shrink-0";
  if (!onPick) {
    return (
      <Badge variant="secondary" className={cls} data-hint={title || name}>
        <span className="truncate">{name}</span>
      </Badge>
    );
  }
  return (
    // The band around the tag says nothing (`data-hint=""` stops hints.ts
    // walking up to the row's caption) and swallows the row's click.
    <span className="relative z-10 inline-flex items-center" data-hint="" onClick={(e) => e.stopPropagation()}>
      <Badge
        variant={active ? "default" : "secondary"}
        className={cn(cls, "cursor-pointer hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50", active && "hover:bg-primary/80")}
        render={<button type="button" />}
        data-hint={active ? `Showing only ${title || name} — press to clear` : `Show only ${title || name}`}
        aria-label={active ? `${name} — showing only this folder, press to clear` : `Show only ${name}`}
        aria-pressed={active}
        onClick={(e: React.MouseEvent) => {
          e.stopPropagation();
          onPick();
        }}
      >
        <span className="truncate">{name}</span>
      </Badge>
    </span>
  );
}

/**
 * The entity-row vocabulary (flow EntityRow) as a plain div that accepts every
 * div prop. The task row hosts NESTED controls — a disclosure caret, an
 * archive button in the mark slot, a folder-chip button, a stretched link —
 * and wears role/tabIndex/keydown itself on the edit arm, which the
 * button/anchor forms of EntityRow cannot carry. Same classes, same slots.
 */
export function RowFrame({ className, selected, interactive, ...props }: ComponentProps<"div"> & { selected?: boolean; interactive?: boolean }) {
  return (
    <div
      data-slot="entity-row"
      className={cn(
        "relative flex items-center gap-3 px-4 py-2 text-sm border-b border-border last:border-b-0 min-w-0 w-full",
        interactive && "cursor-pointer hover:bg-accent/50 focus-visible:outline-none focus-visible:bg-accent/50",
        selected && "bg-accent/30",
        className,
      )}
      {...props}
    />
  );
}

/** The stretched link a row draws over itself: it carries the href, the tab
 *  stop and the accessible name; the row's children carry every pixel of ink.
 *  Modified presses (⌘, middle) are the browser's own — see the callers. */
export function RowLink({ className, ...props }: ComponentProps<"a">) {
  return <a className={cn("tasks-rowlink absolute inset-0 z-0 focus-visible:outline-none focus-visible:bg-accent/50", className)} {...props} />;
}

/** A quiet sentence under a row or above a board: the server's own words for
 *  a refusal, or where an unarchive went. */
export function Note({ className, ...props }: ComponentProps<"p">) {
  return <Muted className={cn("text-xs px-1", className)} {...props} />;
}

/**
 * A hover-revealed row action. Opacity, not display: the button stays in the
 * tab order and lights up for a keyboard that lands on it. The reveal keys off
 * the nearest `group/row`; `data-refiled` on that row hands the slot back to
 * the ring after a filing press (the receipt), until the pointer leaves.
 */
export function RowAction({ className, ...props }: ComponentProps<typeof Button>) {
  return (
    <Button
      variant="ghost"
      size="icon-xs"
      className={cn(
        "tasks-act relative z-10 text-muted-foreground hover:text-foreground opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100 group-data-[refiled]/row:opacity-0 motion-safe:transition-opacity",
        className,
      )}
      {...props}
    />
  );
}

/**
 * The card a Board lane holds. A `<button>` wearing the Card primitive's look
 * (square corners, card ground, hairline ring) because a card IS the press
 * that opens its thread and is the thing that lifts on drag — a div Card
 * cannot be either.
 */
export function CardFrame({ className, ...props }: ComponentProps<"button">) {
  return (
    <button
      type="button"
      data-slot="card"
      className={cn(
        "flex w-full flex-col gap-1.5 rounded-lg bg-card p-3 text-left text-sm text-card-foreground ring-1 ring-foreground/10 shadow-sm",
        "hover:bg-accent/30 focus-visible:outline-none focus-visible:ring-ring/50 focus-visible:ring-[3px]",
        className,
      )}
      {...props}
    />
  );
}

/**
 * A filter menu: a small outline trigger carrying its count, a popover of
 * check rows, and an attached ✕ that drops THIS menu's selections when there
 * are any. `children` gets `close` so a row can shut the menu if it wants to.
 */
export function FilterMenu({
  label,
  count,
  icon,
  onClear,
  children,
}: {
  label: string;
  count: number;
  icon?: ReactNode;
  onClear?: () => void;
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const splittable = !!onClear && count > 0;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <span className="inline-flex items-center [&>*:first-child]:rounded-r-none [&>*:last-child]:rounded-l-none [&>*:only-child]:rounded-md">
        <PopoverTrigger
          render={
            <Button variant="outline" size="sm" aria-expanded={open} className={cn("gap-1.5", splittable && "border-r-0")} />
          }
        >
          {icon}
          {label}
          {count > 0 && (
            <Badge variant="default" className="h-4 min-w-4 px-1 text-[10px] tabular-nums">
              {count}
            </Badge>
          )}
        </PopoverTrigger>
        {splittable && (
          <Button
            variant="outline"
            size="icon-sm"
            title={`Clear the ${label.toLowerCase()} filter`}
            aria-label={`Clear the ${label.toLowerCase()} filter`}
            onClick={onClear}
          >
            ✕
          </Button>
        )}
      </span>
      <PopoverContent align="start" className="w-48 max-h-80 overflow-y-auto scrollbar-auto-hide p-1 gap-0" role="group" aria-label={`Filter by ${label}`}>
        {children(() => setOpen(false))}
      </PopoverContent>
    </Popover>
  );
}

/** One row inside a FilterMenu: a check slot, a mark, a label. */
export function FilterItem({ on, children, className, ...props }: ComponentProps<"button"> & { on: boolean }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent focus-visible:outline-none focus-visible:bg-accent min-w-0",
        className,
      )}
      {...props}
    >
      <span className="flex size-3.5 shrink-0 items-center justify-center text-foreground" aria-hidden>
        {on ? <Check className="size-3.5" /> : null}
      </span>
      {children}
    </button>
  );
}

/** The `chart-1..5` token a folder's hashed colour maps onto — categorical,
 *  never a status colour. schedule-lib.taskColour hands back 0..7. */
export function folderChartVar(colour: number): string {
  return `var(--color-chart-${(Math.abs(colour) % 5) + 1})`;
}

// Google-style month grid, dropped from the date chip on the when-row and from
// the recurrence panel's end-date chip. A dumb view: it hands back a Date and
// keeps only which month is being LOOKED AT, which is not the month selected —
// paging through months must not move the selection.
import { useState } from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { cn } from "@platform/lib/utils";
import { MONTHS } from "./when-lib";

export function MiniCalendar({
  selected,
  onPick,
  minDate,
}: {
  selected: Date;
  onPick: (d: Date) => void;
  // A hard floor, and the ONLY one left: the recurrence section's end date,
  // which cannot precede the anchor it ends. The when-row's grid no longer
  // floors at today — scheduling into the past is a legitimate way to say
  // "run this as soon as you can" (design §9).
  minDate?: Date;
}) {
  const [view, setView] = useState(
    () => new Date(selected.getFullYear(), selected.getMonth(), 1),
  );
  const today = new Date();
  const firstDow = view.getDay();
  const daysInMonth = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();
  const cells: (number | null)[] = [
    ...Array.from({ length: firstDow }, () => null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];
  const same = (d: Date, y: number, m: number, day: number) =>
    d.getFullYear() === y && d.getMonth() === m && d.getDate() === day;

  // The earliest day this grid will hand back, as a midnight stamp; -Infinity
  // when nothing constrains it.
  const floor = minDate
    ? new Date(minDate.getFullYear(), minDate.getMonth(), minDate.getDate()).getTime()
    : -Infinity;

  return (
    <div className="w-60 p-1">
      <div className="flex items-center justify-between pb-1 pl-1.5">
        <span className="text-sm font-medium">
          {MONTHS[view.getMonth()]} {view.getFullYear()}
        </span>
        <div className="flex items-center">
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            tabIndex={-1}
            aria-label="Previous month"
            onClick={() => setView(new Date(view.getFullYear(), view.getMonth() - 1, 1))}
          >
            <ChevronLeftIcon />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            tabIndex={-1}
            aria-label="Next month"
            onClick={() => setView(new Date(view.getFullYear(), view.getMonth() + 1, 1))}
          >
            <ChevronRightIcon />
          </Button>
        </div>
      </div>
      <div className="grid grid-cols-7 gap-px">
        {["S", "M", "T", "W", "T", "F", "S"].map((d, i) => (
          <span
            key={i}
            className="flex h-6 items-center justify-center text-xs text-muted-foreground"
          >
            {d}
          </span>
        ))}
        {cells.map((day, i) => {
          if (day === null) return <span key={`b${i}`} />;
          const d = new Date(view.getFullYear(), view.getMonth(), day);
          const isSelected = same(selected, view.getFullYear(), view.getMonth(), day);
          const isToday = same(today, view.getFullYear(), view.getMonth(), day);
          return (
            <button
              key={day}
              type="button"
              // The grid is one control reached from its chip, not 31 tab
              // stops in the middle of the form.
              tabIndex={-1}
              disabled={d.getTime() < floor}
              aria-pressed={isSelected}
              className={cn(
                "flex h-7 items-center justify-center rounded-md text-sm tabular-nums outline-none transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-40",
                isToday && !isSelected && "font-semibold underline decoration-2 underline-offset-2",
                isSelected && "bg-primary text-primary-foreground hover:bg-primary/90",
              )}
              onClick={() => onPick(d)}
            >
              {day}
            </button>
          );
        })}
      </div>
    </div>
  );
}

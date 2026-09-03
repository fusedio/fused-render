// Google's when-row: a date chip that drops a month grid, a time field that
// drops a 15-minute list, and the Repeat tick beside them. Both controls are
// dumb views over the card's single `when` string — the parent owns it and
// hands down the parts; this row owns only the time field's DRAFT text
// (editable like Google's: type "8:30pm" or pick from the list; an unparseable
// draft falls back to what the field had).
import { useEffect, useRef, useState } from "react";
import { ClockIcon } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { cn } from "@platform/lib/utils";
import { AnchoredPopup } from "./AnchoredPopup";
import { CheckRow } from "./CheckRow";
import { MiniCalendar } from "./MiniCalendar";
import { fmtTime, parseTime } from "./when-lib";
import { IconRow } from "./IconRow";

function TimeList({
  selected,
  onPick,
}: {
  selected: { h: number; m: number };
  onPick: (h: number, m: number) => void;
}) {
  // The NEAREST slot carries the highlight — a typed 10:19pm is not on the
  // 15-minute grid, and matching exactly left the list unmarked and parked at
  // midnight (QA 2026-08-15). Scrolled by container arithmetic, not
  // scrollIntoView: the latter also scrolls the dialog behind the dropdown.
  const ref = useRef<HTMLDivElement>(null);
  const nearest = Math.min(95, Math.round((selected.h * 60 + selected.m) / 15));
  useEffect(() => {
    const list = ref.current;
    const hit = list?.querySelector<HTMLElement>("[aria-selected=true]");
    if (list && hit) {
      list.scrollTop = hit.offsetTop - list.clientHeight / 2 + hit.offsetHeight / 2;
    }
  }, []);
  const slots = Array.from({ length: 96 }, (_, i) => ({
    h: Math.floor(i / 4),
    m: (i % 4) * 15,
  }));
  return (
    // A listbox, said out loud: 96 slots that announced themselves as plain
    // buttons left a screen reader no way to know one of them was the current
    // time, and Tab walked all 96 (audit 2026-08-16).
    <div
      className="relative max-h-52 w-28 overflow-y-auto"
      ref={ref}
      role="listbox"
      aria-label="Time"
    >
      {slots.map(({ h, m }, i) => (
        <button
          key={`${h}:${m}`}
          type="button"
          role="option"
          tabIndex={-1}
          aria-selected={i === nearest}
          className={cn(
            "flex w-full items-center rounded-md px-2 py-1 text-sm tabular-nums outline-none hover:bg-accent",
            i === nearest && "bg-accent font-medium",
          )}
          onClick={() => onPick(h, m)}
        >
          {fmtTime(h, m)}
        </button>
      ))}
    </div>
  );
}

export function WhenRow({
  picked,
  pickedOk,
  dateLabel,
  describedBy,
  onDate,
  onTime,
  repeatOn,
  onRepeat,
}: {
  picked: Date;
  pickedOk: boolean;
  dateLabel: string;
  // The past-time note's id, when there is one to point at.
  describedBy?: string;
  onDate: (d: Date) => void;
  onTime: (h: number, m: number) => void;
  repeatOn: boolean;
  onRepeat: (on: boolean) => void;
}) {
  const [dateOpen, setDateOpen] = useState(false);
  const [timeOpen, setTimeOpen] = useState(false);
  const timeRef = useRef<HTMLInputElement>(null);
  const [timeText, setTimeText] = useState(() =>
    fmtTime(pickedOk ? picked.getHours() : 0, pickedOk ? picked.getMinutes() : 0),
  );
  // The field follows the value whenever the parent moves it (a slot from the
  // list, an Edit's stored time) — but never mid-edit, while the list is up.
  useEffect(() => {
    if (!timeOpen && pickedOk) setTimeText(fmtTime(picked.getHours(), picked.getMinutes()));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [picked]);

  const pickTime = (h: number, m: number) => {
    onTime(h, m);
    setTimeText(fmtTime(h, m));
  };
  const commitTimeText = () => {
    const parsed = parseTime(timeText);
    if (parsed) pickTime(parsed.h, parsed.m);
    else if (pickedOk) setTimeText(fmtTime(picked.getHours(), picked.getMinutes()));
  };

  return (
    <IconRow icon={<ClockIcon />}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Popover
          open={dateOpen}
          onOpenChange={(o) => {
            setDateOpen(o);
            if (o) setTimeOpen(false);
          }}
        >
          <PopoverTrigger
            render={<Button type="button" variant="outline" size="sm" />}
            aria-describedby={describedBy}
          >
            {dateLabel}
          </PopoverTrigger>
          <PopoverContent align="start" className="w-auto p-1">
            {/* No floor at all now. A past day is a one-off saying "run this
                as soon as you can" (design §9), and for a rule it is legitimate
                too — it says "start this pattern, and run the one I missed". */}
            <MiniCalendar
              selected={pickedOk ? picked : new Date()}
              onPick={(d) => {
                onDate(d);
                setDateOpen(false);
              }}
            />
          </PopoverContent>
        </Popover>
        <div
          onBlur={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setTimeOpen(false);
          }}
        >
          <Input
            ref={timeRef}
            type="text"
            className="h-7 w-24 text-[0.8rem] tabular-nums"
            aria-describedby={describedBy}
            aria-expanded={timeOpen}
            aria-label="Time"
            value={timeText}
            onFocus={(e) => {
              setTimeOpen(true);
              setDateOpen(false);
              e.target.select();
            }}
            onChange={(e) => setTimeText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                commitTimeText();
                setTimeOpen(false);
              }
              // Escape dismisses the LIST, not the dialog around it.
              if (e.key === "Escape" && timeOpen) {
                e.stopPropagation();
                setTimeOpen(false);
              }
            }}
            onBlur={commitTimeText}
          />
          <AnchoredPopup open={timeOpen} onClose={() => setTimeOpen(false)} anchor={timeRef}>
            <TimeList
              selected={{
                h: pickedOk ? picked.getHours() : 9,
                m: pickedOk ? picked.getMinutes() : 0,
              }}
              onPick={(h, m) => {
                pickTime(h, m);
                setTimeOpen(false);
              }}
            />
          </AnchoredPopup>
        </div>
        {/* Repeat is a tick on the when-row, not a dropdown that is always
            open (design §6): most tasks run once, and the menu they never use
            was the loudest thing under the time. Unticking clears the rule
            outright — see toggleRepeat in NewJobModal. */}
        <CheckRow className="ml-1" label="Repeat" checked={repeatOn} onChange={onRepeat} />
      </div>
    </IconRow>
  );
}

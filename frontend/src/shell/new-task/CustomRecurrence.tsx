// Custom recurrence (Google's dialog, copied deliberately): Repeat every [n]
// [unit], weekday circles for weeks, Ends never/on/after. A SECTION in the
// dialog's side column, not a dialog of its own: it has no scrim, no focus trap
// and the card beside it stays live.
import { useEffect, useRef, useState } from "react";
import type { RecurrenceRule } from "@platform/lib/api";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@platform/shadcn/ui/popover";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { ChoiceSelect } from "./ChoiceSelect";
import { MiniCalendar } from "./MiniCalendar";
import { DAYS, MONTHS } from "./when-lib";

const ENDS_CHOICES = [
  { key: "never", label: "Never" },
  { key: "on", label: "On" },
  { key: "after", label: "After" },
] as const;
type Ends = (typeof ENDS_CHOICES)[number]["key"];

// A date the recurrence section stores as "YYYY-MM-DD", both ways. Parsed by
// hand rather than through `new Date(ymd)` — that reads a bare date string as
// UTC and lands a day early west of Greenwich.
const ymdOf = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

function untilDate(ymd: string): Date | null {
  const [y, m, d] = ymd.split("-").map(Number);
  return y && m && d ? new Date(y, m - 1, d) : null;
}

function untilLabel(ymd: string): string {
  const d = untilDate(ymd);
  return d ? `${MONTHS[d.getMonth()].slice(0, 3)} ${d.getDate()}, ${d.getFullYear()}` : "Pick a date";
}

const NTH_LABELS = ["first", "second", "third", "fourth", "fifth"];

// The panel's "repeat every N ___" units. Shortest first, the order
// recur.FREQUENCIES is written in — and without `year`, which is off the menu
// for anything new (see `units` below for the one exception).
const RECUR_UNITS: readonly RecurrenceRule["freq"][] = ["hour", "day", "week", "month"];
const LEGACY_RECUR_UNITS: readonly RecurrenceRule["freq"][] = [...RECUR_UNITS, "year"];

export function CustomRecurrence({
  initial,
  anchor,
  onDone,
  onCancel,
}: {
  initial: RecurrenceRule | null;
  anchor: Date;
  onDone: (rule: RecurrenceRule) => void;
  onCancel: () => void;
}) {
  // Escape dismisses the PANEL (as Cancel), captured before the dialog's own
  // document-level Escape — same contract as the explorer beside it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopImmediatePropagation();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [freq, setFreq] = useState<RecurrenceRule["freq"]>(initial?.freq ?? "week");
  // `year` is not a unit any more (Akshil, 2026-08-17), with one exception: a
  // rule that is ALREADY yearly keeps the row while it is being edited — a unit
  // select that could not say "year" would show a value with no matching row
  // and turn any other edit into a silent change of frequency. Read off
  // `initial` so the row does not vanish mid-edit.
  const units = initial?.freq === "year" ? LEGACY_RECUR_UNITS : RECUR_UNITS;
  const [interval, setIntervalN] = useState(initial?.interval ?? 1);
  const [byday, setByday] = useState<number[]>(
    initial?.byday?.length ? initial.byday : [anchor.getDay()],
  );
  const [monthly, setMonthly] = useState<"day" | "nth-weekday">(initial?.monthly ?? "day");
  const [ends, setEnds] = useState<Ends>(
    initial?.until ? "on" : initial?.count ? "after" : "never",
  );
  const [until, setUntil] = useState(initial?.until ?? "");
  const [count, setCount] = useState(initial?.count ?? 13);
  const [untilOpen, setUntilOpen] = useState(false);
  // The section opens on the question it is asking: how often. Without this
  // the reveal landed focus nowhere and a keyboard user had to Tab in from the
  // repeat menu they had just left (audit 2026-08-16).
  const intervalRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    intervalRef.current?.focus();
    intervalRef.current?.select();
  }, []);

  const done = () => {
    const rule: RecurrenceRule = { freq };
    if (interval > 1) rule.interval = interval;
    if (freq === "week") rule.byday = byday;
    if (freq === "month") rule.monthly = monthly;
    if (ends === "on" && until) rule.until = until;
    if (ends === "after") rule.count = count;
    onDone(rule);
  };

  const nth = NTH_LABELS[Math.floor((anchor.getDate() - 1) / 7)];

  return (
    <section className="flex min-h-0 flex-1 flex-col" aria-label="Custom recurrence">
      <div className="border-b border-border px-4 py-2.5 text-sm font-medium">
        Custom recurrence
      </div>
      <div className="flex flex-1 flex-col gap-3 px-4 py-3 text-sm">
        <div className="flex items-center gap-2">
          <span>Repeat every</span>
          <Input
            ref={intervalRef}
            type="number"
            min={1}
            max={99}
            className="h-7 w-16 text-[0.8rem]"
            aria-label="Repeat interval"
            value={interval}
            onChange={(e) => setIntervalN(Math.max(1, Math.min(99, Number(e.target.value) || 1)))}
          />
          <ChoiceSelect
            ariaLabel="Repeat unit"
            value={freq}
            options={units.map((u) => ({ key: u, label: interval > 1 ? `${u}s` : u }))}
            onPick={(u) => setFreq(u as RecurrenceRule["freq"])}
          />
        </div>

        {freq === "week" && (
          <div className="flex items-center gap-2">
            <span>Repeat on</span>
            <ToggleGroup
              multiple
              value={byday.map(String)}
              aria-label="Repeat on"
              spacing={1}
              onValueChange={(next) => {
                // Never empty: a weekly rule with no days is a rule that never
                // fires.
                if (next.length === 0) return;
                setByday(next.map(Number).sort((a, b) => a - b));
              }}
            >
              {["S", "M", "T", "W", "T", "F", "S"].map((label, d) => (
                <ToggleGroupItem
                  key={d}
                  value={String(d)}
                  size="sm"
                  aria-label={DAYS[d]}
                  className="size-7 min-w-7 rounded-full px-0 text-xs data-pressed:bg-primary data-pressed:text-primary-foreground"
                >
                  {label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </div>
        )}

        {freq === "month" && (
          <ChoiceSelect
            ariaLabel="Monthly on"
            value={monthly}
            options={[
              { key: "day", label: `Monthly on day ${anchor.getDate()}` },
              { key: "nth-weekday", label: `Monthly on the ${nth} ${DAYS[anchor.getDay()]}` },
            ]}
            onPick={(v) => setMonthly(v as "day" | "nth-weekday")}
          />
        )}

        {/* Three mutually exclusive answers to one question = a segmented
            control. Only the chosen branch's field is rendered — a greyed-out
            control is a question you cannot answer. */}
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <span>Ends</span>
            <ToggleGroup
              value={[ends]}
              aria-label="Ends"
              variant="outline"
              spacing={0}
              onValueChange={(next) => {
                const pick = next[0];
                // A single-select group can deselect to nothing; "Ends" always
                // has an answer.
                if (pick === "never" || pick === "on" || pick === "after") setEnds(pick);
              }}
            >
              {ENDS_CHOICES.map(({ key, label }) => (
                <ToggleGroupItem
                  key={key}
                  value={key}
                  size="sm"
                  className="data-pressed:bg-accent"
                >
                  {label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </div>

          {ends === "on" && (
            <Popover open={untilOpen} onOpenChange={setUntilOpen}>
              <PopoverTrigger
                render={<Button type="button" variant="outline" size="sm" className="w-fit" />}
                aria-label="End date"
              >
                {untilLabel(until)}
              </PopoverTrigger>
              <PopoverContent align="start" className="w-auto p-1">
                {/* An end date is bounded by its own anchor, not by today. */}
                <MiniCalendar
                  selected={untilDate(until) ?? anchor}
                  minDate={anchor}
                  onPick={(d) => {
                    setUntil(ymdOf(d));
                    setUntilOpen(false);
                  }}
                />
              </PopoverContent>
            </Popover>
          )}

          {ends === "after" && (
            <div className="flex items-center gap-2">
              <Input
                type="number"
                min={1}
                max={999}
                className="h-7 w-16 text-[0.8rem]"
                aria-label="Number of occurrences"
                value={count}
                onChange={(e) => setCount(Math.max(1, Math.min(999, Number(e.target.value) || 1)))}
              />
              <span>occurrences</span>
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center justify-end gap-2 border-t border-border px-3 py-2">
        <Button type="button" variant="outline" size="sm" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="button" size="sm" disabled={ends === "on" && !until} onClick={done}>
          Done
        </Button>
      </div>
    </section>
  );
}

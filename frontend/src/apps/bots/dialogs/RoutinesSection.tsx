// Settings › Routines (OpenBot dialogs.js routines, once its own #rmodal): the bot's routines as cards (Run, Pause or
// Enable, delete with a confirm) and the add form (Every N min / Daily at HH:MM on chosen weekdays, default Mon–Fri /
// Once at a date and time). Every op posts at once; the poll repaints. Hidden for Super Bot (the backend refuses).
// The add form enforces the backend's rules before posting (bot.py routine_add / _next_run would otherwise coerce
// silently: minutes clamp to 5, no weekdays = every day, a blank time = 09:00): a whole number of minutes, 5 or more;
// at least one weekday and a time; a future moment for a one-shot. The reason shows under the form and Add stays off.
import { useState } from "react";
import { Trash2Icon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { api, type RoutineBody } from "../lib/api";
import { fmtWhen } from "../lib/format";
import { DAYS, routineLabel } from "../lib/live";
import { act, showBanner, useBotsSelector } from "../state/store";
import { CARD_CLASS } from "./shell";
import type { SectionProps } from "./SkillsSection";

type Kind = "interval" | "daily" | "once";

export function RoutinesSection({ b, confirm }: SectionProps) {
  const tasks = useBotsSelector((s) => s.usage?.tasks);
  const [task, setTask] = useState("");
  const [kind, setKind] = useState<Kind>("interval");
  const [minutes, setMinutes] = useState("60");
  const [time, setTime] = useState("09:00");
  const [days, setDays] = useState<string[]>(["0", "1", "2", "3", "4"]);
  const [at, setAt] = useState("");
  // Mirrors ROUTINE_MIN_MINUTES / ROUTINE_MAX_FAILS in bot.py (APP_GUIDE tells the bot the same numbers).
  const MIN_MINUTES = 5, MAX_FAILS = 3;
  const invalid = ((): string | null => {
    if (!task.trim()) return null; // the empty task disables Add on its own; no reason needed
    if (kind === "interval") {
      const n = Number(minutes);
      if (!minutes.trim() || !Number.isInteger(n)) return "Minutes must be a whole number.";
      if (n < MIN_MINUTES) return `Routines run at most every ${MIN_MINUTES} minutes.`;
    }
    if (kind === "daily") {
      if (!time) return "Pick a time of day.";
      if (!days.length) return "Pick at least one weekday.";
    }
    if (kind === "once") {
      if (!at) return "Pick a date and time.";
      if (new Date(at).getTime() <= Date.now()) return "That time is in the past.";
    }
    return null;
  })();
  // datetime-local `min`: now, in local time, to the minute (the picker greys out earlier days).
  const nowLocal = ((): string => { const d = new Date(); d.setSeconds(0, 0); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); })();

  const rs = b.routines || [];
  const onOp = async (rid: string, op: "run" | "enable" | "disable" | "delete") => {
    if (op === "delete" && !(await confirm("Delete this routine?", "It stops running and is removed from the list."))) return;
    await act(() => api.routines(b.id, { op, rid }));
  };
  const add = async () => {
    const text = task.trim(); if (!text) return;
    if (invalid) { showBanner(invalid); return; }
    const p: Extract<RoutineBody, { op: "add" }> = { op: "add", text, kind };
    if (kind === "interval") p.minutes = Number(minutes);
    if (kind === "daily") { p.time = time; p.weekdays = days.map(Number).sort((x, y) => x - y); }
    if (kind === "once") p.at = new Date(at).getTime() / 1000;
    const r = await act(() => api.routines(b.id, p));
    if (r?.ok) setTask("");
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3" id="rlist">
        {rs.length ? rs.map((r) => {
          const warn = r.last_result === "error" || (r.fails || 0) > 0;
          return (
            <section key={r.id} className={cn(CARD_CLASS, !r.enabled && "opacity-60")} data-rid={r.id}>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 whitespace-pre-wrap text-[15px] font-medium">{r.task}</div>
                <div className="flex shrink-0 items-center gap-1">
                  <Button variant="outline" size="sm" data-op="run" title="Start this task now" onClick={() => { void onOp(r.id, "run"); }}>Run</Button>
                  <Button variant="outline" size="sm" data-op={r.enabled ? "disable" : "enable"} onClick={() => { void onOp(r.id, r.enabled ? "disable" : "enable"); }}>{r.enabled ? "Pause" : "Enable"}</Button>
                  <Button variant="ghost" size="icon-xs" data-op="delete" aria-label="Delete routine" title="Delete" className="text-muted-foreground hover:text-destructive" onClick={() => { void onOp(r.id, "delete"); }}><Trash2Icon /></Button>
                </div>
              </div>
              <p className="m-0 text-[13px] leading-5 text-muted-foreground">
                {routineLabel(r)} · {tasks?.[r.task] || 0} calls today · next: {r.enabled ? fmtWhen(r.next)
                  : (r.fails || 0) >= MAX_FAILS ? <span className="text-destructive">paused after {r.fails} failed runs in a row</span> : "paused"}
                {r.last ? <> · last: {fmtWhen(r.last)} <span className={warn ? "text-destructive" : undefined}>({r.last_result || ""}{(r.fails || 0) > 1 ? `, ${r.fails} in a row` : ""})</span></> : null}
                {r.last_message ? <> <span>{r.last_message}</span></> : null}
              </p>
            </section>
          );
        }) : <p className="m-0 text-sm text-muted-foreground">No routines yet. Add one below.</p>}
      </div>
      <FieldGroup className={CARD_CLASS}>
        <Field>
          <FieldLabel htmlFor="rtask">New routine</FieldLabel>
          <Textarea id="rtask" rows={2} value={task} onChange={(e) => setTask(e.target.value)} placeholder="e.g. Check my LinkedIn feed and summarise the 10 newest posts" />
        </Field>
        <div className="flex flex-wrap items-center gap-2">
          <NativeSelect id="rkind" className="w-32" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
            <NativeSelectOption value="interval">Every</NativeSelectOption>
            <NativeSelectOption value="daily">Daily at</NativeSelectOption>
            <NativeSelectOption value="once">Once at</NativeSelectOption>
          </NativeSelect>
          {kind === "interval" ? (
            <span className="flex items-center gap-2 text-sm" id="rk-interval"><Input id="rmin" type="number" min={MIN_MINUTES} step={1} value={minutes} aria-invalid={kind === "interval" && !!invalid} onChange={(e) => setMinutes(e.target.value)} className="w-20" /> min</span>
          ) : null}
          {kind === "daily" ? (
            <span className="flex flex-wrap items-center gap-2" id="rk-daily">
              <Input id="rtime" type="time" value={time} onChange={(e) => setTime(e.target.value)} className="w-32" />
              <ToggleGroup id="rdays" multiple value={days} onValueChange={(v) => setDays(v as string[])} variant="outline" size="sm" aria-label="Weekdays">
                {DAYS.map((d, i) => <ToggleGroupItem key={d} value={String(i)} aria-label={d}>{d}</ToggleGroupItem>)}
              </ToggleGroup>
            </span>
          ) : null}
          {kind === "once" ? <Input id="rat" type="datetime-local" min={nowLocal} value={at} aria-invalid={kind === "once" && !!invalid} onChange={(e) => setAt(e.target.value)} className="w-56" /> : null}
          <Button id="radd" className="ml-auto" disabled={!task.trim() || !!invalid} onClick={() => { void add(); }}>Add</Button>
        </div>
        {invalid ? <p id="rwhy" className="m-0 text-[13px] text-destructive">{invalid}</p> : null}
        <FieldDescription>Every N minutes runs at most every {MIN_MINUTES} minutes. Runs start only when the bot is idle; a busy bot skips that slot. A routine pauses itself after {MAX_FAILS} failed runs in a row; Enable starts it again. The bot may still ask you questions during a scheduled run.</FieldDescription>
      </FieldGroup>
    </div>
  );
}

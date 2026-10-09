// Settings › Routines (OpenBot dialogs.js routines, once its own #rmodal): the bot's routines as cards (Run, Pause or
// Enable, delete with a confirm) and the add form (Every N min / Daily at HH:MM on chosen weekdays, default Mon–Fri /
// Once at a date and time). Every op posts at once; the poll repaints. Hidden for Super Bot (the backend refuses).
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

  const rs = b.routines || [];
  const onOp = async (rid: string, op: "run" | "enable" | "disable" | "delete") => {
    if (op === "delete" && !(await confirm("Delete this routine?", "It stops running and is removed from the list."))) return;
    await act(() => api.routines(b.id, { op, rid }));
  };
  const add = async () => {
    const text = task.trim(); if (!text) return;
    const p: Extract<RoutineBody, { op: "add" }> = { op: "add", text, kind };
    if (kind === "interval") p.minutes = Number(minutes) || 60;
    if (kind === "daily") { p.time = time || "09:00"; p.weekdays = days.map(Number).sort((x, y) => x - y); }
    if (kind === "once") { if (!at) { showBanner("Pick a date and time"); return; } p.at = new Date(at).getTime() / 1000; }
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
                {routineLabel(r)} · {tasks?.[r.task] || 0} calls today · next: {r.enabled ? fmtWhen(r.next) : "paused"}
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
            <span className="flex items-center gap-2 text-sm" id="rk-interval"><Input id="rmin" type="number" min={5} step={5} value={minutes} onChange={(e) => setMinutes(e.target.value)} className="w-20" /> min</span>
          ) : null}
          {kind === "daily" ? (
            <span className="flex flex-wrap items-center gap-2" id="rk-daily">
              <Input id="rtime" type="time" value={time} onChange={(e) => setTime(e.target.value)} className="w-32" />
              <ToggleGroup id="rdays" multiple value={days} onValueChange={(v) => setDays(v as string[])} variant="outline" size="sm" aria-label="Weekdays">
                {DAYS.map((d, i) => <ToggleGroupItem key={d} value={String(i)} aria-label={d}>{d}</ToggleGroupItem>)}
              </ToggleGroup>
            </span>
          ) : null}
          {kind === "once" ? <Input id="rat" type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} className="w-56" /> : null}
          <Button id="radd" className="ml-auto" disabled={!task.trim()} onClick={() => { void add(); }}>Add</Button>
        </div>
        <FieldDescription>Runs start only when the bot is idle; a busy bot skips that slot. The bot may still ask you questions during a scheduled run.</FieldDescription>
      </FieldGroup>
    </div>
  );
}

// Settings › Routines (OpenBot dialogs.js routines, once its own #rmodal): the bot's routines as cards (Run, Pause or
// Enable, Edit, delete with a confirm) and the add form (Every N min / Daily at HH:MM on chosen weekdays, default
// Mon–Fri / Once at a date and time). Every op posts at once; the poll repaints. Edit swaps a card for the same form
// prefilled (Save / Cancel; `op: "edit"` keeps the id, the fails count and the run history). Super Bot has routines
// like any bot (2026-10-12): a routine is the user's own task from the Mac, so it runs with its own posture.
// The form enforces the backend's rules before posting (bot.py _routine_fields / _next_run would otherwise coerce
// silently: minutes clamp to 5, no weekdays = every day, a blank time = 09:00): a whole number of minutes, 5 or more;
// at least one weekday and a time; a future moment for a one-shot. The reason shows under the form and Add stays off.
import { useEffect, useState } from "react";
import { PencilIcon, Trash2Icon } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@platform/shadcn/ui/field";
import { Input } from "@platform/shadcn/ui/input";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { Badge } from "@platform/shadcn/ui/badge";
import { api, type Routine, type RoutineBody, type RoutineRun, type SetupReply } from "../lib/api";
import { fmtDay, fmtDur, fmtIn, fmtWhen } from "../lib/format";
import { DAYS, routineLabel } from "../lib/live";
import { ownerLine } from "../lib/setup";
import { act, closeDialog, jumpTo, showBanner, useBotsSelector } from "../state/store";
import { CARD_CLASS } from "./shell";
import type { SectionProps } from "./SkillsSection";

type Kind = Routine["kind"];
/** The schedule half of an add / edit body (everything but op and rid). */
type Schedule = Omit<Extract<RoutineBody, { op: "add" }>, "op">;

// Mirrors ROUTINE_MIN_MINUTES / ROUTINE_MAX_FAILS in bot.py (APP_GUIDE tells the bot the same numbers).
const MIN_MINUTES = 5, MAX_FAILS = 3;
/** Run rows shown per routine (bot.py keeps ROUTINE_RUNS = 10). */
const SHOW_RUNS = 5;

/** The rows the table shows: `runs` (bot.py _run_add), or one row built from the `last*` fields for a routine that
 *  fired before run history existed (2026-10-13), so an upgraded routine is not blank. */
function runRows(r: Routine): RoutineRun[] {
  if (r.runs?.length) return r.runs.slice(0, SHOW_RUNS);
  if (!r.last) return [];
  const skipped = (r.last_result || "").startsWith("skipped");
  return [{ started: r.last, result: skipped ? "skipped" : r.last_result || "done",
    message: skipped ? (r.last_result || "").replace(/^skipped:\s*/, "") : r.last_message, done: r.last_done }];
}

const RESULT_LABEL: Record<string, string> = { started: "running", done: "done", error: "error", stopped: "stopped", skipped: "skipped" };

/** A run's outcome pill: red for an error, quiet for a skip or a stop, filled while it runs. */
function ResultBadge({ run }: { run: RoutineRun }) {
  const v = run.result === "error" ? "destructive" : run.result === "done" ? "secondary" : run.result === "started" ? "default" : "outline";
  return <Badge variant={v} className="h-[18px] px-1.5 text-[11px]">{RESULT_LABEL[run.result] || run.result}{(run.count || 1) > 1 ? ` ×${run.count}` : ""}</Badge>;
}

/** The last runs of one routine (owner, 2026-10-13: "when they ran, what they did, if error then surface"): when it
 *  started, how long it took, the outcome and the bot's closing message (the error text for a failed run, why for a
 *  skip). A row that wrote a transcript line opens the chat there: the dialog closes first (Routines posts every op
 *  at once, so nothing on this tab is unsaved; a General-tab edit left behind would be dropped). */
function RunsTable({ botId, r }: { botId: string; r: Routine }) {
  const rows = runRows(r);
  if (!rows.length) return <p className="m-0 text-[13px] text-muted-foreground">No runs yet.</p>;
  const open = (run: RoutineRun) => { if (run.seq) { closeDialog(); jumpTo(botId, run.seq); } };
  return (
    <table className="w-full border-collapse text-[13px] leading-5" data-runs={r.id}>
      <thead>
        <tr className="text-left text-[11px] uppercase tracking-wide text-muted-foreground">
          <th className="py-1 pr-3 font-medium">When</th>
          <th className="py-1 pr-3 font-medium">Took</th>
          <th className="py-1 pr-3 font-medium">Result</th>
          <th className="py-1 font-medium">What it did</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((run, i) => {
          const err = run.result === "error";
          // No length for a stop: the owner's reconcile walk stamps "stopped: the app quit" at the next launch, hours later.
          const took = run.done && (run.result === "done" || run.result === "error") ? fmtDur(run.done - run.started) : run.result === "started" ? "…" : "";
          return (
            <tr key={`${run.started}-${i}`} data-result={run.result} role={run.seq ? "button" : undefined} tabIndex={run.seq ? 0 : undefined}
              title={run.seq ? "Open the chat at this run" : undefined}
              className={cn("border-t border-foreground/10 align-top", run.seq && "cursor-pointer hover:bg-foreground/[0.04] focus-visible:bg-foreground/[0.04] focus-visible:outline-2 focus-visible:outline-ring")}
              onClick={() => open(run)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(run); } }}>
              <td className="whitespace-nowrap py-1.5 pr-3 tabular-nums text-muted-foreground" title={fmtWhen(run.started)}>{fmtDay(run.started)}{run.manual ? <span title="Run now"> ·&nbsp;manual</span> : null}</td>
              <td className="whitespace-nowrap py-1.5 pr-3 tabular-nums text-muted-foreground">{took || "—"}</td>
              <td className="whitespace-nowrap py-1.5 pr-3"><ResultBadge run={run} /></td>
              <td className={cn("min-w-0 py-1.5", err ? "text-destructive" : "text-foreground/85")}>
                <span className="line-clamp-2 break-words" title={run.message || ""}>{run.message || (run.result === "started" ? "Running now…" : "")}</span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** Epoch seconds → the datetime-local value (local time, to the minute); the inverse of `new Date(value)`. */
function toLocal(ts: number): string {
  const d = new Date(ts * 1000); d.setSeconds(0, 0);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

/** The task + schedule form: blank for Add, prefilled from `initial` for Edit (then with a Cancel). `onSubmit`
 *  gets the checked body; it answers true when the save went through, which clears a blank form. */
function RoutineForm({ initial, onSubmit, onCancel }: { initial?: Routine; onSubmit: (p: Schedule) => Promise<boolean>; onCancel?: () => void }) {
  const [task, setTask] = useState(initial?.task || "");
  const [kind, setKind] = useState<Kind>(initial?.kind || "interval");
  const [minutes, setMinutes] = useState(String(initial?.minutes || 60));
  const [time, setTime] = useState(initial?.time || "09:00");
  const [days, setDays] = useState<string[]>((initial?.weekdays || [0, 1, 2, 3, 4]).map(String));
  const [at, setAt] = useState(initial?.at ? toLocal(initial.at) : "");
  const [pending, setPending] = useState(false);  // one post at a time: a second Add before the first answers would make a twin
  const invalid = ((): string | null => {
    if (!task.trim()) return null; // the empty task disables the button on its own; no reason needed
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
  const nowLocal = toLocal(Date.now() / 1000);
  const submit = async () => {
    const text = task.trim(); if (!text || pending) return;
    if (invalid) { showBanner(invalid); return; }
    const p: Schedule = { text, kind };
    if (kind === "interval") p.minutes = Number(minutes);
    if (kind === "daily") { p.time = time; p.weekdays = days.map(Number).sort((x, y) => x - y); }
    if (kind === "once") p.at = new Date(at).getTime() / 1000;
    setPending(true);
    try {
      if (await onSubmit(p) && !initial) setTask("");
    } finally {
      setPending(false);
    }
  };
  const sfx = initial ? `-${initial.id}` : "";
  return (
    <FieldGroup className={initial ? undefined : CARD_CLASS}>
      <Field>
        <FieldLabel htmlFor={`rtask${sfx}`}>{initial ? "Edit routine" : "New routine"}</FieldLabel>
        <Textarea id={`rtask${sfx}`} rows={2} value={task} onChange={(e) => setTask(e.target.value)} placeholder="e.g. Check my LinkedIn feed and summarise the 10 newest posts" />
      </Field>
      <div className="flex flex-wrap items-center gap-2">
        <NativeSelect id={`rkind${sfx}`} className="w-32" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
          <NativeSelectOption value="interval">Every</NativeSelectOption>
          <NativeSelectOption value="daily">Daily at</NativeSelectOption>
          <NativeSelectOption value="once">Once at</NativeSelectOption>
        </NativeSelect>
        {kind === "interval" ? (
          <span className="flex items-center gap-2 text-sm" id={`rk-interval${sfx}`}><Input id={`rmin${sfx}`} type="number" min={MIN_MINUTES} step={1} value={minutes} aria-invalid={kind === "interval" && !!invalid} onChange={(e) => setMinutes(e.target.value)} className="w-20" /> min</span>
        ) : null}
        {kind === "daily" ? (
          <span className="flex flex-wrap items-center gap-2" id={`rk-daily${sfx}`}>
            <Input id={`rtime${sfx}`} type="time" value={time} onChange={(e) => setTime(e.target.value)} className="w-32" />
            <ToggleGroup id={`rdays${sfx}`} multiple value={days} onValueChange={(v) => setDays(v as string[])} variant="outline" size="sm" aria-label="Weekdays">
              {DAYS.map((d, i) => <ToggleGroupItem key={d} value={String(i)} aria-label={d}>{d}</ToggleGroupItem>)}
            </ToggleGroup>
          </span>
        ) : null}
        {kind === "once" ? <Input id={`rat${sfx}`} type="datetime-local" min={nowLocal} value={at} aria-invalid={kind === "once" && !!invalid} onChange={(e) => setAt(e.target.value)} className="w-56" /> : null}
        <span className="ml-auto flex items-center gap-2">
          {onCancel ? <Button variant="ghost" data-op="cancel" onClick={onCancel}>Cancel</Button> : null}
          <Button id={initial ? `rsave${sfx}` : "radd"} data-op={initial ? "save" : "add"} disabled={!task.trim() || !!invalid || pending} onClick={() => { void submit(); }}>{initial ? "Save" : "Add"}</Button>
        </span>
      </div>
      {invalid ? <p id={`rwhy${sfx}`} className="m-0 text-[13px] text-destructive">{invalid}</p> : null}
      {initial ? null : <FieldDescription>Every N minutes runs at most every {MIN_MINUTES} minutes. Runs start only when the bot is idle; a busy bot skips that slot. A routine pauses itself after {MAX_FAILS} failed runs in a row; Enable starts it again. The bot may still ask you questions during a scheduled run.</FieldDescription>}
    </FieldGroup>
  );
}

export function RoutinesSection({ b, confirm }: SectionProps) {
  const tasks = useBotsSelector((s) => s.usage?.tasks);
  const [editing, setEditing] = useState<string | null>(null);  // the routine's id while its card is the edit form
  // Who ticks these (one owner process per Mac, bots/registry.py): a routine added here runs in the owner, and a
  // card that says "next 10:00" while nothing runs the bots was the "never fired" report this line answers.
  const [setup, setSetup] = useState<SetupReply | null>(null);
  useEffect(() => {
    let live = true;
    api.setup().then((r) => { if (live) setSetup(r); }, () => { /* no check, no line */ });
    return () => { live = false; };
  }, []);
  const owner = ownerLine(setup);

  const rs = b.routines || [];
  const onOp = async (rid: string, op: "run" | "enable" | "disable" | "delete") => {
    if (op === "delete" && !(await confirm("Delete this routine?", "It stops running and is removed from the list."))) return;
    await act(() => api.routines(b.id, { op, rid }));
  };
  const add = async (p: Schedule) => !!(await act(() => api.routines(b.id, { op: "add", ...p })))?.ok;
  const save = async (rid: string, p: Schedule) => {
    const r = await act(() => api.routines(b.id, { op: "edit", rid, ...p }));
    if (r?.ok) setEditing(null);
    return !!r?.ok;
  };

  return (
    <div className="flex flex-col gap-4">
      {owner ? <p id="rowner" className={cn("m-0 text-[13px] leading-5", owner.warn ? "text-destructive" : "text-muted-foreground")}>{owner.text}</p> : null}
      <div className="flex flex-col gap-3" id="rlist">
        {rs.length ? rs.map((r) => {
          const warn = r.last_result === "error" || (r.fails || 0) > 0;
          if (editing === r.id) {
            return (
              <section key={r.id} className={CARD_CLASS} data-rid={r.id} data-editing="">
                <RoutineForm initial={r} onSubmit={(p) => save(r.id, p)} onCancel={() => setEditing(null)} />
              </section>
            );
          }
          return (
            <section key={r.id} className={cn(CARD_CLASS, !r.enabled && "opacity-60")} data-rid={r.id}>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 whitespace-pre-wrap text-[15px] font-medium">{r.task}</div>
                <div className="flex shrink-0 items-center gap-1">
                  <Button variant="outline" size="sm" data-op="run" title="Start this task now" onClick={() => { void onOp(r.id, "run"); }}>Run</Button>
                  <Button variant="outline" size="sm" data-op={r.enabled ? "disable" : "enable"} onClick={() => { void onOp(r.id, r.enabled ? "disable" : "enable"); }}>{r.enabled ? "Pause" : "Enable"}</Button>
                  <Button variant="ghost" size="icon-xs" data-op="edit" aria-label="Edit routine" title="Edit" className="text-muted-foreground" onClick={() => setEditing(r.id)}><PencilIcon /></Button>
                  <Button variant="ghost" size="icon-xs" data-op="delete" aria-label="Delete routine" title="Delete" className="text-muted-foreground hover:text-destructive" onClick={() => { void onOp(r.id, "delete"); }}><Trash2Icon /></Button>
                </div>
              </div>
              {/* Schedule · next run as a countdown (the clock time beside it) · spend; the red tail when the breaker
                  tripped. Everything about past runs is the table below. */}
              <p className="m-0 text-[13px] leading-5 text-muted-foreground">
                {routineLabel(r)} · {tasks?.[r.task] || 0} calls today · {r.last_result === "started"
                  ? <span className="text-foreground">running now</span>  /* a fired one-shot is disabled while it runs */
                  : r.enabled
                    ? <>next <span className="text-foreground" title={fmtWhen(r.next)}>{fmtIn(r.next) || "soon"}</span>{r.next ? <span> ({fmtWhen(r.next)})</span> : null}</>
                    : (r.fails || 0) >= MAX_FAILS ? <span className="text-destructive">paused after {r.fails} failed runs in a row</span> : "paused"}
                {warn && (r.fails || 0) > 1 && (r.fails || 0) < MAX_FAILS ? <span className="text-destructive"> · {r.fails} failed in a row</span> : null}
              </p>
              <RunsTable botId={b.id} r={r} />
            </section>
          );
        }) : <p className="m-0 text-sm text-muted-foreground">No routines yet. Add one below.</p>}
      </div>
      <RoutineForm onSubmit={add} />
    </div>
  );
}

// A bot's routines inside the settings sheet: each with its schedule in words,
// an enabled switch, Run now and Delete; and an Add form (task + every N
// minutes / daily at a time on chosen weekdays / once at a date). Each change
// is its own POST /routines — nothing waits for the sheet's Save.
import { useState } from "react";
import { PlayIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Switch } from "@platform/shadcn/ui/switch";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { cn } from "@platform/lib/utils";
import { api } from "./api";
import { Segmented } from "./controls";
import { localInputToEpoch, routineWhen, timeAgo } from "./lib";
import { Field, SheetSection } from "./Sheet";
import { toast } from "./Toast";
import type { Routine, RoutineBody } from "./types";

const DAY_LETTERS = ["M", "T", "W", "T", "F", "S", "S"];
const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function nextLabel(ts: number | null | undefined): string {
  if (!ts) return "";
  const now = Date.now() / 1000;
  const d = new Date(ts * 1000);
  const same = d.toDateString() === new Date().toDateString();
  const t = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (ts - now < 3600 && ts > now) return `Next in ${Math.max(1, Math.round((ts - now) / 60))} min`;
  return same ? `Next today ${t}` : `Next ${d.toLocaleDateString(undefined, { weekday: "short" })} ${t}`;
}

function ranAgo(ts: number): string {
  const a = timeAgo(ts, Date.now() / 1000);
  return a === "now" ? "just now" : /^\d+[mhd]$/.test(a) ? `${a} ago` : `on ${a}`;
}

export function RoutinesSection({ botId, routines, onChanged }: { botId: string; routines: Routine[]; onChanged: () => void }) {
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmDel, setConfirmDel] = useState<string | null>(null);

  const run = async (body: RoutineBody, ok?: string) => {
    setBusy(body.rid || body.op);
    try {
      await api.routines(botId, body);
      if (ok) toast(ok);
      onChanged();
      return true;
    } catch (e) {
      toast((e as Error).message, "error");
      return false;
    } finally {
      setBusy(null);
    }
  };

  return (
    <SheetSection
      title="Routines"
      aside={
        !adding && (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="-mr-2 flex min-h-11 items-center gap-1 rounded-lg px-2 text-[15px] font-medium text-[var(--fg)]"
          >
            <PlusIcon className="size-4" />
            Add
          </button>
        )
      }
    >
      {routines.length === 0 && !adding && (
        <p className="py-2 text-[14px] text-[var(--fg-muted)]">Nothing scheduled. A routine gives this bot a task on a timer.</p>
      )}
      <ul className="m-0 flex list-none flex-col p-0">
        {routines.map((r) => (
          <li key={r.id} className="border-b border-[var(--sh-border)] py-3 last:border-b-0">
            <div className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <div className={cn("line-clamp-2 text-[15px] leading-snug", !r.enabled && "text-[var(--fg-muted)]")}>{r.task}</div>
                <div className="mt-0.5 text-[13px] text-[var(--fg-muted)]">
                  {routineWhen(r)}
                  {r.enabled && r.next ? ` · ${nextLabel(r.next)}` : ""}
                  {!r.enabled ? " · Off" : ""}
                </div>
                {r.last ? (
                  <div className="mt-0.5 line-clamp-2 text-[12px] text-[var(--fg-muted)]">
                    Last ran {ranAgo(r.last)}{r.last_result ? ` · ${r.last_result}` : ""}
                    {r.last_message ? ` — ${r.last_message}` : ""}
                  </div>
                ) : null}
              </div>
              <Switch
                checked={r.enabled}
                disabled={busy === r.id}
                onCheckedChange={(on) => void run({ op: on ? "enable" : "disable", rid: r.id })}
                aria-label={r.enabled ? "Turn this routine off" : "Turn this routine on"}
                className="mt-1"
              />
            </div>
            <div className="mt-2 flex gap-2">
              <Button
                variant="outline"
                className="h-10 rounded-xl px-3 text-[14px]"
                disabled={busy === r.id}
                onClick={() => void run({ op: "run", rid: r.id }, "Started")}
              >
                <PlayIcon />
                Run now
              </Button>
              {confirmDel === r.id ? (
                <Button
                  variant="destructive"
                  className="h-10 rounded-xl px-3 text-[14px]"
                  disabled={busy === r.id}
                  onClick={() => void run({ op: "delete", rid: r.id }).then(() => setConfirmDel(null))}
                >
                  <Trash2Icon />
                  Delete routine?
                </Button>
              ) : (
                <Button
                  variant="ghost"
                  className="h-10 rounded-xl px-3 text-[14px] text-[var(--fg-muted)]"
                  onClick={() => setConfirmDel(r.id)}
                >
                  <Trash2Icon />
                  Delete
                </Button>
              )}
            </div>
          </li>
        ))}
      </ul>
      {adding && (
        <AddRoutine
          busy={busy === "add"}
          onCancel={() => setAdding(false)}
          onAdd={async (body) => {
            if (await run(body, "Routine added")) setAdding(false);
          }}
        />
      )}
    </SheetSection>
  );
}

function AddRoutine({ busy, onCancel, onAdd }: { busy: boolean; onCancel: () => void; onAdd: (b: RoutineBody) => void }) {
  const [text, setText] = useState("");
  const [kind, setKind] = useState<"interval" | "daily" | "once">("daily");
  const [minutes, setMinutes] = useState("60");
  const [time, setTime] = useState("09:00");
  const [days, setDays] = useState<number[]>([0, 1, 2, 3, 4, 5, 6]);
  const [at, setAt] = useState("");

  const submit = () => {
    const task = text.trim();
    if (!task) return toast("Say what the routine should do", "error");
    if (kind === "interval") {
      const m = Math.round(Number(minutes));
      if (!Number.isFinite(m) || m < 1) return toast("Minutes must be a number", "error");
      return onAdd({ op: "add", text: task, kind, minutes: m });
    }
    if (kind === "daily") {
      if (!days.length) return toast("Pick at least one day", "error");
      return onAdd({ op: "add", text: task, kind, time, weekdays: days });
    }
    const ts = localInputToEpoch(at);
    if (!Number.isFinite(ts)) return toast("Pick a date and time", "error");
    return onAdd({ op: "add", text: task, kind, at: ts });
  };

  return (
    <div className="mt-2 rounded-2xl border border-[var(--sh-border)] p-3">
      <Field label="Task" htmlFor="rt-text">
        <Textarea
          id="rt-text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Check my inbox and summarise anything urgent"
          rows={2}
          className="min-h-16 rounded-xl text-base"
        />
      </Field>
      <Field label="When">
        <Segmented
          label="When"
          value={kind}
          onChange={setKind}
          options={[
            { value: "interval", label: "Every" },
            { value: "daily", label: "Daily" },
            { value: "once", label: "Once" },
          ]}
        />
      </Field>
      {kind === "interval" && (
        <Field label="Every (minutes)" htmlFor="rt-min">
          <Input
            id="rt-min"
            type="number"
            inputMode="numeric"
            min={1}
            value={minutes}
            onChange={(e) => setMinutes(e.target.value)}
            className="h-11 rounded-xl text-base"
          />
        </Field>
      )}
      {kind === "daily" && (
        <>
          <Field label="At" htmlFor="rt-time">
            <Input
              id="rt-time"
              type="time"
              value={time}
              onChange={(e) => setTime(e.target.value)}
              className="h-11 rounded-xl text-base"
            />
          </Field>
          <Field label="On">
            <div className="flex gap-1.5" role="group" aria-label="Weekdays">
              {DAY_LETTERS.map((l, d) => {
                const on = days.includes(d);
                return (
                  <button
                    key={d}
                    type="button"
                    aria-pressed={on}
                    aria-label={DAY_NAMES[d]}
                    onClick={() => setDays((p) => (on ? p.filter((x) => x !== d) : [...p, d].sort()))}
                    className={cn(
                      "flex h-11 flex-1 items-center justify-center rounded-xl text-[15px] font-medium",
                      on ? "bg-[var(--fg)] text-[var(--bg)]" : "bg-[var(--bg-alt)] text-[var(--fg-muted)]",
                    )}
                  >
                    {l}
                  </button>
                );
              })}
            </div>
          </Field>
        </>
      )}
      {kind === "once" && (
        <Field label="At" htmlFor="rt-at">
          <Input
            id="rt-at"
            type="datetime-local"
            value={at}
            onChange={(e) => setAt(e.target.value)}
            className="h-11 rounded-xl text-base"
          />
        </Field>
      )}
      <div className="mt-2 flex gap-2">
        <Button variant="ghost" className="h-11 flex-1 rounded-xl text-[15px]" onClick={onCancel}>
          Cancel
        </Button>
        <Button variant="accent" className="h-11 flex-1 rounded-xl text-[15px]" disabled={busy} onClick={submit}>
          {busy ? "Adding…" : "Add routine"}
        </Button>
      </div>
    </div>
  );
}

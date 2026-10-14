// A bot's settings from the phone: name, instructions, model, effort, approval
// and max steps (one Save → POST /settings with only what changed), its
// routines (each change saves at once), its memory (read-only, loaded with
// detail=1) and Delete (never for Super Bot). Everything Mac-screen — browser,
// permissions, engine, channels — stays on the desktop.
import { useEffect, useState } from "react";
import { Trash2Icon } from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@platform/shadcn/ui/alert-dialog";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { api } from "./api";
import { Choice, Segmented } from "./controls";
import { RoutinesSection } from "./RoutinesSection";
import { Field, Sheet, SheetSection } from "./Sheet";
import { toast } from "./Toast";
import type { BotDetail, SettingsBody, Setup } from "./types";

interface Form {
  name: string;
  instructions: string;
  model: string;
  effort: string;
  approval: "ask" | "auto";
  max_steps: string;
}
const formOf = (b: BotDetail): Form => ({
  name: b.name,
  instructions: b.instructions || "",
  model: b.model || "",
  effort: b.effort || "",
  approval: b.approval === "auto" ? "auto" : "ask",
  max_steps: b.max_steps == null ? "" : String(b.max_steps),
});

export function SettingsSheet({
  open,
  onOpenChange,
  bot,
  setup,
  onChanged,
  onDeleted,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  bot: BotDetail;
  setup: Setup | null;
  /** Something was saved: poll the bot now. */
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const [form, setForm] = useState<Form>(() => formOf(bot));
  // What the sheet opened with. The diff is against this, not the live `bot` (polling refreshes it): a field
  // changed at the Mac while the sheet is open must not read as the user's edit and be written back over.
  const [base, setBase] = useState<Form>(() => formOf(bot));
  const [memory, setMemory] = useState<string | null | undefined>(undefined);
  const [skills, setSkills] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const isSuper = bot.kind === "super";

  // Fresh form each time the sheet opens; memory and skills come with detail=1
  // (since=<seq>: no events, just the bot).
  useEffect(() => {
    if (!open) return;
    const opened = formOf(bot);
    setForm(opened);
    setBase(opened);
    setMemory(undefined);
    let alive = true;
    api
      .page(bot.id, { since: bot.seq, limit: 1, detail: true })
      .then((d) => {
        if (!alive) return;
        setMemory(d.bot.memory ?? null);
        setSkills(d.bot.skills || []);
      })
      .catch(() => alive && setMemory(null));
    return () => {
      alive = false;
    };
  }, [open, bot.id]);

  const changes: SettingsBody = {};
  if (form.name.trim() && form.name.trim() !== base.name) changes.name = form.name.trim();
  // Super Bot's instructions and approval steer every task it runs, routines included: set at the Mac (docs §14).
  if (!isSuper && form.instructions !== base.instructions) changes.instructions = form.instructions;
  if (form.model && form.model !== base.model) changes.model = form.model;
  if (form.effort && form.effort !== base.effort) changes.effort = form.effort;
  if (!isSuper && form.approval !== base.approval) changes.approval = form.approval;
  if (form.max_steps.trim() !== base.max_steps) {
    // "" clears the override (routes._settings drops it on "" or 0; null reads as "not sent").
    const n = form.max_steps.trim() === "" ? "" : Math.round(Number(form.max_steps));
    if (n === "" || Number.isFinite(n)) changes.max_steps = n;
  }
  const dirty = Object.keys(changes).length > 0;

  const save = async () => {
    if (!dirty || saving) return;
    setSaving(true);
    try {
      await api.settings(bot.id, changes);
      toast("Saved");
      onChanged();
      onOpenChange(false);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    try {
      await api.remove(bot.id);
      setConfirm(false);
      onOpenChange(false);
      onDeleted();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const set = <K extends keyof Form>(k: K) => (v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <>
      <Sheet
        open={open}
        onOpenChange={onOpenChange}
        title="Settings"
        className="lan-bots"
        footer={
          <Button variant="accent" className="h-12 w-full rounded-xl text-[16px]" disabled={!dirty || saving} onClick={save}>
            {saving ? "Saving…" : "Save changes"}
          </Button>
        }
      >
        <SheetSection title="General">
          <Field label="Name" htmlFor="st-name">
            <Input
              id="st-name"
              value={form.name}
              onChange={(e) => set("name")(e.target.value)}
              autoComplete="off"
              className="h-11 rounded-xl text-base"
            />
          </Field>
          <Field label="Instructions" htmlFor="st-instr" hint={isSuper ? "Set at the Mac." : undefined}>
            <Textarea
              id="st-instr"
              value={form.instructions}
              readOnly={isSuper}
              onChange={(e) => set("instructions")(e.target.value)}
              rows={5}
              className="max-h-72 min-h-28 rounded-xl text-base"
            />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Model" htmlFor="st-model">
              <Choice id="st-model" value={form.model} options={setup?.models || []} onChange={set("model")} />
            </Field>
            <Field label="Effort" htmlFor="st-effort">
              <Choice id="st-effort" value={form.effort} options={setup?.efforts || []} onChange={set("effort")} />
            </Field>
          </div>
          {isSuper ? (
            <Field label="Approval" hint="Tasks you start from the phone always ask before writing, running or building. Set at the Mac.">
              <div className="text-base">{form.approval === "auto" ? "Auto" : "Ask"}</div>
            </Field>
          ) : (
            <Field label="Approval" hint={form.approval === "ask" ? "Asks before anything that changes something." : "Acts without asking."}>
              <Segmented
                label="Approval"
                value={form.approval}
                onChange={set("approval")}
                options={[
                  { value: "ask", label: "Ask" },
                  { value: "auto", label: "Auto" },
                ]}
              />
            </Field>
          )}
          <Field label="Max steps per task" htmlFor="st-steps" hint="Empty: the default.">
            <Input
              id="st-steps"
              type="number"
              inputMode="numeric"
              min={1}
              value={form.max_steps}
              onChange={(e) => set("max_steps")(e.target.value)}
              placeholder="Default"
              className="h-11 rounded-xl text-base"
            />
          </Field>
        </SheetSection>

        <RoutinesSection botId={bot.id} routines={bot.routines || []} onChanged={onChanged} />

        <SheetSection title="Memory">
          {memory === undefined ? (
            <p className="py-2 text-[14px] text-[var(--fg-muted)]">Loading…</p>
          ) : memory ? (
            <div className="mt-2 max-h-56 overflow-y-auto rounded-xl bg-[var(--bg-alt)] p-3 text-[14px] leading-snug whitespace-pre-wrap">
              {memory}
            </div>
          ) : (
            <p className="py-2 text-[14px] text-[var(--fg-muted)]">Nothing remembered yet.</p>
          )}
          {skills.length > 0 && (
            <p className="pt-2 text-[13px] text-[var(--fg-muted)]">
              Playbooks: {skills.join(", ")}
            </p>
          )}
        </SheetSection>

        {!isSuper && (
          <SheetSection title="Danger zone">
            <Button
              variant="destructive"
              className="mt-2 h-11 w-full rounded-xl text-[15px]"
              onClick={() => setConfirm(true)}
            >
              <Trash2Icon />
              Delete bot
            </Button>
          </SheetSection>
        )}
      </Sheet>

      <AlertDialog open={confirm} onOpenChange={setConfirm}>
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {bot.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              Its chat, memory, routines and files go too. This can't be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={remove}>
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

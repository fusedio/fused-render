// "+ New bot": name, a preset (its description under the picker), instructions,
// model, effort and approval. Always an ordinary bot; the server applies the
// preset's own instructions, face and skills when one is picked.
import { useEffect, useState } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { api } from "./api";
import { Choice, Segmented } from "./controls";
import { Field, Sheet } from "./Sheet";
import { toast } from "./Toast";
import type { Preset, Setup } from "./types";

function describe(p: Preset): string {
  if (p.description) return p.description;
  const t = (p.instructions || "").replace(/\s+/g, " ").trim();
  const first = /^(.{20,220}?[.!?])(\s|$)/.exec(t);
  return first ? first[1] : t.slice(0, 220);
}

export function CreateBotSheet({
  open,
  onOpenChange,
  setup,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  setup: Setup;
  onCreated: (id: string) => void;
}) {
  const [presets, setPresets] = useState<Preset[] | null>(null);
  const [name, setName] = useState("");
  const [preset, setPreset] = useState("");
  const [instructions, setInstructions] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [approval, setApproval] = useState<"ask" | "auto">("ask");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open || presets) return;
    api
      .presets()
      .then((d) => setPresets(d.presets || []))
      .catch(() => setPresets([]));
  }, [open, presets]);

  useEffect(() => {
    if (open) {
      setName("");
      setPreset("");
      setInstructions("");
      setModel("");
      setEffort("");
      setApproval("ask");
    }
  }, [open]);

  const chosen = presets?.find((p) => p.key === preset) || null;

  const pickPreset = (key: string) => {
    const prev = presets?.find((x) => x.key === preset);
    const p = presets?.find((x) => x.key === key);
    setPreset(key);
    if (p && (!name.trim() || name === prev?.name)) setName(p.name);
    // The preset's own instructions fill the box (typed ones replace them, as
    // on the desktop), unless the user already wrote their own.
    if (!instructions.trim() || instructions === (prev?.instructions || "")) setInstructions(p?.instructions || "");
  };

  const create = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      const r = await api.create({
        name: name.trim(),
        ...(preset ? { preset } : {}),
        ...(instructions.trim() ? { instructions: instructions.trim() } : {}),
        ...(model ? { model } : {}),
        ...(effort ? { effort } : {}),
        approval,
      });
      onCreated(r.id);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title="New bot"
      className="lan-bots"
      footer={
        <Button
          variant="accent"
          className="h-12 w-full rounded-xl text-[16px]"
          disabled={!name.trim() || busy}
          onClick={create}
        >
          {busy ? "Creating…" : "Create bot"}
        </Button>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void create();
        }}
      >
        <Field label="Name" htmlFor="nb-name">
          <Input
            id="nb-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Inbox helper"
            autoComplete="off"
            enterKeyHint="done"
            className="h-11 rounded-xl text-base"
          />
        </Field>
        <Field
          label="Start from"
          htmlFor="nb-preset"
          hint={chosen ? describe(chosen) : "A blank bot: you write what it does."}
        >
          <NativeSelect
            id="nb-preset"
            value={preset}
            onChange={(e) => pickPreset(e.target.value)}
            disabled={!presets}
            className="w-full [&_select]:h-11 [&_select]:rounded-xl [&_select]:text-base"
          >
            <NativeSelectOption value="">{presets ? "Blank bot" : "Loading presets…"}</NativeSelectOption>
            {(presets || []).map((p) => (
              <NativeSelectOption key={p.key} value={p.key}>
                {p.name}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
        <Field label="Instructions" htmlFor="nb-instr">
          <Textarea
            id="nb-instr"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="What should this bot do, and how?"
            rows={4}
            className="min-h-24 rounded-xl text-base"
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Model" htmlFor="nb-model">
            <Choice id="nb-model" value={model} options={setup.models || []} onChange={setModel} defaultLabel="Default" />
          </Field>
          <Field label="Effort" htmlFor="nb-effort">
            <Choice id="nb-effort" value={effort} options={setup.efforts || []} onChange={setEffort} defaultLabel="Default" />
          </Field>
        </div>
        <Field
          label="Approval"
          hint={approval === "ask" ? "Asks before anything that changes something." : "Acts without asking. Best for read-only work."}
        >
          <Segmented
            label="Approval"
            value={approval}
            onChange={setApproval}
            options={[
              { value: "ask", label: "Ask" },
              { value: "auto", label: "Auto" },
            ]}
          />
        </Field>
      </form>
    </Sheet>
  );
}

// "+" on the Apps grid: describe an app, optionally name it, Build. The
// computer names it otherwise (`POST /api/phone/tasks/new-app`, the one route
// the phone has of its own — server/routers/phone_tasks.py), scaffolds it and
// starts the build as a task; the caller opens that task. New TASKS have no
// sheet here: the Tasks section is the desktop's own page, whose "+" is the
// desktop's own form.
import { useEffect, useState } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Field, Sheet } from "./ui/Sheet";
import { toast } from "./ui/Toast";

interface NewAppResult {
  key: string;
  entry_id: string;
  app: { name: string; path: string; url: string };
  task_error?: string;
}

async function newApp(body: { prompt: string; name?: string }): Promise<NewAppResult> {
  let res: Response;
  try {
    res = await fetch("/api/phone/tasks/new-app", {
      method: "POST",
      headers: { "X-Fused": "1", "Content-Type": "application/json" },
      cache: "no-store",
      credentials: "same-origin",
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error("Can't reach the computer");
  }
  const data = (await res.json().catch(() => null)) as (NewAppResult & { error?: unknown }) | null;
  if (!res.ok) throw new Error(data && typeof data.error === "string" && data.error ? data.error : `HTTP ${res.status}`);
  return data as NewAppResult;
}

export function NewSheet({
  open,
  onOpenChange,
  onCreated,
  onCreatedWithoutTask,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onCreated: (key: string, entryId: string) => void;
  /** An app was created but its build task could not start: the caller refreshes what it lists. */
  onCreatedWithoutTask?: () => void;
}) {
  const [prompt, setPrompt] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setPrompt("");
      setName("");
      setBusy(false);
    }
  }, [open]);

  const canRun = !busy && prompt.trim().length > 0;
  const submit = async () => {
    if (!canRun) return;
    setBusy(true);
    try {
      const r = await newApp({ prompt: prompt.trim(), ...(name.trim() ? { name: name.trim() } : {}) });
      if (r.task_error) toast(r.task_error, "error");
      if (!r.key) {
        toast(`${r.app.name} was created, but its build task could not start.`, "error");
        onCreatedWithoutTask?.();
        onOpenChange(false);
        return;
      }
      onCreated(r.key, r.entry_id);
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
      title="New app"
      footer={
        <Button variant="accent" className="h-12 w-full rounded-xl text-[16px]" onClick={submit} disabled={!canRun}>
          {busy ? "Creating…" : "Build"}
        </Button>
      }
    >
      <Field label="What should it do?" htmlFor="new-app-prompt">
        <Textarea
          id="new-app-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={5}
          placeholder="A page that shows my monthly spending by category…"
          className="min-h-28 rounded-xl text-[16px]"
        />
      </Field>
      <Field label="Name" htmlFor="new-app-name" hint="Optional. Left empty, the computer names it from the description.">
        <Input
          id="new-app-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="spending-by-category"
          autoCapitalize="off"
          autoCorrect="off"
          className="h-11 rounded-xl text-[16px]"
        />
      </Field>
      <p className="pt-1 text-[12px] leading-snug text-[var(--fg-muted)]">
        The app lands on the grid at once; the build is followed as a task.
      </p>
    </Sheet>
  );
}

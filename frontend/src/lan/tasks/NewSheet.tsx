// "+" on the Tasks list and on the Apps grid: a sheet with two tabs. Task — a
// prompt and a folder picked from the list the server knows (a project of an
// existing task, or one under the workspace), Run. New app — what it should
// do and an optional name (the computer names it otherwise), Build. Either
// answers a task key; the caller opens it.
import { useEffect, useState } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { Input } from "@platform/shadcn/ui/input";
import { NativeSelect, NativeSelectOption } from "@platform/shadcn/ui/native-select";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Segmented } from "../bots/controls";
import { Field, Sheet } from "../bots/Sheet";
import { toast } from "../bots/Toast";
import { api } from "./api";
import { projectOptions } from "./lib";
import type { Project } from "./types";

export type NewTab = "task" | "app";

export function NewSheet({
  open,
  onOpenChange,
  tab: initialTab,
  projects,
  onCreated,
  onCreatedWithoutTask,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  /** The tab the sheet opens on. */
  tab: NewTab;
  /** The folder facet from the list; null when the sheet opens from the grid, which fetches it. */
  projects: Project[] | null;
  onCreated: (key: string, entryId: string) => void;
  /** An app was created but its build task could not start: the caller refreshes what it lists. */
  onCreatedWithoutTask?: () => void;
}) {
  const [tab, setTab] = useState<NewTab>(initialTab);
  const [prompt, setPrompt] = useState("");
  const [folder, setFolder] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [folders, setFolders] = useState<Project[] | null>(projects);

  useEffect(() => {
    if (open) {
      setTab(initialTab);
      setPrompt("");
      setName("");
      setBusy(false);
    }
  }, [open, initialTab]);
  useEffect(() => {
    if (projects) setFolders(projects);
  }, [projects]);
  // Opened from the grid: the folders come from the list route.
  useEffect(() => {
    if (!open || folders) return;
    api
      .list()
      .then((d) => setFolders("unchanged" in d ? [] : d.projects))
      .catch(() => setFolders([]));
  }, [open, folders]);

  const options = folders ? projectOptions(folders) : [];
  const folderKnown = options.some((p) => p.path === folder);
  // A folder that left the list (its last task deleted) is not kept selected.
  useEffect(() => {
    if (options.length && !folderKnown) setFolder(options[0].path);
  }, [options, folder, folderKnown]);

  const canRun = !busy && prompt.trim().length > 0 && (tab === "app" || folderKnown);
  const submit = async () => {
    if (!canRun) return;
    setBusy(true);
    try {
      if (tab === "task") {
        const r = await api.create({ prompt: prompt.trim(), target: folder });
        onCreated(r.key, r.entry_id);
      } else {
        const r = await api.newApp({ prompt: prompt.trim(), ...(name.trim() ? { name: name.trim() } : {}) });
        if (r.task_error) toast(r.task_error, "error");
        if (!r.key) {
          toast(`${r.app.name} was created, but its build task could not start.`, "error");
          onCreatedWithoutTask?.();
          onOpenChange(false);
          return;
        }
        onCreated(r.key, r.entry_id);
      }
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
      title={tab === "task" ? "New task" : "New app"}
      footer={
        <Button variant="accent" className="h-12 w-full rounded-xl text-[16px]" onClick={submit} disabled={!canRun}>
          {busy ? (tab === "task" ? "Starting…" : "Creating…") : tab === "task" ? "Run" : "Build"}
        </Button>
      }
    >
      <div className="pb-2">
        <Segmented
          value={tab}
          label="What to start"
          options={[
            { value: "task", label: "Task" },
            { value: "app", label: "New app" },
          ]}
          onChange={setTab}
        />
      </div>
      {tab === "task" ? (
        <>
          <Field label="What should the agent do?" htmlFor="new-task-prompt">
            <Textarea
              id="new-task-prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={5}
              placeholder="Fix the failing test in app.py…"
              className="min-h-28 rounded-xl text-[16px]"
            />
          </Field>
          <Field
            label="Folder"
            htmlFor="new-task-folder"
            hint={folders && !options.length ? "No folders yet. Start one from the computer, or build a new app." : undefined}
          >
            <NativeSelect id="new-task-folder" value={folder} onChange={(e) => setFolder(e.target.value)} disabled={!options.length}>
              {!folders && <NativeSelectOption value="">Loading…</NativeSelectOption>}
              {options.map((p) => (
                <NativeSelectOption key={p.path} value={p.path}>
                  {p.label} · {p.count}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </Field>
          <p className="pt-1 text-[12px] leading-snug text-[var(--fg-muted)]">
            Runs with the computer's default agent and asks before anything risky.
          </p>
        </>
      ) : (
        <>
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
        </>
      )}
    </Sheet>
  );
}

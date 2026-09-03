// Memory section: a read-only viewer of Claude Code's persistent memory under
// projects/*/memory/, with the per-folder git lifecycle beside it.
//
// The memory FILE is the row, grouped under the project that owns it. Each
// file's row shows its name and the one-line `description` from its own YAML
// frontmatter (claude_config/memory.py parses it; nothing shown when the
// frontmatter is missing or malformed — the filename already carries the
// fallback).
//
// Contents are Claude's to author — nothing here edits a memory file. What the
// UI adds is the lifecycle the files themselves can't express: which folders
// have uncommitted drift, a path-limited Commit per project, and a Clear that
// deletes the .md files and commits the deletion (recoverable from History,
// which is why Clear is a confirm and not a two-step). All three live on the
// project GROUP, not on an individual file row, because they act on the
// whole folder. Reveal stays visible outright (informational); Commit — only
// when the project has drift — and Clear fade in on hover/focus-within.
//
// A group is titled by the PROJECT FOLDER, not by the projects/ directory
// name: that name is a munged cwd and reads as nothing. The server resolves
// it and sends null when it cannot, in which case this shows the slug rather
// than a path that might not exist.
import { useCallback } from "react";
import { Check, Folder, Trash2 } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { Button } from "@platform/shadcn/ui/button";
import { StatusBadge } from "@platform/ui/flow/StatusIcon";
import { SectionHeading, Tiny } from "@platform/ui/flow/Typography";
import * as cc from "../api";
import {
  Empty,
  ErrorNote,
  List,
  ListRow,
  ListSkeleton,
  SKELETON_ROWS,
  SectionToolbar,
  guard,
  toastOk,
  useChangePreview,
  useModuleData,
} from "../bits";
import type { SectionProps } from "../bits";

export default function MemorySection({ onChanged }: SectionProps) {
  const load = useCallback(() => cc.memory.list(), []);
  const { data, error, reload } = useModuleData(load);
  const { node: modal, ask } = useChangePreview();

  const commit = async (project: string) => {
    if (!(await guard(cc.memory.commit(project)))) return;
    toastOk("Committed");
    onChanged();
    reload();
  };

  const clear = async (project: string) => {
    const ok = await ask<boolean>({
      title: `Clear memory for ${project}?`,
      note: "Every .md file in this project's memory folder is deleted and the deletion is committed — recoverable from History.",
      buttons: [
        { label: "Cancel", value: false },
        { label: "Clear", value: true, primary: true, danger: true },
      ],
    });
    if (!ok) return;
    if (!(await guard(cc.memory.clear(project)))) return;
    toastOk("Cleared");
    onChanged();
    reload();
  };

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <ListSkeleton rows={SKELETON_ROWS} label="Loading memory" />;

  const fileCount = data.projects.reduce((n, p) => n + p.files.length, 0);

  return (
    <>
      {modal}
      <SectionToolbar
        summary={`${data.projects.length} project(s) · ${fileCount} file(s)`}
        onRefresh={reload}
      />
      {!data.projects.length && (
        <Empty>No persistent memory found under projects/*/memory/.</Empty>
      )}
      {data.projects.map((p) => {
        const dirty = p.changes.length;
        const count = `${p.files.length} file${p.files.length === 1 ? "" : "s"}`;
        return (
          <section className="space-y-2 group/mem" key={p.project}>
            <div className="flex items-center gap-3 min-w-0">
              <SectionHeading
                className={cn("flex items-center gap-2 min-w-0 normal-case tracking-normal", p.pathConfirmed && "font-mono text-xs")}
              >
                {/* The folder, when the server could confirm which one it is —
                    mono, because it is a path. Otherwise the raw slug, NOT
                    dressed up as a path. */}
                <span className="truncate" title={p.path ?? undefined}>
                  {p.path ?? p.project}
                </span>
                <Tiny className="font-sans shrink-0">{count}</Tiny>
                {dirty > 0 && <StatusBadge bucket="orange" className="font-sans">{dirty} uncommitted</StatusBadge>}
              </SectionHeading>
              <div className="ml-auto flex items-center gap-0.5 shrink-0">
                <Button
                  variant="ghost"
                  size="icon-xs"
                  title="Reveal in Finder"
                  aria-label={`Reveal the memory folder for ${p.path ?? p.project} in Finder`}
                  onClick={() => guard(cc.memory.open(p.project))}
                >
                  <Folder />
                </Button>
                <span className="flex items-center gap-0.5 opacity-0 group-hover/mem:opacity-100 group-focus-within/mem:opacity-100 motion-safe:transition-opacity">
                  {dirty > 0 && (
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      title="Commit this project's memory folder"
                      aria-label={`Commit memory for ${p.path ?? p.project}`}
                      onClick={() => commit(p.project)}
                    >
                      <Check />
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    className="hover:text-destructive"
                    title="Clear this project's memory"
                    aria-label={`Clear memory for ${p.path ?? p.project}`}
                    onClick={() => clear(p.project)}
                  >
                    <Trash2 />
                  </Button>
                </span>
              </div>
            </div>
            <List>
              {p.files.map((f) => (
                <ListRow
                  key={f.name}
                  name={f.name}
                  nameMono
                  secondary={f.description ?? undefined}
                  secondaryClamp2
                  secondaryTitle={f.description ?? undefined}
                />
              ))}
            </List>
          </section>
        );
      })}
    </>
  );
}

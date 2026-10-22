// Memory section: Claude Code's persistent memory under
// projects/*/memory/.
//
// The memory FILE is the row, grouped under the project that owns it: the files
// ARE the content of this tab (which lesson lives where). Each file's row shows
// its name and the one-line `description` from its own YAML frontmatter
// (claude_config/memory.py parses it; nothing is shown when the frontmatter is
// missing or malformed — the row's own filename already carries the fallback,
// so there is no separate "untitled" state to invent).
//
// Each file row opens an in-app editor and has a delete of its own. Reveal and
// Clear sit on the project GROUP, because they act on the whole folder; Clear
// deletes the folder's .md files behind a confirm.
//
// A group is titled by the PROJECT FOLDER, not by the projects/ directory
// name: that name is a munged cwd ("-Users-me-Work-fused-render") and reads
// as nothing. The server resolves it — from a session transcript's recorded
// cwd, else against the filesystem — and sends null when it cannot, in which
// case this shows the slug rather than a path that might not exist. See
// claude_config/memory.py's _project_path for why a "-" -> "/" replace is not
// an option.
import { useCallback } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import * as cc from "../api";
import {
  Empty,
  Icon,
  List,
  ListRow,
  ListSkeleton,
  SKELETON_ROWS,
  Head,
  RefreshButton,
  plural,
  guard,
  toastErr,
  toastOk,
  useConfirm,
  useModuleData,
} from "../bits";
import type { HeadedSectionProps } from "../bits";
import { FileEditor } from "../FileEditor";

function MemoryFileEditor({ project, file, onDelete }: { project: string; file: string; onDelete: () => void }) {
  const load = useCallback(async () => {
    const res = await cc.memory.read(project, file);
    if (!res.ok) throw new Error(res.error || "Could not read the file");
    return res.text;
  }, [project, file]);
  const save = useCallback(
    async (text: string) => {
      const res = await cc.memory.write(project, file, text);
      return res.ok ? null : res.error || "Save failed";
    },
    [project, file],
  );
  return (
    <FileEditor
      label={file}
      load={load}
      save={save}
      extra={
        <Button size="sm" variant="destructive" className="ml-auto" onClick={onDelete}>
          Delete file
        </Button>
      }
    />
  );
}

export default function MemorySection({ filter = "", title, blurb }: HeadedSectionProps) {
  const load = useCallback(() => cc.memory.list(), []);
  const { data, error, reload } = useModuleData(load);
  const { node: modal, ask } = useConfirm();

  const clear = async (project: string) => {
    const ok = await ask<boolean>({
      title: `Clear memory for ${project}?`,
      note: "Every .md file in this project's memory folder is deleted. This can't be undone.",
      buttons: [
        { label: "Cancel", value: false },
        { label: "Clear", value: true, primary: true, danger: true },
      ],
    });
    if (!ok) return;
    if (!(await guard(cc.memory.clear(project)))) return;
    toastOk("Cleared");
    reload();
  };

  const deleteFile = async (project: string, file: string) => {
    const ok = await ask<boolean>({
      title: `Delete ${file}?`,
      note: "The file is deleted. This can't be undone.",
      buttons: [
        { label: "Cancel", value: false },
        { label: "Delete", value: true, primary: true, danger: true },
      ],
    });
    if (!ok) return;
    const res = await guard(cc.memory.deleteFile(project, file));
    if (!res) return;
    if (!res.ok) {
      toastErr(res.error || "Delete failed");
      return;
    }
    toastOk("Deleted");
    reload();
  };

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data)
    return (
      <>
        <Head title={title} blurb={blurb} />
        <ListSkeleton rows={SKELETON_ROWS} label="Loading memory" />
      </>
    );

  const needle = filter.trim().toLowerCase();
  const projects = data.projects
    .map((p) => ({
      ...p,
      shown: needle
        ? p.files.filter(
            (f) => `${f.name} ${f.description ?? ""} ${p.path ?? p.project}`.toLowerCase().includes(needle),
          )
        : p.files,
    }))
    .filter((p) => p.shown.length > 0 || !needle);

  const fileCount = data.projects.reduce((n, p) => n + p.files.length, 0);

  return (
    <>
      {modal}
      <Head
        title={title}
        blurb={`${plural(fileCount, "file")} across ${plural(data.projects.length, "project")}`}
        aside={<RefreshButton onClick={reload} />}
      />
      {!projects.length && (
        <Empty>{needle ? "No memory files match." : "No persistent memory found under projects/*/memory/."}</Empty>
      )}
      {projects.map((p) => {
        const count = plural(p.files.length, "file");
        return (
          <div className="cc-memgroup" key={p.project}>
            <div className="cc-memgroup-head">
              <div className="cc-memgroup-title">
                {/* The folder, when the server could confirm which one it is —
                    mono, because it is a path. Otherwise the raw slug, NOT
                    dressed up as a path: the encoding is lossy and a
                    plausible-looking /Users/me/Work/fused/render that doesn't
                    exist would be worse than the slug it came from. */}
                <span className={p.pathConfirmed ? "cc-mono" : undefined} title={p.path ?? undefined}>
                  {p.path ?? p.project}
                </span>
                <span className="cc-memgroup-count">{count}</span>
              </div>
              <div className="cc-memgroup-actions">
                <button
                  type="button"
                  className="cc-iconbtn"
                  title="Reveal in file manager"
                  aria-label={`Reveal the memory folder for ${p.path ?? p.project} in the file manager`}
                  onClick={() => guard(cc.memory.open(p.project))}
                >
                  <Icon name="folder" />
                </button>
                <button
                  type="button"
                  className="cc-iconbtn cc-iconbtn-danger"
                  title="Clear this project's memory"
                  aria-label={`Clear memory for ${p.path ?? p.project}`}
                  onClick={() => clear(p.project)}
                >
                  <Icon name="trash" />
                </button>
              </div>
            </div>
            <List>
              {p.shown.map((f) => (
                <ListRow
                  key={f.name}
                  name={f.name}
                  nameMono
                  secondary={f.description ?? undefined}
                  secondaryClass="cc-lrow-sub-clamp2"
                  secondaryTitle={f.description ?? undefined}
                  details={
                    <MemoryFileEditor
                      project={p.project}
                      file={f.name}
                      onDelete={() => void deleteFile(p.project, f.name)}
                    />
                  }
                />
              ))}
            </List>
          </div>
        );
      })}
    </>
  );
}

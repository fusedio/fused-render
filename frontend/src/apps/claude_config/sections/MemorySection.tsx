// Memory section: a read-only viewer of Claude Code's persistent memory under
// projects/*/memory/, with the per-folder git lifecycle beside it.
//
// Contents are Claude's to author — nothing here edits a memory file. What the
// UI adds is the lifecycle the files themselves can't express: which folders
// have uncommitted drift, a path-limited commit per project, and a Clear that
// deletes the .md files and commits the deletion (recoverable from History,
// which is why Clear is a confirm and not a two-step).
//
// A row is titled by the PROJECT FOLDER, not by the projects/ directory name:
// that name is a munged cwd ("-Users-me-Work-fused-render") and reads as
// nothing. The server resolves it — from a session transcript's recorded cwd,
// else against the filesystem — and sends null when it cannot, in which case
// this shows the slug rather than a path that might not exist. See
// claude_config/memory.py's _project_path for why a "-" -> "/" replace is not
// an option.
import { useCallback } from "react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import {
  Empty,
  Icon,
  ListRow,
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

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <SkeletonLines rows={SKELETON_ROWS} label="Loading memory" />;

  const files = data.projects.reduce((n, p) => n + p.files.length, 0);

  return (
    <>
      {modal}
      <SectionToolbar
        summary={`${data.projects.length} project(s) · ${files} file(s)`}
        onRefresh={reload}
      />
      {!data.projects.length && (
        <Empty>No persistent memory found under projects/*/memory/.</Empty>
      )}
      {data.projects.map((p) => {
        const dirty = p.changes.length;
        const count = `${p.files.length} file${p.files.length === 1 ? "" : "s"}`;
        return (
          <ListRow
            key={p.project}
            // The folder, when the server could confirm which one it is —
            // mono, because it is a path. Otherwise the raw slug, NOT dressed
            // up as a path: the encoding is lossy and a plausible-looking
            // /Users/me/Work/fused/render that doesn't exist would be worse
            // than the slug it came from.
            name={p.path ?? p.project}
            nameMono={p.pathConfirmed}
            secondary={count}
            // The file names ARE the content of this tab — which folder holds
            // what — so they move from a `·`-joined line that ellipsized into
            // nothing to the expanded panel, one per line. The slug rides along
            // because it is what the folder is actually called on disk.
            details={
              <dl className="cc-lrow-dl">
                {p.files.length > 0 && (
                  <>
                    <dt className="cc-lrow-dt">Files</dt>
                    <dd className="cc-lrow-dd cc-mono">{p.files.join("\n")}</dd>
                  </>
                )}
                <dt className="cc-lrow-dt">Folder</dt>
                <dd className="cc-lrow-dd cc-mono">
                  {p.path ?? "unknown — no session transcript records this project's folder"}
                </dd>
                <dt className="cc-lrow-dt">Stored as</dt>
                <dd className="cc-lrow-dd cc-mono">projects/{p.project}/memory</dd>
              </dl>
            }
            meta={
              dirty > 0 ? <span className="cc-change">{dirty} uncommitted</span> : null
            }
            actions={
              <>
                <button
                  type="button"
                  className="cc-iconbtn"
                  title="Reveal in Finder"
                  aria-label={`Reveal the memory folder for ${p.path ?? p.project} in Finder`}
                  onClick={() => guard(cc.memory.open(p.project))}
                >
                  <Icon name="folder" />
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={dirty === 0}
                  onClick={() => commit(p.project)}
                >
                  Commit
                </button>
                <button type="button" className="btn btn-danger" onClick={() => clear(p.project)}>
                  Clear
                </button>
              </>
            }
          />
        );
      })}
    </>
  );
}

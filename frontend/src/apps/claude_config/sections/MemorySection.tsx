// Memory section: a read-only viewer of Claude Code's persistent memory under
// projects/*/memory/, with the per-folder git lifecycle beside it.
//
// Contents are Claude's to author — nothing here edits a memory file. What the
// UI adds is the lifecycle the files themselves can't express: which folders
// have uncommitted drift, a path-limited commit per project, and a Clear that
// deletes the .md files and commits the deletion (recoverable from History,
// which is why Clear is a confirm and not a two-step).
import { useCallback } from "react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import * as cc from "../api";
import {
  Empty,
  Icon,
  ListRow,
  SKELETON_ROWS,
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
  if (!data.projects.length)
    return <Empty>No persistent memory found under projects/*/memory/.</Empty>;

  return (
    <>
      {modal}
      {data.projects.map((p) => {
        const dirty = p.changes.length;
        const count = `${p.files.length} file${p.files.length === 1 ? "" : "s"}`;
        return (
          <ListRow
            key={p.project}
            name={p.project}
            nameMono
            secondary={count}
            // The file names ARE the content of this tab — which folder holds
            // what — so they move from a `·`-joined line that ellipsized into
            // nothing to the expanded panel, one per line.
            details={
              p.files.length ? (
                <dl className="cc-lrow-dl">
                  <dt className="cc-lrow-dt">Files</dt>
                  <dd className="cc-lrow-dd cc-mono">{p.files.join("\n")}</dd>
                </dl>
              ) : null
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
                  aria-label={`Reveal ${p.project} in Finder`}
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

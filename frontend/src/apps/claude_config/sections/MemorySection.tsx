// Memory section: a read-only viewer of Claude Code's persistent memory under
// projects/*/memory/, with the per-folder git lifecycle beside it.
//
// Round 2 redesign: the memory FILE is the row now, not the project. Before
// this, a row was a project folder and the files hid inside an expanded <dl>
// as one newline-joined blob — the files ARE the content of this tab (which
// lesson lives where), so they earn a row each, grouped under the project
// that owns them. Each file's row shows its name and the one-line
// `description` from its own YAML frontmatter (claude_config/memory.py parses
// it; falls back to nothing shown when the frontmatter is missing or
// malformed — the row's own filename already carries the fallback, so there
// is no separate "untitled" state to invent).
//
// Contents are Claude's to author — nothing here edits a memory file. What the
// UI adds is the lifecycle the files themselves can't express: which folders
// have uncommitted drift, and a Clear that deletes the .md files and commits
// the deletion (recoverable from History, which is why Clear is a confirm and
// not a two-step). Both live on the project GROUP, not on an individual file
// row, because they act on the whole folder.
//
// There used to be a per-project Commit button here too (round 2 removed it —
// the "N uncommitted" drift marker stays as the fact, but the *act* of
// committing belongs on History, the one page whose job is the whole repo's
// commit log; this page's job is showing what memory exists). Only the UI
// affordance and its api.ts wrapper (cc.memory.commit) went — the server's
// `memory` module still has a path-limited `commit` action; History's own
// commit path doesn't need it.
//
// A group is titled by the PROJECT FOLDER, not by the projects/ directory
// name: that name is a munged cwd ("-Users-me-Work-fused-render") and reads
// as nothing. The server resolves it — from a session transcript's recorded
// cwd, else against the filesystem — and sends null when it cannot, in which
// case this shows the slug rather than a path that might not exist. See
// claude_config/memory.py's _project_path for why a "-" -> "/" replace is not
// an option.
import { useCallback } from "react";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import * as cc from "../api";
import {
  Empty,
  Icon,
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
                {dirty > 0 && <span className="cc-change">{dirty} uncommitted</span>}
              </div>
              <div className="cc-memgroup-actions">
                <button
                  type="button"
                  className="cc-iconbtn"
                  title="Reveal in Finder"
                  aria-label={`Reveal the memory folder for ${p.path ?? p.project} in Finder`}
                  onClick={() => guard(cc.memory.open(p.project))}
                >
                  <Icon name="folder" />
                </button>
                <button type="button" className="btn btn-danger" onClick={() => clear(p.project)}>
                  Clear
                </button>
              </div>
            </div>
            <List>
              {p.files.map((f) => (
                <ListRow
                  key={f.name}
                  name={f.name}
                  nameMono
                  secondary={f.description ?? undefined}
                  secondaryTitle={f.description ?? undefined}
                />
              ))}
            </List>
          </div>
        );
      })}
    </>
  );
}

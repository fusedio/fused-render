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
// have uncommitted drift, a path-limited Commit per project, and a Clear that
// deletes the .md files and commits the deletion (recoverable from History,
// which is why Clear is a confirm and not a two-step). All three live on the
// project GROUP, not on an individual file row, because they act on the
// whole folder.
//
// Commit's history: round 2 first dropped it outright (argument: the drift
// marker is the fact, the *act* of committing belongs on History) — then the
// user asked for it back, smaller. It returns as a plain .cc-iconbtn beside
// Reveal and Clear rather than the old always-visible, always-enabled .btn,
// and it renders ONLY when this project actually has drift (`dirty > 0`)
// instead of sitting there disabled with no indication of what it would do —
// that ambiguity was the original complaint. Fading in on
// hover/focus-within like Clear (see .cc-memgroup-actions in
// claude-config.css); Reveal alone stays permanently visible, since it is
// informational rather than an act on the repo.
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
              {/* Reveal is informational and stays visible outright. Commit
                  and Clear are acts on the repo — Commit only exists to look
                  at when there is drift to act on, and Clear is the most
                  destructive thing on this page — so both fade in on
                  hover/focus-within (cc-memgroup-head:hover/:focus-within
                  below) rather than sitting there always, reachable by
                  keyboard and visible outright where hover doesn't apply.
                  Reveal being first is what the CSS rule keys off of. */}
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
                {dirty > 0 && (
                  <button
                    type="button"
                    className="cc-iconbtn"
                    title="Commit this project's memory folder"
                    aria-label={`Commit memory for ${p.path ?? p.project}`}
                    onClick={() => commit(p.project)}
                  >
                    <Icon name="check" />
                  </button>
                )}
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
              {p.files.map((f) => (
                <ListRow
                  key={f.name}
                  name={f.name}
                  nameMono
                  secondary={f.description ?? undefined}
                  secondaryClass="cc-lrow-sub-clamp2"
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

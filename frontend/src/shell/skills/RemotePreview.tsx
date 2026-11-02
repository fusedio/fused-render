// The right-hand preview of a skills.sh skill: the repo's README (or the
// SKILL.md when it has none; frontmatter stripped, it is metadata), a file
// count and size line, who would read it once added, and the
// buttons to add it or open its repo.
import { useEffect, useState } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { MarkdownView } from "@apps/claude/ui/MarkdownView";
import * as cc from "@apps/claude_config/api";
import { CLI_LABEL } from "@apps/claude_config/readers";
import { stripFrontmatter } from "@apps/claude_config/skill-detail";

/** "2.1 MB", "340 B". */
export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

type Loaded = { readme: string; readmeIsSkill: boolean; fileCount: number; totalBytes: number } | { error: string } | { tooLarge: true } | null;

export function RemotePreview({
  skill,
  added,
  busy,
  readers,
  onAdd,
}: {
  skill: cc.RemoteSkill;
  added: boolean;
  busy: boolean;
  /** The CLIs that will read the skill once it is added. */
  readers: cc.CliId[];
  onAdd: () => void;
}) {
  const [loaded, setLoaded] = useState<Loaded>(null);

  useEffect(() => {
    let alive = true;
    setLoaded(null);
    cc.skills.remotePreview(skill.source, skill.skillId).then(
      (r) => alive && setLoaded(r.tooLarge ? { tooLarge: true } : r.ok ? { readme: r.readme, readmeIsSkill: r.readmeIsSkill, fileCount: r.fileCount, totalBytes: r.totalBytes } : { error: r.error || "Could not read the skill" }),
      (e) => alive && setLoaded({ error: (e as Error).message }),
    );
    return () => {
      alive = false;
    };
  }, [skill.source, skill.skillId]);

  return (
    <aside className="cc-skill-aside" aria-label={`${skill.name} preview`} data-testid="remote-preview">
      <h2 className="cc-aside-title m-0">{skill.name}</h2>
      <p className="cc-aside-src m-0">{skill.source} · skills.sh</p>
      <div className="cc-aside-actions">
        {added ? (
          <span className="cc-lrow-meta">Added</span>
        ) : (
          <Button size="sm" disabled={busy} onClick={onAdd}>
            {busy ? "Adding…" : "Add skill"}
          </Button>
        )}
        <a
          className="cc-link"
          href={`https://github.com/${skill.source}`}
          target="_blank"
          rel="noreferrer"
        >
          View on GitHub ↗
        </a>
      </div>
      {loaded && "readme" in loaded && (
        <p className="cc-aside-label m-0" data-testid="remote-preview-meta">
          {loaded.fileCount === 1 ? "1 file" : `${loaded.fileCount} files`} · {formatBytes(loaded.totalBytes)}
        </p>
      )}
      {readers.length > 0 && (
        <div className="cc-aside-readers">
          <span className="cc-aside-label">Read by</span>
          {readers.map((r) => (
            <span key={r} className="cc-reader">
              {CLI_LABEL[r]}
            </span>
          ))}
        </div>
      )}
      {loaded === null && <p className="cc-skill-empty">Loading…</p>}
      {loaded && "tooLarge" in loaded && (
        <p className="cc-aside-label m-0" data-testid="remote-preview-toolarge">
          This repo is too large to preview here. Open it on GitHub to read it.
        </p>
      )}
      {loaded && "error" in loaded && <ErrorBanner>{loaded.error}</ErrorBanner>}
      {loaded && "readme" in loaded && (
        <>
          <p className="cc-aside-label m-0">{loaded.readmeIsSkill ? "SKILL.md" : `README · ${skill.source}`}</p>
          <div className="cc-skill-card cc-aside-body">
            <MarkdownView text={stripFrontmatter(loaded.readme)} enhance={false} />
          </div>
        </>
      )}
    </aside>
  );
}

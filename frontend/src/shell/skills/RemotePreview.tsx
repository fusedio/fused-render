// The right-hand preview of a skills.sh skill: its SKILL.md (frontmatter
// stripped, it is metadata), its files, who would read it once added, and the
// buttons to add it or open its repo.
import { useEffect, useState } from "react";
import { Button } from "@platform/shadcn/ui/button";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { MarkdownView } from "@apps/claude/ui/MarkdownView";
import * as cc from "@apps/claude_config/api";
import { CLI_LABEL } from "@apps/claude_config/readers";
import { stripFrontmatter } from "@apps/claude_config/skill-detail";

type Loaded = { text: string; files: string[] } | { error: string } | null;

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
      (r) => alive && setLoaded(r.ok ? { text: r.text, files: r.files } : { error: r.error || "Could not read the skill" }),
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
      {loaded === null && <p className="cc-skill-empty">Loading SKILL.md…</p>}
      {loaded && "error" in loaded && <ErrorBanner>{loaded.error}</ErrorBanner>}
      {loaded && "text" in loaded && (
        <>
          <div className="cc-skill-card cc-aside-body">
            <MarkdownView text={stripFrontmatter(loaded.text)} enhance={false} />
          </div>
          <p className="cc-aside-label m-0">{loaded.files.length === 1 ? "1 file" : `${loaded.files.length} files`}</p>
          <ul className="cc-aside-files m-0">
            {loaded.files.map((f) => (
              <li key={f} className="cc-mono">
                {f}
              </li>
            ))}
          </ul>
        </>
      )}
    </aside>
  );
}

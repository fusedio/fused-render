// /claude-artifacts: the Explorer homepage's "Artifacts" tab, promoted to a
// page of its own under the sidebar's CLAUDE group. Same data and the same
// card as the tab (GET /api/claude-sessions → FolderPreviewCard, one
// entry per project folder that holds Claude Code transcripts, newest first);
// the difference is that a page has room to list every folder instead of
// folding at the tab's MAX_CARDS, so there is no "Show more" here.
//
// The explorer's copy stays where it is — this is a second door onto the same
// list, for people who think of it as a Claude thing rather than a file thing.
import { useEffect, useState } from "react";
import { getClaudeSessionFolders, type ClaudeSessionFolder } from "@platform/lib/api";
import { FolderPreviewCard } from "@apps/explorer/BookmarkCards";

export default function ClaudeArtifacts() {
  // One cheap GET on mount — no client-side cache to reconcile, and the shell
  // remounts this page on every navigation anyway (App.tsx keys on nav epoch).
  const [folders, setFolders] = useState<ClaudeSessionFolder[] | null>(null);
  useEffect(() => {
    let alive = true;
    getClaudeSessionFolders().then(
      (r) => alive && setFolders(r.folders),
      () => alive && setFolders([]),
    );
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="cc-root">
      <main className="cc-main">
        <div className="cc-page-head">
          <div>
            <h2 className="cc-heading">Artifacts</h2>
            <div className="cc-caption cc-mono">
              project folders with Claude Code sessions
            </div>
          </div>
        </div>
        {folders === null ? (
          <p className="fh-empty">Looking for artifacts…</p>
        ) : folders.length ? (
          <div className="fhb-grid">
            {folders.map((f) => (
              <FolderPreviewCard key={f.path} path={f.path} />
            ))}
          </div>
        ) : (
          <p className="fh-empty">No Claude Code sessions found on this machine.</p>
        )}
      </main>
    </div>
  );
}

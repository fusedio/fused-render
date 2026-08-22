import { useEffect, useState } from "react";
import { shortCommit } from "./hub";
import { getAiModelRevisions, type AiModelRepo, type AiModelRevision } from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

// The revisions drawer: fetched per repo when a row is expanded, since
// resolving every snapshot symlink in every repo is exactly what the
// biggest-first overview avoids doing.
export function Revisions({
  repo,
  inUse,
  onDelete,
}: {
  repo: AiModelRepo;
  /** Why deleting from this repo is refused right now, or "" — the same
   *  sentence the repo's own Delete uses. A revision is not the safer target it
   *  looks like: the one a resident worker holds open is the one it is reading
   *  from, so both buttons answer to the same rule (AI-5f). */
  inUse: string;
  onDelete: (revision: AiModelRevision) => void;
}) {
  const [rows, setRows] = useState<AiModelRevision[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    // Dropped BEFORE the refetch, not replaced after it. These numbers are
    // relative to the other revisions: deleting one makes its siblings' shared
    // blobs exclusive, so every surviving row's "frees this much" grows the
    // moment a sibling goes. Holding the old rows through the refetch would
    // show understated sizes, and a delete clicked in that window would freeze
    // one into a confirmation that promised to free far less than it will.
    setRows(null);
    setError(null);
    getAiModelRevisions(repo.dir).then(
      (r) => alive && setRows(r.revisions),
      (e: Error) => alive && setError(e.message),
    );
    return () => {
      alive = false;
    };
    // Re-fetched when the repo's revision count changes under a deletion.
  }, [repo.dir, repo.revisions]);

  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!rows) return <div className="am-drawer-note">Reading revisions…</div>;
  if (!rows.length) return <div className="am-drawer-note">No revisions materialised.</div>;
  return (
    <div className="am-drawer">
      {rows.map((rev) => (
        <div className="am-rev" key={rev.commit}>
          <span className="am-rev-main">
            <span className="am-rev-commit cc-mono">{shortCommit(rev.commit)}</span>
            {rev.refs.map((ref) => (
              <span className="cc-pill" key={ref}>
                {ref}
              </span>
            ))}
            <span className="am-rev-meta">
              {rev.files} {rev.files === 1 ? "file" : "files"}
              {rev.shared ? ` · ${formatSize(rev.shared)} shared with other revisions` : ""}
            </span>
          </span>
          {/* The size that matters is what deleting THIS revision frees, not
              what it appears to contain — see the endpoint's docstring. */}
          <span className="am-rev-size" title="Freed by deleting this revision">
            {formatSize(rev.size)}
          </span>
          <button
            type="button"
            className="cc-iconbtn cc-iconbtn-danger"
            title={
              inUse
                ? `Cannot delete a revision of ${repo.id}: ${inUse}`
                : `Delete revision ${shortCommit(rev.commit)}`
            }
            aria-label={`Delete revision ${shortCommit(rev.commit)}`}
            disabled={!!inUse}
            onClick={() => onDelete(rev)}
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
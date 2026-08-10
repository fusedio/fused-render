// Preferences > Indexing — the file index's settings and manual controls.
//
// The index is what makes the explorer's in-folder search instant and
// cross-session; everything here is either "what does it skip" or "rebuild it
// now". It is deliberately a small surface: the index maintains itself (a scan
// on every startup, incremental after the first), so these are the escape
// hatches, not the normal path.
import { useEffect, useState } from "react";
import {
  deleteIndex,
  getIndexConfig,
  putIndexConfig,
  startIndexScan,
} from "@platform/lib/api";
import type { IndexConfig } from "@platform/lib/api";
import { useIndexStatus } from "@platform/lib/index-status";
import { formatMtimeFull } from "@platform/lib/format";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";

// The editor is a textarea, one pattern per line, because that IS the format:
// the server's own parser takes newline-separated text, comments and all
// (index/specs/scan-ignore.md §2). A row-per-chip widget would be a second,
// lossier representation of the same list.
function patternsToText(patterns: string[]): string {
  return patterns.join("\n");
}

function textToPatterns(text: string): string[] {
  return text.split("\n");
}

export function IndexingPanel() {
  const [config, setConfig] = useState<IndexConfig | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Bumped after every action so the status poll restarts immediately rather
  // than waiting for its next tick to notice a scan it just started.
  const [nonce, setNonce] = useState(0);
  const status = useIndexStatus(true, nonce);
  const scanning = !!status?.scanning;

  useEffect(() => {
    let alive = true;
    getIndexConfig().then(
      (c) => {
        if (!alive) return;
        setConfig(c);
        setText(patternsToText(c.ignore));
      },
      (e: Error) => alive && setError(e.message)
    );
    return () => {
      alive = false;
    };
  }, []);

  const act = async (what: () => Promise<string | null>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      setNote(await what());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      setNonce((n) => n + 1);
    }
  };

  const save = () =>
    act(async () => {
      const saved = await putIndexConfig({ ignore: textToPatterns(text) });
      setConfig(saved);
      setText(patternsToText(saved.ignore));
      // The engine fingerprints the rules an index was built under, so a
      // changed list leaves the store disagreeing with it until a scan
      // rebuilds. The server starts that scan on save; say so, because the
      // alternative is a user wondering why an excluded folder is still
      // showing up in search.
      return saved.needs_rescan
        ? "Saved. Rebuilding the index so the new rules apply."
        : "Saved.";
    });

  const restoreDefaults = () => {
    if (config) setText(patternsToText(config.defaults));
  };

  const dirty = config !== null && text !== patternsToText(config.ignore);

  return (
    <>
      <section className="prefs-section">
        <h2>File index</h2>
        <p className="deploy-muted">
          A local index of your files' names, sizes and dates — no file contents. It is what
          makes searching inside a folder instant instead of re-walking the tree, and it
          survives restarts. It is rebuilt in the background when the app starts;
          unchanged folders cost one check each, so that is usually a second or two.
        </p>
        {!status && <SkeletonLines rows={2} label="Loading index status" />}
        {status && (
          <p className="deploy-muted">
            {status.has_index ? (
              <>
                <b>{status.files_indexed.toLocaleString()} files</b> indexed
                {status.last_completed_at
                  ? `, last updated ${formatMtimeFull(status.last_completed_at)}`
                  : ""}
                .
              </>
            ) : (
              <b>No index yet.</b>
            )}{" "}
            {scanning
              ? `Scanning now — ${status.files.toLocaleString()} files so far${
                  status.root ? ` under ${status.root}` : ""
                }.`
              : config?.roots.length
                ? `Covers ${config.roots.join(", ")}.`
                : ""}
            {!status.has_index && !scanning
              ? " Searching a folder walks it live until one exists."
              : ""}
          </p>
        )}
        <div className="prefs-actions">
          <button
            type="button"
            disabled={busy || scanning}
            title="Check for changes since the last scan (fast — unchanged folders are skipped)"
            onClick={() =>
              act(async () => {
                await startIndexScan();
                return "Scan started.";
              })
            }
          >
            {scanning ? "Scanning…" : "Re-index"}
          </button>
          <button
            type="button"
            disabled={busy || scanning}
            title="Rebuild from scratch, ignoring what the last scan recorded — use this if results look wrong"
            onClick={() =>
              act(async () => {
                await startIndexScan({ full: true });
                return "Full rebuild started.";
              })
            }
          >
            Full scan
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={busy}
            title="Delete the index. Search keeps working — it falls back to walking the folder — until the next scan."
            onClick={() =>
              act(async () => {
                await deleteIndex();
                return "Index deleted. Search falls back to walking folders until the next scan.";
              })
            }
          >
            Delete index
          </button>
        </div>
        {note && <p className="deploy-muted">{note}</p>}
        {error && <ErrorBanner>{error}</ErrorBanner>}
      </section>

      <section className="prefs-section">
        <h2>Skipped folders</h2>
        <p className="deploy-muted">
          Folders the index never looks inside — dependency and build caches, which are huge
          and machine-generated. One rule per line. A bare name (<code>node_modules</code>)
          matches at any depth, <code>*.egg-info</code> matches a name pattern, and anything
          containing a slash (<code>~/Library/Caches</code>) matches that path and everything
          under it. Lines starting with <code>#</code> are comments.
        </p>
        <p className="deploy-muted">
          Remote mounts are never indexed and cannot be added here: reading them means network
          round-trips per folder, and a background crawl of one can break the mount.
        </p>
        {!config && !error && <SkeletonLines rows={4} label="Loading skip rules" />}
        {config && (
          <>
            <textarea
              className="prefs-textarea"
              rows={10}
              spellCheck={false}
              value={text}
              onChange={(e) => setText(e.target.value)}
              aria-label="Skipped folders, one rule per line"
            />
            <div className="prefs-actions">
              <button type="button" disabled={busy || !dirty} onClick={save}>
                Save
              </button>
              <button
                type="button"
                disabled={busy || text === patternsToText(config.defaults)}
                onClick={restoreDefaults}
              >
                Restore defaults
              </button>
            </div>
            <p className="deploy-muted">
              Changing these rules rebuilds the index, so folders you just excluded stop
              appearing in search and ones you re-included start appearing.
            </p>
            <p className="deploy-muted">
              Stored at <code>{config.location}</code>.
            </p>
          </>
        )}
      </section>
    </>
  );
}

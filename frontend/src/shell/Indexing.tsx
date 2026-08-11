// Preferences > Indexing — the file index's settings and manual controls.
//
// The index is what makes the explorer's in-folder search instant and
// cross-session; everything here is either "what does it skip" or "rebuild it
// now". It is deliberately a small surface: the index maintains itself (a scan
// on every startup, incremental after the first), so these are the escape
// hatches, not the normal path.
import { useEffect, useState } from "react";
import {
  askIndex,
  deleteIndex,
  getIndexConfig,
  putIndexConfig,
  runIndexQuery,
  startIndexScan,
} from "@platform/lib/api";
import type { IndexConfig } from "@platform/lib/api";
import type { IndexQueryOutcome } from "@platform/lib/index-query";
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

      <QuerySection />
    </>
  );
}

// Rows a query asks for. Enough to see a shape, short enough that the table
// stays scrollable rather than becoming the page; the server's own cap is far
// higher for a caller that means it.
const QUERY_LIMIT = 200;

const EXAMPLE_SQL =
  "SELECT ext, count(*) AS files, sum(size) AS bytes\nFROM files\nGROUP BY ext\nORDER BY files DESC\nLIMIT 20";

// Preferences > Indexing > Query — read-only SQL over the index.
//
// The index is two parquet tables and the interesting questions about it
// ("what is eating my disk", "what did I touch last week") are aggregate ones
// that no search box can express. Statements run confined: read-only, and unable
// to reach a path outside the index directory (index/specs/query.md §5). Ask
// mode sends the question to the AI relay instead and shows the SQL it compiled,
// which goes through exactly the same guard.
function QuerySection() {
  const [text, setText] = useState("");
  const [ask, setAsk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<IndexQueryOutcome | null>(null);

  const run = async () => {
    const body = text.trim();
    if (!body || busy) return;
    setBusy(true);
    // The previous result is dropped before the request, not after: leaving a
    // stale table under a running query reads as the answer to the new one.
    setOutcome(null);
    try {
      setOutcome(
        ask
          ? await askIndex({ prompt: body, limit: QUERY_LIMIT })
          : await runIndexQuery({ sql: body, limit: QUERY_LIMIT }),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="prefs-section">
      <h2>Query</h2>
      <p className="deploy-muted">
        Read-only SQL over the index. Two tables: <code>files</code>(path, dir, name, ext,
        size, mtime, depth) and <code>dirs</code>(dir, n_files, total_size, mtime_ns,
        n_subdirs, depth). <code>size</code> is bytes and <code>mtime</code> is epoch
        seconds. Nothing here can write, and nothing can read a file outside the index.
      </p>
      <label className="prefs-radio">
        <input type="checkbox" checked={ask} onChange={(e) => setAsk(e.target.checked)} />
        <span>
          <b>Ask in plain English.</b> The question goes to Claude, which writes the SQL;
          the statement it produced is shown with the results and runs under the same
          guard as one you typed.
        </span>
      </label>
      <textarea
        className="prefs-textarea index-query-input"
        rows={ask ? 3 : 6}
        spellCheck={false}
        value={text}
        placeholder={ask ? "Which folders are using the most space?" : EXAMPLE_SQL}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          // ⌘↵ / Ctrl+↵ runs, because Enter has to stay a newline in a
          // multi-line statement.
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void run();
          }
        }}
        aria-label={ask ? "Question about the index" : "SQL to run against the index"}
      />
      <div className="prefs-actions">
        <button type="button" disabled={busy || !text.trim()} onClick={() => void run()}>
          {busy ? "Running…" : ask ? "Ask" : "Run"}
        </button>
        <span className="deploy-muted">⌘↵</span>
      </div>
      {outcome?.sql && (
        <pre className="index-query-sql">
          <code>{outcome.sql}</code>
        </pre>
      )}
      {outcome && !outcome.ok && <ErrorBanner>{outcome.error}</ErrorBanner>}
      {outcome?.ok && <QueryTable outcome={outcome} />}
    </section>
  );
}

function QueryTable({ outcome }: { outcome: IndexQueryOutcome & { ok: true } }) {
  const { columns, rows, truncated } = outcome.table;
  if (rows.length === 0) {
    return <p className="deploy-muted">No rows.</p>;
  }
  return (
    <>
      <div className="index-query-results">
        <table>
          <thead>
            <tr>
              {columns.map((c, i) => (
                <th key={i}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j}>{cell}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="deploy-muted">
        {rows.length.toLocaleString()} {rows.length === 1 ? "row" : "rows"}
        {truncated ? ` — stopped at ${QUERY_LIMIT}; add a LIMIT or an aggregate.` : "."}
      </p>
    </>
  );
}

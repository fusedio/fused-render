// Preferences > Indexing — the file index's settings and manual controls.
//
// The index is what makes the explorer's in-folder search instant and
// cross-session; everything here is either "what does it skip" or "rebuild it
// now". It is deliberately a small surface: the index maintains itself (a scan
// on every startup, incremental after the first), so these are the escape
// hatches, not the normal path.
//
// Built from the shared form kit (`platform/ui/form`) — see that directory's
// README for the old class → component map.
import { useEffect, useState } from "react";
import {
  askIndex,
  deleteIndex,
  getIndexConfig,
  putIndexConfig,
  putIndexingEnabled,
  runIndexQuery,
  startIndexScan,
} from "@platform/lib/api";
import type { IndexConfig, Prefs } from "@platform/lib/api";
import type { IndexQueryOutcome } from "@platform/lib/index-query";
import { useIndexStatus } from "@platform/lib/index-status";
import { formatMtimeFull } from "@platform/lib/format";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Skeleton } from "@platform/shadcn/ui/skeleton";
import { Switch } from "@platform/shadcn/ui/switch";
import { Button } from "@platform/shadcn/ui/button";
import { TableHeader, TableBody, TableRow, TableHead, TableCell } from "@platform/shadcn/ui/table";
import {
  SettingsSection,
  ChoiceRow,
  MutedText,
  CodeChip,
  ActionRow,
  DataTable,
  dataTableHeadClass,
  dataTableCellClass,
  MonoTextarea,
  SqlReadout,
} from "@platform/ui/form";
import {
  missingDefaults,
  patternsToText,
  scanErrorLine,
  textToPatterns,
  unionWithDefaults,
} from "./indexing-lib";

// A few shimmer bars, standing in for a block of content while it loads.
function SkeletonLines({ rows = 3 }: { rows?: number }) {
  return (
    <div className="flex max-w-[420px] flex-col gap-2.5 py-1.5" role="status" aria-busy="true">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-2.5 motion-reduce:animate-none" style={{ width: `${[72, 54, 63][i % 3]}%` }} />
      ))}
    </div>
  );
}

// Same pattern as Preferences.tsx's ReaderToggle: local busy/error, a PUT
// that returns the full Prefs, and the parent re-renders from it. Kept here
// rather than in Preferences.tsx because it is entirely about indexing, and
// every other control on this panel already lives here.
function IndexingToggle({
  prefs,
  onChange,
}: {
  prefs: Prefs;
  onChange: (p: Prefs) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const enabled = prefs.indexing.enabled;

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      onChange(await putIndexingEnabled(!enabled));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsSection>
      <ChoiceRow control={<Switch checked={enabled} disabled={busy} onCheckedChange={toggle} />}>
        <b>Enable file indexing.</b> Turning this off stops all background scans — search
        falls back to slower live walks of the folder you're in, and the existing index
        keeps answering until it goes stale.
      </ChoiceRow>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingsSection>
  );
}

export function IndexingPanel({
  prefs,
  onChange,
}: {
  prefs: Prefs;
  onChange: (p: Prefs) => void;
}) {
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

  // Union, not replace: a saved config predating a `defaults` addition (e.g.
  // the `dist`/`build`/`out` group, D656) is missing patterns the user never
  // chose to exclude — they just never had the chance to. Replacing would
  // also discard whatever the user has added of their own, which is exactly
  // why a user in that position would rightly not press this button.
  const restoreDefaults = () => {
    if (config) setText(unionWithDefaults(text, config.defaults));
  };

  const dirty = config !== null && text !== patternsToText(config.ignore);
  const stale = config !== null ? missingDefaults(config.ignore, config.defaults) : [];
  const indexingOff = !prefs.indexing.enabled;

  return (
    <>
      <IndexingToggle prefs={prefs} onChange={onChange} />
      <SettingsSection title="File index">
        <MutedText>
          A local index of your files' names, sizes and dates — no file contents. It is what
          makes searching inside a folder instant instead of re-walking the tree, and it
          survives restarts. It is rebuilt in the background when the app starts;
          unchanged folders cost one check each, so that is usually a second or two.
        </MutedText>
        {!status && <SkeletonLines rows={2} />}
        {status && (
          <MutedText>
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
          </MutedText>
        )}
        {/* How the last scan ENDED, when it ended badly. Nothing used to show
            this anywhere in the app: a worker that dies without a `run_end`
            (killed, OOM, a spawn that never reached Python) reads as
            `scanning` for ABANDONED_RUN_S and then simply stops, leaving "No
            index yet" with nothing running and no reason given — which is how
            a first-run failure can be waited out for twenty minutes. Only
            while idle: mid-scan this is the PREVIOUS run's verdict, and the
            counts above are the live story. */}
        {status && status.error && !scanning && (
          <ErrorBanner>
            The last scan did not finish: {scanErrorLine(status.error)}
          </ErrorBanner>
        )}
        <ActionRow>
          <Button
            type="button"
            variant="secondary"
            disabled={busy || scanning || indexingOff}
            title={
              indexingOff
                ? "Indexing is off — turn it back on above to scan"
                : "Check for changes since the last scan (fast — unchanged folders are skipped)"
            }
            onClick={() =>
              act(async () => {
                await startIndexScan();
                return "Scan started.";
              })
            }
          >
            {scanning ? "Scanning…" : "Re-index"}
          </Button>
          <Button
            type="button"
            variant="secondary"
            disabled={busy || scanning || indexingOff}
            title={
              indexingOff
                ? "Indexing is off — turn it back on above to scan"
                : "Rebuild from scratch, ignoring what the last scan recorded — use this if results look wrong"
            }
            onClick={() =>
              act(async () => {
                await startIndexScan({ full: true });
                return "Full rebuild started.";
              })
            }
          >
            Full scan
          </Button>
          <Button
            type="button"
            variant="destructive"
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
          </Button>
        </ActionRow>
        {indexingOff && (
          <MutedText>
            Indexing is off, so Re-index and Full scan have nothing to do — turn it back on
            above first.
          </MutedText>
        )}
        {note && <MutedText>{note}</MutedText>}
        {error && <ErrorBanner>{error}</ErrorBanner>}
      </SettingsSection>

      <SettingsSection title="Skipped folders">
        <MutedText>
          Folders the index never looks inside — dependency and build caches, which are huge
          and machine-generated. One rule per line. A bare name (<CodeChip>node_modules</CodeChip>)
          matches at any depth, <CodeChip>*.egg-info</CodeChip> matches a name pattern, and
          anything containing a slash (<CodeChip>~/Library/Caches</CodeChip>) matches that path
          and everything under it. Lines starting with <CodeChip>#</CodeChip> are comments.
        </MutedText>
        <MutedText>
          Remote mounts are never indexed and cannot be added here: reading them means network
          round-trips per folder, and a background crawl of one can break the mount.
        </MutedText>
        {!config && !error && <SkeletonLines rows={4} />}
        {config && (
          <>
            <MonoTextarea
              rows={10}
              spellCheck={false}
              value={text}
              onChange={(e) => setText(e.target.value)}
              aria-label="Skipped folders, one rule per line"
            />
            <ActionRow>
              <Button type="button" variant="secondary" disabled={busy || !dirty} onClick={save}>
                Save
              </Button>
              <Button
                type="button"
                variant="secondary"
                disabled={busy || unionWithDefaults(text, config.defaults) === text}
                onClick={restoreDefaults}
              >
                Restore defaults
              </Button>
            </ActionRow>
            {stale.length > 0 && (
              <MutedText>
                New skip rules are available: {stale.join(", ")}. Restoring defaults adds
                them; your own entries are kept.
              </MutedText>
            )}
            <MutedText>
              Changing these rules rebuilds the index, so folders you just excluded stop
              appearing in search and ones you re-included start appearing.
            </MutedText>
            <MutedText>
              Stored at <CodeChip>{config.location}</CodeChip>.
            </MutedText>
          </>
        )}
      </SettingsSection>

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
    <SettingsSection title="Query">
      <MutedText>
        Read-only SQL over the index. Two tables: <CodeChip>files</CodeChip>(path, dir, name,
        ext, size, mtime, depth) and <CodeChip>dirs</CodeChip>(dir, n_files, total_size,
        mtime_ns, n_subdirs, depth). <CodeChip>size</CodeChip> is bytes and{" "}
        <CodeChip>mtime</CodeChip> is epoch seconds. Nothing here can write, and nothing can
        read a file outside the index.
      </MutedText>
      <ChoiceRow control={<Switch checked={ask} onCheckedChange={(v) => setAsk(v)} />}>
        <b>Ask in plain English.</b> The question goes to Claude, which writes the SQL;
        the statement it produced is shown with the results and runs under the same
        guard as one you typed.
      </ChoiceRow>
      <MonoTextarea
        noWrap
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
      <ActionRow>
        <Button type="button" variant="secondary" disabled={busy || !text.trim()} onClick={() => void run()}>
          {busy ? "Running…" : ask ? "Ask" : "Run"}
        </Button>
        <MutedText as="span">⌘↵</MutedText>
      </ActionRow>
      {outcome?.sql && <SqlReadout>{outcome.sql}</SqlReadout>}
      {outcome && !outcome.ok && <ErrorBanner>{outcome.error}</ErrorBanner>}
      {outcome?.ok && <QueryTable outcome={outcome} />}
    </SettingsSection>
  );
}

function QueryTable({ outcome }: { outcome: IndexQueryOutcome & { ok: true } }) {
  const { columns, rows, truncated } = outcome.table;
  if (rows.length === 0) {
    return <MutedText>No rows.</MutedText>;
  }
  return (
    <>
      <DataTable>
        <TableHeader>
          <TableRow>
            {columns.map((c, i) => (
              <TableHead key={i} className={dataTableHeadClass}>
                {c}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row, i) => (
            <TableRow key={i}>
              {row.map((cell, j) => (
                <TableCell key={j} className={dataTableCellClass}>
                  {cell}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </DataTable>
      <MutedText>
        {rows.length.toLocaleString()} {rows.length === 1 ? "row" : "rows"}
        {truncated ? ` — stopped at ${QUERY_LIMIT}; add a LIMIT or an aggregate.` : "."}
      </MutedText>
    </>
  );
}

// Preferences > Indexing — the file index's settings and manual controls.
//
// The index is what makes the explorer's in-folder search instant and
// cross-session; everything here is either "what does it skip" or "rebuild it
// now". It is deliberately a small surface: the index maintains itself (a scan
// on every startup, incremental after the first), so these are the escape
// hatches, not the normal path.
//
// Only consumer: shell/Preferences.tsx, whose row vocabulary
// (shell/prefs/SettingRow.tsx) this panel shares.
import { useEffect, useId, useState } from "react";
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
import { SkeletonLines } from "@platform/ui/Skeleton";
import { Button } from "@platform/shadcn/ui/button";
import { Kbd } from "@platform/shadcn/ui/kbd";
import { Switch } from "@platform/shadcn/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@platform/shadcn/ui/table";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { Muted } from "@platform/ui/flow/Typography";
import { Code, SettingRow, SettingRows, SettingsSection } from "@shell/prefs/SettingRow";

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

// Same pattern as Preferences.tsx's SwitchRow: local busy/error, a PUT
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
  const id = useId();

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
    <SettingRow
      label="Enable file indexing"
      controlId={id}
      description="Turning this off stops all background scans — search falls back to slower live walks of the folder you're in, and the existing index keeps answering until it goes stale."
      note={error && <ErrorBanner>{error}</ErrorBanner>}
    >
      <Switch id={id} checked={enabled} disabled={busy} onCheckedChange={() => void toggle()} />
    </SettingRow>
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

  const restoreDefaults = () => {
    if (config) setText(patternsToText(config.defaults));
  };

  const dirty = config !== null && text !== patternsToText(config.ignore);
  const indexingOff = !prefs.indexing.enabled;
  const scanTitle = indexingOff ? "Indexing is off — turn it back on above to scan" : undefined;

  return (
    <>
      <SettingsSection
        title="File index"
        description="A local index of your files' names, sizes and dates — no file contents. It is what makes searching inside a folder instant instead of re-walking the tree, and it survives restarts. It is rebuilt in the background when the app starts; unchanged folders cost one check each, so that is usually a second or two."
      >
        <SettingRows>
          <IndexingToggle prefs={prefs} onChange={onChange} />
          <SettingRow
            label="Index"
            description={
              !status ? (
                <SkeletonLines rows={1} label="Loading index status" />
              ) : (
                <>
                  {status.has_index ? (
                    <>
                      <b>{status.files_indexed.toLocaleString()} files</b> indexed
                      {status.last_completed_at ? `, last updated ${formatMtimeFull(status.last_completed_at)}` : ""}.
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
                  {!status.has_index && !scanning ? " Searching a folder walks it live until one exists." : ""}
                </>
              )
            }
            note={
              <>
                {indexingOff && (
                  <>Indexing is off, so Re-index and Full scan have nothing to do — turn it back on above first.</>
                )}
                {note && <span className="block">{note}</span>}
                {error && <ErrorBanner>{error}</ErrorBanner>}
              </>
            }
          >
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={busy || scanning || indexingOff}
              title={scanTitle ?? "Check for changes since the last scan (fast — unchanged folders are skipped)"}
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
              variant="outline"
              size="sm"
              disabled={busy || scanning || indexingOff}
              title={
                scanTitle ??
                "Rebuild from scratch, ignoring what the last scan recorded — use this if results look wrong"
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
              size="sm"
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
          </SettingRow>
        </SettingRows>
      </SettingsSection>

      <SettingsSection
        title="Skipped folders"
        description={
          <>
            Folders the index never looks inside — dependency and build caches, which are huge and
            machine-generated. One rule per line. A bare name (<Code>node_modules</Code>) matches at any depth,{" "}
            <Code>*.egg-info</Code> matches a name pattern, and anything containing a slash (
            <Code>~/Library/Caches</Code>) matches that path and everything under it. Lines starting with{" "}
            <Code>#</Code> are comments. Remote mounts are never indexed and cannot be added here: reading them
            means network round-trips per folder, and a background crawl of one can break the mount.
          </>
        }
      >
        {!config && !error && <SkeletonLines rows={4} label="Loading skip rules" />}
        {config && (
          <div className="space-y-2">
            <Textarea
              className="font-mono text-xs min-h-48"
              rows={10}
              spellCheck={false}
              value={text}
              onChange={(e) => setText(e.target.value)}
              aria-label="Skipped folders, one rule per line"
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" size="sm" disabled={busy || !dirty} onClick={save}>
                Save
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={busy || text === patternsToText(config.defaults)}
                onClick={restoreDefaults}
              >
                Restore defaults
              </Button>
            </div>
            <Muted className="text-xs">
              Changing these rules rebuilds the index, so folders you just excluded stop appearing in search and
              ones you re-included start appearing. Stored at <Code>{config.location}</Code>.
            </Muted>
          </div>
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
  const askId = useId();

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
    <SettingsSection
      title="Query"
      description={
        <>
          Read-only SQL over the index. Two tables: <Code>files</Code>(path, dir, name, ext, size, mtime, depth)
          and <Code>dirs</Code>(dir, n_files, total_size, mtime_ns, n_subdirs, depth). <Code>size</Code> is
          bytes and <Code>mtime</Code> is epoch seconds. Nothing here can write, and nothing can read a file
          outside the index.
        </>
      }
    >
      <SettingRows>
        <SettingRow
          label="Ask in plain English"
          controlId={askId}
          description="The question goes to Claude, which writes the SQL; the statement it produced is shown with the results and runs under the same guard as one you typed."
        >
          <Switch id={askId} checked={ask} onCheckedChange={(c) => setAsk(c)} />
        </SettingRow>
      </SettingRows>
      <div className="space-y-2">
        <Textarea
          className={ask ? "min-h-20" : "font-mono text-xs min-h-32"}
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
        <div className="flex items-center gap-2">
          <Button type="button" size="sm" disabled={busy || !text.trim()} onClick={() => void run()}>
            {busy ? "Running…" : ask ? "Ask" : "Run"}
          </Button>
          <Kbd>⌘↵</Kbd>
        </div>
      </div>
      {outcome?.sql && (
        <pre className="border border-border rounded-lg bg-card p-3 font-mono text-xs overflow-x-auto">
          <code>{outcome.sql}</code>
        </pre>
      )}
      {outcome && !outcome.ok && <ErrorBanner>{outcome.error}</ErrorBanner>}
      {outcome?.ok && <QueryTable outcome={outcome} />}
    </SettingsSection>
  );
}

function QueryTable({ outcome }: { outcome: IndexQueryOutcome & { ok: true } }) {
  const { columns, rows, truncated } = outcome.table;
  if (rows.length === 0) {
    return <Muted>No rows.</Muted>;
  }
  return (
    <div className="space-y-2">
      <div className="border border-border rounded-lg bg-card max-h-96 overflow-auto scrollbar-auto-hide">
        <Table className="font-mono text-xs">
          <TableHeader>
            <TableRow>
              {columns.map((c, i) => (
                <TableHead key={i} className="whitespace-nowrap">
                  {c}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row, i) => (
              <TableRow key={i}>
                {row.map((cell, j) => (
                  <TableCell key={j} className="whitespace-nowrap tabular-nums">
                    {cell}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <Muted className="text-xs">
        {rows.length.toLocaleString()} {rows.length === 1 ? "row" : "rows"}
        {truncated ? ` — stopped at ${QUERY_LIMIT}; add a LIMIT or an aggregate.` : "."}
      </Muted>
    </div>
  );
}

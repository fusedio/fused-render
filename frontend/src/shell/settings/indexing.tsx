// File index: the file index's switches and its escape hatches.
//
// Once on, the index maintains itself (a scan on every startup, incremental
// after the first; on by default only in the desktop app), so the rows here are "is it on", "how does it rank", "what does it
// skip" and "rebuild it now" — not a normal path anybody walks daily.
//
// The status poll and the skip-rules config are SHARED by the rows through
// `IndexStateProvider`, and armed only while at least one row that needs them
// is mounted: a reader on General costs no index polling, and the rows in a
// search result agree with each other because they read the same poll.
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import {
  deleteIndex,
  getIndexConfig,
  putIndexConfig,
  putIndexingEnabled,
  putIndexLowImpactEnabled,
  putRankedSearchEnabled,
  startIndexScan,
  type IndexConfig,
  type IndexStatus,
} from "@platform/lib/api";
import { publishRankedSearchEnabled } from "@apps/explorer/lib/ranked-search-pref";
import { useIndexStatus } from "@platform/lib/index-status";
import { useFda } from "@platform/lib/fda";
import { INDEXING_FDA_COPY, IndexFdaCta } from "@apps/explorer/IndexFdaCta";
import { formatMtimeFull } from "@platform/lib/format";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { Button } from "@platform/shadcn/ui/button";
import { Textarea } from "@platform/shadcn/ui/textarea";
import {
  isFdaRefusal,
  missingDefaults,
  patternsToText,
  scanErrorLine,
  scanningLine,
  textToPatterns,
  unionWithDefaults,
} from "../indexing-lib";
import { PrefToggleRow, SettingRow, type PrefControlProps } from "./SettingRow";

interface IndexState {
  status: IndexStatus | null;
  config: IndexConfig | null;
  configError: string | null;
  setConfig: (c: IndexConfig) => void;
  /** Restart the status poll now — after an action that started a scan, so
   *  "Scanning…" shows without waiting for the next idle tick. */
  bump: () => void;
  /** Mount bookkeeping: the poll runs while this is above zero. */
  claim: () => () => void;
}

const IndexStateContext = createContext<IndexState | null>(null);

export function IndexStateProvider({ children }: { children: ReactNode }) {
  const [users, setUsers] = useState(0);
  const [nonce, setNonce] = useState(0);
  const [config, setConfig] = useState<IndexConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const active = users > 0;
  const status = useIndexStatus(active, nonce);

  useEffect(() => {
    if (!active || config) return;
    let alive = true;
    getIndexConfig().then(
      (c) => alive && setConfig(c),
      (e: Error) => alive && setConfigError(e.message),
    );
    return () => {
      alive = false;
    };
  }, [active, config]);

  const value: IndexState = {
    status,
    config,
    configError,
    setConfig,
    bump: () => setNonce((n) => n + 1),
    claim: () => {
      setUsers((n) => n + 1);
      return () => setUsers((n) => n - 1);
    },
  };
  return <IndexStateContext.Provider value={value}>{children}</IndexStateContext.Provider>;
}

function useIndexState(): IndexState {
  const state = useContext(IndexStateContext)!;
  const { claim } = state;
  // Claimed once per mount; `claim` is a fresh closure each render.
  useEffect(() => claim(), []);
  return state;
}

/** The packaged mac app without Full Disk Access refuses every scan
 *  (shell/index_gate.py). Mirrored here so the card is up before the user
 *  presses anything, not only after the 409. */
function useFdaBlocked(): boolean {
  const fda = useFda();
  return !!fda && !fda.granted;
}

function StatusLine({ status, config }: { status: IndexStatus; config: IndexConfig | null }) {
  const scanning = !!status.scanning;
  return (
    <>
      {status.has_index ? (
        <>
          <b>{status.files_indexed.toLocaleString()} files</b> indexed
          {status.last_completed_at ? `, last updated ${formatMtimeFull(status.last_completed_at)}` : ""}.
        </>
      ) : (
        <b>No index yet.</b>
      )}{" "}
      {scanning ? scanningLine(status) : config?.roots.length ? `Covers ${config.roots.join(", ")}.` : ""}
      {!status.has_index && !scanning ? " Searching a folder walks it live until one exists." : ""}
    </>
  );
}

export function IndexingControl({ prefs, onChange }: PrefControlProps) {
  const { status, config } = useIndexState();
  const fdaBlocked = useFdaBlocked();
  const enabled = prefs.indexing.enabled;
  return (
    <PrefToggleRow
      id="indexing"
      checked={enabled}
      put={putIndexingEnabled}
      status={status ? <StatusLine status={status} config={config} /> : <SkeletonLines rows={1} label="Loading index status" />}
      onChange={onChange}
    >
      {/* How the last scan ENDED, when it ended badly — only while idle: mid-
          scan this is the previous run's verdict, and the line above is the
          live story. Without it a worker that died before `run_end` read as
          "No index yet" with nothing running and no reason given. */}
      {status?.error && !status.scanning && (
        <ErrorBanner>The last scan did not finish: {scanErrorLine(status.error)}</ErrorBanner>
      )}
      {fdaBlocked && enabled && <IndexFdaCta copy={INDEXING_FDA_COPY} />}
    </PrefToggleRow>
  );
}

// The explorer's two search boxes read this through their own module cache
// (ranked-search-pref.ts), so the new value is published as well as saved —
// a query fired right after the switch settles must not race a GET.
export function RankedSearchControl({ prefs, onChange }: PrefControlProps) {
  return (
    <PrefToggleRow
      id="ranked-search"
      checked={prefs.indexing.ranked}
      put={putRankedSearchEnabled}
      after={(next) => publishRankedSearchEnabled(next.indexing.ranked)}
      onChange={onChange}
    />
  );
}

// Greyed out while indexing itself is off: there is no background work to quiet.
export function LowImpactControl({ prefs, onChange }: PrefControlProps) {
  const off = !prefs.indexing.enabled;
  return (
    <PrefToggleRow
      id="low-impact"
      checked={prefs.indexing.low_impact ?? false}
      put={putIndexLowImpactEnabled}
      disabled={off}
      off={off}
      onChange={onChange}
    />
  );
}

export function SkippedFoldersControl() {
  const { config, configError, setConfig, bump } = useIndexState();
  const fdaBlocked = useFdaBlocked();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const saved = config ? patternsToText(config.ignore) : "";
  const draft = text ?? saved;
  const dirty = config !== null && draft !== saved;
  const stale = config ? missingDefaults(config.ignore, config.defaults) : [];

  const save = async () => {
    if (!config || busy) return;
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const next = await putIndexConfig({ ignore: textToPatterns(draft) });
      setConfig(next);
      setText(null);
      // The server starts the rebuild on save and swallows a refusal, so
      // `needs_rescan` alone would promise a rebuild that never starts while
      // the FDA gate is shut. Say what actually happens.
      setNote(
        next.needs_rescan && fdaBlocked
          ? "Saved. The index rebuilds once Full Disk Access is granted and FusedRender relaunches."
          : next.needs_rescan
            ? "Saved. Rebuilding the index so the new rules apply."
            : "Saved.",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      bump();
    }
  };

  return (
    <SettingRow
      id="skipped-folders"
      status={stale.length > 0 ? `New skip rules are available: ${stale.join(", ")}.` : undefined}
      control={
        <Button variant="outline" size="sm" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Close" : "Edit…"}
        </Button>
      }
    >
      {open && (
        <div className="settings-editor">
          <p className="settings-row-desc">
            One rule per line. A bare name (<code>node_modules</code>) matches at any depth,{" "}
            <code>*.egg-info</code> matches a name pattern, and anything containing a slash (
            <code>~/Library/Caches</code>) matches that path and everything under it. Lines starting with{" "}
            <code>#</code> are comments. The fused-render data folder is never indexed.
          </p>
          {!config && !configError && <SkeletonLines rows={4} label="Loading skip rules" />}
          {configError && <ErrorBanner>{configError}</ErrorBanner>}
          {config && (
            <>
              <Textarea
                className="settings-mono"
                rows={10}
                spellCheck={false}
                value={draft}
                onChange={(e) => setText(e.target.value)}
                aria-label="Skipped folders, one rule per line"
              />
              <div className="settings-actions">
                <Button size="sm" variant="accent" disabled={busy || !dirty} onClick={() => void save()}>
                  Save
                </Button>
                {/* Union, not replace: a saved list predating a `defaults`
                    addition is missing patterns the user never chose to drop,
                    and replacing would discard their own entries too. */}
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy || unionWithDefaults(draft, config.defaults) === draft}
                  onClick={() => setText(unionWithDefaults(draft, config.defaults))}
                >
                  Restore defaults
                </Button>
              </div>
              <p className="settings-row-desc">
                Saving rebuilds the index, so excluded folders leave search and re-included ones come back. Stored at{" "}
                <code>{config.location}</code>.
              </p>
            </>
          )}
          {note && <p className="settings-row-desc">{note}</p>}
          {error && <ErrorBanner>{error}</ErrorBanner>}
        </div>
      )}
    </SettingRow>
  );
}

export function RebuildIndexControl({ prefs }: PrefControlProps) {
  const { status, bump } = useIndexState();
  const fdaBlocked = useFdaBlocked();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const scanning = !!status?.scanning;
  const indexingOff = !prefs.indexing.enabled;
  // FDA outranks "off": with no access, turning indexing on still gets no scan.
  // Worded without "above": in search results this row can be alone.
  const blocked = indexingOff
    ? "Indexing is off — turn on File indexing to scan"
    : fdaBlocked
      ? "Indexing needs Full Disk Access first"
      : null;

  const act = async (what: () => Promise<string>) => {
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
      bump();
    }
  };

  return (
    <SettingRow
      id="rebuild-index"
      status={indexingOff ? "Indexing is off, so Re-index and Full scan have nothing to do." : undefined}
      control={
        <>
          <Button
            variant="outline"
            size="sm"
            disabled={busy || scanning || !!blocked}
            title={blocked ?? "Check for changes since the last scan (fast — unchanged folders are skipped)"}
            onClick={() => void act(async () => (await startIndexScan(), "Scan started."))}
          >
            {scanning ? "Scanning…" : "Re-index"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={busy || scanning || !!blocked}
            title={blocked ?? "Rebuild from scratch, ignoring what the last scan recorded — use this if results look wrong"}
            onClick={() => void act(async () => (await startIndexScan({ full: true }), "Full rebuild started."))}
          >
            Full scan
          </Button>
          <Button
            variant="destructive"
            size="sm"
            disabled={busy}
            title="Delete the index. Search keeps working — it walks the folder — until the next scan."
            onClick={() =>
              void act(
                async () => (await deleteIndex(), "Index deleted. Search falls back to walking folders until the next scan."),
              )
            }
          >
            Delete index
          </Button>
        </>
      }
    >
      {note && <p className="settings-row-desc">{note}</p>}
      {/* The FDA refusal is answered with the card that fixes it (grant, or
          relaunch), not a banner: a wall with no door is what it replaced. The
          File indexing row shows the same card up front; this one is for a
          press made here, which in search results may be the only row shown. */}
      {error && fdaBlocked && isFdaRefusal(error) ? (
        <IndexFdaCta copy={INDEXING_FDA_COPY} />
      ) : (
        error && <ErrorBanner>{error}</ErrorBanner>
      )}
    </SettingRow>
  );
}

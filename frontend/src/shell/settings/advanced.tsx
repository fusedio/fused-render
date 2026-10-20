// Advanced: the call log, the index query console, diagnostics.
import { useEffect, useState } from "react";
import {
  askIndex,
  buildDiagnostics,
  fetchDiagnosticsPlan,
  fetchServerHealth,
  putCallsEnabled,
  putCallsParamsMode,
  putCallsRetentionDays,
  putClaudeMaxConcurrentSessions,
  runIndexQuery,
  type CallsParamsMode,
  type DiagnosticsPlan,
  type DiagnosticsResult,
} from "@platform/lib/api";
import type { IndexQueryOutcome } from "@platform/lib/index-query";
import { copyToClipboard } from "@platform/lib/clipboard";
import { formatBytes } from "@platform/lib/sysmon";
import { chordLabel, ENTER_LABEL, isMac, MOD_LABEL } from "@platform/lib/platform";
import { navigate } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Button } from "@platform/shadcn/ui/button";
import { Switch } from "@platform/shadcn/ui/switch";
import { Textarea } from "@platform/shadcn/ui/textarea";
import { RowSwitch, SettingRow, SettingSelect, usePrefWrite, type PrefControlProps } from "./SettingRow";

// ── Call log (fused_render/calls.py) ──────────────────────────────────────
// The switch shows the STORED pref and the status line what is actually in
// force. They diverge whenever FUSED_RENDER_CALLS wins, and the switch is
// disabled then so the discrepancy cannot be acted on. A non-null
// `*_forced_by` is the server's assertion that the variable is IN FORCE, not
// merely set — never re-derived here from the value's shape.

export function CallLogControl({ prefs, onChange }: PrefControlProps) {
  const { busy, error, run } = usePrefWrite(onChange);
  const calls = prefs.calls;
  const locked = calls.enabled_forced_by !== null;
  return (
    <SettingRow
      id="call-log"
      status={
        <>
          Currently <b>{calls.effective_enabled ? "recording" : "not recording"}</b>
          {locked && (
            <>
              {" "}
              — locked by <code>FUSED_RENDER_CALLS={calls.enabled_forced_by}</code> for this process; the switch
              applies once the variable is removed
            </>
          )}
          . Stored at <code>{calls.dir}</code>
          {calls.dir_exists ? "." : " (nothing recorded yet)."}
        </>
      }
      control={
        <>
          {/* In-app, not the OS file manager: open the folder, click a
              .calls.jsonl, and it renders in log_studio. Disabled until the
              writer has created the store on its first append. */}
          <Button
            variant="outline"
            size="sm"
            disabled={!calls.dir_exists}
            title={calls.dir_exists ? undefined : "No calls have been recorded yet"}
            onClick={() => navigate(calls.dir, { isDir: true })}
          >
            Browse
          </Button>
          <RowSwitch
            id="call-log"
            checked={calls.enabled}
            disabled={busy || locked}
            onChange={(v) => void run(() => putCallsEnabled(v))}
          />
        </>
      }
    >
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

// Gated on what is actually recording, not on the stored pref — otherwise an
// env-forced off leaves these live, and an env-forced on greys them out while
// calls are landing.
export function CallParamsControl({ prefs, onChange }: PrefControlProps) {
  const { busy, error, run } = usePrefWrite(onChange);
  const calls = prefs.calls;
  return (
    <SettingRow
      id="call-params"
      off={!calls.effective_enabled}
      control={
        <SettingSelect
          id="call-params"
          value={calls.params}
          options={[
            { key: "full", label: "Record values" },
            { key: "keys", label: "Names only" },
            { key: "off", label: "Record nothing" },
          ]}
          disabled={busy || !calls.effective_enabled}
          onChange={(v) => void run(() => putCallsParamsMode(v as CallsParamsMode))}
        />
      }
    >
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

// Tasks past this many running Claude sessions wait in the queue. 0 = no cap.
export function ClaudeMaxSessionsControl({ prefs, onChange }: PrefControlProps) {
  const { busy, error, run } = usePrefWrite(onChange);
  const current = prefs.claude_sessions?.max_concurrent ?? 10;
  const choices = [4, 6, 8, 10, 12, 16];
  // A value set by hand in prefs.json is still the truth: show it, ordered in.
  if (current > 0 && !choices.includes(current)) {
    choices.push(current);
    choices.sort((a, b) => a - b);
  }
  return (
    <SettingRow
      id="claude-max-sessions"
      control={
        <SettingSelect
          id="claude-max-sessions"
          value={String(current)}
          options={[
            ...choices.map((n) => ({ key: String(n), label: String(n) })),
            { key: "0", label: "Unlimited" },
          ]}
          disabled={busy}
          onChange={(v) => void run(() => putClaudeMaxConcurrentSessions(Number(v)))}
        />
      }
    >
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

function describeRetention(days: number): string {
  if (days === 0) return "until the size cap";
  return days === 1 ? "1 day" : `${days} days`;
}

export function CallRetentionControl({ prefs, onChange }: PrefControlProps) {
  const { busy, error, run } = usePrefWrite(onChange);
  const calls = prefs.calls;
  const locked = calls.retention_forced_by !== null;
  return (
    <SettingRow
      id="call-retention"
      off={!calls.effective_enabled}
      status={
        locked ? (
          <>
            Currently keeping <b>{describeRetention(calls.effective_retention_days)}</b> — locked by{" "}
            <code>FUSED_RENDER_CALLS_RETENTION_DAYS={calls.retention_forced_by}</code> for this process.
          </>
        ) : undefined
      }
      control={
        <SettingSelect
          id="call-retention"
          value={String(calls.retention_days)}
          options={[
            { key: "1", label: "1 day" },
            { key: "7", label: "7 days" },
            { key: "14", label: "14 days" },
            { key: "90", label: "90 days" },
            { key: "0", label: "Until the size cap" },
          ]}
          disabled={busy || !calls.effective_enabled || locked}
          onChange={(v) => void run(() => putCallsRetentionDays(Number(v)))}
        />
      }
    >
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

// ── Index query ────────────────────────────────────────────────────────────
// Read-only SQL over the index's two parquet tables: the aggregate questions
// ("what is eating my disk") no search box can express. Statements run
// confined — read-only, unable to reach a path outside the index directory
// (index/specs/query.md §5). Ask mode sends the question to the AI relay and
// shows the SQL it compiled, which goes through the same guard. It was the
// last section of the Indexing tab; it is a tool, not a setting, so it opens
// on demand.

// Rows a query asks for: enough to see a shape, short enough that the table
// stays a table rather than becoming the page.
const QUERY_LIMIT = 200;

const EXAMPLE_SQL =
  "SELECT ext, count(*) AS files, sum(size) AS bytes\nFROM files\nGROUP BY ext\nORDER BY files DESC\nLIMIT 20";

export function IndexQueryControl() {
  const [open, setOpen] = useState(false);
  return (
    <SettingRow
      id="index-query"
      control={
        <Button variant="outline" size="sm" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Close" : "Open console"}
        </Button>
      }
    >
      {open && <QueryConsole />}
    </SettingRow>
  );
}

function QueryConsole() {
  const [text, setText] = useState("");
  const [ask, setAsk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<IndexQueryOutcome | null>(null);

  const run = async () => {
    const body = text.trim();
    if (!body || busy) return;
    setBusy(true);
    // Dropped before the request: a stale table under a running query reads
    // as the answer to the new one.
    setOutcome(null);
    try {
      setOutcome(
        ask ? await askIndex({ prompt: body, limit: QUERY_LIMIT }) : await runIndexQuery({ sql: body, limit: QUERY_LIMIT }),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="settings-editor">
      <p className="settings-row-desc">
        Two tables: <code>files</code>(path, dir, name, ext, size, mtime, depth) and <code>dirs</code>(dir, n_files,
        total_size, mtime_ns, n_subdirs, depth). <code>size</code> is bytes and <code>mtime</code> is epoch seconds.
      </p>
      <label className="settings-inline-switch">
        <Switch checked={ask} onCheckedChange={(c) => setAsk(!!c)} className="settings-switch" />
        <span>
          Ask in plain English — Claude writes the SQL, which is shown with the results and runs under the same guard.
        </span>
      </label>
      <Textarea
        className="settings-mono index-query-input"
        rows={ask ? 3 : 6}
        spellCheck={false}
        value={text}
        placeholder={ask ? "Which folders are using the most space?" : EXAMPLE_SQL}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          // The chord runs: Enter has to stay a newline in a multi-line statement.
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void run();
          }
        }}
        aria-label={ask ? "Question about the index" : "SQL to run against the index"}
      />
      <div className="settings-actions">
        <Button size="sm" variant="accent" disabled={busy || !text.trim()} onClick={() => void run()}>
          {busy ? "Running…" : ask ? "Ask" : "Run"}
        </Button>
        <span className="settings-row-desc">{chordLabel([MOD_LABEL, ENTER_LABEL])}</span>
      </div>
      {outcome?.sql && (
        <pre className="index-query-sql">
          <code>{outcome.sql}</code>
        </pre>
      )}
      {outcome && !outcome.ok && <ErrorBanner>{outcome.error}</ErrorBanner>}
      {outcome?.ok && <QueryTable outcome={outcome} />}
    </div>
  );
}

function QueryTable({ outcome }: { outcome: IndexQueryOutcome & { ok: true } }) {
  const { columns, rows, truncated } = outcome.table;
  if (rows.length === 0) return <p className="settings-row-desc">No rows.</p>;
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
      <p className="settings-row-desc">
        {rows.length.toLocaleString()} {rows.length === 1 ? "row" : "rows"}
        {truncated ? ` — stopped at ${QUERY_LIMIT}; add a LIMIT or an aggregate.` : "."}
      </p>
    </>
  );
}

// ── Diagnostics (SPEC §50) ────────────────────────────────────────────────
// One button that zips logs, crash reports, memory samples and the open-apps
// list to the Desktop. The plan line (what the zip WOULD hold) is read up
// front so the reader knows the size before pressing. WINDOW PRESETS, not a
// date picker: a reporter knows "a few minutes ago" or "all day", never a
// clock time. The bulky collectors honour the window; the small join keys are
// always included. The macOS Help menu, the menu-bar Dock and the CLI save
// the same bundle with the defaults; this is the one surface with options,
// and the only GUI one on Linux.
type DiagnosticsWindow = "30m" | "2h" | "boot" | "24h";
const DIAGNOSTICS_WINDOWS: { key: DiagnosticsWindow; label: string }[] = [
  { key: "30m", label: "Last 30 minutes" },
  { key: "2h", label: "Last 2 hours" },
  { key: "boot", label: "Since the app started" },
  { key: "24h", label: "Last 24 hours" },
];

function diagnosticsSince(win: DiagnosticsWindow, startedAt: number | null): number | undefined {
  const now = Date.now() / 1000;
  switch (win) {
    case "30m":
      return now - 30 * 60;
    case "2h":
      return now - 2 * 3600;
    case "24h":
      return now - 24 * 3600;
    case "boot":
      return startedAt ?? undefined;
  }
}

export function DiagnosticsControl() {
  const [plan, setPlan] = useState<DiagnosticsPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<DiagnosticsResult | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [win, setWin] = useState<DiagnosticsWindow>("2h");
  // The macOS unified log is the ONE slow collector (15–45 s). Off here by
  // default; the menu items and the CLI keep it on, because a reporter's
  // bundle is the one that has to carry the jetsam evidence.
  const [systemLog, setSystemLog] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    fetchServerHealth()
      .then((h) => alive && setStartedAt(h.started_at))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    setPlan(null);
    fetchDiagnosticsPlan(diagnosticsSince(win, startedAt))
      .then((p) => alive && setPlan(p))
      // The plan is a nicety: without it the button still works.
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [win, startedAt]);

  const save = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setResult(null);
    setCopied(false);
    try {
      setResult(await buildDiagnostics({ since_s: diagnosticsSince(win, startedAt), system_log: systemLog }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingRow
      id="diagnostics"
      status={
        busy
          ? systemLog
            ? "Reading the system log… 15–45 seconds"
            : "Collecting…"
          : plan
            ? `${plan.files} ${plan.files === 1 ? "file" : "files"} · ${formatBytes(plan.bytes)} · ${plan.crash_reports} ${plan.crash_reports === 1 ? "crash report" : "crash reports"}`
            : "Measuring…"
      }
      control={
        <>
          <SettingSelect
            id="diagnostics"
            ariaLabel="Diagnostics window"
            value={win}
            options={DIAGNOSTICS_WINDOWS.map((w) => ({
              key: w.key,
              label: w.label,
              disabled: w.key === "boot" && startedAt === null,
            }))}
            disabled={busy}
            onChange={(v) => setWin(v as DiagnosticsWindow)}
          />
          <Button variant="accent" size="sm" disabled={busy} onClick={() => void save()}>
            {busy ? "Collecting…" : "Save…"}
          </Button>
        </>
      }
    >
      {isMac && (
        <label className="settings-inline-switch">
          <Switch
            checked={systemLog}
            disabled={busy}
            onCheckedChange={(c) => setSystemLog(!!c)}
            className="settings-switch"
          />
          <span>Include the macOS system log — memory kills and App Nap events. Adds 15–45 seconds.</span>
        </label>
      )}
      {result && (
        <div className="settings-actions">
          <span className="settings-row-desc">
            Saved {formatBytes(result.bytes)} to <code className="break-all">{result.path}</code>
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void copyToClipboard(result.path).then((ok) => ok && setCopied(true))}
          >
            {copied ? "Copied" : "Copy path"}
          </Button>
        </div>
      )}
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </SettingRow>
  );
}

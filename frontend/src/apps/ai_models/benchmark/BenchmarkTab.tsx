// The Benchmark tab: one section per AI capability, each listing the models this
// machine has downloaded for it with what they measured last time and a button
// to measure again (SPEC AI-16).
//
// **The question this tab exists to answer is "how fast is THIS model on THIS
// laptop", and the only way to answer it comparably is to fix the work.** So a
// run is not configurable: the server owns one frozen workload per capability
// (ai/benchmark.py) and this tab presses a button. That is the deliberate cost —
// a number exists only where somebody pressed it — and the deliberate gain: two
// models here, or one model across two app versions, are legitimately
// comparable, which the passive Usage tab's figures never are.
//
// THE LISTING IS NOT THIS TAB'S. `scan` arrives from the page above
// (lib/useCacheScan.ts) exactly as it does for the Local tab, because "which
// models could I benchmark" is the question that shared cache walk already
// answers, and a second crawl behind this tab is precisely the cost that hook
// exists to avoid.
//
// Three-state loading discipline throughout, the same one UsageTab and LocalTab
// follow: `null` is "not answered yet" and draws a skeleton, `[]` is "answered,
// and there is nothing", and a failure draws an ErrorBanner while KEEPING the
// last good value — a failed refresh must not blank a history somebody is
// reading.
//
// **A run holds its HTTP request open for minutes** (the server does this
// deliberately — see routers/ai_benchmark.py), so the click cannot be awaited
// as if it were a save. Only the pressed model's button goes dead; the rest of
// the page stays live, and the job row carries the progress through the shared
// ModelProgress the other tabs use.
import { useEffect, useState } from "react";
import { BenchmarkChart } from "./BenchmarkChart";
import { ModelProgress } from "@apps/ai_models/shared/ModelProgress";
import { CAPABILITY_ORDER } from "@apps/ai_models/lib/aiModelGroups";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import { readParam, writeParams } from "@apps/ai_models/lib/params";
import {
  DASH,
  formatLoad,
  formatMemory,
  formatPrimary,
  latestByModel,
  orderCapabilities,
  primaryMetric,
  primaryValue,
  runsFor,
  summaryLine,
  type ModelLatest,
} from "@apps/ai_models/lib/benchmark";
import { type CacheScan } from "@apps/ai_models/lib/useCacheScan";
import { refreshAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import {
  deleteAiBenchmarks,
  getAiBenchmarks,
  runAiBenchmark,
  type AiBenchmarkRun,
  type AiModelRepo,
} from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";

export function BenchmarkTab({ scan }: { scan: CacheScan }) {
  const { data, repos, jobByModel, scanEpoch } = scan;
  // Every run ever recorded, oldest first. `null` until the store has answered.
  const [runs, setRuns] = useState<AiBenchmarkRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The model with a run in flight, and the job row carrying its progress. One
  // at a time per capability is a SERVER rule (a second load would evict the
  // first model mid-measurement), and the button disables on this.
  const [running, setRunning] = useState<{ model: string; jobId: string } | null>(null);
  // The optional focus filter, SEEDED from `?cap=` once and held in state
  // thereafter. State rather than reading the URL every render because
  // `writeParams` uses `history.replaceState`, which deliberately fires no
  // navigation event (a filter change must not stack a history entry) — so a
  // component that read only the URL would clear the param and go on drawing
  // the old filter.
  const [focus, setFocus] = useState<string | null>(() => readParam("cap"));

  // On the same trigger as the cache walk, for the reason the Local tab's
  // catalog fetch rides it: a run that just finished is a new row here, and a
  // model that just landed on disk is a new row to put it in. Two answers one
  // poll apart would draw a model with no history beside a history with no
  // model.
  useEffect(() => {
    let alive = true;
    getAiBenchmarks().then(
      (history) => {
        if (!alive) return;
        setRuns(history.runs);
        setError(null);
      },
      (e) => {
        if (!alive) return;
        setError((e as Error).message);
        // KEEP whatever is already drawn. A failed re-fetch costs the update,
        // never the history — the same discipline LocalTab's `?? []` follows,
        // with `?? []` here too so a FIRST failure still leaves the three-state
        // rule intact (answered, and empty) rather than a permanent skeleton.
        setRuns((prev) => prev ?? []);
      },
    );
    return () => {
      alive = false;
    };
  }, [scanEpoch]);

  const start = async (model: string, capability: string) => {
    setError(null);
    // Optimistic only about the BUTTON, never about a result: the jobId comes
    // back with the finished run, so until then the progress row has no id to
    // watch and says "Preparing…" from ModelProgress's own default.
    setRunning({ model, jobId: "" });
    try {
      const { run } = await runAiBenchmark(model, capability);
      // Append rather than re-fetch: the server just handed back the very
      // record it appended, so a second read of the same file would be a round
      // trip to learn what we hold.
      setRuns((prev) => [...(prev ?? []), run]);
      // A benchmark loads a model, so the runtime's idea of what is resident
      // has changed — the Local tab's Loaded badges are reading it.
      refreshAiRuntime();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(null);
    }
  };

  const forget = async (id: string) => {
    setError(null);
    try {
      // The endpoint answers with the fresh history, so the page adopts state it
      // just re-read rather than splicing an array it hopes still matches disk —
      // the same discipline the Local tab's delete follows.
      const history = await deleteAiBenchmarks([id]);
      setRuns(history.runs);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!data && runs === null) return <SkeletonLines rows={6} label="Loading benchmarks" />;

  // Which capabilities get a section: every one this machine has a downloaded
  // model for, UNION every one with a recorded run. The union is what keeps a
  // history readable after its model was deleted — the runs are still the truth
  // about what happened, and dropping the section would silently hide them.
  const all = orderCapabilities([
    ...new Set([
      ...CAPABILITY_ORDER,
      ...repos.map((r) => r.capability).filter((c): c is string => !!c),
      ...(runs ?? []).map((r) => r.capability),
    ]),
  ]);

  // `?cap=` narrows the page to one section. The SAME param the playground reads
  // (lib/params.ts, routes.ts) and the same one Home's cards link in with, so a
  // link that opens the playground on image generation opens this tab on image
  // generation too. A `cap` naming nothing here is IGNORED rather than shown as
  // an empty page: the param travels between tabs (`tabHref` keeps the query),
  // so this tab will see values that were never meant for it.
  const focused = focus && all.includes(focus) ? focus : null;
  const capabilities = focused ? [focused] : all;

  return (
    <div className="am-bench">
      <ErrorBanner>{error}</ErrorBanner>
      {focused && (
        <p className="am-group-note">
          Showing {capabilityLabel(focused)} only.{" "}
          <button
            type="button"
            className="am-bench-linkbtn"
            // Both halves: the state is what this tab draws from, and the URL
            // is dropped so a copied link no longer carries a filter the reader
            // has just cleared. `writeParams` (replaceState) rather than a
            // navigation — a view narrowing is not a page to go back to.
            onClick={() => {
              setFocus(null);
              writeParams({ cap: null });
            }}
          >
            Show every capability
          </button>
        </p>
      )}
      {capabilities.map((capability) => (
        <CapabilitySection
          key={capability}
          capability={capability}
          repos={repos.filter((r) => r.capability === capability)}
          runs={runs === null ? null : runsFor(runs, capability)}
          running={running}
          job={running ? jobByModel.get(running.model) : undefined}
          onRun={start}
          onForget={forget}
        />
      ))}
    </div>
  );
}

function CapabilitySection({
  capability,
  repos,
  runs,
  running,
  job,
  onRun,
  onForget,
}: {
  capability: string;
  repos: AiModelRepo[];
  /** null while the history has not answered. */
  runs: AiBenchmarkRun[] | null;
  running: { model: string; jobId: string } | null;
  job?: ReturnType<CacheScan["jobByModel"]["get"]>;
  onRun: (model: string, capability: string) => void;
  onForget: (id: string) => void;
}) {
  const metric = primaryMetric(capability);
  const latest = new Map<string, ModelLatest>(
    (runs ? latestByModel(runs) : []).map((row) => [row.model, row]),
  );
  // Models with a card, then models that only have HISTORY — a run whose model
  // has since been deleted is still a fact, and it belongs under its own
  // capability rather than nowhere.
  const orphans = [...latest.keys()].filter((model) => !repos.some((r) => r.id === model));

  return (
    <section className="am-section">
      <div className="am-section-head">
        <h3 className="am-section-title">{capabilityLabel(capability)}</h3>
        {/* What this section's numbers MEAN, in the heading's right-hand slot —
            the one place a unit can be stated once for the whole section
            instead of on every row. A capability this frontend does not know
            has no primary metric and says nothing, rather than guessing. */}
        {metric && <span className="am-bench-metric">{metric.label} · {metric.unit}</span>}
      </div>

      {runs === null ? (
        <SkeletonLines rows={2} label={`Loading ${capabilityLabel(capability)} benchmarks`} />
      ) : repos.length === 0 && orphans.length === 0 ? (
        // Answered, and empty. It says WHICH nothing — no model rather than no
        // benchmark — because the two have different next steps.
        <p className="am-group-note">No {capabilityLabel(capability).toLowerCase()} model is downloaded yet.</p>
      ) : (
        <div className="am-bench-rows">
          {repos.map((repo) => (
            <BenchmarkRow
              key={repo.id}
              model={repo.id}
              row={latest.get(repo.id) ?? null}
              busy={running?.model === repo.id}
              job={running?.model === repo.id ? job : undefined}
              // Any run at all blocks every button in the section: the server
              // refuses a second run on the same capability, so an enabled
              // button here would be a click that 409s.
              blocked={!!running}
              onRun={() => onRun(repo.id, capability)}
            />
          ))}
          {orphans.map((model) => (
            <BenchmarkRow key={model} model={model} row={latest.get(model)!} gone />
          ))}
        </div>
      )}

      {/* Siblings of the rows, not children of them: the rows are the current
          answer, and these two are the evidence behind it. The chart draws
          nothing until something has been measured — it returns null rather
          than an empty axis, which would read as a measurement of zero. */}
      {runs !== null && runs.length > 0 && (
        <>
          <BenchmarkChart capability={capability} runs={runs} />
          <RunTable capability={capability} runs={runs} onForget={onForget} />
        </>
      )}
    </section>
  );
}

function BenchmarkRow({
  model,
  row,
  busy,
  job,
  blocked,
  gone,
  onRun,
}: {
  model: string;
  row: ModelLatest | null;
  busy?: boolean;
  job?: ReturnType<CacheScan["jobByModel"]["get"]>;
  blocked?: boolean;
  /** The model is no longer on disk; its history is shown, its button is not. */
  gone?: boolean;
  onRun?: () => void;
}) {
  return (
    <div className="am-bench-row">
      <div className="am-bench-model">
        <span className="cc-mono">{model}</span>
        {gone && <span className="am-bench-gone">not on this machine any more</span>}
      </div>
      <div className="am-bench-latest">
        {busy ? (
          <ModelProgress detail="Benchmarking…" job={job} />
        ) : row ? (
          <>
            <span className="am-bench-summary">{summaryLine(row.latest)}</span>
            {row.delta && (
              // The sign is not the meaning — on an image section a negative
              // change is the improvement — so `better` decides the class and
              // the sign is only printed.
              <span className={"am-bench-delta" + (row.delta.better ? " better" : " worse")}>
                {row.delta.percent >= 0 ? "+" : ""}
                {row.delta.percent.toFixed(1)}%
              </span>
            )}
          </>
        ) : (
          <span className="am-bench-never">Never benchmarked</span>
        )}
      </div>
      {!gone && (
        <button
          type="button"
          className="cc-btn"
          disabled={blocked}
          onClick={onRun}
          title={
            busy
              ? "This benchmark is running — it takes minutes"
              : blocked
                ? "Another benchmark is running for this capability"
                : "Run the fixed workload for this capability"
          }
        >
          {busy ? "Running…" : row ? "Run again" : "Run benchmark"}
        </button>
      )}
    </div>
  );
}

/** The table's columns, and how each one sorts.
 *
 *  A table rather than a switch in the comparator, so a column cannot exist in
 *  the header and be unsortable in the body — which is what a per-column `if`
 *  produced the first time round.
 *
 *  Every `value` may return null, and null always sorts LAST regardless of
 *  direction. That is deliberate: an unmeasured metric is not "the smallest",
 *  and sorting by throughput must not fill the top of the table with runs that
 *  measured nothing.
 */
const COLUMNS: {
  key: string;
  label: string;
  numeric: boolean;
  value: (run: AiBenchmarkRun) => number | string | null;
}[] = [
  { key: "date", label: "When", numeric: true, value: (r) => r.startedAt },
  { key: "model", label: "Model", numeric: false, value: (r) => r.model },
  // Labelled by the capability's own metric at render time — "Throughput",
  // "Per step" — because one heading cannot name four different things.
  { key: "metric", label: "", numeric: true, value: primaryValue },
  { key: "memory", label: "Memory", numeric: true, value: (r) => r.peakResidentBytes },
  { key: "load", label: "Load", numeric: true, value: (r) => r.loadSeconds },
  { key: "device", label: "Device", numeric: false, value: (r) => r.device },
  { key: "version", label: "App", numeric: false, value: (r) => r.appVersion },
];

/** Every run for one capability, newest first by default.
 *
 *  **Collapsed by default**, because it is the archive and the rows above are
 *  the answer: a section with four models and thirty runs would otherwise open
 *  as a wall of numbers with the current state buried at the top of it. The
 *  summary line says how many are hiding, so nothing is invisible — only
 *  folded.
 */
function RunTable({
  capability,
  runs,
  onForget,
}: {
  capability: string;
  runs: AiBenchmarkRun[];
  onForget: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  // Newest first: the default question about a history is "what happened last".
  const [sort, setSort] = useState<{ key: string; desc: boolean }>({ key: "date", desc: true });
  const metric = primaryMetric(capability);

  const column = COLUMNS.find((c) => c.key === sort.key) ?? COLUMNS[0]!;
  const ordered = [...runs].sort((a, b) => {
    const left = column.value(a);
    const right = column.value(b);
    // Nulls last in BOTH directions — see COLUMNS.
    if (left === null && right === null) return 0;
    if (left === null) return 1;
    if (right === null) return -1;
    const cmp =
      typeof left === "number" && typeof right === "number"
        ? left - right
        : String(left).localeCompare(String(right));
    return sort.desc ? -cmp : cmp;
  });

  return (
    <details className="am-bench-history" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        {runs.length} recorded {runs.length === 1 ? "run" : "runs"}
      </summary>
      <div className="am-bench-tablewrap">
        <table className="am-bench-table">
          <thead>
            <tr>
              {COLUMNS.map((col) => (
                <th key={col.key} className={col.numeric ? "num" : undefined}>
                  <button
                    type="button"
                    className="am-bench-sort"
                    aria-sort={
                      sort.key === col.key ? (sort.desc ? "descending" : "ascending") : "none"
                    }
                    onClick={() =>
                      setSort((prev) =>
                        prev.key === col.key
                          ? { key: col.key, desc: !prev.desc }
                          : // A fresh column starts DESCENDING for a number and
                            // ASCENDING for a name, which is what each one's
                            // interesting end is.
                            { key: col.key, desc: col.numeric },
                      )
                    }
                  >
                    {col.key === "metric" ? (metric?.label ?? "Result") : col.label}
                    {sort.key === col.key && <span aria-hidden="true">{sort.desc ? " ↓" : " ↑"}</span>}
                  </button>
                </th>
              ))}
              {/* No header for the delete column: a heading over a column of ✕
                  buttons names the action, not the data. */}
              <th />
            </tr>
          </thead>
          <tbody>
            {ordered.map((run) => (
              <tr key={run.id} className={run.ok ? undefined : "failed"}>
                {/* Locale date and time, not a relative age: two runs an hour
                    apart are the interesting case, and "3 days ago" cannot tell
                    them apart. */}
                <td className="num">{new Date(run.startedAt * 1000).toLocaleString()}</td>
                <td className="cc-mono">{run.model}</td>
                {/* A failed run's cell carries the REASON rather than a dash:
                    the row exists because something went wrong, and the dash
                    would make it look like a page bug. */}
                <td className="num" title={run.ok ? undefined : (run.error ?? "")}>
                  {run.ok ? formatPrimary(run) : "failed"}
                </td>
                <td className="num">{formatMemory(run)}</td>
                <td className="num">{formatLoad(run)}</td>
                <td>{run.device ?? DASH}</td>
                <td>
                  {run.appVersion}
                  {/* The workload revision, shown only where it is NOT the
                      newest one in this section — a seam the reader has to know
                      about, because runs either side of it are not comparable
                      and the chart deliberately draws no delta across it. */}
                  {run.workload.revision !== newestRevision(runs) && (
                    <span className="am-bench-rev" title="A different workload version — not comparable with the newest runs">
                      {" "}
                      w{run.workload.revision}
                    </span>
                  )}
                </td>
                <td>
                  <button
                    type="button"
                    className="am-bench-forget"
                    title="Forget this run"
                    aria-label={`Forget the run from ${new Date(run.startedAt * 1000).toLocaleString()}`}
                    onClick={() => onForget(run.id)}
                  >
                    ✕
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/** The workload revision the newest run in this section was measured under —
 *  what every other row's revision is marked AGAINST. Computed from the runs
 *  rather than from the frontend's own idea of "current", because this page has
 *  no such idea: the server owns the revision, and a hardcoded copy here would
 *  start marking every row the day the server bumped it. */
function newestRevision(runs: AiBenchmarkRun[]): number | null {
  let newest: AiBenchmarkRun | null = null;
  for (const run of runs) if (!newest || run.startedAt > newest.startedAt) newest = run;
  return newest ? newest.workload.revision : null;
}

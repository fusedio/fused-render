// The Benchmark tab: one section per AI capability, each listing the models this
// machine has downloaded for it with what they measured last time and a button
// to measure again (SPEC AI-14).
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
// as if it were a save. Only the pressed capability's buttons go dead; the rest
// of the page stays live.
//
// **A benchmark opens no download-manager row, and this tab must not reach for
// one.** Server job rows are keyed by TITLE (`useCacheScan` maps
// `job.title -> job`) and `supervisor.load` already owns the row titled with the
// model id, so a benchmark row either cannot be found or shadows the load's —
// which put the manager's only ✕ on the load and let a cold run spin to its
// hour-long timeout. So the in-progress state is a plain spinner in the row, and
// through a COLD run the load's own row shows up in the manager with real byte
// counts, which is the progress that was always worth watching. See
// `ai/benchmark.py`.
import { useEffect, useState } from "react";
import { BenchmarkChart } from "./BenchmarkChart";
import { CAPABILITY_ORDER } from "@apps/ai_models/lib/aiModelGroups";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import { readParam, writeParams } from "@apps/ai_models/lib/params";
import { tabHref } from "@apps/ai_models/routes";
import {
  DASH,
  failureReason,
  formatLoad,
  formatMemory,
  formatPrimary,
  latestByModel,
  leaderboard,
  middleEllipsis,
  orderCapabilities,
  primaryMetric,
  primaryValue,
  resolveCapability,
  rowDetail,
  rowHeadline,
  runButtonState,
  runCountsByCapability,
  runsFor,
  shortModelName,
  stoppedNote,
  type LeaderboardRow,
  type ModelLatest,
  type RunButtonState,
  type RunsInFlight,
} from "@apps/ai_models/lib/benchmark";
import { type CacheScan } from "@apps/ai_models/lib/useCacheScan";
import { refreshAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { navigateUrl } from "@platform/lib/router";
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
  const { data, repos, scanEpoch } = scan;
  // Every run ever recorded, oldest first. `null` until the store has answered.
  const [runs, setRuns] = useState<AiBenchmarkRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Which capability has a run in flight, and on which model. **Keyed by
  // capability, not a single slot**, because that is the unit the server
  // serialises on: one resident model per capability, so a second text run
  // would evict the first's model (a 409) while an image run alongside it is
  // explicitly permitted. A single slot greyed out every other section under a
  // tooltip claiming a per-capability rule, making a legal action unreachable.
  const [inFlight, setInFlight] = useState<RunsInFlight>({});
  // A run that came back STOPPED rather than measured. Its own state, not
  // `error`: this is not a request failure and must not draw the ErrorBanner —
  // see `stoppedNote`. Cleared when the next run starts, rather than on a timer:
  // a timer that hides an explanation before it has been read is worse than a
  // line that waits to be replaced.
  const [stopped, setStopped] = useState<string | null>(null);
  // The selector's raw choice — `null` until the reader (or a landing
  // `?cap=`) has actually picked one, at which point `resolveCapability`
  // below stops filling in a default and just honours it. SEEDED from
  // `?cap=` once and held in state thereafter, the same reason the tab's old
  // single-capability filter was: `writeParams` uses `history.replaceState`,
  // which deliberately fires no navigation event (a selection must not stack
  // a history entry) — so a component that read only the URL would clear the
  // param and go on drawing the old choice.
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
    setStopped(null);
    // Optimistic only about the BUTTON, never about a result. Functional
    // updates on both halves, because two capabilities can be running at once
    // and a `{...inFlight}` closed over at click time would drop whichever one
    // started in between.
    setInFlight((prev) => ({ ...prev, [capability]: model }));
    try {
      const { run, cancelled } = await runAiBenchmark(model, capability);
      // Stopped from outside — say so. Silence here was finding 6: nothing
      // appended, no error, the button quietly re-enabled, so several minutes of
      // waiting ended with no signal at all.
      if (cancelled) setStopped(stoppedNote(model));
      // **Presence of `run`, not `run.ok`.** A cancelled run answers with no
      // `run` at all, because nothing was measured; appending it would draw a
      // phantom "Failed — cancelled" row that becomes this model's LATEST — so
      // the delta and the summary compare against it — until a reload. A run
      // that genuinely failed DOES come back and does belong in the history.
      if (run) {
        // Append rather than re-fetch: the server just handed back the very
        // record it appended, so a second read of the same file would be a
        // round trip to learn what we hold.
        setRuns((prev) => [...(prev ?? []), run]);
      }
      // A benchmark loads a model either way, so the runtime's idea of what is
      // resident has changed — the Local tab's Loaded badges are reading it.
      refreshAiRuntime();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setInFlight((prev) => {
        const next = { ...prev };
        delete next[capability];
        return next;
      });
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

  // Whether the history has answered at all — the same predicate the early
  // return below used to gate on. Named here because the sync effect right
  // after it needs to know the SAME thing: don't write a resolved default
  // into `?cap=` while `runs` is still `null` and `all` therefore reflects no
  // recorded run yet, which could clobber an explicit `?cap=` with a
  // premature guess the moment the real counts land one render later.
  const loading = !data && runs === null;

  // Which capabilities the selector offers: every one this machine has a
  // downloaded model for, UNION every one with a recorded run. The union is
  // what keeps a history reachable after its model was deleted — the runs are
  // still the truth about what happened, and dropping it from the list would
  // silently hide them.
  const all = orderCapabilities([
    ...new Set([
      ...CAPABILITY_ORDER,
      ...repos.map((r) => r.capability).filter((c): c is string => !!c),
      ...(runs ?? []).map((r) => r.capability),
    ]),
  ]);

  // One capability at a time now — the tab used to stack all four sections and
  // `?cap=` only narrowed that stack to one; `resolveCapability` (lib/
  // benchmark.ts) is the SAME rule made the selector's only state: the URL's
  // `?cap=` when it names a real capability, otherwise the most-run one
  // (`defaultCapability`), ties broken by registry order and falling back to
  // the first capability when nothing has ever run. `runs` may still be `null`
  // (history not answered yet) — an empty count map picks the same "first in
  // registry order" default `defaultCapability` gives an all-zero one, so the
  // choice does not flicker once real counts arrive UNLESS a capability
  // genuinely turns out to have more runs, which is the point of the feature.
  const counts = runCountsByCapability(runs ?? []);
  const selected = resolveCapability(all, focus, counts);

  // Keep the URL in sync with whatever is actually selected — landing on a
  // default (no `?cap=` yet) writes it in, and choosing a different capability
  // updates it — via `replaceState` (`writeParams`), never a navigation: a
  // selector change is not a page to go Back to. Runs after render rather than
  // during it, since writing history is a side effect.
  //
  // **This hook must run on EVERY render, loading or not** — React throws
  // ("Rendered more hooks than during the previous render") the moment a hook
  // sits below a conditional return, because the loading render then calls one
  // fewer hook than the render after it. The `loading` guard therefore lives
  // INSIDE the effect, on the WRITE, not on the hook: skipping the call while
  // `runs` is still `null` is what stops a not-yet-known history from
  // clobbering an explicit `?cap=` with a premature "first in registry order"
  // guess one render before the real counts arrive.
  useEffect(() => {
    if (loading) return;
    writeParams({ cap: selected });
  }, [loading, selected]);

  if (loading) return <SkeletonLines rows={6} label="Loading benchmarks" />;

  return (
    <div className="am-bench">
      <ErrorBanner>{error}</ErrorBanner>
      {stopped && <p className="am-bench-stopped">{stopped}</p>}
      {/* THE SELECTOR. A native <select> with a plain label, per the same
          "boring control, name it and get out of the way" rule `EnginesTab`
          uses for its own per-capability selects — four items is not enough to
          earn a bespoke segmented control, and a second control vocabulary on
          one page is a cost with no reader benefit at this count. */}
      <div className="am-bench-capsel">
        <label htmlFor="am-bench-cap">Capability</label>
        <select
          id="am-bench-cap"
          className="field-control am-bench-capsel-input"
          value={selected ?? ""}
          onChange={(e) => setFocus(e.target.value)}
        >
          {all.map((capability) => {
            const count = counts[capability] ?? 0;
            return (
              <option key={capability} value={capability}>
                {capabilityLabel(capability)}
                {count > 0 ? ` (${count})` : ""}
              </option>
            );
          })}
        </select>
      </div>
      {selected && (
        <CapabilitySection
          key={selected}
          capability={selected}
          repos={repos.filter((r) => r.capability === selected)}
          runs={runs === null ? null : runsFor(runs, selected)}
          inFlight={inFlight}
          onRun={start}
          onForget={forget}
        />
      )}
    </div>
  );
}

function CapabilitySection({
  capability,
  repos,
  runs,
  inFlight,
  onRun,
  onForget,
}: {
  capability: string;
  repos: AiModelRepo[];
  /** null while the history has not answered. */
  runs: AiBenchmarkRun[] | null;
  /** Every capability's in-flight run, not just this one's — `runButtonState`
   *  reads its own key out, which keeps the "which capability blocks which"
   *  rule in one tested place rather than in each section's props. */
  inFlight: RunsInFlight;
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
  const gone = new Set(orphans);

  // Ranked best-first: the leaderboard is the reason this tab shows a list at
  // all, and `leaderboard` (lib/benchmark.ts) owns the ordering and every
  // bar's length so the rule about which way a metric points is tested once
  // rather than guessed again here.
  const ranked: LeaderboardRow[] = leaderboard(capability, [
    ...repos.map((r) => ({ model: r.id, row: latest.get(r.id) ?? null })),
    ...orphans.map((model) => ({ model, row: latest.get(model) ?? null })),
  ]);

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
        // benchmark — and points at the next step rather than leaving the
        // reader to guess where a model would come from.
        <p className="am-group-note">
          No {capabilityLabel(capability).toLowerCase()} model is downloaded yet. Get one from the{" "}
          <a
            href={tabHref("local")}
            onClick={(e) => {
              if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
                return;
              e.preventDefault();
              navigateUrl(tabHref("local"));
            }}
          >
            Local tab
          </a>
          .
        </p>
      ) : (
        <>
          {/* THE HERO: the chart leads the section, not the model list — the
              trend is the first question this tab answers, and the list below
              is the second one. It draws nothing until something has been
              measured, returning null rather than an empty axis (which would
              read as a measurement of zero). */}
          {runs !== null && runs.length > 0 && <BenchmarkChart capability={capability} runs={runs} />}
          <div className="am-bench-rows">
            {ranked.map(({ model, row, barFraction }) => {
              const button = gone.has(model)
                ? undefined
                : runButtonState(capability, model, inFlight, row !== null);
              return (
                <BenchmarkRow
                  key={model}
                  model={model}
                  row={row}
                  barFraction={barFraction}
                  button={button}
                  gone={gone.has(model)}
                  onRun={() => onRun(model, capability)}
                />
              );
            })}
          </div>
        </>
      )}

      {/* The archive, under the leaderboard: the ranked rows are the current
          answer, and this is the evidence behind it. */}
      {runs !== null && runs.length > 0 && <RunTable capability={capability} runs={runs} onForget={onForget} />}
    </section>
  );
}

function BenchmarkRow({
  model,
  row,
  barFraction,
  button,
  gone,
  onRun,
}: {
  model: string;
  row: ModelLatest | null;
  /** 0..1 against the section's best model, or null for no bar — see
   *  `leaderboard`. Decided there, not here, for the same reason `button` is:
   *  which way a metric points is exactly the thing a screenshot cannot check. */
  barFraction: number | null;
  /** What the Run button says and whether it can be pressed — decided by
   *  `runButtonState`, never here: the rule about which run blocks which button
   *  is exactly the thing a screenshot cannot check. Absent for a `gone` row,
   *  which has no button at all. */
  button?: RunButtonState;
  /** The model is no longer on disk; its history is shown, its button is not. */
  gone?: boolean;
  onRun?: () => void;
}) {
  // The one line beyond the headline — TTFT, load time, device, or a failed
  // run's own error — behind an expander so it never dominates the row. Only
  // drawn when there is something to say: a row whose whole story fits the
  // headline gets no expander at all.
  const detail = row ? (row.latest.ok ? rowDetail(row.latest) : failureReason(row.latest)) : null;

  return (
    <div className="am-bench-row">
      <div className="am-bench-model" title={model}>
        {/* Budget (28) is a hair under the column's own 30ch so the CSS
            `overflow: hidden` safety net (ai-models.css) never has to fire
            for a monospace glyph at this size — see `middleEllipsis`'s own
            comment for why the ellipsis goes in the MIDDLE rather than the
            tail. */}
        <span className="cc-mono">{middleEllipsis(shortModelName(model), 28)}</span>
        {gone && <span className="am-bench-gone">not on this machine any more</span>}
      </div>
      <div className="am-bench-latest">
        {button?.busy ? (
          // A plain spinner, not `ModelProgress`: that component draws a JOB
          // row's detail and byte counts, and a benchmark has no row. There is
          // genuinely nothing to report until the request returns — the server
          // is holding it open — so pretending otherwise with an invented
          // percentage is what makes live work read as frozen.
          <span className="am-bench-busy" role="status">
            <span className="am-runtime-dot" />
            Benchmarking… this takes minutes
          </span>
        ) : row ? (
          <>
            {/* The bar: width proportional to how this model compares to the
                section's best (`leaderboard`), never drawn for a failed or
                unmeasured latest run — a bar of length zero would read as
                "measured, and terrible" rather than "nothing to compare". */}
            {barFraction !== null && (
              <span className="am-bench-bar" aria-hidden="true">
                <span className="am-bench-barfill" style={{ width: `${barFraction * 100}%` }} />
              </span>
            )}
            <span className="am-bench-headline">{rowHeadline(row.latest)}</span>
            {row.delta && (
              // The sign is not the meaning — on an image section a negative
              // change is the improvement — so `better` decides the class and
              // the sign is only printed.
              <span className={"am-bench-delta" + (row.delta.better ? " better" : " worse")}>
                {row.delta.percent >= 0 ? "+" : ""}
                {row.delta.percent.toFixed(1)}%
              </span>
            )}
            {detail && (
              <details className="am-bench-rowdetail">
                <summary>{row.latest.ok ? "Details" : "Failed — details"}</summary>
                <p>{detail}</p>
              </details>
            )}
          </>
        ) : (
          <span className="am-bench-never">Never benchmarked</span>
        )}
      </div>
      {!gone && button && (
        <button
          type="button"
          className="cc-btn"
          disabled={button.blocked}
          onClick={onRun}
          title={button.title}
        >
          {button.label}
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

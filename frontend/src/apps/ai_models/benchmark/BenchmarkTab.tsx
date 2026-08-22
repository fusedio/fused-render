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
import { ModelProgress } from "@apps/ai_models/shared/ModelProgress";
import { CAPABILITY_ORDER } from "@apps/ai_models/lib/aiModelGroups";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import {
  latestByModel,
  orderCapabilities,
  primaryMetric,
  runsFor,
  summaryLine,
  type ModelLatest,
} from "@apps/ai_models/lib/benchmark";
import { type CacheScan } from "@apps/ai_models/lib/useCacheScan";
import { refreshAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import {
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

  if (!data && runs === null) return <SkeletonLines rows={6} label="Loading benchmarks" />;

  // Which capabilities get a section: every one this machine has a downloaded
  // model for, UNION every one with a recorded run. The union is what keeps a
  // history readable after its model was deleted — the runs are still the truth
  // about what happened, and dropping the section would silently hide them.
  const capabilities = orderCapabilities([
    ...new Set([
      ...CAPABILITY_ORDER,
      ...repos.map((r) => r.capability).filter((c): c is string => !!c),
      ...(runs ?? []).map((r) => r.capability),
    ]),
  ]);

  return (
    <div className="am-bench">
      <ErrorBanner>{error}</ErrorBanner>
      {capabilities.map((capability) => (
        <CapabilitySection
          key={capability}
          capability={capability}
          repos={repos.filter((r) => r.capability === capability)}
          runs={runs === null ? null : runsFor(runs, capability)}
          running={running}
          job={running ? jobByModel.get(running.model) : undefined}
          onRun={start}
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
}: {
  capability: string;
  repos: AiModelRepo[];
  /** null while the history has not answered. */
  runs: AiBenchmarkRun[] | null;
  running: { model: string; jobId: string } | null;
  job?: ReturnType<CacheScan["jobByModel"]["get"]>;
  onRun: (model: string, capability: string) => void;
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

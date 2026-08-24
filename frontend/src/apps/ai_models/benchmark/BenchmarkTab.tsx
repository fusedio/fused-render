// The Benchmark tab: one capability at a time, THREE instruments for it — a
// ranked comparison chart (the hero: "which of these is fastest here"), a
// leaderboard of every model with its own action (Run, Details), and a
// per-model trend chart (secondary: "is THIS model getting faster or
// slower") — plus the archive underneath and a button to measure again
// (SPEC AI-14).
//
// **The question this tab exists to answer is "how fast is THIS model on THIS
// laptop", and the only way to answer it comparably is to fix the work.** So a
// run is not configurable: the server owns one frozen workload per capability
// (ai/benchmark.py) and this tab presses a button. That is the deliberate cost —
// a number exists only where somebody pressed it — and the deliberate gain: two
// models here, or one model across two app versions, are legitimately
// comparable, which the passive Usage tab's figures never are.
//
// **Comparison and trend are two DIFFERENT questions, and this tab has tried
// to answer them with one chart TWICE, wrong both times.** First attempt:
// every model as its own series on one shared timeline — with one or two runs
// per model that was a scatter of near-unlabelable dots, which is the real
// reason it needed edge-avoiding end labels and kept repeating one date three
// times. Second attempt, after splitting the trend out: make the LEADERBOARD's
// own inline mini-bar the whole comparison story and give the per-model trend
// chart the hero's spot. That one shipped and broke differently — the trend
// chart needs TWO RUNS OF THE SAME MODEL, and real usage spreads a handful of
// runs ACROSS several different models far more often than it re-runs one, so
// the trend chart's "single" state fired for nearly every model and the page
// had NO CHART AT ALL. `ComparisonChart` is the actual fix: a real, gridlined
// bar chart across every BENCHMARKED model, which renders whenever more than
// one model has a measurement — the normal case — and the leaderboard's own
// inline bar is deleted, since drawing the identical proportional comparison
// twice (once properly, with an axis, once as an unlabelled sliver in each
// row) was the duplicated ink. `ModelTrendChart` keeps its own spot, now
// correctly secondary, for the model a reader picks by clicking a leaderboard
// row.
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
// **A benchmark now opens its OWN download-manager row for the measurement
// phase, titled distinctly from the load's** (`ai/benchmark.py`'s
// `_MeasurementRow`/`_bench_job_title`) — the fourth design, after three that
// collided on TITLE. Server job rows are keyed by TITLE (`useCacheScan` maps
// `job.title -> job`) and `supervisor.load` already owns the row titled with
// the bare model id; a benchmark row sharing that title either could not be
// found or SHADOWED the load's, which put the manager's only ✕ on the load and
// let a cold run spin to its hour-long timeout. `_bench_job_title` fixes the
// title rather than removing the row, so through a COLD run the load's own row
// still shows up first, with real byte counts, and once loading ends this
// module's own row takes over — the phase that used to be total silence.
//
// **This tab's OWN busy row is a second, complementary view of the same run —
// phase plus a REAL elapsed clock, never an invented percentage** (see
// `busyRowText` in lib/benchmark.ts, and the ai-models.css comment near line
// 1282 for the house rule against invented bars). It reuses `lib/aiRuntime.ts`'s
// already-polled table rather than adding a second poll: while that table
// still reports the model loading, the row says so; once it does not, the row
// switches to "Measuring — mm:ss" ticking from the moment Run was pressed.
import { useEffect, useRef, useState } from "react";
import { ComparisonChart } from "./ComparisonChart";
import { ShareChartButton } from "./ShareChartButton";
import { ModelTrendChart } from "./ModelTrendChart";
import { CAPABILITY_ORDER } from "@apps/ai_models/lib/aiModelGroups";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import { readParam, writeParams } from "@apps/ai_models/lib/params";
import { tabHref, tabLabel } from "@apps/ai_models/routes";
import {
  DASH,
  availableMetrics,
  chartSeries,
  commonDevice,
  comparisonBars,
  failureReason,
  formatLoad,
  formatMemory,
  benchmarkableCapabilities,
  busyRowText,
  formatMetricSpecValue,
  formatPrimary,
  latestByModel,
  leaderboard,
  metricOptionLabel,
  middleEllipsis,
  orderCapabilities,
  primaryMetric,
  primaryValue,
  resolveCapability,
  resolveMetric,
  resolveModel,
  rowDetail,
  rowHeadline,
  runButtonState,
  runCountsByCapability,
  runCountsByModel,
  runsFor,
  shortModelName,
  stoppedNote,
  trendKind,
  type LeaderboardRow,
  type MetricSpec,
  type ModelLatest,
  type RunButtonState,
  type RunsInFlight,
} from "@apps/ai_models/lib/benchmark";
import { type CacheScan } from "@apps/ai_models/lib/useCacheScan";
import { refreshAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import {
  advanceQueue,
  observeStop,
  queueableModels,
  queueStatus,
  queueTally,
  requestQueueStop,
  startQueue,
  type BenchmarkQueue,
} from "@apps/ai_models/lib/benchmarkQueue";
import { navigateUrl } from "@platform/lib/router";
import {
  cancelAiGeneration,
  deleteAiBenchmarks,
  getAiBenchmarks,
  runAiBenchmark,
  type AiBenchmarkMachine,
  type AiBenchmarkRun,
  type AiRuntime,
} from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { SkeletonLines } from "@platform/ui/Skeleton";

export function BenchmarkTab({ scan }: { scan: CacheScan }) {
  const { data, repos, scanEpoch } = scan;
  // Every run ever recorded, oldest first. `null` until the store has answered.
  const [runs, setRuns] = useState<AiBenchmarkRun[] | null>(null);
  // The server's own answer to "which capabilities can a Run press actually
  // measure" (`AiBenchmarkHistory.workloadCapabilities`, exactly `benchmark.
  // WORKLOADS`' keys) — narrower than `CAPABILITY_ORDER`'s full list. `[]`
  // before the first fetch resolves is safe: `loading` (below) gates every
  // render that reads `all` until `runs` and this land together out of the
  // same response.
  const [workloadCapabilities, setWorkloadCapabilities] = useState<string[]>([]);
  // THIS machine, as the server sees it now — the caption the share card is
  // unshareable without ("62 tok/s" means nothing without the laptop that
  // produced it). Read from the history rather than from each run, because it
  // travels there for exactly this reason (`AiBenchmarkHistory.machine`): the
  // page has to caption a comparison spanning several runs, and picking one
  // run's block would caption every bar with whichever model happened to be
  // last. `null` until the first fetch answers — `hardwareLine` draws what it
  // has, so a card made in that window is short a line rather than broken.
  const [machine, setMachine] = useState<AiBenchmarkMachine | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Which capability has a run in flight, and on which model. **Keyed by
  // capability, not a single slot**, because that is the unit the server
  // serialises on: one resident model per capability, so a second text run
  // would evict the first's model (a 409) while an image run alongside it is
  // explicitly permitted. A single slot greyed out every other section under a
  // tooltip claiming a per-capability rule, making a legal action unreachable.
  const [inFlight, setInFlight] = useState<RunsInFlight>({});
  // WHEN the currently in-flight run was pressed, epoch ms, keyed by
  // capability like `inFlight` itself — the busy row's elapsed clock
  // (`busyRowText`, lib/benchmark.ts) counts from here, not from whenever the
  // load happens to finish, so "Measuring — 1:24" means what it says: time
  // actually spent, including the load, matching the wall clock a person
  // watching the tab experienced.
  const [runStartedAt, setRunStartedAt] = useState<Record<string, number>>({});
  // Ticks once a second while ANYTHING is in flight, for no reason but to
  // force the busy row's elapsed clock to re-render — `busyRowText` is pure
  // and reads `Date.now()` itself, so this state's VALUE is never read,
  // only its change. Stopped the moment `inFlight` empties: an idle tab
  // re-rendering every second for a clock nothing is showing would be the
  // exact kind of waste `useAiRuntime`'s own idle/active split (aiRuntime.ts)
  // exists to avoid elsewhere on this page.
  const [, setClockTick] = useState(0);
  const anyInFlight = Object.keys(inFlight).length > 0;
  useEffect(() => {
    if (!anyInFlight) return;
    const timer = window.setInterval(() => setClockTick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [anyInFlight]);
  // A run that came back STOPPED rather than measured. Its own state, not
  // `error`: this is not a request failure and must not draw the ErrorBanner —
  // see `stoppedNote`. Cleared when the next run starts, rather than on a timer:
  // a timer that hides an explanation before it has been read is worse than a
  // line that waits to be replaced.
  const [stopped, setStopped] = useState<string | null>(null);
  // The three selectors' raw choices — each `null` until the reader (or a
  // landing `?benchCap=`/`?benchMetric=`/`?benchModel=`) has actually picked
  // one, at which point the matching `resolve*` function stops filling in a
  // default and just honours it. SEEDED from the URL once and held in state
  // thereafter, the same reason the capability filter always was:
  // `writeParams` uses `history.replaceState`, which deliberately fires no
  // navigation event (a selection must not stack a history entry) — so a
  // component that read only the URL would clear the param and go on drawing
  // the old choice.
  //
  // **All THREE are tab-private names, never `?cap=`/`?metric=`/`?model=`.**
  // `?model=` already means something specific and page-wide (the
  // Playground's own picker seed, carried across tabs by `tabHref`), and
  // reusing it here would mean clicking a leaderboard row silently changes
  // what model the Playground preselects on the next tab switch, and a Local
  // tab "Try" link would silently jump this tab's trend chart to an unrelated
  // model. `?cap=` is the same hazard in the OTHER direction, and it shipped
  // once: `focus` used to read AND write the shared `?cap=` — Home's cards
  // seed Playground with it (routes.ts) — so merely opening this tab, with no
  // click at all, resolved a default capability and wrote it into `?cap=`
  // (the effect below), and switching to Playground right after landed on
  // that default as if it had been asked for. A private key cannot collide
  // with anything else later, which is the guarantee "only write after an
  // explicit selection" does not give — that rule has to be re-derived
  // correctly at every future capability this tab grows, and getting it
  // wrong once is exactly how `?cap=` ended up written unconditionally here.
  const [focus, setFocus] = useState<string | null>(() => readParam("benchCap"));
  const [metricParam, setMetricParam] = useState<string | null>(() => readParam("benchMetric"));
  const [modelParam, setModelParam] = useState<string | null>(() => readParam("benchModel"));

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
        setWorkloadCapabilities(history.workloadCapabilities);
        setMachine(history.machine);
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

  // Returns whether a comparable measurement came out of it — a real `run`
  // with `ok: true`. `runAllFor` below is the one caller that reads this
  // return value (to feed `advanceQueue`); a plain single-button click
  // ignores it exactly as it always has.
  const start = async (model: string, capability: string): Promise<{ ok: boolean }> => {
    setError(null);
    setStopped(null);
    // Optimistic only about the BUTTON, never about a result. Functional
    // updates on both halves, because two capabilities can be running at once
    // and a `{...inFlight}` closed over at click time would drop whichever one
    // started in between.
    setInFlight((prev) => ({ ...prev, [capability]: model }));
    setRunStartedAt((prev) => ({ ...prev, [capability]: Date.now() }));
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
      return { ok: run?.ok === true };
    } catch (e) {
      setError((e as Error).message);
      return { ok: false };
    } finally {
      setInFlight((prev) => {
        const next = { ...prev };
        delete next[capability];
        return next;
      });
    }
  };

  // One queue per capability, the same scoping `inFlight` already uses and
  // for the identical reason: the server serialises per capability, so a
  // "Run all" over text-generation and one over embeddings are two
  // independent, legitimately-parallel queues.
  const [queues, setQueues] = useState<Record<string, BenchmarkQueue>>({});

  // **A REF, mirroring `queues`, because `runAllFor`'s loop needs to observe
  // a Stop that happens WHILE it is awaiting `start` — and React state does
  // not give it that.** The bug this fixes: the loop used to drive
  // `advanceQueue` off its own LOCAL `queue` variable, reassigning it only
  // from `advanceQueue`'s own return value, while `stopAllFor` called
  // `setQueues` — a state update the loop's closed-over variable never reads
  // back. So `requestQueueStop` could be dispatched all day and the running
  // loop's `queue.stopped` stayed `false` forever: Stop killed the in-flight
  // model (recorded as a failure) and every model after it started anyway.
  // `queuesRef.current` is written SYNCHRONOUSLY by `setQueue` below,
  // wherever `setQueues` used to be called directly, so a read of it right
  // after an `await` sees whatever `stopAllFor` wrote in the meantime —
  // unlike `queues` itself, which is only current as of the last render.
  // `observeStop` (benchmarkQueue.ts) is the pure fold that turns that
  // observation into the queue the loop advances next; see its own docstring
  // for the exact mechanism and why the bug shipped past every test in
  // `benchmarkQueue.test.ts` (they reassign the SAME variable this loop used
  // not to).
  const queuesRef = useRef<Record<string, BenchmarkQueue>>({});
  const setQueue = (capability: string, queue: BenchmarkQueue) => {
    queuesRef.current = { ...queuesRef.current, [capability]: queue };
    setQueues(queuesRef.current);
  };

  // Drives `benchmarkQueue.ts`'s pure state machine with the REAL requests —
  // this is the one place that awaits `start` in a loop rather than firing
  // it once. Deliberately lives here, not inside `CapabilitySection`: a
  // capability's queue must keep running even if the reader switches to a
  // DIFFERENT capability's card (`CapabilitySection` is remounted per
  // `selected`, via its own `key`), the same way a single in-flight run
  // already survives a capability switch. Switching the METRIC selector
  // never touches this at all — metric is a display choice, the queue does
  // not read it.
  const runAllFor = async (capability: string, models: string[]) => {
    if (models.length === 0) return;
    let queue = startQueue(capability, models);
    setQueue(capability, queue);
    while (queue.current) {
      const model = queue.current;
      const { ok } = await start(model, capability);
      // Re-read the REF, not the closed-over `queue`, so a `requestQueueStop`
      // written by `stopAllFor` while `start` was in flight is actually seen
      // — see the ref's own comment above for the bug this closes.
      const observed = queuesRef.current[capability]?.stopped ?? false;
      queue = advanceQueue(observeStop(queue, observed), { model, ok });
      setQueue(capability, queue);
    }
  };

  // Stop: mark the queue so it will not start another model once the
  // in-flight one settles (`requestQueueStop`, pure), AND separately
  // interrupt that in-flight request — through `cancelAiGeneration`
  // (`POST /api/ai/cancel`), the SAME mechanism a single text run's own Stop
  // button already uses elsewhere in the app (Playground's `TextStage`). This
  // used to call `unloadAiModel` instead, on the theory that unloading the
  // model a benchmark is using would resolve its held-open request with
  // `cancelled: true` — **that theory was wrong.** `unload` terminates the
  // worker PROCESS; it does not touch the in-flight generation's own
  // `cancelled` flag, so the held-open `/generate` read does not get a clean
  // `{"cancelled": true}` frame — it gets the process disappearing out from
  // under it (`ConnectionResetError`/`IncompleteRead`), which `run()`'s
  // generic `except BaseException` then recorded as a normal `ok:false`
  // failure — a real, permanent "this model failed" row in the one history
  // this feature exists to keep trustworthy, for a run somebody stopped on
  // purpose. `cancel_generation` (`ai/supervisor.py`, untouched) is the
  // COOPERATIVE channel: it asks the resident worker to set its own
  // `cancelled` flag, which `_measure_text`/`_measure_image`/
  // `_measure_transcript` all already recognise and turn into
  // `benchmark.Cancelled` — nothing stored, exactly like a single run's ✕
  // (see `benchmark.Cancelled`'s own docstring). `start` already turns the
  // resulting `{cancelled: true}` into a `stoppedNote` and an `ok: false`
  // for the queue, unchanged by this switch.
  const stopAllFor = (capability: string) => {
    // Read and write through the REF, not the `queues` state closure — the
    // same reason `runAllFor`'s loop does: this is the write half of the
    // exact race that comment describes, and a click handler closing over a
    // stale `queues` from its own last render is as capable of missing a
    // concurrent update as the loop was.
    const queue = queuesRef.current[capability];
    if (queue) setQueue(capability, requestQueueStop(queue));
    if (queue?.current) cancelAiGeneration(capability).catch(() => {});
  };

  const forget = async (id: string) => {
    setError(null);
    try {
      // The endpoint answers with the fresh history, so the page adopts state it
      // just re-read rather than splicing an array it hopes still matches disk —
      // the same discipline the Local tab's delete follows.
      const history = await deleteAiBenchmarks([id]);
      setRuns(history.runs);
      setWorkloadCapabilities(history.workloadCapabilities);
      setMachine(history.machine);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // Whether the history has answered at all — the same predicate the early
  // return below used to gate on. Named here because the sync effect right
  // after it needs to know the SAME thing: don't write a resolved default
  // into the URL while `runs` is still `null` and every count below therefore
  // reflects no recorded run yet, which could clobber an explicit param with a
  // premature guess the moment the real counts land one render later.
  const loading = !data && runs === null;

  // Which capabilities the selector offers: every one this machine has a
  // downloaded model for, UNION every one with a recorded run. The union is
  // what keeps a history reachable after its model was deleted — the runs are
  // still the truth about what happened, and dropping it from the list would
  // silently hide them.
  //
  // **The two SPECULATIVE sources — `CAPABILITY_ORDER` and the on-disk
  // repos — are filtered to `workloadCapabilities` first; recorded RUNS
  // never are.** A capability with a downloaded model but no workload (video
  // generation today) would otherwise render a section whose only Run
  // outcome is a 400 — `/api/ai/benchmark`'s own refusal, since the POST
  // route checks the identical `benchmark.WORKLOADS` this list is filtered
  // against. A recorded run, by contrast, could only exist for a capability
  // that HAD a workload at the time it ran — the same route already refused
  // it otherwise — so it is real history regardless of what the CURRENT
  // table says, and hiding it would be the same silent loss the comment
  // above already argues against for a deleted model.
  const all = orderCapabilities([
    ...new Set([
      ...benchmarkableCapabilities(
        [...CAPABILITY_ORDER, ...repos.map((r) => r.capability).filter((c): c is string => !!c)],
        workloadCapabilities,
      ),
      ...(runs ?? []).map((r) => r.capability),
    ]),
  ]);

  const capabilityCounts = runCountsByCapability(runs ?? []);
  const selected = resolveCapability(all, focus, capabilityCounts);

  // Everything below is scoped to the ONE selected capability, computed here
  // (not inside a child component) because the URL-sync effect needs the
  // final answers — `selectedMetric` and `selectedModel` — to write, and a
  // hook cannot read state a child component holds.
  const capabilityRepos = selected ? repos.filter((r) => r.capability === selected) : [];
  const capabilityRuns = selected && runs !== null ? runsFor(runs, selected) : null;

  // The metric selector's options recompute per capability (and per its own
  // runs) — `availableMetrics` (lib/benchmark.ts) drops anything nothing has
  // measured yet, so the dropdown never offers an option that would render an
  // empty chart.
  const metricSpecs = selected ? availableMetrics(selected, capabilityRuns ?? []) : [];
  const selectedMetric = resolveMetric(metricSpecs, metricParam);

  // The leaderboard — ranked best-first BY THE SELECTED METRIC, never pinned
  // to the capability's primary. `leaderboard` (lib/benchmark.ts) owns the
  // ordering and every bar's length, so the rule about which way a metric
  // points is tested once rather than guessed again here.
  const latest = new Map<string, ModelLatest>(
    (capabilityRuns ? latestByModel(capabilityRuns, selectedMetric) : []).map((row) => [row.model, row]),
  );
  // Models with a card, then models that only have HISTORY — a run whose model
  // has since been deleted is still a fact, and it belongs in the ranking
  // rather than nowhere.
  const orphans = [...latest.keys()].filter((model) => !capabilityRepos.some((r) => r.id === model));
  const gone = new Set(orphans);
  const ranked: LeaderboardRow[] = leaderboard(selectedMetric, [
    ...capabilityRepos.map((r) => ({ model: r.id, row: latest.get(r.id) ?? null })),
    ...orphans.map((model) => ({ model, row: latest.get(model) ?? null })),
  ]);

  // The trend chart's model: the URL's `?benchModel=` when it names a model IN
  // THIS CAPABILITY's leaderboard, otherwise the one with the most recorded
  // runs here — ties broken by the leaderboard's OWN rank (`ranked`'s order),
  // so a tie breaks toward whichever model is already reading as the better
  // one rather than an arbitrary list order.
  const modelCounts = capabilityRuns ? runCountsByModel(capabilityRuns) : {};
  const selectedModel = resolveModel(ranked.map((r) => r.model), modelParam, modelCounts);
  const trendRuns = selectedModel && capabilityRuns
    ? capabilityRuns.filter((r) => r.model === selectedModel)
    : [];

  // Keep the URL in sync with whatever is actually selected — landing on a
  // default (no param yet) writes it in, and choosing a different capability,
  // metric or model updates it — via `replaceState` (`writeParams`), never a
  // navigation: a selector change is not a page to go Back to. Runs after
  // render rather than during it, since writing history is a side effect.
  //
  // **This hook must run on EVERY render, loading or not** — React throws
  // ("Rendered more hooks than during the previous render") the moment a hook
  // sits below a conditional return, because the loading render would then
  // call one fewer hook than the render after it. The `loading` guard
  // therefore lives INSIDE the effect, on the WRITE, not on the hook: skipping
  // the call while `runs` is still `null` is what stops a not-yet-known
  // history from clobbering an explicit param with a premature guess one
  // render before the real counts arrive.
  useEffect(() => {
    if (loading) return;
    writeParams({
      benchCap: selected,
      benchMetric: selectedMetric?.key ?? null,
      benchModel: selectedModel,
    });
  }, [loading, selected, selectedMetric, selectedModel]);

  if (loading) return <SkeletonLines rows={6} label="Loading benchmarks" />;

  return (
    <div className="am-bench">
      <ErrorBanner>{error}</ErrorBanner>
      {stopped && <p className="am-bench-stopped">{stopped}</p>}
      {/* THE CAPABILITY SELECTOR. A native `<select>`, unlabelled ON SCREEN — the
          reader asked for the redundant "Capability" caption gone, since the
          option text (a capability's own name, e.g. "Text generation")
          already says what the control is. `aria-label` keeps it an
          accessible control without reintroducing the visible chrome. This is
          the one selector that stays in the page-level toolbar rather than
          inside the card below — it chooses WHICH section you are looking
          at, which is a page-level question, unlike Metric (now inside
          `CapabilitySection`, right beside the instruments it actually
          governs, and unlabelled on screen there too — same reasoning, an
          option's own text names the metric, so both selects now carry their
          name in `aria-label` alone). No run count in the option labels
          any more — the reader asked for that gone too, and `capabilityCounts`
          still drives `resolveCapability`'s default pick, it just no longer
          prints itself. */}
      <div className="am-bench-controls">
        <select
          id="am-bench-cap"
          aria-label="Capability"
          className="field-control am-bench-capsel-input"
          value={selected ?? ""}
          onChange={(e) => setFocus(e.target.value)}
        >
          {all.map((capability) => (
            <option key={capability} value={capability}>
              {capabilityLabel(capability)}
            </option>
          ))}
        </select>
      </div>
      {selected && (
        <CapabilitySection
          key={selected}
          capability={selected}
          metric={selectedMetric}
          metricSpecs={metricSpecs}
          onSelectMetric={setMetricParam}
          runs={capabilityRuns}
          machine={machine}
          ranked={ranked}
          gone={gone}
          selectedModel={selectedModel}
          trendRuns={trendRuns}
          onSelectModel={setModelParam}
          inFlight={inFlight}
          runStartedAt={runStartedAt}
          runtime={scan.runtime}
          onRun={start}
          onForget={forget}
          queue={queues[selected]}
          onRunAll={runAllFor}
          onStopAll={stopAllFor}
        />
      )}
    </div>
  );
}

function CapabilitySection({
  capability,
  metric,
  metricSpecs,
  onSelectMetric,
  runs,
  machine,
  ranked,
  gone,
  selectedModel,
  trendRuns,
  onSelectModel,
  inFlight,
  runStartedAt,
  runtime,
  onRun,
  onForget,
  queue,
  onRunAll,
  onStopAll,
}: {
  capability: string;
  /** The reader's SELECTED metric — not necessarily the primary — resolved by
   *  `BenchmarkTab`. Null only for a capability this frontend does not know. */
  metric: MetricSpec | null;
  /** The Metric `<select>`'s own options — every metric this capability
   *  offers that at least one run has actually measured (`availableMetrics`,
   *  lib/benchmark.ts). Lives here, not in the page-level toolbar: the metric
   *  changes what the chart plots and what the rows rank by, both of which
   *  are inside this card, unlike Capability (still in the toolbar — it
   *  picks WHICH card). Empty for a capability with nothing to select. */
  metricSpecs: MetricSpec[];
  onSelectMetric: (key: string) => void;
  /** null while the history has not answered. */
  runs: AiBenchmarkRun[] | null;
  /** This machine, for the share card's caption — null until the history has
   *  answered. Nothing on screen reads it: a reader looking at their own
   *  laptop does not need it spelled out, but a card leaving the laptop does. */
  machine: AiBenchmarkMachine | null;
  /** Every model this capability knows about, ranked best-first — computed by
   *  `BenchmarkTab` (`leaderboard`), since its length already answers "is
   *  there anything to show" (repos + history, deleted models included). */
  ranked: LeaderboardRow[];
  /** Which of `ranked`'s models are orphans — on disk no longer, history
   *  only. A model whose weights are gone still shows its history and still
   *  answers a click (the trend chart draws it fine); it just has no button. */
  gone: Set<string>;
  /** The model the trend chart is currently showing, or null when there is
   *  truly nothing to pick from. */
  selectedModel: string | null;
  /** `selectedModel`'s own runs, already filtered — `ModelTrendChart` draws
   *  nothing else. */
  trendRuns: AiBenchmarkRun[];
  onSelectModel: (model: string) => void;
  /** Every capability's in-flight run, not just this one's — `runButtonState`
   *  reads its own key out, which keeps the "which capability blocks which"
   *  rule in one tested place rather than in each section's props. */
  inFlight: RunsInFlight;
  /** When the in-flight run on THIS capability was pressed, epoch ms — absent
   *  for a capability with nothing running. Feeds the busy row's elapsed
   *  clock (`busyRowText`); `BenchmarkTab` is the one place that knows when a
   *  click happened, so it owns this rather than each row inventing its own
   *  start time. */
  runStartedAt: Record<string, number>;
  /** The AI runtime table (`lib/aiRuntime.ts`), already polled by the page —
   *  read here ONLY to answer "is the in-flight model still loading, or is it
   *  measuring now" (`busyRowText`'s `stillLoading`). Reusing this poll is
   *  why the busy row does not need one of its own. */
  runtime: AiRuntime;
  onRun: (model: string, capability: string) => void;
  onForget: (id: string) => void;
  /** This capability's own "Run all" queue, or undefined before one has ever
   *  been started here. Persists across a capability switch (owned by
   *  `BenchmarkTab`, keyed by capability) — this component only reads it. */
  queue: BenchmarkQueue | undefined;
  onRunAll: (capability: string, models: string[]) => void;
  onStopAll: (capability: string) => void;
}) {
  // The trend instrument's shape — `trendKind` (lib/benchmark.ts) decides
  // "none" / "single" / "trend" from how many of `trendRuns` actually
  // measured `metric`. Computed here, once, rather than inline in the JSX
  // below: both the compact single-run state and the full chart need the
  // series `chartSeries` already produced, and a second call would just be
  // the first one's answer computed twice.
  const trendSeries = metric ? chartSeries(trendRuns, metric).series[0] ?? null : null;
  const trend = trendKind(trendSeries?.points.length ?? 0);
  // The comparison chart's own data — every model with a real value, ranked
  // best-first, direction included (`comparisonBars`, lib/benchmark.ts).
  const bars = comparisonBars(ranked, metric);
  // Every model "Run all" would attempt — everything in `ranked` except the
  // `gone` ones (no weights, no button to press). Recomputed on every render
  // rather than cached in state: it has to reflect whatever is on disk RIGHT
  // NOW at the moment the button is pressed, not whatever it was when the
  // queue started (a model deleted mid-queue is skipped the same way a
  // single Run press already can't reach it).
  const runnable = queueableModels(ranked, gone);
  const status = queue ? queueStatus(queue) : null;
  // Blocked by ANY in-flight run on this capability, not just a queue's own —
  // a manual single "Run again" press sets the identical `inFlight` slot a
  // queue's own `start()` calls do, and the server allows only one resident
  // model per capability either way.
  const busy = inFlight[capability] !== undefined;
  // The busy row's own text, computed here rather than in `BenchmarkRow`:
  // this is where `capability` and `inFlight` are both already in scope, and
  // a single site keeps "which phase" (the runtime's own answer) and "how
  // long" (this tab's own click timestamp) from drifting into two different
  // readings for two different rows of the same run.
  const busyModel = inFlight[capability];
  const stillLoading = busyModel
    ? runtime.loaded.some(
        (m) => m.model === busyModel && m.capability === capability &&
          m.state !== "ready" && m.state !== "error",
      )
    : false;
  const busyText = busyModel
    ? busyRowText(stillLoading, runStartedAt[capability] ?? Date.now(), Date.now())
    : null;
  // The device most of THIS section's models last ran on — the hardware
  // doesn't change per model, so a row's own detail line (`BenchmarkRow`
  // below, via `rowDetail`) drops it whenever it MATCHES this, and keeps it
  // only for the outlier that differs (see `commonDevice`, lib/benchmark.ts).
  const expectedDevice = commonDevice(
    ranked.map((r) => r.row).filter((row): row is ModelLatest => row !== null),
  );
  // The app version the plotted numbers were MEASURED under — the newest run's
  // (`runs` is oldest-first), not the running build. The app is part of what a
  // benchmark measures, so a card drawn after an upgrade must keep naming the
  // version that produced the bars. Only the share card reads this; nothing on
  // screen does (a per-run "Details" expander already shows each run's own).
  const measuredVersion = runs && runs.length > 0 ? runs[runs.length - 1]!.appVersion : null;

  return (
    <section className="am-section">
      {/* The heading STAYS a plain `<h3>` — this `<section>` also frames the
          leaderboard (every model, not just the selected one) and the run
          archive below, and a landmark section needs its own accessible
          name rather than borrowing a sibling `<select>`'s current value, the
          select's value changes on click, the heading should not blink with
          it.

          **The Metric select lives HERE now, not in the page-level toolbar
          above** — it changes what the comparison chart plots and what the
          leaderboard rows rank by, and both of those are inside this card;
          Capability (still in the toolbar) picks WHICH card, a page-level
          question. Hidden rather than disabled when there is nothing to pick
          from — a select with zero options renders as an empty,
          clickable-looking box, and there is nothing honest for it to say.

          **The unit and the direction cue are IN the option text**
          (`metricOptionLabel`, lib/benchmark.ts — "Speed (× realtime)", "Peak
          memory (lower is better)"), not in a badge beside the select. Two
          badges have now been tried and both failed on the same axis. Trailing
          the select it was a third mark in a row that already holds an `<h3>`
          and a Share button, saying nothing the control it followed could not
          say itself. Moved to the LEFT of the select — where the "Metric"
          caption used to be, so the row would read unit-then-choice — it broke
          outright on transcription: `× realtime` is a multiplier SUFFIX, it
          parses only when it trails a number ("1.4× realtime"), and alone in a
          bordered pill beside a control it read as the dismiss ✕ of a
          removable filter chip — a shape this app uses elsewhere for exactly
          that (D448's Tasks filter pills). Inside the option the words have a
          subject again, the empty-unit metric (`Peak memory` formats through
          the byte formatter and has no unit string, so the pill there held
          only "lower is better") stops being a special case, and the fact is
          stated where the choice is made rather than beside it.

          The CUE is one-sided on purpose: "lower is better" for exactly the
          metrics where the ordinary "longer bar / bigger number wins" habit
          reads backwards (Peak memory, Load time, …), nothing extra for the
          metrics where that habit already reads right — labelling both
          directions everywhere would bury the one case actually worth
          flagging. Read by both instruments below (the comparison chart's
          shorter-is-better bars and the trend chart's downward-is-better line
          invert the same way), and still said only once: the old
          per-model-name-plus-unit pill on the trend heading below is GONE
          too. */}
      <div className="am-section-head am-bench-section-head">
        <h3 className="am-section-title">{capabilityLabel(capability)}</h3>
        {/* The head's controls, as one group: the Metric select and — only
            when there is actually a chart to send — Share. Share sits HERE
            rather than over the chart because what it shares is this
            section's current selection (capability + metric), which is
            precisely what these two controls between them decide. */}
        <div className="am-bench-headtools">
          {metricSpecs.length > 0 && (
            <div className="am-bench-metricsel">
              <select
                id={`am-bench-metric-${capability}`}
                className="field-control am-bench-capsel-input"
                aria-label="Metric"
                value={metric?.key ?? ""}
                onChange={(e) => onSelectMetric(e.target.value)}
              >
                {metricSpecs.map((spec) => (
                  <option key={spec.key} value={spec.key}>
                    {metricOptionLabel(spec)}
                  </option>
                ))}
              </select>
            </div>
          )}
          {/* Rendered on exactly the condition the chart itself is (below): a
              Share button above "no runs recorded yet" offers to send an empty
              axis. */}
          {metric && bars.length > 0 && (
            <ShareChartButton
              card={{
                capability,
                metric,
                bars,
                machine,
                device: expectedDevice,
                appVersion: measuredVersion,
              }}
            />
          )}
        </div>
      </div>

      {runs === null ? (
        <SkeletonLines rows={2} label={`Loading ${capabilityLabel(capability)} benchmarks`} />
      ) : ranked.length === 0 ? (
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
            {tabLabel("local")} tab
          </a>
          .
        </p>
      ) : (
        <>
          {/* INSTRUMENT ONE, THE HERO: the comparison chart — one bar per
              BENCHMARKED model, ranked best-first. This is what answers the
              question a reader arrives with ("which of these is fastest"),
              and it renders whenever more than one model has been
              benchmarked at all — the normal case. It replaces an earlier
              design where the trend chart tried to be the hero: that needs
              TWO RUNS OF THE SAME MODEL, and real usage spreads a handful of
              runs across several different models far more often than it
              re-runs one, so the trend chart's "single" state fired for
              nearly every model and the page had no chart at all. Failed and
              never-benchmarked models have nothing to plot (`comparisonBars`
              excludes them) but stay fully visible in the rows below, which
              is where their action — Run, Details — lives anyway. */}
          {metric && bars.length > 0 ? (
            <ComparisonChart bars={bars} metric={metric} />
          ) : (
            <p className="am-group-note">
              No {(metric?.label ?? "runs").toLowerCase()} recorded for any {capabilityLabel(capability).toLowerCase()} model yet — press Run on one below.
            </p>
          )}
          {/* RUN ALL — benchmarks every runnable model in this section, one
              after another, reusing the exact same `start()` a single "Run"
              press does (`BenchmarkTab`'s `runAllFor`), so a queued run and a
              manual one are indistinguishable to the server and to the
              history. Sits between the two chart instruments and the ledger
              rows it drives, since it acts on exactly that list.

              **The label states the true cost up front** — "Run all 6
              models" — rather than reading like a single cheap click; each
              one is a COLD load (the benchmark unloads whatever it loaded),
              so six Whisper models is many minutes and several GB of
              repeated downloads-from-disk. `runnable.length === 0` hides the
              button entirely rather than disabling it — there is nothing
              honest for a Run All button to say when every model is
              already `gone`. */}
          {runnable.length > 0 && (
            <div className="am-bench-runall">
              {status === "running" && queue ? (
                <>
                  <span className="am-bench-runall-progress" role="status">
                    <span className="am-runtime-dot" />
                    Running {queue.started} of {queue.models.length} —{" "}
                    {shortModelName(queue.current!)}
                  </span>
                  <button
                    type="button"
                    className="cc-iconbtn"
                    onClick={() => onStopAll(capability)}
                    title="Stop"
                    aria-label="Stop"
                  >
                    {MenuIcons.stop}
                  </button>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    className="cc-btn"
                    disabled={busy}
                    title={
                      busy
                        ? `Waiting for the ${inFlight[capability]} benchmark to finish`
                        : `Benchmark all ${runnable.length} models in this section, one after another — each is a cold load and this can take a while`
                    }
                    onClick={() => onRunAll(capability, runnable)}
                  >
                    Run all {runnable.length} models
                  </button>
                  {/* The one category Run All silently leaves out — say so,
                      rather than a count that quietly excludes it with no
                      explanation. Each `gone` row already states its own
                      reason ("not on this machine any more"); this is the
                      same fact stated once for the button that skips all of
                      them at once. */}
                  {gone.size > 0 && (
                    <span className="am-bench-runall-note">
                      {gone.size} not on this machine — skipped
                    </span>
                  )}
                  {/* The tally from the LAST completed or stopped run, until
                      the next one starts (a fresh `startQueue` clears
                      `results`, which flips `status` back to "running" before
                      this branch is ever reached again). Says WHICH ended it
                      — a stop reads differently from simply finishing. */}
                  {status && queue && (
                    <span className="am-bench-runall-tally">
                      {(() => {
                        const tally = queueTally(queue);
                        const parts = [`${tally.succeeded} succeeded`, `${tally.failed} failed`];
                        if (status === "stopped") parts.push(`${tally.remaining} not run — stopped`);
                        return parts.join(", ");
                      })()}
                    </span>
                  )}
                </>
              )}
            </div>
          )}
          {/* INSTRUMENT TWO: the ledger — every model, ranked, one line each,
              with the action (Run, Details) the chart above has no room for.
              Doubles as the trend chart's picker below: click a row (or focus
              it and press Enter/Space) to choose which model that instrument
              is showing. No bar here any more — that was the SAME
              proportional comparison the chart above now draws once,
              properly, with an axis; two copies of one comparison was the
              duplicated ink this replaces. */}
          <div className="am-bench-rows">
            {ranked.map(({ model, row }) => {
              const button = gone.has(model)
                ? undefined
                : runButtonState(capability, model, inFlight, row !== null);
              return (
                <BenchmarkRow
                  key={model}
                  model={model}
                  row={row}
                  metric={metric}
                  expectedDevice={expectedDevice}
                  button={button}
                  busyText={model === busyModel ? busyText : null}
                  gone={gone.has(model)}
                  selected={model === selectedModel}
                  onSelect={() => onSelectModel(model)}
                  onRun={() => onRun(model, capability)}
                />
              );
            })}
          </div>
          {/* INSTRUMENT THREE, SECONDARY: the trend — one model's own
              history, a real time axis. Earns its space once someone has
              actually re-run a model; until then it says so plainly rather
              than drawing an empty frame (see `trendKind` below).

              **Titled by the MODEL alone now** — no metric badge here any
              more. The section head above already names the metric, in the
              `<select>`'s own option text (unit and direction cue included,
              via `metricOptionLabel`), and repeating it a few lines
              down was the exact "drawn twice" duplication that select's own
              move into this card was meant to end. A model with a card gone
              from disk (`gone`) still gets a title here: its history is
              still what this instrument is showing. */}
          {metric && selectedModel && (
            <div className="am-bench-trend-head">
              <h4 className="am-bench-trend-title">{shortModelName(selectedModel)}</h4>
            </div>
          )}
          {/* Three shapes, not two. A single measured point is NOT a trend:
              it has no before to compare against, and drawing it in the same
              ~400px frame as a real chart was the actual bug (one dot, ~95%
              empty frame, the largest element on the page). So it gets its
              own compact state — the value stated plainly, at a fraction of
              the height — and only two-or-more points earn
              `ModelTrendChart`. */}
          {!metric || !selectedModel ? null : trend === "trend" ? (
            <ModelTrendChart runs={trendRuns} metric={metric} />
          ) : trend === "single" ? (
            <div className="am-bench-trend-single">
              <span className="am-bench-trend-value">
                {formatMetricSpecValue(trendSeries!.points[0]!.y, metric)}
              </span>
              <span className="am-bench-trend-note">one run · run again to see a trend</span>
            </div>
          ) : (
            <p className="am-group-note">
              No {metric.label.toLowerCase()} recorded for {shortModelName(selectedModel)} yet.
            </p>
          )}
        </>
      )}

      {/* The archive, under both instruments: the ranked rows and the trend
          are the current answer, and this is the evidence behind them. Always
          shows every run for the capability, independent of the metric/model
          selection above — it is the raw record, not a filtered view. */}
      {runs !== null && runs.length > 0 && <RunTable capability={capability} runs={runs} onForget={onForget} />}
    </section>
  );
}

function BenchmarkRow({
  model,
  row,
  metric,
  expectedDevice,
  button,
  busyText,
  gone,
  selected,
  onSelect,
  onRun,
}: {
  model: string;
  row: ModelLatest | null;
  /** The SELECTED metric — what the headline reads. Decided in
   *  `BenchmarkTab`/`leaderboard`, never here. */
  metric: MetricSpec | null;
  /** The device most of this section's OTHER models report — `rowDetail`
   *  drops this row's own device when it matches, and keeps it when it
   *  doesn't (`commonDevice`, lib/benchmark.ts). */
  expectedDevice: string | null;
  /** What the Run button says and whether it can be pressed — decided by
   *  `runButtonState`, never here: the rule about which run blocks which button
   *  is exactly the thing a screenshot cannot check. Absent for a `gone` row,
   *  which has no button at all. */
  button?: RunButtonState;
  /** `busyRowText`'s own answer for THIS model, or null when it is not the one
   *  running — computed once in `CapabilitySection` (`busyRowText`,
   *  lib/benchmark.ts) from the AI runtime's own phase plus the tab's own
   *  click timestamp, never invented here. */
  busyText?: string | null;
  /** The model is no longer on disk; its history is shown, its button is not. */
  gone?: boolean;
  /** This row is the one the trend chart above is currently showing. */
  selected: boolean;
  /** Choose this model for the trend chart — a click anywhere on the row, or
   *  Enter/Space while it has focus. */
  onSelect: () => void;
  onRun?: () => void;
}) {
  // The one line beyond the headline — TTFT, load time, device, or a failed
  // run's own error — behind an expander so it never dominates the row. Only
  // drawn when there is something to say: a row whose whole story fits the
  // headline gets no expander at all.
  const detail = row
    ? row.latest.ok
      ? rowDetail(row.latest, metric, expectedDevice)
      : failureReason(row.latest)
    : null;

  return (
    // A div-as-button, not a `<button>`: the Run button lives inside this row
    // (the same shape the Playground's model cards settled on, D428) and a
    // button inside a button is markup browsers are free to mangle. Clicking
    // Run also selects the model for the trend chart — a harmless, arguably
    // useful side effect (you are clearly interested in that model right
    // now), so its own click is left to bubble here rather than stopped.
    <div
      className={"am-bench-row" + (selected ? " selected" : "")}
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
    >
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
          // A plain spinner, not `ModelProgress`: that component draws a
          // download-manager row's OWN detail and byte counts, and reading it
          // here would be a second, possibly-stale copy of exactly what the
          // corner already shows for `ai/benchmark.py`'s own measurement row
          // (see that module's docstring). This is a DIFFERENT view of the
          // same run: phase plus a real elapsed clock (`busyText`,
          // `busyRowText` in lib/benchmark.ts) rather than an invented
          // percentage — "Loading weights into memory…" while the AI runtime
          // still reports this model coming up, then "Measuring — 1:24"
          // ticking from the moment Run was pressed.
          <span className="am-bench-busy" role="status">
            <span className="am-runtime-dot" />
            {busyText}
          </span>
        ) : row ? (
          <>
            {/* No bar here any more — the comparison chart above draws the
                SAME proportional comparison once, properly, with a real
                axis. Two copies of it (a mini-bar per row AND a chart) was
                the duplicated ink this row is compacted to remove. */}
            <span className="am-bench-headline">{rowHeadline(row.latest, metric)}</span>
            {row.delta && (
              // The sign is not the meaning — on a lower-is-better metric a
              // negative change is the improvement — so `better` decides the
              // class and the sign is only printed.
              <span className={"am-bench-delta" + (row.delta.better ? " better" : " worse")}>
                {row.delta.percent >= 0 ? "+" : ""}
                {row.delta.percent.toFixed(1)}%
              </span>
            )}
            {detail && (
              <details
                className="am-bench-rowdetail"
                // A click inside the expander (opening it, or on the summary)
                // is not a model selection — without this, opening "Details"
                // on a row you did NOT mean to select would select it anyway.
                onClick={(e) => e.stopPropagation()}
              >
                {/* Icon-only, per the icon-buttons pass — but still a real
                    `<summary>`, so the disclosure semantics (native toggle,
                    keyboard, screen-reader "expanded/collapsed" state) are
                    untouched. `aria-label`/`title` carry the exact words the
                    icon replaced; `am-bench-rowdetail-chevron` is what rotates
                    the glyph 90° on `[open]` (ai-models.css) rather than
                    swapping to a second path. */}
                <summary
                  className="am-bench-rowdetail-chevron"
                  aria-label={row.latest.ok ? "Details" : "Failed — details"}
                  title={row.latest.ok ? "Details" : "Failed — details"}
                >
                  {MenuIcons.chevron}
                </summary>
                <p>{detail}</p>
              </details>
            )}
          </>
        ) : (
          <span className="am-bench-never">Never benchmarked</span>
        )}
      </div>
      {!gone && button && (
        // No `stopPropagation` — see the row's own comment above. Pressing
        // Run bubbles its click up to the row's `onClick` too, which selects
        // this model for the trend chart. That is a plain assignment
        // (`onSelect` sets state to the same model `onRun` is about to run),
        // not a toggle, so it cannot fight the button's `disabled` state —
        // there is nothing here for the two handlers to disagree about.
        <button
          type="button"
          className="cc-iconbtn"
          disabled={button.blocked}
          onClick={onRun}
          title={button.title}
          aria-label={button.label}
        >
          {/* `button.busy`'s spinner is still disabled (`button.blocked` is
              true whenever `busy` is — `runButtonState`, lib/benchmark.ts) —
              this is a status glyph on a dead button, not a second way to
              start or stop the run. `.am-icon-spin` (ai-models.css) is the
              only thing that turns the static ring into motion; the glyph
              itself does not encode spinning. */}
          <span className={button.busy ? "am-icon-spin" : undefined}>
            {button.busy ? MenuIcons.spinner : MenuIcons.play}
          </span>
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
  // Labelled by the capability's own PRIMARY metric at render time —
  // "Throughput", "Per step" — because one heading cannot name four different
  // things. Deliberately the primary, not the tab's current selection: the
  // archive is the raw record, independent of whatever the reader has the
  // leaderboard/trend chart showing right now.
  { key: "metric", label: "", numeric: true, value: primaryValue },
  { key: "memory", label: "Memory", numeric: true, value: (r) => r.peakResidentBytes },
  { key: "load", label: "Load", numeric: true, value: (r) => r.loadSeconds },
  { key: "device", label: "Device", numeric: false, value: (r) => r.device },
  { key: "version", label: "App", numeric: false, value: (r) => r.appVersion },
];

/** Every run for one capability, newest first by default.
 *
 *  **Collapsed by default**, because it is the archive and the two instruments
 *  above are the answer: a section with four models and thirty runs would
 *  otherwise open as a wall of numbers with the current state buried at the
 *  top of it. The summary line says how many are hiding, so nothing is
 *  invisible — only folded.
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

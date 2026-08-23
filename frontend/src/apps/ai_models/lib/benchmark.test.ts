import { describe, expect, it } from "bun:test";
import type { AiBenchmarkRun } from "@platform/lib/api";
import {
  DASH,
  availableMetrics,
  chartAxisTicks,
  chartSeries,
  commonDevice,
  comparisonBars,
  defaultCapability,
  defaultModel,
  failureReason,
  formatDuration,
  formatLoad,
  formatMemory,
  formatMetricSpecValue,
  formatPrimary,
  formatRunDate,
  formatRunTime,
  latestByModel,
  leaderboard,
  metricUnitAndCue,
  metricValueForSpec,
  middleEllipsis,
  niceAxisMax,
  niceAxisTicks,
  orderCapabilities,
  paddedAxisMax,
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
  summaryLine,
  trendKind,
  yAxisTicks,
  type MetricSpec,
  type ModelLatest,
} from "@apps/ai_models/lib/benchmark";

const MACHINE = { platform: "Darwin", arch: "arm64", cpuCount: 10, totalMemoryBytes: 3.2e10 };

// Two metric fixtures reused across the axis-tick tests below: one ordinary
// (already in its own display unit — a tick is just a rounded number) and
// one byte-valued (`peakResidentBytes` stores bytes, but a reader reads
// memory in MB/GB — see `formatMetricSpecValue`'s own comment on why that key
// is special-cased to `formatSize`).
const REALTIME: MetricSpec = {
  key: "realtimeFactor",
  label: "Speed",
  unit: "× realtime",
  higherIsBetter: true,
  digits: 1,
};

const MEMORY: MetricSpec = {
  key: "peakResidentBytes",
  label: "Peak memory",
  unit: "",
  higherIsBetter: false,
  digits: 0,
};

const DECODE_TIME: MetricSpec = {
  key: "totalSeconds",
  label: "Decode time",
  unit: "s",
  higherIsBetter: false,
  digits: 1,
};

function run(over: Partial<AiBenchmarkRun> = {}): AiBenchmarkRun {
  return {
    id: Math.random().toString(16).slice(2),
    startedAt: 1_700_000_000,
    capability: "text-generation",
    model: "org/model",
    runner: "mlx-text",
    device: "mps",
    appVersion: "0.4.44",
    ok: true,
    error: null,
    loadSeconds: 8.4,
    peakResidentBytes: 5_600_000_000,
    machine: MACHINE,
    workload: { name: "text-128-tokens", revision: 1, params: {} },
    metrics: { tokensPerSecond: 42.1, ttftMs: 310, promptTokensPerSecond: 90, outputTokens: 128 },
    ...over,
  };
}

// -- the primary metric per capability ----------------------------------------

describe("primaryMetric", () => {
  it("names one metric per capability, with its unit", () => {
    // The four are pinned by VALUE, not just by presence: the unit is what the
    // reader compares two models on, and a mislabelled one ("tok/s" on a
    // seconds-per-step figure) inverts the whole reading.
    expect(primaryMetric("text-generation")).toMatchObject({
      key: "tokensPerSecond",
      unit: "tok/s",
      higherIsBetter: true,
    });
    expect(primaryMetric("text-to-image")).toMatchObject({
      key: "secondsPerStep",
      unit: "s/step",
      // The one capability where LOWER wins, which is exactly why the flag
      // exists rather than "bigger is greener" being assumed everywhere.
      higherIsBetter: false,
    });
    expect(primaryMetric("automatic-speech-recognition")).toMatchObject({
      key: "realtimeFactor",
      unit: "× realtime",
      higherIsBetter: true,
    });
    expect(primaryMetric("embeddings")).toMatchObject({
      key: "textsPerSecond",
      unit: "texts/s",
      higherIsBetter: true,
    });
  });

  it("has no answer for a capability it does not know", () => {
    // Null rather than a guess: a capability added server-side must render as
    // a section with no chart, not as a chart of the wrong number.
    expect(primaryMetric("telepathy")).toBeNull();
  });
});

// -- null is never zero -------------------------------------------------------

describe("a metric that was not measured", () => {
  it("reads as a dash, never as a zero", () => {
    const unmeasured = run({ metrics: { tokensPerSecond: null, ttftMs: null } });
    expect(primaryValue(unmeasured)).toBeNull();
    expect(formatPrimary(unmeasured)).toBe(DASH);
    // The trap this pins: `0` is falsy, so any `value || DASH` or `value ?? 0`
    // in a renderer turns a real zero into a dash or an absence into a zero.
    expect(formatPrimary(run({ metrics: { tokensPerSecond: 0 } }))).toBe("0 tok/s");
  });

  it("reads as a dash for a failed run, which carries no metrics at all", () => {
    const failed = run({ ok: false, error: "out of memory", metrics: {} });
    expect(primaryValue(failed)).toBeNull();
    expect(formatPrimary(failed)).toBe(DASH);
  });

  it("reads as a dash for a warm run's load time and an unmeasured memory", () => {
    expect(formatLoad(run({ loadSeconds: null }))).toBe(DASH);
    expect(formatLoad(run({ loadSeconds: 8.42 }))).toBe("8.4 s");
    expect(formatMemory(run({ peakResidentBytes: null }))).toBe(DASH);
    // Bytes go through the platform's own formatter rather than a second one.
    expect(formatMemory(run({ peakResidentBytes: 5_600_000_000 }))).toBe("5.2 GB");
  });
});

describe("summaryLine", () => {
  it("reads as the row the page promises", () => {
    expect(summaryLine(run())).toBe("42.1 tok/s · TTFT 310 ms · 5.2 GB · loaded in 8.4 s · mps");
  });

  it("drops the parts that were not measured rather than showing dashes", () => {
    // A row of dashes is noise; the primary metric is the one part that always
    // has a slot, because its absence is itself the news.
    expect(
      summaryLine(
        run({ loadSeconds: null, peakResidentBytes: null, device: null, metrics: { tokensPerSecond: 12 } }),
      ),
    ).toBe("12 tok/s");
  });

  it("says what went wrong instead, for a failed run", () => {
    expect(summaryLine(run({ ok: false, error: "out of memory", metrics: {} }))).toBe(
      "Failed — out of memory",
    );
  });
});

// -- latest and delta ---------------------------------------------------------

describe("latestByModel", () => {
  it("takes the newest run per model and compares it with the one before", () => {
    const rows = latestByModel([
      run({ model: "a", startedAt: 1, metrics: { tokensPerSecond: 40 } }),
      run({ model: "b", startedAt: 2, metrics: { tokensPerSecond: 10 } }),
      run({ model: "a", startedAt: 3, metrics: { tokensPerSecond: 50 } }),
    ]);
    expect(rows.map((r) => r.model)).toEqual(["a", "b"]);
    const a = rows[0]!;
    expect(a.latest.metrics.tokensPerSecond).toBe(50);
    expect(a.delta).not.toBeNull();
    expect(a.delta!.percent).toBeCloseTo(25);
    // 50 tok/s over 40 is an improvement, and the sign alone does not say so —
    // on an image section the same +25% would be a regression.
    expect(a.delta!.better).toBe(true);
    expect(rows[1]!.delta).toBeNull(); // b has only ever run once
  });

  it("counts a smaller seconds-per-step as better", () => {
    const rows = latestByModel([
      run({ capability: "text-to-image", startedAt: 1, metrics: { secondsPerStep: 4 } }),
      run({ capability: "text-to-image", startedAt: 2, metrics: { secondsPerStep: 3 } }),
    ]);
    expect(rows[0]!.delta!.percent).toBeCloseTo(-25);
    expect(rows[0]!.delta!.better).toBe(true);
  });

  it("draws no delta across a workload revision bump", () => {
    // The seam. The old run measured different work, so a "-50%" here would be
    // a fabricated regression — the whole reason `revision` is stored per run.
    const rows = latestByModel([
      run({ startedAt: 1, workload: { name: "text-128-tokens", revision: 1, params: {} }, metrics: { tokensPerSecond: 80 } }),
      run({ startedAt: 2, workload: { name: "text-128-tokens", revision: 2, params: {} }, metrics: { tokensPerSecond: 40 } }),
    ]);
    expect(rows[0]!.latest.metrics.tokensPerSecond).toBe(40);
    expect(rows[0]!.delta).toBeNull();
  });

  it("skips back past a bump to the last run of the same revision", () => {
    // Two v1 runs with a v2 run between them are still comparable to each
    // other: what breaks comparability is the revision, not the position.
    const rows = latestByModel([
      run({ startedAt: 1, workload: { name: "w", revision: 1, params: {} }, metrics: { tokensPerSecond: 40 } }),
      run({ startedAt: 2, workload: { name: "w", revision: 2, params: {} }, metrics: { tokensPerSecond: 99 } }),
      run({ startedAt: 3, workload: { name: "w", revision: 1, params: {} }, metrics: { tokensPerSecond: 50 } }),
    ]);
    expect(rows[0]!.delta!.percent).toBeCloseTo(25);
  });

  it("ignores an unmeasured run when looking for something to compare against", () => {
    const rows = latestByModel([
      run({ startedAt: 1, metrics: { tokensPerSecond: 40 } }),
      run({ startedAt: 2, ok: false, error: "boom", metrics: {} }),
      run({ startedAt: 3, metrics: { tokensPerSecond: 50 } }),
    ]);
    expect(rows[0]!.delta!.percent).toBeCloseTo(25);
  });

  it("shows a failed newest run as the latest, with no delta", () => {
    const rows = latestByModel([
      run({ startedAt: 1, metrics: { tokensPerSecond: 40 } }),
      run({ startedAt: 2, ok: false, error: "boom", metrics: {} }),
    ]);
    expect(rows[0]!.latest.ok).toBe(false);
    expect(rows[0]!.delta).toBeNull();
  });

  it("scores the delta against an EXPLICITLY selected metric, not always the primary", () => {
    // The leaderboard bar and its delta must read the same measurement — a
    // memory selection shows a memory delta, never a throughput one hiding
    // under a memory-labelled bar.
    const memory = availableMetrics("text-generation", [run()]).find(
      (s) => s.key === "peakResidentBytes",
    )!;
    const rows = latestByModel(
      [
        run({ startedAt: 1, peakResidentBytes: 4_000_000_000, metrics: { tokensPerSecond: 100 } }),
        run({ startedAt: 2, peakResidentBytes: 2_000_000_000, metrics: { tokensPerSecond: 10 } }),
      ],
      memory,
    );
    // Memory is LOWER-is-better: it halved, which is an IMPROVEMENT, even
    // though the same run's tokensPerSecond collapsed.
    expect(rows[0]!.delta!.percent).toBeCloseTo(-50);
    expect(rows[0]!.delta!.better).toBe(true);
  });
});

// -- the chart ----------------------------------------------------------------

describe("chartSeries", () => {
  it("is one polyline per model, in run order, over a domain that starts at zero", () => {
    const { series, yMax, yMin } = chartSeries([
      run({ model: "a", startedAt: 1, metrics: { tokensPerSecond: 40 } }),
      run({ model: "b", startedAt: 2, metrics: { tokensPerSecond: 10 } }),
      run({ model: "a", startedAt: 3, metrics: { tokensPerSecond: 60 } }),
    ]);
    expect(series.map((s) => s.model)).toEqual(["a", "b"]);
    // x is the run's position in the capability's whole history, so two models'
    // points interleave on one shared axis instead of each restarting at 0.
    expect(series[0]!.points.map((p) => [p.x, p.y])).toEqual([
      [0, 40],
      [2, 60],
    ]);
    expect(series[1]!.points.map((p) => [p.x, p.y])).toEqual([[1, 10]]);
    expect(yMin).toBe(0); // a throughput axis that does not start at zero lies
    expect(yMax).toBe(60);
    expect(series[0]!.points[0]!.run.id).toBeDefined(); // the point carries its run
  });

  it("leaves out the runs it cannot plot without inventing a value", () => {
    const { series, yMax } = chartSeries([
      run({ startedAt: 1, ok: false, error: "boom", metrics: {} }),
      run({ startedAt: 2, metrics: { tokensPerSecond: null } }),
      run({ startedAt: 3, metrics: { tokensPerSecond: 7 } }),
    ]);
    expect(series[0]!.points.map((p) => p.y)).toEqual([7]);
    expect(yMax).toBe(7);
  });

  it("has no series at all when nothing has been measured", () => {
    const { series, yMax } = chartSeries([]);
    expect(series).toEqual([]);
    // Zero rather than -Infinity out of a Math.max over nothing, which would
    // produce an unrenderable SVG.
    expect(yMax).toBe(0);
  });

  it("plots an EXPLICITLY selected metric instead of the capability's primary", () => {
    // The per-model trend chart now plots whatever the reader picked — load
    // time here, which lives on the run record itself, not in `metrics`.
    const load = availableMetrics("text-generation", [run()]).find(
      (s) => s.key === "loadSeconds",
    )!;
    const { series, yMax } = chartSeries(
      [
        run({ startedAt: 1, loadSeconds: 3.5, metrics: { tokensPerSecond: 999 } }),
        run({ startedAt: 2, loadSeconds: null, metrics: { tokensPerSecond: 1 } }),
      ],
      load,
    );
    // The warm run (loadSeconds: null) contributes no point, even though its
    // OTHER metric (tokensPerSecond) was measured — chartSeries must not fall
    // back to the primary just because the selected one came up empty.
    expect(series[0]!.points.map((p) => p.y)).toEqual([3.5]);
    expect(yMax).toBe(3.5);
  });
});

// -- sections -----------------------------------------------------------------

describe("orderCapabilities", () => {
  it("sorts the four into the same order the rest of the page uses", () => {
    expect(
      orderCapabilities(["embeddings", "automatic-speech-recognition", "text-to-image", "text-generation"]),
    ).toEqual(["text-generation", "text-to-image", "automatic-speech-recognition", "embeddings"]);
  });

  it("keeps a capability the frontend has never heard of, after the known ones", () => {
    // Same rule as the Local tab's grouping: a capability added server-side
    // appears here rather than vanishing from the page.
    expect(orderCapabilities(["telepathy", "embeddings"])).toEqual(["embeddings", "telepathy"]);
  });
});

describe("runsFor", () => {
  it("keeps only one capability's runs, oldest first", () => {
    const runs = [
      run({ capability: "embeddings", startedAt: 3 }),
      run({ capability: "text-generation", startedAt: 2 }),
      run({ capability: "text-generation", startedAt: 1 }),
    ];
    expect(runsFor(runs, "text-generation").map((r) => r.startedAt)).toEqual([1, 2]);
  });
});

// -- the Run button's state ---------------------------------------------------

describe("runButtonState", () => {
  it("blocks only the capability that has a run in flight", () => {
    // The server permits one run PER CAPABILITY concurrently (it holds one
    // resident model per capability, so an embedding benchmark cannot evict a
    // text one), and `test_a_different_capability_may_run_alongside` pins that.
    // A single page-level "something is running" flag greys out every other
    // section with a tooltip that says "for this capability", which is both a
    // false sentence and a permitted action made unreachable.
    const inFlight = { "text-generation": "org/text" };
    expect(runButtonState("text-generation", "org/other", inFlight).blocked).toBe(true);
    expect(runButtonState("text-to-image", "org/img", inFlight).blocked).toBe(false);
    expect(runButtonState("embeddings", "org/emb", inFlight).blocked).toBe(false);
  });

  it("marks the running model itself busy, not merely blocked", () => {
    const inFlight = { "text-generation": "org/text" };
    const mine = runButtonState("text-generation", "org/text", inFlight);
    expect(mine.busy).toBe(true);
    expect(mine.blocked).toBe(true);
    const sibling = runButtonState("text-generation", "org/other", inFlight);
    expect(sibling.busy).toBe(false);
    expect(sibling.blocked).toBe(true);
  });

  it("is free when nothing is running", () => {
    const free = runButtonState("text-generation", "org/text", {});
    expect(free.busy).toBe(false);
    expect(free.blocked).toBe(false);
  });

  it("says which model is holding the capability, never a bare claim", () => {
    // The tooltip has to name the run that is blocking, because the reader's
    // next question is "blocked by what" and the button cannot answer it.
    const sibling = runButtonState("text-generation", "org/other", {
      "text-generation": "org/text",
    });
    expect(sibling.title).toContain("org/text");
    expect(runButtonState("text-generation", "org/text", {
      "text-generation": "org/text",
    }).title).toContain("minutes");
  });

  it("labels a model with history 'Run again'", () => {
    expect(runButtonState("text-generation", "org/text", {}, false).label).toBe(
      "Run benchmark",
    );
    expect(runButtonState("text-generation", "org/text", {}, true).label).toBe("Run again");
    expect(runButtonState("text-generation", "org/text", {
      "text-generation": "org/text",
    }, true).label).toBe("Running…");
  });
});

// -- a run that was stopped from outside --------------------------------------

describe("stoppedNote", () => {
  it("says the run was stopped by something else, and that nothing was kept", () => {
    // Finding 6: the run just vanished. No row appended, no error set, the
    // button silently re-enabled — so the person who pressed Run and waited
    // minutes got no signal at all that their benchmark had died.
    const note = stoppedNote("org/model");
    expect(note).toContain("org/model");
    // Both halves matter: WHY it stopped (nobody pressed anything here) and
    // that there is nothing to look for in the history.
    expect(note.toLowerCase()).toContain("stopped");
    expect(note.toLowerCase()).toContain("nothing was recorded");
  });

  it("does not blame the model, and does not read as a failure", () => {
    // The distinction the note exists to draw: a failed run is a fact about the
    // model and is kept; a stopped one is a fact about the app and is not. The
    // words "failed" and "error" would collapse them.
    const note = stoppedNote("org/model").toLowerCase();
    expect(note).not.toContain("failed");
    expect(note).not.toContain("error");
  });

  it("gives an example cause without asserting one", () => {
    // There is no cancel control on a benchmark, so "cancelled" with no
    // explanation reads as a bug — but the client cannot tell WHICH of the three
    // possible cancels happened (`fused.ai.cancel()`, the ✕ on the load's own
    // download row, or the ✕ on a queued-transcription row a speech benchmark
    // inherited). An earlier draft claimed the first and was wrong for the other
    // two, so the wording offers it as an example instead.
    const note = stoppedNote("org/model");
    expect(note).toContain("fused.ai.cancel()");
    expect(note).toContain("for example");
    expect(note).not.toContain("most likely");
  });
});

// -- the compact row: a short name, a one-line headline, and what's left over --

describe("shortModelName", () => {
  it("drops the org prefix, which is what the compact row scans for", () => {
    expect(shortModelName("mlx-community/whisper-large-v3-mlx")).toBe(
      "whisper-large-v3-mlx",
    );
  });

  it("returns the whole id when there is no org to drop", () => {
    expect(shortModelName("standalone-model")).toBe("standalone-model");
  });
});

const TEXT_METRIC = primaryMetric("text-generation");

describe("rowHeadline", () => {
  it("is the SELECTED metric and memory — nothing else", () => {
    // Load time and device used to dominate the row; they belong in the detail
    // now, not the one line every model gets scanned by.
    expect(rowHeadline(run(), TEXT_METRIC)).toBe("42.1 tok/s · 5.2 GB");
  });

  it("drops memory when it was not measured", () => {
    expect(rowHeadline(run({ peakResidentBytes: null }), TEXT_METRIC)).toBe("42.1 tok/s");
  });

  it("reads a DIFFERENT metric when a different one is selected", () => {
    // The leaderboard now ranks by whatever the reader picked, so the row it
    // leads with has to follow — a memory selection headlines memory, not
    // throughput, and does not repeat it a second time.
    const memory = availableMetrics("text-generation", [run()]).find(
      (m) => m.key === "peakResidentBytes",
    )!;
    expect(rowHeadline(run(), memory)).toBe("5.2 GB");
  });

  it("says just 'Failed' — the reason lives behind the details expander", () => {
    expect(
      rowHeadline(run({ ok: false, error: "out of memory", metrics: {} }), TEXT_METRIC),
    ).toBe("Failed");
  });

  it("is a dash when there is no metric to read at all", () => {
    expect(rowHeadline(run(), null)).toBe(DASH);
  });
});

describe("rowDetail", () => {
  it("carries exactly what the headline left out", () => {
    expect(rowDetail(run(), TEXT_METRIC)).toBe("TTFT 310 ms · loaded in 8.4 s · mps");
  });

  it("is null when there is nothing beyond the headline — no expander to draw", () => {
    expect(
      rowDetail(
        run({ loadSeconds: null, device: null, metrics: { tokensPerSecond: 12 } }),
        TEXT_METRIC,
      ),
    ).toBeNull();
  });

  it("drops load time from the detail when LOAD TIME is the selected metric", () => {
    // It is already the headline; repeating it in the detail line would say
    // the same fact twice under two different names.
    const loadMetric = availableMetrics("text-generation", [run()]).find(
      (m) => m.key === "loadSeconds",
    )!;
    expect(rowDetail(run(), loadMetric)).toBe("TTFT 310 ms · mps");
  });

  it("is null for a failed run — its detail comes from failureReason instead", () => {
    expect(rowDetail(run({ ok: false, error: "boom", metrics: {} }), TEXT_METRIC)).toBeNull();
  });

  // The device is noise repeated on every model's row when the hardware
  // doesn't change between them — dropped whenever it matches the section's
  // own `expectedDevice` (the third argument, computed by `commonDevice`).
  it("drops the device when it matches the section's expected device", () => {
    expect(rowDetail(run({ device: "mps" }), TEXT_METRIC, "mps")).toBe(
      "TTFT 310 ms · loaded in 8.4 s",
    );
  });

  it("keeps the device when it DIFFERS from the section's expected one — the outlier is the signal", () => {
    expect(rowDetail(run({ device: "cpu" }), TEXT_METRIC, "mps")).toBe(
      "TTFT 310 ms · loaded in 8.4 s · cpu",
    );
  });

  it("keeps the device by default, with no expected device to compare against", () => {
    // No third argument — the old, always-shown behaviour, for a caller with
    // no opinion about what this section's hardware "should" be.
    expect(rowDetail(run(), TEXT_METRIC)).toBe("TTFT 310 ms · loaded in 8.4 s · mps");
  });
});

describe("commonDevice", () => {
  function latest(device: string | null, ok = true): ModelLatest {
    const record = ok ? run({ device }) : run({ device, ok: false, error: "boom", metrics: {} });
    return { model: "m", latest: record, delta: null };
  }

  it("is the device most models report", () => {
    expect(commonDevice([latest("mps"), latest("mps"), latest("cpu")])).toBe("mps");
  });

  it("is null with nothing to compare — no false 'expected' to contrast an outlier against", () => {
    expect(commonDevice([])).toBeNull();
  });

  it("ignores a failed run's device — it never actually ran on it", () => {
    expect(commonDevice([latest("mps"), latest("cpu", false)])).toBe("mps");
  });

  it("is null when every model failed or reported nothing", () => {
    expect(commonDevice([latest(null), latest(null, false)])).toBeNull();
  });

  it("is null on a genuine tie for the top spot — no majority, no 'expected'", () => {
    // The bug: a strict `count > bestCount` only ever REPLACES the leader, so
    // a 1-mps/1-cpu section returned whichever device happened to be first in
    // iteration order (insertion order — i.e. whichever model reported it
    // first) despite the doc above promising null for a tie.
    expect(commonDevice([latest("mps"), latest("cpu")])).toBeNull();
    // Order must not matter — the same tie, reported the other way round.
    expect(commonDevice([latest("cpu"), latest("mps")])).toBeNull();
  });

  it("is not fooled into a tie by a later MAJORITY", () => {
    expect(commonDevice([latest("mps"), latest("cpu"), latest("mps")])).toBe("mps");
  });
});

describe("failureReason", () => {
  it("is the run's own error", () => {
    expect(failureReason(run({ ok: false, error: "out of memory" }))).toBe("out of memory");
  });

  it("has a fallback for a run that failed with no reason given", () => {
    expect(failureReason(run({ ok: false, error: null }))).toBe("no reason given");
  });
});

// -- the chart's y axis --------------------------------------------------------

describe("yAxisTicks", () => {
  it("divides the domain into equal, labelled steps from zero to the peak", () => {
    const ticks = yAxisTicks(100, REALTIME, 4);
    expect(ticks.map((t) => t.value)).toEqual([0, 25, 50, 75, 100]);
    expect(ticks.map((t) => t.label)).toEqual(["0", "25", "50", "75", "100"]);
  });

  it("trims trailing zeros the same way every other formatted number does", () => {
    const ticks = yAxisTicks(1, { ...REALTIME, digits: 2 }, 4);
    expect(ticks.map((t) => t.label)).toEqual(["0", "0.25", "0.5", "0.75", "1"]);
  });

  it("has no ticks over an empty domain — there is nothing to draw a scale for", () => {
    expect(yAxisTicks(0, REALTIME)).toEqual([]);
  });

  // The bug this fixes: the trend chart's y axis went through `formatNumber`
  // even for `peakResidentBytes`, which stores raw bytes — a reader picking
  // "Peak memory" saw the axis printed as "294748160" rather than as memory.
  // `formatMetricSpecValue` (the same formatter every row's own memory figure
  // already uses) is what turns a byte count into "MB"/"GB"; every tick this
  // chart prints has to go through it, memory included, or the next chart to
  // add a byte-valued metric regresses the exact same way.
  it("formats a byte metric's ticks through the metric's own formatter, not a bare number", () => {
    const ticks = yAxisTicks(900 * 1024 * 1024, MEMORY, 4);
    expect(ticks.map((t) => t.label)).toEqual(["0 B", "225 MB", "450 MB", "675 MB", "900 MB"]);
  });

  // The trend chart's own equal-division axis has the identical bug for a
  // sub-second duration domain — a padded 0.042-second max divided into 4
  // used to print "0.0" (trimmed to "0") on every tick.
  it("formats a sub-second duration metric's ticks in milliseconds, not '0'", () => {
    const ticks = yAxisTicks(0.042, DECODE_TIME, 4);
    expect(ticks.map((t) => t.label)).toEqual(["0 ms", "11 ms", "21 ms", "32 ms", "42 ms"]);
  });
});

describe("formatRunDate", () => {
  it("is a short month-and-day label — not a full timestamp", () => {
    // Locale-dependent in exact wording, so this pins the SHAPE ("Aug 12"),
    // not a fixed string a CI timezone would break.
    expect(formatRunDate(1_700_000_000)).toMatch(/^[A-Za-z]{3,}\.? \d{1,2}$/);
  });
});

// -- the leaderboard: order and bar length -------------------------------------

describe("leaderboard", () => {
  function latestFor(runs: AiBenchmarkRun[]): Map<string, ModelLatest> {
    return new Map(latestByModel(runs).map((r) => [r.model, r]));
  }

  it("orders measured models best-first and gives the winner a full bar", () => {
    const latest = latestFor([
      run({ model: "slow", startedAt: 1, metrics: { tokensPerSecond: 10 } }),
      run({ model: "fast", startedAt: 2, metrics: { tokensPerSecond: 40 } }),
    ]);
    const rows = leaderboard(primaryMetric("text-generation"), [
      { model: "slow", row: latest.get("slow")! },
      { model: "fast", row: latest.get("fast")! },
    ]);
    expect(rows.map((r) => r.model)).toEqual(["fast", "slow"]);
    expect(rows[0]!.barFraction).toBe(1);
    expect(rows[1]!.barFraction).toBeCloseTo(0.25);
  });

  it("gives the SMALLER number the full bar when lower is better", () => {
    // text-to-image's seconds-per-step: the fastest model has the smallest
    // number, and the bar has to read "longer is better" on every section —
    // including this one, where that means inverting the ratio.
    const latest = latestFor([
      run({ capability: "text-to-image", model: "slow", startedAt: 1, metrics: { secondsPerStep: 4 } }),
      run({ capability: "text-to-image", model: "fast", startedAt: 2, metrics: { secondsPerStep: 1 } }),
    ]);
    const rows = leaderboard(primaryMetric("text-to-image"), [
      { model: "slow", row: latest.get("slow")! },
      { model: "fast", row: latest.get("fast")! },
    ]);
    expect(rows.map((r) => r.model)).toEqual(["fast", "slow"]);
    expect(rows[0]!.barFraction).toBe(1);
    expect(rows[1]!.barFraction).toBeCloseTo(0.25);
  });

  it("puts a failed latest run above never-benchmarked, neither with a bar", () => {
    const latest = latestFor([
      run({ model: "winner", startedAt: 1, metrics: { tokensPerSecond: 40 } }),
      run({ model: "broken", startedAt: 2, ok: false, error: "boom", metrics: {} }),
    ]);
    const rows = leaderboard(primaryMetric("text-generation"), [
      { model: "winner", row: latest.get("winner")! },
      { model: "broken", row: latest.get("broken")! },
      { model: "never", row: null },
    ]);
    expect(rows.map((r) => r.model)).toEqual(["winner", "broken", "never"]);
    expect(rows[1]!.barFraction).toBeNull();
    expect(rows[2]!.barFraction).toBeNull();
  });

  it("draws no bars at all for a capability this frontend does not know", () => {
    // Same posture as `primaryMetric`: no guessed number rather than a bar
    // scaled on the wrong thing.
    const rows = leaderboard(primaryMetric("telepathy"), [{ model: "x", row: null }]);
    expect(rows[0]!.barFraction).toBeNull();
  });

  it("does not blow up when every model in the section is unmeasured", () => {
    const rows = leaderboard(primaryMetric("text-generation"), [
      { model: "a", row: null },
      { model: "b", row: null },
    ]);
    expect(rows.every((r) => r.barFraction === null)).toBe(true);
  });
});

// -- the comparison chart: one bar per MEASURED model, best first ------------
//
// The chart that replaces the leaderboard's own inline mini-bar (a real
// instrument answering "which model is fastest here", rather than the
// per-model trend chart, which needs two runs of the SAME model and almost
// never has them — real usage spreads a handful of runs across several
// different models, so the trend chart's "single" state fires for nearly
// every model on a real machine, and the page was left with no chart at
// all).

describe("comparisonBars", () => {
  function latestFor(runs: AiBenchmarkRun[]): Map<string, ModelLatest> {
    return new Map(latestByModel(runs).map((r) => [r.model, r]));
  }

  it("is every measured model, best first, with its real value — not a normalised fraction", () => {
    const latest = latestFor([
      run({ model: "slow", startedAt: 1, metrics: { tokensPerSecond: 10 } }),
      run({ model: "fast", startedAt: 2, metrics: { tokensPerSecond: 40 } }),
    ]);
    const metric = primaryMetric("text-generation");
    const ranked = leaderboard(metric, [
      { model: "slow", row: latest.get("slow")! },
      { model: "fast", row: latest.get("fast")! },
    ]);
    expect(comparisonBars(ranked, metric)).toEqual([
      { model: "fast", value: 40 },
      { model: "slow", value: 10 },
    ]);
  });

  it("keeps the leaderboard's own order for a lower-is-better metric — the smaller number leads", () => {
    // This is the whole point of the fix: a naive value-proportional bar
    // chart reads correctly ONLY if the order already puts the best model
    // first, because bar length here is the RAW value (a real, honest axis),
    // not an inverted "goodness" fraction. seconds-per-step's winner has the
    // SMALLEST number and therefore the SHORTEST bar — which is exactly
    // "shortest bar wins" for this metric, not a bug.
    const latest = latestFor([
      run({ capability: "text-to-image", model: "slow", startedAt: 1, metrics: { secondsPerStep: 4 } }),
      run({ capability: "text-to-image", model: "fast", startedAt: 2, metrics: { secondsPerStep: 1 } }),
    ]);
    const metric = primaryMetric("text-to-image");
    const ranked = leaderboard(metric, [
      { model: "slow", row: latest.get("slow")! },
      { model: "fast", row: latest.get("fast")! },
    ]);
    expect(comparisonBars(ranked, metric)).toEqual([
      { model: "fast", value: 1 },
      { model: "slow", value: 4 },
    ]);
  });

  it("excludes a failed latest run and a never-benchmarked model — nothing to plot for either", () => {
    // Both stay VISIBLE elsewhere (the leaderboard rows, BenchmarkTab.tsx) —
    // this function only decides what the CHART draws, and a model with no
    // number cannot get a bar without inventing one.
    const latest = latestFor([
      run({ model: "winner", startedAt: 1, metrics: { tokensPerSecond: 40 } }),
      run({ model: "broken", startedAt: 2, ok: false, error: "boom", metrics: {} }),
    ]);
    const metric = primaryMetric("text-generation");
    const ranked = leaderboard(metric, [
      { model: "winner", row: latest.get("winner")! },
      { model: "broken", row: latest.get("broken")! },
      { model: "never", row: null },
    ]);
    expect(comparisonBars(ranked, metric)).toEqual([{ model: "winner", value: 40 }]);
  });

  it("is empty for a capability this frontend does not know", () => {
    const metric = primaryMetric("telepathy");
    const ranked = leaderboard(metric, [{ model: "x", row: null }]);
    expect(comparisonBars(ranked, metric)).toEqual([]);
  });

  it("is empty when nothing has been measured", () => {
    const metric = primaryMetric("text-generation");
    const ranked = leaderboard(metric, [
      { model: "a", row: null },
      { model: "b", row: null },
    ]);
    expect(comparisonBars(ranked, metric)).toEqual([]);
  });
});

// -- the capability selector: default pick and the ?cap= round-trip ----------

describe("runCountsByCapability", () => {
  it("counts recorded runs per capability", () => {
    const counts = runCountsByCapability([
      run({ capability: "text-generation", startedAt: 1 }),
      run({ capability: "text-generation", startedAt: 2 }),
      run({ capability: "embeddings", startedAt: 3 }),
    ]);
    expect(counts).toEqual({ "text-generation": 2, embeddings: 1 });
  });

  it("is empty over no runs — there is nothing to count", () => {
    expect(runCountsByCapability([])).toEqual({});
  });
});

describe("defaultCapability", () => {
  it("picks the capability with the most recorded runs", () => {
    expect(
      defaultCapability(
        ["text-generation", "text-to-image", "automatic-speech-recognition", "embeddings"],
        { "text-generation": 2, embeddings: 5 },
      ),
    ).toBe("embeddings");
  });

  it("breaks a tie by registry order — the earlier one in the list wins", () => {
    expect(
      defaultCapability(["text-generation", "embeddings"], {
        "text-generation": 3,
        embeddings: 3,
      }),
    ).toBe("text-generation");
  });

  it("falls back to the first capability in registry order when nothing has ever run", () => {
    expect(
      defaultCapability(["text-generation", "text-to-image", "embeddings"], {}),
    ).toBe("text-generation");
  });

  it("is null when there are no capabilities to choose from", () => {
    expect(defaultCapability([], {})).toBeNull();
  });
});

describe("resolveCapability", () => {
  const CAPS = ["text-generation", "text-to-image", "automatic-speech-recognition", "embeddings"];

  it("keeps the URL's ?cap= when it names a real capability", () => {
    expect(resolveCapability(CAPS, "embeddings", {})).toBe("embeddings");
  });

  it("falls back to the default pick when ?cap= is absent", () => {
    expect(resolveCapability(CAPS, null, { embeddings: 4 })).toBe("embeddings");
  });

  it("falls back to the default pick when ?cap= names an unknown capability", () => {
    // A stale or foreign link (`?cap=telepathy`), the same forgiving posture
    // `orderCapabilities`/`tabFromPath` take toward an unrecognised value —
    // never an empty page over a param that travelled here from somewhere else.
    expect(resolveCapability(CAPS, "telepathy", { embeddings: 4 })).toBe("embeddings");
  });

  it("is null only when there is truly nothing to select", () => {
    expect(resolveCapability([], "text-generation", {})).toBeNull();
  });

  it("preserves an explicit ?cap= through the render BEFORE the history has answered", () => {
    // The scenario behind a real bug: BenchmarkTab.tsx computes `selected`
    // (and syncs it to the URL) on every render, including the very first one,
    // where `runs` is still `null` and `runCountsByCapability` therefore hands
    // this function an EMPTY count map — indistinguishable, from resolveCapability's
    // side, from "nothing has ever run". If an unanswered history silently
    // overrode a landing `?cap=` with the empty-counts default, a link to
    // `?cap=embeddings` would flash to whatever sorts first in registry order
    // and then jump back once the real counts arrived. It must not: an
    // explicit, valid param always wins, counts or no counts.
    expect(resolveCapability(CAPS, "embeddings", {})).toBe("embeddings");
  });
});

// -- the chart's x axis: dates, or times when a date would just repeat -------

describe("formatRunTime", () => {
  it("is a short hour:minute label — not a date, not seconds", () => {
    // Locale-dependent wording (12h vs 24h), so this pins the SHAPE, the same
    // way formatRunDate's own test avoids a fixed string a CI timezone would
    // break.
    expect(formatRunTime(1_700_000_000)).toMatch(/^\d{1,2}:\d{2}(\s?[AP]M)?$/i);
  });
});

describe("chartAxisTicks", () => {
  const DAY = 24 * 60 * 60;

  it("uses dates when the runs span multiple days", () => {
    const runs = [
      run({ startedAt: 1_700_000_000 }),
      run({ startedAt: 1_700_000_000 + 3 * DAY }),
    ];
    const { ticks, dateCaption } = chartAxisTicks(runs);
    expect(ticks.map((t) => t.label)).toEqual([
      formatRunDate(runs[0]!.startedAt),
      formatRunDate(runs[1]!.startedAt),
    ]);
    // The ticks already carry the date — repeating it a third time in a
    // caption says nothing new.
    expect(dateCaption).toBeNull();
  });

  it("switches to times, with the date stated ONCE, when every run lands the same day", () => {
    // The bug this exists to fix: four runs inside one day rendered
    // "22 Aug / 22 Aug / 22 Aug" — three identical labels telling the reader
    // nothing about when, within that day, each run happened.
    //
    // Anchored at LOCAL noon rather than a raw epoch constant — a fixed epoch
    // (e.g. 1_700_000_000) lands at a different LOCAL time in every timezone,
    // and happened to sit close enough to local midnight in this suite's own
    // timezone that adding a few thousand seconds crossed into the next
    // calendar day, defeating the very premise this test means to set up
    // (see the midnight-crossing test below, which exercises that case on
    // purpose). Noon has hours of margin either side in any timezone.
    const base = new Date(2024, 0, 15, 12, 0, 0).getTime() / 1000;
    const runs = [
      run({ startedAt: base }),
      run({ startedAt: base + 2000 }),
      run({ startedAt: base + 5000 }),
      run({ startedAt: base + 7000 }),
    ];
    const { ticks, dateCaption } = chartAxisTicks(runs);
    expect(ticks.map((t) => t.label)).toEqual([
      formatRunTime(runs[0]!.startedAt),
      formatRunTime(runs[1]!.startedAt), // the middle of 4 runs, index 1
      formatRunTime(runs[3]!.startedAt),
    ]);
    expect(dateCaption).toBe(formatRunDate(runs[0]!.startedAt));
  });

  it("switches to times for exactly two runs the same day too", () => {
    // The two-run case is the common one right now (most models have one or
    // two runs), and it must not fall through to dates just because the
    // "middle tick" logic above only applies at three or more.
    const base = new Date(2024, 0, 15, 12, 0, 0).getTime() / 1000;
    const runs = [run({ startedAt: base }), run({ startedAt: base + 3000 })];
    const { ticks, dateCaption } = chartAxisTicks(runs);
    expect(ticks.map((t) => t.label)).toEqual([
      formatRunTime(runs[0]!.startedAt),
      formatRunTime(runs[1]!.startedAt),
    ]);
    expect(dateCaption).toBe(formatRunDate(runs[0]!.startedAt));
  });

  // The reported bug: whisper-tiny.en-8bit's real runs spanned 23:02 one
  // evening to 14:07 the next afternoon — under 24 hours elapsed, but NOT the
  // same calendar day. The old rule (`span < 24h` alone) drew time-only
  // ticks ("11:02 PM" / "01:50 PM") under a single "Aug 22" caption that was
  // wrong for the later point, which actually happened on Aug 23.
  it("uses dates rather than a lying single-day caption when the span crosses midnight", () => {
    const base = new Date(2026, 7, 22, 23, 2, 53).getTime() / 1000; // Aug 22, 23:02:53 local
    const runs = [run({ startedAt: base }), run({ startedAt: base + 13 * 60 * 60 })];
    const { ticks, dateCaption } = chartAxisTicks(runs);
    expect(ticks.map((t) => t.label)).toEqual([
      formatRunDate(runs[0]!.startedAt),
      formatRunDate(runs[1]!.startedAt),
    ]);
    expect(dateCaption).toBeNull();
  });

  it("stays on time ticks for a same-day span that runs right up near midnight but doesn't cross it", () => {
    const base = new Date(2026, 7, 22, 20, 0, 0).getTime() / 1000;
    const runs = [run({ startedAt: base }), run({ startedAt: base + 3 * 60 * 60 })]; // 20:00 -> 23:00, same day
    const { ticks, dateCaption } = chartAxisTicks(runs);
    expect(ticks.map((t) => t.label)).toEqual([
      formatRunTime(runs[0]!.startedAt),
      formatRunTime(runs[1]!.startedAt),
    ]);
    expect(dateCaption).toBe(formatRunDate(runs[0]!.startedAt));
  });

  it("picks first, middle and last for four or more runs", () => {
    const runs = [1, 2, 3, 4, 5].map((n) => run({ startedAt: n }));
    const { ticks } = chartAxisTicks(runs);
    expect(ticks.map((t) => t.x)).toEqual([0, 2, 4]);
  });

  it("picks just the two ends for exactly two runs", () => {
    const runs = [run({ startedAt: 1 }), run({ startedAt: 2 })];
    const { ticks } = chartAxisTicks(runs);
    expect(ticks.map((t) => t.x)).toEqual([0, 1]);
  });

  it("is a single DATE tick for one run — a lone time has no day to anchor it", () => {
    const runs = [run({ startedAt: 1_700_000_000 })];
    const { ticks, dateCaption } = chartAxisTicks(runs);
    expect(ticks).toEqual([{ x: 0, label: formatRunDate(1_700_000_000) }]);
    expect(dateCaption).toBeNull();
  });

  it("is empty over no runs", () => {
    expect(chartAxisTicks([])).toEqual({ ticks: [], dateCaption: null });
  });
});

// -- the leaderboard's name: truncate where the DIFFERENCE survives ----------

describe("middleEllipsis", () => {
  it("returns the name unchanged when it already fits", () => {
    expect(middleEllipsis("whisper-tiny", 20)).toBe("whisper-tiny");
  });

  it("keeps the head AND the tail, eliding the middle — where tail-truncation would eat the difference", () => {
    // The bug this exists to fix: "whisper-large-v3-mlx" and
    // "whisper-large-v3-turbo" share their first 17 characters and differ
    // only in the last few — exactly what a trailing "…" throws away first.
    const a = middleEllipsis("whisper-large-v3-mlx", 16);
    const b = middleEllipsis("whisper-large-v3-turbo", 16);
    expect(a).not.toBe(b);
    expect(a).toContain("…");
    expect(a.length).toBe(16);
    // Both ends survive: the shared prefix AND each name's own distinct tail.
    expect(a.startsWith("whisper")).toBe(true);
    expect(a.endsWith("mlx")).toBe(true);
    expect(b.endsWith("turbo")).toBe(true);
  });

  it("is a no-op for a budget too small to hold head, tail and the ellipsis usefully", () => {
    expect(middleEllipsis("whisper-large-v3-mlx", 0)).toBe("whisper-large-v3-mlx");
  });
});

// -- the metric selector: which numbers a capability actually offers --------

describe("availableMetrics", () => {
  it("lists a capability's declared metrics, primary first", () => {
    const specs = availableMetrics("text-generation", [run()]);
    expect(specs.map((s) => s.key)).toEqual([
      "tokensPerSecond",
      "ttftMs",
      "promptTokensPerSecond",
      "peakResidentBytes",
      "loadSeconds",
    ]);
    expect(specs[0]).toEqual(primaryMetric("text-generation")!);
  });

  it("never invents a workload parameter as a metric", () => {
    // steps/width/height/dim/batch/audioSeconds are the FIXED workload
    // describing itself, constant across runs — a flat line, not a trend.
    const image = availableMetrics("text-to-image", [run({ capability: "text-to-image" })]);
    expect(image.map((s) => s.key)).not.toContain("steps");
    expect(image.map((s) => s.key)).not.toContain("width");
    const embed = availableMetrics("embeddings", [run({ capability: "embeddings" })]);
    expect(embed.map((s) => s.key)).not.toContain("dim");
    expect(embed.map((s) => s.key)).not.toContain("batch");
  });

  it("drops a metric no run in this capability ever measured", () => {
    // Every run here was warm (`loadSeconds: null`), so offering "Load time"
    // would be a dropdown option that always renders an empty chart.
    const specs = availableMetrics("text-generation", [
      run({ loadSeconds: null }),
      run({ loadSeconds: null }),
    ]);
    expect(specs.map((s) => s.key)).not.toContain("loadSeconds");
    // But the ones that WERE measured stay.
    expect(specs.map((s) => s.key)).toContain("tokensPerSecond");
  });

  it("offers the full list before anything has ever run — nothing to filter against yet", () => {
    expect(availableMetrics("text-generation", []).length).toBeGreaterThan(1);
  });

  it("falls back to the full list rather than stranding the selector with zero options", () => {
    // A contrived case: every run failed, so every metric reads null. Better
    // to offer the whole declared set than an empty dropdown.
    const specs = availableMetrics("text-generation", [
      run({ ok: false, error: "boom", metrics: {} }),
    ]);
    expect(specs.length).toBeGreaterThan(0);
  });
});

describe("metricValueForSpec", () => {
  it("reads a capability metric out of run.metrics", () => {
    expect(metricValueForSpec(run(), primaryMetric("text-generation"))).toBe(42.1);
  });

  it("reads memory and load time off the RUN ITSELF, not run.metrics", () => {
    const memory = availableMetrics("text-generation", [run()]).find(
      (s) => s.key === "peakResidentBytes",
    )!;
    const load = availableMetrics("text-generation", [run()]).find(
      (s) => s.key === "loadSeconds",
    )!;
    expect(metricValueForSpec(run(), memory)).toBe(5_600_000_000);
    expect(metricValueForSpec(run(), load)).toBe(8.4);
  });

  it("is null when the spec itself is null", () => {
    expect(metricValueForSpec(run(), null)).toBeNull();
  });
});

describe("formatMetricSpecValue", () => {
  it("formats a plain metric with its unit", () => {
    expect(formatMetricSpecValue(42.1, primaryMetric("text-generation")!)).toBe("42.1 tok/s");
  });

  it("formats memory through the platform's own byte formatter, not digits-and-a-unit", () => {
    const memory = availableMetrics("text-generation", [run()]).find(
      (s) => s.key === "peakResidentBytes",
    )!;
    expect(formatMetricSpecValue(5_600_000_000, memory)).toBe("5.2 GB");
  });

  it("is a dash for null, regardless of which metric", () => {
    expect(formatMetricSpecValue(null, primaryMetric("text-generation")!)).toBe(DASH);
  });

  // The reported bug: whisper-tiny.en-8bit's totalSeconds is genuinely
  // 0.022–0.035 across five runs — a fast model doing a fast job, not a
  // missing measurement — but at this metric's one decimal place that used
  // to round to "0.0", trimmed to a bare, misleading "0". A duration under
  // one second now reports in milliseconds instead.
  it("reports a sub-second duration in milliseconds, not a misleading '0'", () => {
    const asrRun = run({
      capability: "automatic-speech-recognition",
      metrics: { realtimeFactor: 1410.4, totalSeconds: 0.022 },
    });
    const decode = availableMetrics("automatic-speech-recognition", [asrRun]).find(
      (s) => s.key === "totalSeconds",
    )!;
    expect(formatMetricSpecValue(0.022, decode)).toBe("22 ms");
  });

  it("keeps an at-or-above-one-second duration in seconds, unchanged", () => {
    const load = availableMetrics("text-generation", [run()]).find((s) => s.key === "loadSeconds")!;
    expect(formatMetricSpecValue(24.6, load)).toBe("24.6 s");
    expect(formatMetricSpecValue(1, load)).toBe("1 s");
  });
});

describe("formatDuration", () => {
  it("reports a sub-second value in whole milliseconds", () => {
    expect(formatDuration(0.022, 1)).toBe("22 ms");
  });

  it("reports zero as milliseconds too — the same side of the one-second boundary", () => {
    expect(formatDuration(0, 1)).toBe("0 ms");
  });

  it("reports a one-second-or-above value in seconds, at the metric's own precision", () => {
    expect(formatDuration(24.6, 1)).toBe("24.6 s");
    expect(formatDuration(1, 1)).toBe("1 s");
  });

  it("spans four orders of magnitude without either printing '0s' or six decimals", () => {
    expect(formatDuration(0.0221, 1)).toBe("22 ms");
    expect(formatDuration(24.63, 1)).toBe("24.6 s");
  });
});

describe("metricUnitAndCue", () => {
  it("is just the unit for a higher-is-better metric — no mirror-image cue", () => {
    expect(metricUnitAndCue(REALTIME)).toBe("× realtime");
  });

  it("adds 'lower is better' for a lower-is-better metric with a unit", () => {
    expect(metricUnitAndCue(DECODE_TIME)).toBe("s · lower is better");
  });

  it("is just the cue when the metric has no unit of its own (peak memory's is dynamic)", () => {
    expect(metricUnitAndCue(MEMORY)).toBe("lower is better");
  });

  it("never states the metric's own name — the select beside it already does", () => {
    expect(metricUnitAndCue(REALTIME)).not.toContain("Speed");
    expect(metricUnitAndCue(MEMORY)).not.toContain("Peak memory");
  });
});

describe("resolveMetric", () => {
  const SPECS: MetricSpec[] = availableMetrics("text-generation", [run()]);

  it("keeps an explicit, valid metric key", () => {
    expect(resolveMetric(SPECS, "loadSeconds")?.key).toBe("loadSeconds");
  });

  it("falls back to the primary (first) metric when the param is absent or unknown", () => {
    expect(resolveMetric(SPECS, null)?.key).toBe(SPECS[0]!.key);
    expect(resolveMetric(SPECS, "telepathyRate")?.key).toBe(SPECS[0]!.key);
  });

  it("is null when there is nothing to select from", () => {
    expect(resolveMetric([], "tokensPerSecond")).toBeNull();
  });
});

// -- the model picker: which model the trend chart opens on ------------------

describe("runCountsByModel", () => {
  it("counts runs per model rather than per capability", () => {
    expect(
      runCountsByModel([
        run({ model: "a" }),
        run({ model: "a" }),
        run({ model: "b" }),
      ]),
    ).toEqual({ a: 2, b: 1 });
  });
});

describe("defaultModel", () => {
  it("picks the model with the most runs, ties broken by the given order", () => {
    expect(defaultModel(["a", "b"], { a: 1, b: 1 })).toBe("a");
    expect(defaultModel(["a", "b"], { a: 1, b: 5 })).toBe("b");
  });

  it("is null with nothing to choose from", () => {
    expect(defaultModel([], {})).toBeNull();
  });
});

describe("resolveModel", () => {
  it("keeps an explicit ?benchModel= naming a real model in this capability", () => {
    expect(resolveModel(["a", "b"], "b", { a: 5 })).toBe("b");
  });

  it("falls back to the default when the param names a model from a DIFFERENT capability", () => {
    // The reader switched ?cap=; a model that belonged to the old one is
    // exactly like a stale or foreign param.
    expect(resolveModel(["a", "b"], "some/other-capabilitys-model", { a: 5, b: 1 })).toBe("a");
  });
});

// -- the trend chart's own threshold: a point is not a trend -----------------

describe("trendKind", () => {
  it("is 'none' with nothing measured", () => {
    expect(trendKind(0)).toBe("none");
  });

  it("is 'single' for exactly one measured point — nothing to compare it against", () => {
    expect(trendKind(1)).toBe("single");
  });

  it("is 'trend' from two points on — the smallest count with a direction", () => {
    expect(trendKind(2)).toBe("trend");
    expect(trendKind(5)).toBe("trend");
  });
});

describe("paddedAxisMax", () => {
  it("pads past the peak, so the highest point does not sit ON the top gridline", () => {
    // Pinning the domain top to the exact peak (859.7 at the very top, the
    // bug this fixes) reads as the point being clipped against the frame
    // rather than as the best result on the chart.
    expect(paddedAxisMax(100)).toBeGreaterThan(100);
  });

  it("is a fixed, deliberate 20% — not derived from digits or unit", () => {
    expect(paddedAxisMax(100)).toBeCloseTo(120);
  });

  it("stays zero over an empty domain — nothing to pad", () => {
    expect(paddedAxisMax(0)).toBe(0);
  });
});

describe("niceAxisTicks", () => {
  it("lands on round numbers derived from the peak's magnitude, not an even division of it", () => {
    // The reported bug: a peak of 859.7 evenly divided into 4 lands on
    // 214.9 / 429.9 / 644.8 / 859.7 — none of them a number anyone would
    // choose for an axis. A bar chart has no headroom problem (the winning
    // bar reaching the end of the axis IS "this is the maximum"), so the top
    // should be the smallest round number at or past the peak, not a padded
    // fraction of it.
    const ticks = niceAxisTicks(859.7, REALTIME, 4);
    expect(ticks.map((t) => t.value)).toEqual([0, 250, 500, 750, 1000]);
    // Unlabelled by unit — the section's own metric badge and every row's
    // value already say "× realtime" once; repeating it on every gridline
    // would be the wrong kind of literal (see `axisTickLabel`'s own comment).
    expect(ticks.map((t) => t.label)).toEqual(["0", "250", "500", "750", "1000"]);
  });

  it("never sets a top below the true peak — every bar must fit inside the axis", () => {
    const ticks = niceAxisTicks(42, REALTIME, 4);
    const top = ticks[ticks.length - 1]!.value;
    expect(top).toBeGreaterThanOrEqual(42);
  });

  it("picks a round step for a small, sub-1 domain too", () => {
    const ticks = niceAxisTicks(1, { ...REALTIME, digits: 2 }, 4);
    expect(ticks.map((t) => t.value)).toEqual([0, 0.25, 0.5, 0.75, 1]);
  });

  it("has no ticks over an empty domain — there is nothing to draw a scale for", () => {
    expect(niceAxisTicks(0, REALTIME)).toEqual([]);
  });

  // The bug this section fixes: switching the Metric select to "Peak memory"
  // used to plot the axis in raw BYTES (a huge, unreadable integer) because
  // the tick label went through `formatNumber` instead of the metric's own
  // formatter. `formatMetricSpecValue` — the same function every bar's own
  // end-value label already goes through — is what turns a byte count into
  // "250 MB", and every number this chart prints must go through it, memory
  // included.
  it("scales a byte metric's step in a human unit, not raw bytes, and labels it through the metric's own formatter", () => {
    const peak = 900 * 1024 * 1024; // 900 MB
    const ticks = niceAxisTicks(peak, MEMORY, 4);
    expect(ticks.map((t) => t.label)).toEqual(
      ticks.map((t) => formatMetricSpecValue(t.value, MEMORY)),
    );
    // Round in MB, not in bytes — 250 MB steps, not some ragged byte count
    // that happens to format into an ugly number.
    expect(ticks.map((t) => t.label)).toEqual(["0 B", "250 MB", "500 MB", "750 MB", "1000 MB"]);
    expect(ticks[ticks.length - 1]!.value).toBeGreaterThanOrEqual(peak);
  });

  it("scales a multi-gigabyte byte peak in GB rather than staying in MB", () => {
    const peak = 2.5 * 1024 ** 3; // 2.5 GB
    const ticks = niceAxisTicks(peak, MEMORY, 4);
    expect(ticks.map((t) => t.value)).toEqual([0, 1, 2, 3, 4].map((n) => n * 1024 ** 3));
    expect(ticks[ticks.length - 1]!.value).toBeGreaterThanOrEqual(peak);
  });

  // The reported bug: a nice-tick algorithm run directly on a 0.0349-second
  // domain (whisper-tiny.en-8bit's real totalSeconds peak) degenerates —
  // every tick rounds to "0" at the metric's one decimal place. Stepping in
  // MILLISECONDS below one second (`secondsStepDivisor`) is what keeps the
  // steps round AND legible once formatted.
  it("steps a sub-second duration peak in milliseconds, not degenerate zero ticks", () => {
    const ticks = niceAxisTicks(0.0349, DECODE_TIME, 4);
    expect(ticks.map((t) => t.label)).toEqual(["0 ms", "10 ms", "20 ms", "30 ms", "40 ms"]);
    expect(ticks[ticks.length - 1]!.value).toBeGreaterThanOrEqual(0.0349);
  });
});

describe("niceAxisMax", () => {
  it("is the top tick niceAxisTicks would draw", () => {
    expect(niceAxisMax(859.7, REALTIME, 4)).toBe(1000);
  });

  it("stays zero over an empty domain", () => {
    expect(niceAxisMax(0, REALTIME)).toBe(0);
  });
});


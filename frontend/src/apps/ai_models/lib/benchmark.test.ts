import { describe, expect, it } from "bun:test";
import type { AiBenchmarkRun } from "@platform/lib/api";
import {
  DASH,
  chartSeries,
  failureReason,
  formatLoad,
  formatMemory,
  formatPrimary,
  formatRunDate,
  latestByModel,
  leaderboard,
  orderCapabilities,
  primaryMetric,
  primaryValue,
  rowDetail,
  rowHeadline,
  runButtonState,
  runsFor,
  shortModelName,
  stoppedNote,
  summaryLine,
  yAxisTicks,
  type ModelLatest,
} from "@apps/ai_models/lib/benchmark";

const MACHINE = { platform: "Darwin", arch: "arm64", cpuCount: 10, totalMemoryBytes: 3.2e10 };

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

describe("rowHeadline", () => {
  it("is the primary metric and memory — nothing else", () => {
    // Load time and device used to dominate the row; they belong in the detail
    // now, not the one line every model gets scanned by.
    expect(rowHeadline(run())).toBe("42.1 tok/s · 5.2 GB");
  });

  it("drops memory when it was not measured", () => {
    expect(rowHeadline(run({ peakResidentBytes: null }))).toBe("42.1 tok/s");
  });

  it("says just 'Failed' — the reason lives behind the details expander", () => {
    expect(rowHeadline(run({ ok: false, error: "out of memory", metrics: {} }))).toBe(
      "Failed",
    );
  });
});

describe("rowDetail", () => {
  it("carries exactly what the headline left out", () => {
    expect(rowDetail(run())).toBe("TTFT 310 ms · loaded in 8.4 s · mps");
  });

  it("is null when there is nothing beyond the headline — no expander to draw", () => {
    expect(
      rowDetail(run({ loadSeconds: null, device: null, metrics: { tokensPerSecond: 12 } })),
    ).toBeNull();
  });

  it("is null for a failed run — its detail comes from failureReason instead", () => {
    expect(rowDetail(run({ ok: false, error: "boom", metrics: {} }))).toBeNull();
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
    const ticks = yAxisTicks(100, 1, 4);
    expect(ticks.map((t) => t.value)).toEqual([0, 25, 50, 75, 100]);
    expect(ticks.map((t) => t.label)).toEqual(["0", "25", "50", "75", "100"]);
  });

  it("trims trailing zeros the same way every other formatted number does", () => {
    const ticks = yAxisTicks(1, 2, 4);
    expect(ticks.map((t) => t.label)).toEqual(["0", "0.25", "0.5", "0.75", "1"]);
  });

  it("has no ticks over an empty domain — there is nothing to draw a scale for", () => {
    expect(yAxisTicks(0, 1)).toEqual([]);
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
    const rows = leaderboard("text-generation", [
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
    const rows = leaderboard("text-to-image", [
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
    const rows = leaderboard("text-generation", [
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
    const rows = leaderboard("telepathy", [{ model: "x", row: null }]);
    expect(rows[0]!.barFraction).toBeNull();
  });

  it("does not blow up when every model in the section is unmeasured", () => {
    const rows = leaderboard("text-generation", [
      { model: "a", row: null },
      { model: "b", row: null },
    ]);
    expect(rows.every((r) => r.barFraction === null)).toBe(true);
  });
});

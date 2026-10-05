import { expect, test } from "bun:test";
import {
  BUSY_CPU_PCT,
  COLD_RETRIES,
  FAST_POLL_MS,
  SLOW_POLL_MS,
  formatBytes,
  formatCpu,
  pollIntervalFor,
} from "@shell/system-lib";
import type { SystemActivity } from "@platform/lib/sysmon";

const MB = 1024 ** 2;

function payload(cpus: (number | null)[]): SystemActivity {
  return {
    supported: true,
    host: null,
    history: [],
    totals: { cpuPct: null, memBytes: null },
    procs: cpus.map((cpuPct, i) => ({
      pid: i + 1,
      ppid: null,
      kind: "other",
      label: `p${i}`,
      cpuPct,
      memBytes: 1,
      startedAt: null,
    })),
  };
}

test("formatBytes writes sizes the way Activity Monitor does", () => {
  expect(formatBytes(1.02 * 1024 * MB)).toBe("1.02 GB");
  expect(formatBytes(194.3 * MB)).toBe("194.3 MB");
  expect(formatBytes(812 * 1024)).toBe("812 KB");
  expect(formatBytes(1.44 * 1024 * MB, true)).toBe("1.4 GB");
  expect(formatBytes(null)).toBe("–");
});

test("formatCpu keeps one decimal and dashes an unknown", () => {
  expect(formatCpu(12.34)).toBe("12.3%");
  expect(formatCpu(0)).toBe("0.0%");
  expect(formatCpu(null)).toBe("–");
});

test("poll is fast while asked to, or while any process is busy", () => {
  expect(pollIntervalFor(null, false)).toBe(SLOW_POLL_MS);
  expect(pollIntervalFor(null, true)).toBe(FAST_POLL_MS);
  expect(pollIntervalFor(payload([1, null, BUSY_CPU_PCT]), false)).toBe(SLOW_POLL_MS);
  expect(pollIntervalFor(payload([1, BUSY_CPU_PCT + 0.1]), false)).toBe(FAST_POLL_MS);
});

test("the slow cadence outlasts the sampler's 15 s idle, so an idle chip lets it sleep", () => {
  expect(SLOW_POLL_MS).toBeGreaterThan(15_000);
});

test("a cold read with no CPU yet is retried after 1 s, at most COLD_RETRIES times", () => {
  const cold = payload([null]);
  expect(pollIntervalFor(cold, false, 1)).toBe(FAST_POLL_MS);
  expect(pollIntervalFor(cold, false, COLD_RETRIES)).toBe(FAST_POLL_MS);
  expect(pollIntervalFor(cold, false, COLD_RETRIES + 1)).toBe(SLOW_POLL_MS);
  expect(pollIntervalFor(cold, false, 0)).toBe(SLOW_POLL_MS);
});

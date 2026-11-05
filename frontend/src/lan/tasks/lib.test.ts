import { describe, expect, test } from "bun:test";
import { parseRoute, routePath } from "../bots/lib";
import {
  cardStatus,
  caption,
  chipLanes,
  dayBreak,
  dueEpoch,
  group,
  inWords,
  laneCaption,
  laneOf,
  matches,
  mergeTurns,
  parseFilter,
  persistedFilter,
  projectOptions,
  queueLine,
  statusOf,
  titleLine,
} from "./lib";
import type { PhoneCard, PhoneTask, PhoneTurn } from "./types";

const task = (over: Partial<PhoneTask> = {}): PhoneTask => ({
  key: "s1",
  task_id: "TASK-001",
  title: "Fix the build",
  title_source: "ai",
  status: "done",
  failed: false,
  blocked_reason: "",
  attention: null,
  live: false,
  unread: 0,
  starred: false,
  project: "~/code/app",
  project_label: "app",
  target_label: "app",
  entry_url: "",
  repo: "",
  prs: [],
  backend: "claude",
  model: "",
  last_message: null,
  last_reply: "",
  started: 1_700_000_000,
  last_active: 1_700_000_100,
  happened_at: 1_700_000_100,
  message_count: 1,
  next_run: null,
  next_run_repeats: false,
  queue_position: null,
  queue_ahead_title: "",
  entry_id: "",
  bot: "",
  ...over,
});

describe("routes", () => {
  test("the tasks pages parse and print, a key may hold a colon", () => {
    expect(parseRoute("/tasks")).toEqual({ name: "tasks" });
    expect(parseRoute("/tasks/")).toEqual({ name: "tasks" });
    expect(parseRoute("/tasks/6a96b827-1111-2222-3333-444444444444")).toEqual({ name: "task", key: "6a96b827-1111-2222-3333-444444444444" });
    expect(parseRoute("/tasks/pending:20261105-100000-abc123")).toEqual({ name: "task", key: "pending:20261105-100000-abc123" });
    expect(parseRoute("/tasks/a/b")).toEqual({ name: "grid" });
    expect(parseRoute("/tasks/with space")).toEqual({ name: "grid" });
    expect(routePath({ name: "tasks" })).toBe("/tasks");
    expect(routePath({ name: "task", key: "pending:e1" })).toBe("/tasks/pending%3Ae1");
    // A link's percent-encoded colon round-trips: the browser keeps it in pathname.
    expect(parseRoute(routePath({ name: "task", key: "pending:20261105-100000-abc123" }))).toEqual({ name: "task", key: "pending:20261105-100000-abc123" });
    expect(parseRoute("/tasks/pending%3Ae1")).toEqual({ name: "task", key: "pending:e1" });
    expect(parseRoute("/tasks/%E2%80%A6")).toEqual({ name: "grid" });
    expect(parseRoute("/tasks/%zz")).toEqual({ name: "grid" });
    expect(parseRoute("/bots/x")).toEqual({ name: "bot", id: "x" });
  });
});

describe("lanes and chips", () => {
  test("a status lands in one lane", () => {
    expect(laneOf({ status: "needs_attention" })).toBe("needs");
    expect(laneOf({ status: "blocked" })).toBe("needs");
    expect(laneOf({ status: "in_progress" })).toBe("running");
    expect(laneOf({ status: "queued" })).toBe("queued");
    expect(laneOf({ status: "upcoming" })).toBe("upcoming");
    expect(laneOf({ status: "done" })).toBe("done");
    expect(laneOf({ status: "archived" })).toBe("archived");
    expect(laneOf({ status: "whatever" })).toBe("done");
  });
  test("Running is the work in flight and the line behind it; no chip is everything but the archive", () => {
    expect(chipLanes("running")).toEqual(["running", "queued"]);
    expect(chipLanes(null)).not.toContain("archived");
    expect(chipLanes("archived")).toEqual(["archived"]);
  });
  test("group: fixed lane order, folders and search applied, upcoming soonest first", () => {
    const rows = [
      task({ key: "a", status: "done" }),
      task({ key: "b", status: "upcoming", next_run: 300 }),
      task({ key: "c", status: "upcoming", next_run: 100 }),
      task({ key: "d", status: "needs_attention", project: "~/other", project_label: "other" }),
      task({ key: "e", status: "in_progress" }),
      task({ key: "f", status: "archived" }),
    ];
    const all = group(rows, { chip: null, folders: [] }, "");
    expect(all.map((s) => s.lane)).toEqual(["needs", "running", "upcoming", "done"]);
    expect(all[2].rows.map((r) => r.key)).toEqual(["c", "b"]);
    expect(group(rows, { chip: "archived", folders: [] }, "").map((s) => s.lane)).toEqual(["archived"]);
    expect(group(rows, { chip: null, folders: ["~/other"] }, "").flatMap((s) => s.rows.map((r) => r.key))).toEqual(["d"]);
    expect(group(rows, { chip: null, folders: [] }, "other").flatMap((s) => s.rows.map((r) => r.key))).toEqual(["d"]);
  });
  test("laneCaption counts running and queued apart", () => {
    expect(laneCaption("running", [task({ status: "in_progress" }), task({ status: "queued" })])).toBe("1 running, 1 queued");
    expect(laneCaption("running", [task({ status: "in_progress" })])).toBe("1 running");
    expect(laneCaption("done", [task(), task()])).toBe("2");
  });
  test("the filter persists without Archived on", () => {
    expect(persistedFilter({ chip: "archived", folders: ["~/x"] })).toEqual({ chip: null, folders: ["~/x"] });
    expect(parseFilter(JSON.stringify({ chip: "needs", folders: ["~/x", 3] }))).toEqual({ chip: "needs", folders: ["~/x"] });
    expect(parseFilter("{bad")).toEqual({ chip: null, folders: [] });
    expect(parseFilter(null)).toEqual({ chip: null, folders: [] });
    expect(parseFilter(JSON.stringify({ chip: "nope" }))).toEqual({ chip: null, folders: [] });
  });
  test("projectOptions: most tasks first, then by name", () => {
    expect(
      projectOptions([
        { path: "~/b", label: "b", count: 1 },
        { path: "~/a", label: "a", count: 1 },
        { path: "~/z", label: "z", count: 5 },
      ]).map((p) => p.label),
    ).toEqual(["z", "a", "b"]);
  });
});

describe("rows", () => {
  test("search matches every word somewhere across title, reply, folder and id", () => {
    const t = task({ title: "Fix the build", last_reply: "Done, tests pass", task_id: "TASK-042" });
    expect(matches(t, "")).toBe(true);
    expect(matches(t, "fix tests")).toBe(true);
    expect(matches(t, "task-042")).toBe(true);
    expect(matches(t, "app")).toBe(true);
    expect(matches(t, "nope")).toBe(false);
  });
  test("titleLine: the newest user message's first line, else the title", () => {
    expect(titleLine(task())).toBe("Fix the build");
    expect(titleLine(task({ last_message: { role: "user", text: "  also the tests\nplease", at: 1 } }))).toBe("also the tests");
    expect(titleLine(task({ last_message: { role: "assistant", text: "ok", at: 1 } }))).toBe("Fix the build");
    expect(titleLine(task({ title: "", last_message: null }))).toBe("Untitled");
  });
  test("statusOf words and tones", () => {
    expect(statusOf(task({ status: "needs_attention" }))).toEqual({ word: "Needs you", tone: "needs" });
    expect(statusOf(task({ status: "blocked", blocked_reason: "usage_limit" }))).toEqual({ word: "Usage limit", tone: "needs" });
    expect(statusOf(task({ status: "blocked", blocked_reason: "failed" }))).toEqual({ word: "Failed", tone: "error" });
    expect(statusOf(task({ status: "in_progress" }))).toEqual({ word: "Working", tone: "busy" });
    expect(statusOf(task({ status: "done", failed: true }))).toEqual({ word: "Failed", tone: "error" });
    expect(statusOf(task({ status: "archived" }))).toEqual({ word: "Archived", tone: "idle" });
  });
  test("queueLine and caption", () => {
    expect(queueLine(task({ queue_position: 2, queue_ahead_title: "Fix the build" }))).toBe("2nd in line behind Fix the build");
    expect(queueLine(task({ queue_position: 1 }))).toBe("1st in line");
    expect(queueLine(task())).toBe("");
    expect(caption(task(), 1_700_000_100 + 7200)).toBe("app · Claude · 2h");
    expect(inWords(100, 70)).toBe("in 30s");
    expect(inWords(100 + 5 * 60, 100)).toBe("in 5 min");
    expect(inWords(100 + 3 * 3600, 100)).toBe("in 3 h");
    expect(inWords(100 + 2 * 86400, 100)).toBe("in 2 days");
    expect(inWords(100, 200)).toBe("now");
    expect(caption(task({ backend: "", happened_at: null, last_active: null, started: null }), 1)).toBe("app");
  });
  test("dueEpoch reads ISO and epoch stamps", () => {
    expect(dueEpoch("2026-11-05T10:00:00Z")).toBe(Date.parse("2026-11-05T10:00:00Z") / 1000);
    expect(dueEpoch(1_700_000_000)).toBe(1_700_000_000);
    expect(dueEpoch(1_700_000_000_000)).toBe(1_700_000_000);
    expect(Number.isNaN(dueEpoch("soon"))).toBe(true);
    expect(Number.isNaN(dueEpoch(null))).toBe(true);
  });
});

describe("thread", () => {
  const turn = (i: number, kind: PhoneTurn["kind"] = "user", text = `t${i}`): PhoneTurn =>
    kind === "tool"
      ? { i, id: `id${i}`, kind, name: "Bash", summary: text, status: "ok", detail: "", at: null }
      : kind === "system"
        ? { i, id: `id${i}`, kind, text, tone: "info", at: null }
        : { i, id: `id${i}`, kind, text, at: null };
  test("mergeTurns replaces by index and appends, sorted; reset takes the incoming whole", () => {
    const old = [turn(0), turn(1, "assistant", "half")];
    const merged = mergeTurns(old, [turn(1, "assistant", "whole"), turn(2, "tool")]);
    expect(merged.map((t) => t.i)).toEqual([0, 1, 2]);
    expect((merged[1] as { text: string }).text).toBe("whole");
    expect(mergeTurns(old, [])).toBe(old);
    expect(mergeTurns(old, [turn(5)], true).map((t) => t.i)).toEqual([5]);
  });
  test("dayBreak only between stamped turns on different days", () => {
    const a = { ...turn(0), at: Date.UTC(2026, 10, 5, 10) / 1000 };
    const b = { ...turn(1), at: Date.UTC(2026, 10, 5, 11) / 1000 };
    const c = { ...turn(2), at: Date.UTC(2026, 10, 7, 11) / 1000 };
    expect(dayBreak(undefined, a)).toBe(true);
    expect(dayBreak(a, b)).toBe(false);
    expect(dayBreak(b, c)).toBe(true);
    expect(dayBreak(a, turn(3))).toBe(false);
  });
  test("cardStatus per verdict", () => {
    const card = (verdict: PhoneCard["verdict"], kind: PhoneCard["kind"] = "permission"): PhoneCard => ({
      request_id: "r",
      kind,
      tool: "Bash",
      summary: "",
      detail: "",
      risk: null,
      verdict,
      held: verdict === "held",
      created_at: 0,
      questions: null,
      answers: {},
    });
    expect(cardStatus(card("allow")).text).toBe("✓ Allowed");
    expect(cardStatus(card("allow", "question")).text).toBe("✓ Answered");
    expect(cardStatus(card("deny")).tone).toBe("deny");
    expect(cardStatus(card("held")).tone).toBe("queued");
    expect(cardStatus(card("expired")).text).toBe("Expired");
    expect(cardStatus(card("pending"))).toEqual({ tone: "", text: "" });
  });
});

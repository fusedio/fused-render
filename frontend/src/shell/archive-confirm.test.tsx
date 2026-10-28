// The single-archive ask, driven through the hook the Cards, Peek, Calendar and
// chat Kebab archive buttons share, and a pin that each of them goes through it.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { act, create } from "react-test-renderer";

const { useArchiveConfirm } = await import("./archive-confirm");
import type { ArchiveFacts } from "./task-selection";

const mounted: ReturnType<typeof create>[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

function harness() {
  let guard!: ReturnType<typeof useArchiveConfirm>["guard"];
  const Probe = () => {
    const a = useArchiveConfirm();
    guard = a.guard;
    return a.node as never;
  };
  const r = create(createElement(Probe));
  mounted.push(r);
  return { r, guard: (t: ArchiveFacts, go: () => void) => act(() => guard(t, go)) };
}

const job = { count: 1, run_id: "r", jobs: [{ id: "j", title: "" }] };

test("a needs_attention task asks first, and archives only on confirm", () => {
  const { r, guard } = harness();
  let went = 0;
  guard({ status: "needs_attention" }, () => (went += 1));
  expect(went).toBe(0);
  const dlg = r.root.findAllByProps({ title: "Archive task" })[0];
  expect(dlg.props.message).toBe("Archive 1 task? 1 is still running and will be stopped.");
  act(() => dlg.props.onCancel());
  expect(went).toBe(0);
  expect(r.root.findAllByProps({ title: "Archive task" }).length).toBe(0);
});

test("a Done task with a live background job asks; a plain Done task does not", () => {
  const { r, guard } = harness();
  let went = 0;
  guard({ status: "done" }, () => (went += 1));
  expect(went).toBe(1);
  guard({ status: "done", background_jobs: job }, () => (went += 1));
  expect(went).toBe(1);
  expect(r.root.findAllByProps({ title: "Archive task" }).length).toBeGreaterThan(0);
});

test("every single-archive door goes through the hook", () => {
  const dir = join(import.meta.dir);
  for (const f of ["TaskCards.tsx", "TaskPeek.tsx", "ScheduleCalendar.tsx", "../apps/claude/ui/Kebab.tsx"]) {
    const src = readFileSync(join(dir, f), "utf8");
    expect(src).toContain("useArchiveConfirm()");
    expect(src).toContain("archiveAsk.node");
  }
});

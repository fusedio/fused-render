// The project view caps each section at ten rows and ends it with a
// "Show N more" row (Surya, 2026-10-23).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement, useState } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { Task } from "@platform/lib/api";

const { groupFrames, cappedSectionsHolding, PROJECT_GROUP_ROW_CAP } = await import("./ScheduleTaskViews");

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

const tasks = (n: number) => Array.from({ length: n }, (_, i) => ({ key: `t${i}` }) as Task);
const group = (n: number) => [{ key: "/p", label: "p", rows: tasks(n) }] as never;

function Harness({ n, by }: { n: number; by: string }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const idx: number[] = [];
  const out = groupFrames(
    group(n), (t, i) => { idx.push(i); return createElement("div", { key: t.key, className: "row" }); },
    by as never, new Set(), undefined, () => {}, expanded, setExpanded,
  );
  return createElement("div", null, out, createElement("i", { id: "idx" }, idx.join(",")));
}

function render(n: number, by = "project") {
  let r!: ReactTestRenderer;
  act(() => { r = create(createElement(Harness, { n, by })); });
  mounted.push(r);
  return r;
}
const rows = (r: ReactTestRenderer) => r.root.findAll((x) => x.props.className === "row").length;
const more = (r: ReactTestRenderer) => r.root.findAll((x) => x.type === "button" && x.props.className === "tasks-group-more");

test("caps at 10 with Show 5 more, reveals, then Show less", () => {
  const r = render(15);
  expect(rows(r)).toBe(PROJECT_GROUP_ROW_CAP);
  expect(more(r)[0]!.children).toEqual(["Show 5 more"]);
  act(() => more(r)[0]!.props.onClick());
  expect(rows(r)).toBe(15);
  expect(more(r)[0]!.children).toEqual(["Show less"]);
  act(() => more(r)[0]!.props.onClick());
  expect(rows(r)).toBe(10);
});

test("no row at 10 or fewer, and no cap for other groupings", () => {
  expect(more(render(10))).toHaveLength(0);
  const r = render(15, "pr");
  expect(rows(r)).toBe(15);
  expect(more(r)).toHaveLength(0);
});

test("cappedSectionsHolding names sections with the key beyond the cap", () => {
  const g = group(15);
  expect(cappedSectionsHolding(g, "project", new Set(["t12"]))).toEqual(["project:/p"]);
  expect(cappedSectionsHolding(g, "project", new Set(["t3"]))).toEqual([]);
  expect(cappedSectionsHolding(g, "pr" as never, new Set(["t12"]))).toEqual([]);
});

// `TaskRowItem`'s `showProject`: the folder chip is off on a borrowed list by
// default (no `onPickProject` filter to arm) and a host can opt it back in as a
// plain label — the Home Tasks widget does.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { Task } from "@platform/lib/api";

const { TaskRowItem, IdentityChip } = await import("./ScheduleTaskViews");

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

const task = {
  key: "sess-1",
  task_id: "TASK-099",
  title: "say hi",
  project: "/repo/alpha",
  target: "/repo/alpha/x.py",
  session_id: "sess-1",
  status: "done",
  last_active: 1,
} as Task;

function chips(props: { showProject?: boolean }) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(TaskRowItem, { task, href: "/tasks", onPress: () => {}, ...props }));
  });
  mounted.push(r);
  return r.root.findAllByType(IdentityChip);
}

test("TaskRowItem draws no folder chip by default", () => {
  expect(chips({})).toHaveLength(0);
});

test("TaskRowItem draws the folder chip with showProject", () => {
  expect(chips({ showProject: true }).length).toBeGreaterThan(0);
});

// "OPEN IN TASKS" in every task ⋮ that can be up off the Tasks page. The URL it writes must be the one the Tasks page
// reads back; the hosts are read off the source (their mounts need a rail
// chassis, the New task form and the whole chat), as task-head-hosts.test.ts.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { openInTasksHref, TASKS_PEEK_PARAM } from "@platform/lib/task-actions";
import { PEEK_PARAM, readPeekParam } from "./task-peek-store";

const read = (name: string) => readFileSync(join(import.meta.dir, name), "utf8");

test("the href is the Tasks page's own deep link", () => {
  expect(TASKS_PEEK_PARAM).toBe(PEEK_PARAM);
  const href = openInTasksHref({ on: true, taskKey: "k/1", pathname: "/" })!;
  expect(readPeekParam(href.slice(href.indexOf("?")))).toBe("k/1");
});

test("rail cards, New task card and chat panes offer it", () => {
  const rail = read("claude-rail/RailPeek.tsx");
  expect(rail).toContain("on: true,");
  expect(rail).toContain("void navigateUrl(href);\n        onHeld(false);\n        onDismiss(\"door\");");
  const tasksCard = read("claude-rail/TasksPeek.tsx");
  expect(tasksCard.match(/openInTasksItem\(\{ on: true,/g)?.length).toBe(1);
  const nt = read("NewJobModal.tsx");
  expect(nt.match(/openInTasksItem\(\{ on: true, taskKey: liveChat\.key/g)?.length).toBe(1);
  const chat = read("../apps/claude/ClaudeChat.tsx");
  expect(chat).toContain("on: headUnified,");
  expect(chat).toContain("taskKey: inChat ? (head.task?.key ?? null) : null");
});

test("the Tasks page's own views do not offer it", () => {
  expect(read("TaskPeek.tsx")).not.toContain("openInTasks");
  expect(read("TaskCards.tsx")).not.toContain("openInTasks");
});

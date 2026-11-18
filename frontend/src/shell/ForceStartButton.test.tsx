// FORCE START ON A CARD WITH NO CHAT YET — the button the rail's placeholders
// draw for a queued task (ForceStartButton.tsx's header).
//
// A RENDER TEST, like queue-caption-press.test.tsx beside it: what matters is
// that the button is THERE on a queued task, absent on everything else, and
// that a press reaches the one endpoint with the name the card was given.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { act, create, type ReactTestRenderer, type ReactTestInstance } from "react-test-renderer";

import type { QueueFacts } from "@platform/lib/queue";

const { ForceStartButton, queuedLine } = await import("./ForceStartButton");

const mounted: ReactTestRenderer[] = [];
const realFetch = globalThis.fetch;
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  (globalThis as { fetch: unknown }).fetch = realFetch;
});

const QUEUED: QueueFacts = { status: "queued", queue_position: 2, queue_ahead: "TASK-046" };

function mount(props: Parameters<typeof ForceStartButton>[0]) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(ForceStartButton, props));
  });
  mounted.push(r);
  return r;
}

const buttons = (r: ReactTestRenderer): ReactTestInstance[] => r.root.findAllByType("button");
const notes = (r: ReactTestRenderer): ReactTestInstance[] => r.root.findAllByProps({ role: "alert" });

function press(button: ReactTestInstance) {
  act(() => {
    (button.props as { onClick: (e: unknown) => void }).onClick({ stopPropagation() {} });
  });
}

/** `fetch`, answering every call with `answer` and keeping what was asked. */
function fakeFetch(answer: () => Response | Promise<Response>) {
  const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
  (globalThis as { fetch: unknown }).fetch = async (url: string, init?: RequestInit) => {
    if (String(url).includes("/api/tasks/queue/force")) {
      calls.push({ url: String(url), body: JSON.parse(String(init?.body)) as Record<string, unknown> });
      return answer();
    }
    // Anything else the press wakes (the listing's re-read) gets an empty answer.
    return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
  };
  return calls;
}

test("a queued task draws the button, with the Tasks page's own word and hint", () => {
  const r = mount({ facts: QUEUED, target: { key: "pending:e1" } });
  expect(buttons(r)).toHaveLength(1);
  expect(buttons(r)[0]!.props.children).toBe("Force start");
  expect(buttons(r)[0]!.props.title).toBe("Run immediately");
  // …at the head of the line too: "next" is not "now".
  expect(buttons(mount({ facts: { ...QUEUED, queue_position: 1 }, target: { key: "k" } }))).toHaveLength(1);
});

test("nothing is drawn for a task that stands in no line, or a card that has not heard yet", () => {
  expect(buttons(mount({ facts: null, target: { key: "k" } }))).toHaveLength(0);
  expect(buttons(mount({ facts: undefined, target: { key: "k" } }))).toHaveLength(0);
  expect(buttons(mount({ facts: { status: "queued", queue_position: 0 }, target: { key: "k" } }))).toHaveLength(0);
  expect(buttons(mount({ facts: { status: "in_progress" }, target: { key: "k" } }))).toHaveLength(0);
  expect(buttons(mount({ facts: { status: "upcoming" }, target: { key: "k" } }))).toHaveLength(0);
});

test("the press posts the card's own name to /api/tasks/queue/force, once, and tells the card", async () => {
  const calls = fakeFetch(() => ({ ok: true, status: 200, json: async () => ({ ok: true, started: true }) }) as unknown as Response);
  let done = 0;
  const r = mount({ facts: QUEUED, target: { entry_id: "e1" }, onDone: () => (done += 1) });
  press(buttons(r)[0]!);
  // BUSY WHILE IT IS ASKED: a second press does not start a second run.
  expect(buttons(r)[0]!.props.disabled).toBe(true);
  press(buttons(r)[0]!);
  await act(async () => {});
  expect(calls).toHaveLength(1);
  expect(calls[0]!.url).toBe("/api/tasks/queue/force");
  expect(calls[0]!.body).toEqual({ entry_id: "e1" });
  expect(done).toBe(1);
  expect(buttons(r)[0]!.props.disabled).toBe(false);
  expect(notes(r)).toHaveLength(0);
});

test("a refusal is said beside the button, in the server's words, and a new press clears it", async () => {
  let refuse = true;
  const calls = fakeFetch(() =>
    refuse
      ? ({ ok: false, status: 409, statusText: "Conflict", json: async () => ({ detail: "That task is already running." }), text: async () => JSON.stringify({ detail: "That task is already running." }) } as unknown as Response)
      : ({ ok: true, status: 200, json: async () => ({ ok: true, started: true }) } as unknown as Response),
  );
  let done = 0;
  const r = mount({ facts: QUEUED, target: { key: "pending:e1" }, onDone: () => (done += 1) });
  press(buttons(r)[0]!);
  await act(async () => {});
  // A `pending:<entry>` key goes as its entry id: the row may rekey under the press.
  expect(calls[0]!.body).toEqual({ entry_id: "e1" });
  expect(notes(r)).toHaveLength(1);
  expect(String(notes(r)[0]!.props.children).length).toBeGreaterThan(0);
  // The card is told either way: a refusal usually means the row moved on.
  expect(done).toBe(1);
  expect(buttons(r)[0]!.props.disabled).toBe(false);
  refuse = false;
  press(buttons(r)[0]!);
  await act(async () => {});
  expect(notes(r)).toHaveLength(0);
});

test("a task that has a session is named by its key", async () => {
  const calls = fakeFetch(() => ({ ok: true, status: 200, json: async () => ({ ok: true, started: true }) }) as unknown as Response);
  const r = mount({ facts: QUEUED, target: { key: "sess-1" } });
  press(buttons(r)[0]!);
  await act(async () => {});
  expect(calls[0]!.body).toEqual({ key: "sess-1" });
});

test("queuedLine says where a queued task stands, and nothing for any other", () => {
  expect(queuedLine(QUEUED)).toBe("Queued · after TASK-046 | 2nd");
  // The server placed it nowhere: the word alone, never "Queued · queued".
  expect(queuedLine({ status: "queued" })).toBe("Queued");
  expect(queuedLine({ status: "in_progress", queue_position: 1 })).toBe("");
  expect(queuedLine(null)).toBe("");
});

test("every rail placeholder for a task with no chat draws the button", () => {
  // The three seats that used to show a queued task with nothing to press: the
  // rail / favourite peek card's plain body, the Tasks card's "no conversation
  // yet" note, and the New task card's "Starting the task…".
  const read = (rel: string) => readFileSync(join(import.meta.dir, rel), "utf8");
  for (const rel of ["claude-rail/RailPeek.tsx", "claude-rail/TasksPeek.tsx", "NewJobModal.tsx"]) {
    expect(read(rel)).toContain("<ForceStartButton");
  }
  // …and its skin is in a sheet the barrel loads on every page.
  expect(read("../styles/claude-rail.css")).toContain(".force-start-btn {");
});

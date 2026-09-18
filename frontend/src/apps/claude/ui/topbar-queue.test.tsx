// THE HEADER'S QUEUED STATE — the dashed ring and the sentence the Tasks row
// has always worn, in the pane that is actually waiting.
//
// The bug (Akshil, 2026-09-17): a send into a busy folder drew the dashed bubble
// and the "1 message waiting" pill, and the header above them — `Claude · alpha
// · T048` — said nothing at all. A chat waiting behind somebody else's run
// looked exactly like an idle one, while the Tasks list three pixels away showed
// the state plainly.
//
// WHAT IS ASSERTED is only what could drift: that the words are `queueCaption`'s
// (never a second wording minted here), that the ring is the LIST's ring class
// (`schedule-ring--queued`, so the two surfaces cannot restyle apart), and that
// a row which is not queued draws neither.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { QueueFacts } from "@platform/lib/queue";

const { Topbar } = await import("./Topbar");

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

function render(queue: QueueFacts | null) {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(
      createElement(Topbar, {
        sessionId: "",
        subtitle: "alpha",
        taskId: "TASK-048",
        running: false,
        queue,
      }),
    );
  });
  mounted.push(r);
  return r;
}

/** Every string the tree prints, joined — the header is one line and reads as
 *  one sentence, so that is how it is asserted. */
function text(r: ReactTestRenderer): string {
  const out: string[] = [];
  const walk = (node: unknown): void => {
    if (typeof node === "string") {
      out.push(node);
      return;
    }
    if (Array.isArray(node)) {
      for (const n of node) walk(n);
      return;
    }
    if (node && typeof node === "object" && "children" in node) {
      walk((node as { children: unknown }).children);
    }
  };
  walk(r.toJSON() as unknown);
  return out.join("");
}

function ringClasses(r: ReactTestRenderer): string[] {
  return r.root
    .findAll((n) => typeof n.type === "string" && typeof n.props.className === "string")
    .map((n) => String(n.props.className))
    .filter((c) => c.includes("schedule-ring"));
}

test("a queued row wears the list's dashed ring and the queue's own words", async () => {
  const r = render({
    status: "queued",
    queue_position: 1,
    queue_ahead: "TASK-046",
  });
  expect(text(r)).toContain("queued · 1st in line · behind TASK-046");
  expect(ringClasses(r).some((c) => c.includes("schedule-ring--queued"))).toBe(true);
});

test("the placeless answer is still a sentence", async () => {
  // The server could not place it — an honest answer, and NOT the head. It reads
  // "in line" rather than "0th in line" (platform/lib/queue).
  expect(text(render({ status: "queued" }))).toContain("queued · in line");
});

test("a running row draws no queued state at all", async () => {
  const r = render({ status: "in_progress", queue_position: 2 });
  expect(text(r)).not.toContain("queued");
  expect(ringClasses(r)).toEqual([]);
});

test("no row is the header main has always drawn", async () => {
  const r = render(null);
  const said = text(r);
  expect(said).toContain("Claude");
  expect(said).toContain("alpha");
  expect(said).toContain("T048");
  expect(said).not.toContain("queued");
});

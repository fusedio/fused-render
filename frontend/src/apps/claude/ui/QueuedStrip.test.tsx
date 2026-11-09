import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

const { QueuedStrip } = await import("./QueuedStrip");

const render = (props: Partial<Parameters<typeof QueuedStrip>[0]>) =>
  renderToStaticMarkup(
    createElement(QueuedStrip, { lines: [], running: true, onSendNow: () => {}, onPull: () => {}, ...props }),
  );

describe("QueuedStrip", () => {
  test("renders nothing with no lines", () => {
    expect(render({})).toBe("");
  });
  test("every line is its own row with the send-now affordance while running", () => {
    const out = render({ lines: [{ id: "a", text: "again" }, { id: "b", text: "again" }] });
    expect(out.match(/c-queue-row/g)).toHaveLength(2);
    expect(out.match(/Ctrl\+X to send now/g)).toHaveLength(2);
    expect(out).not.toContain(">edit<");
  });
  test("no send-now when nothing is running", () => {
    expect(render({ running: false, lines: [{ id: "a", text: "x" }] })).not.toContain("Ctrl+X");
  });
  test("an outbox line can be pulled back; a held one says what it waits for", () => {
    const out = render({ lines: [{ id: "a", text: "x", pending: "queued" }, { id: "b", text: "y", pending: "held" }] });
    expect(out.match(/>edit</g)).toHaveLength(2);
    expect(out).toContain("(queued until this turn is done)");
  });
});

import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

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
  test("a line still on its way to the run is listed with nothing to press", () => {
    // Not the page's to pull back, not yet the run's to be sent ahead of.
    const out = render({ lines: [{ id: "a", text: "on its way", pending: "sending" }] });
    expect(out).toContain("on its way");
    expect(out).not.toContain("Ctrl+X");
    expect(out).not.toContain(">edit<");
  });
  test("a compact tile lists the lines with no actions", () => {
    const out = render({ compact: true, lines: [{ id: "a", text: "x", pending: "queued" }] });
    expect(out).toContain("c-queue-compact");
    expect(out).toContain("c-queue-row");
    expect(out).not.toContain("Ctrl+X");
    expect(out).not.toContain(">edit<");
  });

  // THE ARRIVAL FLASH (styles/sched.css `.c-queue-row.is-arriving`): on a line
  // that joined while the strip was up, and on nothing else.
  const mountStrip = (lines: Parameters<typeof QueuedStrip>[0]["lines"], ready = true) => {
    let isReady = ready;
    const props = (next: typeof lines) => ({ lines: next, running: true, onSendNow: () => {}, onPull: () => {}, ready: isReady });
    let r!: ReactTestRenderer;
    act(() => {
      r = create(createElement(QueuedStrip, props(lines)));
    });
    const arriving = () =>
      r.root
        .findAll((n) => n.type === "li")
        .filter((n) => String(n.props.className).split(" ").includes("is-arriving"))
        .map((n) => String(n.findByProps({ className: "c-queue-text" }).props.children));
    return {
      tags: () => r.root.findAllByProps({ className: "c-queue-tag" }).map((n) => String(n.props.children)),
      arriving,
      set: (next: typeof lines, nowReady = isReady) => {
        isReady = nowReady;
        act(() => r.update(createElement(QueuedStrip, props(next))));
      },
      unmount: () => act(() => r.unmount()),
    };
  };
  test("a line that was already waiting when the strip mounted does not flash", () => {
    // A rail card opened on hover, a pane switched back to: the chat mounts
    // with its queue in it, and nothing just happened (Bugbot, PR #2347).
    const s = mountStrip([{ id: "a", text: "old" }]);
    expect(s.arriving()).toEqual([]);
    expect(s.tags()).toEqual([]);
    s.set([{ id: "a", text: "old" }]);
    expect(s.arriving()).toEqual([]);
    s.unmount();
  });
  test("a line that joins flashes, alone, whether the strip was empty or not", () => {
    const s = mountStrip([]);
    s.set([{ id: "a", text: "first" }]);
    expect(s.arriving()).toEqual(["first"]);
    // …and says so in a word, for as long as the arrival lasts.
    expect(s.tags()).toEqual(["Queued"]);
    s.set([{ id: "a", text: "first" }, { id: "b", text: "second" }]);
    expect(s.arriving()).toEqual(["first", "second"]);
    // One leaving is not an arrival for the ones that stay.
    const t = mountStrip([{ id: "a", text: "old" }, { id: "b", text: "older" }]);
    t.set([{ id: "b", text: "older" }]);
    expect(t.arriving()).toEqual([]);
    t.set([{ id: "b", text: "older" }, { id: "c", text: "new" }]);
    expect(t.arriving()).toEqual(["new"]);
    s.unmount();
    t.unmount();
  });
  test("lines a still-loading chat restores are the baseline, and the next one after it is an arrival", () => {
    // A cold reopen: the strip mounts empty before the history read returns,
    // and the restore then brings the lines that were waiting all along.
    const s = mountStrip([], false);
    s.set([{ id: "a", text: "was waiting" }], false);
    expect(s.arriving()).toEqual([]);
    s.set([{ id: "a", text: "was waiting" }], true);
    expect(s.arriving()).toEqual([]);
    s.set([{ id: "a", text: "was waiting" }, { id: "b", text: "typed now" }], true);
    expect(s.arriving()).toEqual(["typed now"]);
    s.unmount();
  });
  test("a line the strip stopped drawing for a while has not arrived when it is drawn again", () => {
    const s = mountStrip([{ id: "a", text: "continue" }]);
    s.set([{ id: "h", text: "held twin" }]);
    expect(s.arriving()).toEqual(["held twin"]);
    // The held twin is pulled back: the first line is drawn again. It never left.
    s.set([{ id: "a", text: "continue" }]);
    expect(s.arriving()).toEqual([]);
    s.unmount();
  });
  test("an id with a newline in it is one id", () => {
    // An inbox row with no id of its own is keyed by its words.
    const s = mountStrip([]);
    s.set([{ id: "first\nsecond", text: "two lines" }]);
    expect(s.arriving()).toEqual(["two lines"]);
    s.unmount();
  });
});

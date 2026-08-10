// The published-node store behind the crumb bar's two portals. The relocation
// case is the whole of it: when the bar moves, the incoming slot publishes
// before the outgoing one is torn down, and an unconditional retract there
// would blank a bar that is already live.
import { describe, expect, test } from "bun:test";
import { createNodeSlot } from "./node-slot";

// The store only ever holds and compares the node, so a stand-in is enough
// here — these tests run without a DOM.
const node = (name: string) => ({ name }) as unknown as HTMLElement;

describe("node slot", () => {
  test("empty until something publishes", () => {
    expect(createNodeSlot().get()).toBe(null);
  });

  test("publish then retract leaves it empty again", () => {
    const slot = createNodeSlot();
    const el = node("a");
    slot.publish(el);
    expect(slot.get()).toBe(el);
    slot.retract(el);
    expect(slot.get()).toBe(null);
  });

  test("a stale node's retract cannot blank the live slot", () => {
    const slot = createNodeSlot();
    const outgoing = node("outgoing");
    const incoming = node("incoming");
    slot.publish(outgoing);
    // The relocation order: the new container's layout effect runs before the
    // old one's cleanup.
    slot.publish(incoming);
    slot.retract(outgoing);
    expect(slot.get()).toBe(incoming);
  });

  test("subscribers hear every change, and only real changes", () => {
    const slot = createNodeSlot();
    let calls = 0;
    const off = slot.subscribe(() => calls++);
    const el = node("a");
    slot.publish(el);
    expect(calls).toBe(1);
    slot.publish(el); // same node re-published: no churn
    expect(calls).toBe(1);
    slot.retract(node("other")); // not the live node: no churn
    expect(calls).toBe(1);
    slot.retract(el);
    expect(calls).toBe(2);
    off();
    slot.publish(el);
    expect(calls).toBe(2);
  });

  test("reset empties the slot for the next test", () => {
    const slot = createNodeSlot();
    slot.publish(node("a"));
    slot.reset();
    expect(slot.get()).toBe(null);
  });

  test("two slots are independent", () => {
    const a = createNodeSlot();
    const b = createNodeSlot();
    a.publish(node("a"));
    expect(b.get()).toBe(null);
  });
});

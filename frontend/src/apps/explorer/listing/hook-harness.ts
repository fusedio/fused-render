// A render harness for the listing's hooks, so the behaviours that only exist
// as a SEQUENCE — a poll cycle, a reply that outlives its folder, a query
// typed over an answer in flight — can be tested by driving them rather than
// by grepping the implementation for the expression that is supposed to
// handle them.
//
// Why this exists at all: the suite has no DOM, so everything the ranked
// search box does across time was covered by `toContain` guards over the
// hook's own source. Those fail on a rename and pass on a semantic
// regression — and the one they let through (a selection cleared and
// re-placed on every poll tick, remounting the preview each time) is exactly
// the kind a driven test catches on the first assertion.
//
// react-test-renderer, not a DOM library: it runs effects and state through
// real React with no document, which is all a hook needs. React 18 is pinned
// here, which is where that renderer is supported.
//
// The clock is virtual and the module boundary is mocked, so a test states a
// SCHEDULE — issue a query, let the request land, advance past the poll
// interval — and asserts on what the hook returned.
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement, type ReactElement } from "react";

interface Timer {
  at: number;
  fn: () => void;
}

/** A virtual clock standing in for `window`'s timers and `Date.now`. */
export class Clock {
  now = 1_000_000;
  private timers = new Map<number, Timer>();
  private nextId = 1;
  private realNow = Date.now;

  install(): void {
    const self = this;
    // The hook schedules through `window.setTimeout`; there is no window here.
    (globalThis as Record<string, unknown>).window = {
      setTimeout: (fn: () => void, ms = 0) => {
        const id = self.nextId++;
        self.timers.set(id, { at: self.now + ms, fn });
        return id;
      },
      clearTimeout: (id: number) => void self.timers.delete(id),
      requestIdleCallback: undefined,
    };
    // `location` is read for the URL-synced query. Every test here runs the
    // hook with urlSync=false (the embedded mode), but the initial read
    // happens before that is consulted.
    (globalThis as Record<string, unknown>).location = { search: "", pathname: "/x" };
    Date.now = () => self.now;
  }

  restore(): void {
    Date.now = this.realNow;
    delete (globalThis as Record<string, unknown>).window;
    delete (globalThis as Record<string, unknown>).location;
    this.timers.clear();
  }

  /** Move the clock, firing every timer that comes due, oldest first. */
  advance(ms: number): void {
    const target = this.now + ms;
    for (;;) {
      const due = [...this.timers.entries()]
        .filter(([, t]) => t.at <= target)
        .sort((a, b) => a[1].at - b[1].at);
      if (!due.length) break;
      const [id, timer] = due[0];
      this.timers.delete(id);
      this.now = Math.max(this.now, timer.at);
      timer.fn();
    }
    this.now = target;
  }

  get pending(): number {
    return this.timers.size;
  }
}

/**
 * A promise whose resolution a test controls.
 *
 * Every request the hook makes is one of these, so a test can hold a reply
 * open — which is the only way to observe what the box does while an answer
 * is in flight, and half of what these tests are for.
 */
export class Deferred<T> {
  readonly promise: Promise<T>;
  resolve!: (value: T) => void;
  reject!: (err: Error) => void;

  constructor() {
    this.promise = new Promise<T>((res, rej) => {
      this.resolve = res;
      this.reject = rej;
    });
  }
}

/** Mount a hook and expose its latest return value. */
export function renderHook<P extends unknown[], R>(
  hook: (...args: P) => R,
  ...args: P
): {
  current: () => R;
  rerender: (...next: P) => void;
  unmount: () => void;
} {
  let latest: R;
  let renderer: ReactTestRenderer;
  const Probe = (props: { args: P }): ReactElement | null => {
    latest = hook(...props.args);
    return null;
  };
  act(() => {
    renderer = create(createElement(Probe, { args }));
  });
  return {
    current: () => latest,
    rerender: (...next: P) => {
      act(() => {
        renderer.update(createElement(Probe, { args: next }));
      });
    },
    unmount: () => {
      act(() => {
        renderer.unmount();
      });
    },
  };
}

/** Run `fn` inside `act` and let any microtasks it releases settle. */
export async function flush(fn: () => void = () => {}): Promise<void> {
  await act(async () => {
    fn();
    await Promise.resolve();
    await Promise.resolve();
  });
}

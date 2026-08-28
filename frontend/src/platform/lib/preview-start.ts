// Shared startup budget for display-only card previews.
//
// A Home row can mount several iframes at once. Each one is a real document:
// even a cached shell still pays JS parse/execute, React startup and its own
// API calls. Let cards paint first, then admit only a small number of preview
// navigations at a time. A slot is held until the iframe loads, errors, times
// out, or unmounts; after that the document may stay mounted without blocking
// the next preview from starting.
import { useCallback, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

type Start = (release: () => void) => void;

// How urgent a queued start is. `true` is a REAL GESTURE (a hover) and jumps
// the queue outright. A GETTER is a claim that can change while the task waits
// — "this card is on screen right now" — and is read at ADMISSION time rather
// than at request time. That is what lets a card scrolled into view overtake
// the lookahead cards queued ahead of it without its component re-requesting:
// a re-request tears the iframe down and starts it again (usePreviewStart's
// effect resets `started`), so promotion must never travel through the deps.
type Priority = boolean | (() => boolean);

const isHot = (p: Priority): boolean => (typeof p === "function" ? p() : p);

export interface PreviewStartQueue {
  request(start: Start, priority?: Priority): () => void;
  active(): number;
  pending(): number;
}

export function createPreviewStartQueue(limit: number): PreviewStartQueue {
  if (!Number.isInteger(limit) || limit < 1) throw new Error("preview start limit must be positive");

  type Task = {
    start: Start;
    priority: Priority;
    started: boolean;
    cancelled: boolean;
    release?: () => void;
  };
  const waiting: Task[] = [];
  let running = 0;

  // The next task to admit: the oldest HOT one (see Priority), else the oldest
  // task. Re-evaluated at every admission, so what ranks a task is its hotness
  // at the moment a slot frees — not its hotness when it was queued.
  const takeNext = (): Task => {
    const hot = waiting.findIndex((t) => !t.cancelled && isHot(t.priority));
    return waiting.splice(hot === -1 ? 0 : hot, 1)[0];
  };

  const drain = () => {
    while (running < limit && waiting.length) {
      const task = takeNext();
      if (task.cancelled) continue;
      task.started = true;
      running += 1;
      let released = false;
      task.release = () => {
        if (released) return;
        released = true;
        running -= 1;
        drain();
      };
      task.start(task.release);
    }
  };

  return {
    request(start, priority = false) {
      const task: Task = { start, priority, started: false, cancelled: false };
      // A gesture goes to the HEAD as well as reading hot: the on-screen cards
      // are hot too, and the hover has to outrank them.
      if (priority === true) waiting.unshift(task);
      else waiting.push(task);
      drain();
      return () => {
        if (task.cancelled) return;
        task.cancelled = true;
        if (task.started) task.release?.();
        else {
          const i = waiting.indexOf(task);
          if (i !== -1) waiting.splice(i, 1);
        }
      };
    },
    active: () => running,
    pending: () => waiting.length,
  };
}

const previewStarts = createPreviewStartQueue(2);
const START_TIMEOUT_MS = 10_000;

// Expand a bit past the actual viewport: previews are ready before a card
// scrolls on screen, while cards several rows away do not consume a whole
// iframe document. The same root selector covers the Apps hub and both Home
// surfaces, each of which owns its vertical scroller.
//
// This is a lookahead in BOTH directions (rootMargin, not a one-sided
// threshold), and its size has to be judged against the densest layout it
// runs in, not the sparsest. Home stacks four ~330px card rows inside one
// scroller — Fused Apps, AI Playground, Claude Sessions, Recent files — so an
// 800px margin covered essentially every card in every row on first paint:
// each one read as "near the viewport" and queued a full embed-shell document
// (React boot + its own API calls) for three rows the reader had not
// scrolled to yet. 300px is still roughly a row of lookahead — a preview is
// ready before its card actually crosses the edge — while a row two screens
// down now costs nothing until the reader approaches it.
const NEAR_VIEWPORT_MARGIN = "300px 0px";

// Two observers, not one: `near` (the lookahead margin above) decides whether an
// iframe may exist at all, `visible` (the real viewport) decides which of the
// waiting ones goes first. One observer cannot answer both — rootMargin is
// fixed per observer — and the second is a per-card cost of one more entry in
// the same callback machinery, against a whole iframe document per card.
//
// `near` is STATE because mounting an iframe is a render; `visible` is a REF
// behind a stable getter because ranking an already-queued start is not.
// Crossing the real viewport edge happens for every card on every scroll, and
// the only consumer (a Priority getter the queue reads at admission) reads it
// out of a ref anyway — as state it would re-render every card twice per
// scroll-past for a value no render depends on, and Home's bookmark cards
// would pay that for a tuple slot they never destructure.
//
// The third tuple slot is additive: callers that only gate mounting keep
// destructuring `[ref, near]`.
export function useNearViewport<T extends Element>(): [RefObject<T>, boolean, () => boolean] {
  const ref = useRef<T>(null);
  const [near, setNear] = useState(false);
  const visible = useRef(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const root = el.closest(".apps-page, .files-home, .home-page");
    const observe = (rootMargin: string, set: (v: boolean) => void) => {
      const io = new IntersectionObserver(
        (entries) => set(entries[entries.length - 1].isIntersecting),
        { root, rootMargin },
      );
      io.observe(el);
      return io;
    };
    const nearIo = observe(NEAR_VIEWPORT_MARGIN, setNear);
    const visibleIo = observe("0px", (v) => {
      visible.current = v;
    });
    return () => {
      nearIo.disconnect();
      visibleIo.disconnect();
    };
  }, []);
  const isVisible = useCallback(() => visible.current, []);
  return [ref, near, isVisible];
}

type IdleWindow = Window & {
  requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
  cancelIdleCallback?: (id: number) => void;
};

// `priority` true is a real hover gesture — it skips the idle wait AND jumps
// the queue. A GETTER (see Priority) only ranks the task among those already
// waiting; it must be STABLE across renders, since this effect re-running
// restarts the iframe. Background previews wait for an idle turn so their
// iframe navigation cannot compete with the page's first React paint.
export function usePreviewStart(enabled = true, priority: Priority = false): {
  started: boolean;
  settled: () => void;
} {
  const [started, setStarted] = useState(false);
  const releaseRef = useRef<(() => void) | null>(null);
  const timeoutRef = useRef<number | null>(null);

  const settled = useCallback(() => {
    if (timeoutRef.current !== null) window.clearTimeout(timeoutRef.current);
    timeoutRef.current = null;
    releaseRef.current?.();
    releaseRef.current = null;
  }, []);

  useEffect(() => {
    setStarted(false);
    if (!enabled) return;

    let alive = true;
    let cancelRequest: (() => void) | null = null;
    let fallbackTimer: number | null = null;
    let idleId: number | null = null;
    const idleWindow = window as IdleWindow;

    const enqueue = () => {
      if (!alive) return;
      cancelRequest = previewStarts.request((release) => {
        if (!alive) {
          release();
          return;
        }
        releaseRef.current = release;
        timeoutRef.current = window.setTimeout(settled, START_TIMEOUT_MS);
        setStarted(true);
      }, priority);
    };

    // `=== true` deliberately, not truthiness: a getter is always truthy, and
    // an on-screen card at first mount is exactly the decorative iframe work
    // the idle wait exists to put after the page's own paint. It still gets
    // admitted first — the queue reads the getter when a slot frees.
    if (priority === true) {
      enqueue();
    } else if (idleWindow.requestIdleCallback) {
      idleId = idleWindow.requestIdleCallback(enqueue, { timeout: 500 });
    } else {
      // One short turn is enough to put the shell's first paint and data-fetch
      // effects ahead of decorative iframe work on browsers without rIC.
      fallbackTimer = window.setTimeout(enqueue, 50);
    }

    return () => {
      alive = false;
      if (idleId !== null) idleWindow.cancelIdleCallback?.(idleId);
      if (fallbackTimer !== null) window.clearTimeout(fallbackTimer);
      if (timeoutRef.current !== null) window.clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
      cancelRequest?.();
      releaseRef.current = null;
    };
  }, [enabled, priority, settled]);

  return { started, settled };
}

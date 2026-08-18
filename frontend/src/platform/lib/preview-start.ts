// Shared startup budget for display-only card previews.
//
// A Home row can mount several iframes at once. Each one is a real document:
// even a cached shell still pays JS parse/execute, React startup and its own
// API calls. Let cards paint first, then admit only a small number of preview
// navigations at a time. A slot is held until the iframe loads, errors, times
// out, or unmounts; after that the document may stay mounted without blocking
// the next preview from starting.
import { useCallback, useEffect, useRef, useState } from "react";

type Start = (release: () => void) => void;

export interface PreviewStartQueue {
  request(start: Start, priority?: boolean): () => void;
  active(): number;
  pending(): number;
}

export function createPreviewStartQueue(limit: number): PreviewStartQueue {
  if (!Number.isInteger(limit) || limit < 1) throw new Error("preview start limit must be positive");

  type Task = { start: Start; started: boolean; cancelled: boolean; release?: () => void };
  const waiting: Task[] = [];
  let running = 0;

  const drain = () => {
    while (running < limit && waiting.length) {
      const task = waiting.shift()!;
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
      const task: Task = { start, started: false, cancelled: false };
      if (priority) waiting.unshift(task);
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

type IdleWindow = Window & {
  requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
  cancelIdleCallback?: (id: number) => void;
};

// `priority` is for a real hover gesture. Background previews wait for an idle
// turn so their iframe navigation cannot compete with Home's first React paint.
export function usePreviewStart(enabled = true, priority = false): {
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

    if (priority) {
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

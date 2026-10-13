// Polling that sits out a hidden page: `fn` runs now, then every `ms` while the
// document is visible; becoming visible again runs it at once. A run never
// overlaps the previous one. Changing `ms` takes effect on the next tick.
import { useCallback, useEffect, useRef } from "react";

export function usePoll(fn: () => Promise<unknown> | void, ms: number, enabled = true) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const msRef = useRef(ms);
  msRef.current = ms;
  const kick = useRef<() => void>(() => {});

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let running = false;
    let again = false; // kicked mid-run: run once more right after
    const tick = async () => {
      timer = null;
      if (!alive) return;
      if (running) {
        again = true;
        return;
      }
      if (document.visibilityState !== "visible") return; // resumed by the listener
      running = true;
      try {
        await fnRef.current();
      } catch {
        /* the caller shows its own errors */
      } finally {
        running = false;
      }
      const wait = again ? 0 : msRef.current;
      again = false;
      if (alive && timer === null) timer = setTimeout(tick, wait);
    };
    kick.current = () => {
      if (timer) clearTimeout(timer);
      timer = null;
      void tick();
    };
    const onVis = () => document.visibilityState === "visible" && kick.current();
    document.addEventListener("visibilitychange", onVis);
    void tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [enabled]);

  /** Poll now (after a send), not at the next tick. Stable across renders. */
  return useCallback(() => kick.current(), []);
}

/** The visual viewport's height (the part not under the keyboard), as a CSS
 *  variable on <html>: --vvh. The chat's frame is sized by it so the composer
 *  rides on top of the keyboard. */
export function useVisualViewportVar() {
  useEffect(() => {
    const vv = window.visualViewport;
    const root = document.documentElement;
    const set = () => {
      const h = vv ? vv.height : window.innerHeight;
      root.style.setProperty("--vvh", `${Math.round(h)}px`);
      root.style.setProperty("--vvtop", `${Math.round(vv ? vv.offsetTop : 0)}px`);
      // The keyboard is up: the composer drops its home-indicator padding.
      if (window.innerHeight - h > 120) root.setAttribute("data-kb", "");
      else root.removeAttribute("data-kb");
    };
    set();
    vv?.addEventListener("resize", set);
    vv?.addEventListener("scroll", set);
    window.addEventListener("resize", set);
    return () => {
      vv?.removeEventListener("resize", set);
      vv?.removeEventListener("scroll", set);
      window.removeEventListener("resize", set);
      root.style.removeProperty("--vvh");
      root.style.removeProperty("--vvtop");
      root.removeAttribute("data-kb");
    };
  }, []);
}

export function readSeen(key: string): number | null {
  try {
    const v = localStorage.getItem(key);
    if (v == null) return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  } catch {
    return null;
  }
}
export function writeSeen(key: string, seq: number) {
  try {
    localStorage.setItem(key, String(seq));
  } catch {
    /* private mode */
  }
}

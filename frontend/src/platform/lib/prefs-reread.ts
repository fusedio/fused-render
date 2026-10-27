// Re-reading the prefs when a window comes back, for the one-fetch flag
// modules (contrib-flags.ts, share-app-flag.ts).
//
// Each of those reads `/api/prefs` once and then only moves when THIS page's
// Preferences publishes a new value — right within one window, stale in every
// other: toggle a switch in one window and a second window (another native
// window, another tab) keeps the answer it loaded with. So when a window comes
// back into use (`focus`, or `visibilitychange` to visible — a native window
// switch fires the first, a tab switch the second, often both), ONE fresh GET
// is made and handed to every registered module, which applies it the way its
// own first read would.
//
// ONE GET PER RETURN, NOT ONE PER MODULE: the read is single-flight (a return
// while one is in flight joins it) and the shared in-flight GET is dropped
// first (api.ts `dropSharedPrefsRead`), so the answer is fresh rather than a
// join onto some older read. A module's own generation counter still decides
// whether the answer may land — a publish from a PUT made meanwhile wins.
// A FAILED read changes nothing: a refused GET after a laptop wake must not
// flip a live switch (same rule as apps/claude/feature-flag.ts rereadFlags).
import { dropSharedPrefsRead, getPrefsShared, type Prefs } from "@platform/lib/api";

type Handler = (read: Promise<Prefs>) => void;

const handlers = new Set<Handler>();
let inFlight: Promise<Prefs> | null = null;
let installed = false;

function reread() {
  if (handlers.size === 0) return;
  if (!inFlight) {
    dropSharedPrefsRead();
    const p = getPrefsShared();
    inFlight = p;
    const clear = () => {
      if (inFlight === p) inFlight = null;
    };
    p.then(clear, clear);
  }
  for (const handler of handlers) handler(inFlight);
}

function install() {
  if (installed || typeof window === "undefined") return;
  installed = true;
  window.addEventListener("focus", reread);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") reread();
  });
}

/** Register a module for every re-read. The handler gets the shared read and
 *  applies its result under its own generation guard; it must swallow a
 *  rejection (a failed read is not an answer). */
export function onPrefsReread(handler: Handler): void {
  handlers.add(handler);
  install();
}

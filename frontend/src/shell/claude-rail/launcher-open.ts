// THE SEARCH DOOR'S OPEN STATE (owner, 2026-11-09: "only show active state
// when the window is open … for +, Tasks, Bots and Search as well"). The
// launcher is a native panel, not a card this page draws, so the rail learns
// it stands two ways: optimistically from its own press, then from the app
// (`GET /api/launcher/shown`, polled while the door believes it open). It
// lets go the moment the page is pressed (the panel closes on any click) or
// the window gets its focus back after losing it to the panel. An app with
// no `shown` route (an older one) answers by the press and those two alone.
import { useCallback, useEffect, useState } from "react";
import { getLauncherShown } from "@platform/lib/api";

const POLL_MS = 400;
/** The panel is shown on the main thread a beat after the POST answers: a
 *  poll saying "not shown" inside this grace is not yet an answer. */
const GRACE_MS = 1500;

export function useLauncherOpen(): { open: boolean; opened: () => void; failed: () => void } {
  const [open, setOpen] = useState(false);
  const opened = useCallback(() => setOpen(true), []);
  const failed = useCallback(() => setOpen(false), []);
  useEffect(() => {
    if (!open) return;
    const since = Date.now();
    let seen = false;
    /** No `shown` route (an older app) or no answer: `seen` can never turn
     *  true, so a press is the close again. */
    let blind = false;
    let blurred = false;
    let stopped = false;
    let timer: number | null = null;
    const close = () => {
      if (!stopped) setOpen(false);
    };
    // A press before the panel has shown is not "a click closing it": the
    // show lands a beat after the POST, so only a seen panel yields to it.
    const onPointerDown = () => {
      if (seen || blind) close();
    };
    const onBlur = () => {
      blurred = true;
    };
    const onFocus = () => {
      if (blurred) close();
    };
    const poll = () => {
      timer = null;
      getLauncherShown()
        .then(({ shown }) => {
          if (stopped) return;
          if (shown) seen = true;
          else if (seen || Date.now() - since > GRACE_MS) return close();
          timer = window.setTimeout(poll, POLL_MS);
        })
        // No route (an older app) or no answer: the press and focus rules
        // alone decide.
        .catch(() => {
          blind = true;
        });
    };
    // The press that opened it is over (pointerdown came before the click),
    // so the next one is a press elsewhere.
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("blur", onBlur);
    window.addEventListener("focus", onFocus);
    timer = window.setTimeout(poll, POLL_MS);
    return () => {
      stopped = true;
      if (timer !== null) window.clearTimeout(timer);
      document.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("focus", onFocus);
    };
  }, [open]);
  return { open, opened, failed };
}

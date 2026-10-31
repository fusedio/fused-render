// Whether the collapsed Claude rail on a canvas page shows a chat as a live
// floating card (RailPeek.tsx) — the `canvas_rail_peek_enabled` preference
// (shell/prefs.py), default OFF — and what opens it: a press on a ring or a
// hover (`canvas_rail_peek_trigger`, default "click").
//
// The shape of `./feature-flag.ts` (the Canvases switch itself): one shared
// prefs GET, a generation guard so a publish beats a slower in-flight read,
// and `null` meaning "not asked yet", which renders as off — the flag's own
// default, so a rail never grows a card the reader turned off because a GET
// was still out.
import { useEffect, useState } from "react";
import { dropSharedPrefsRead, getPrefsShared, type CanvasRailPeekTrigger, type Prefs } from "@platform/lib/api";

interface Answer {
  enabled: boolean;
  trigger: CanvasRailPeekTrigger;
}

const OFF: Answer = { enabled: false, trigger: "click" };

let answer: Answer | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: Answer) => void>();

/** The prefs payload's sub-object, read the server's way: only `true` is on,
 *  only "hover" is hover (an absent field — an older server — is "click"). */
function fromPrefs(p: Prefs["canvas_rail_peek"]): Answer {
  return { enabled: p?.enabled === true, trigger: p?.trigger === "hover" ? "hover" : "click" };
}

function set(next: Answer) {
  if (answer && answer.enabled === next.enabled && answer.trigger === next.trigger) return;
  answer = next;
  for (const listener of listeners) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefsShared()
    .then((p) => {
      if (generation === departed) set(fromPrefs(p.canvas_rail_peek));
    })
    .catch(() => {
      // Not an answer: stay off (the default) and let the next mount ask again.
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** A known-fresh answer — the prefs payload a Settings control's PUT
 *  returned — so a canvas page in this tab follows the switch at once. */
export function publishCanvasRailPeek(next: Prefs["canvas_rail_peek"]) {
  generation += 1;
  reading = Promise.resolve();
  set(fromPrefs(next));
}

function useAnswer(): Answer {
  const [current, setCurrent] = useState<Answer>(answer ?? OFF);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(answer ?? OFF);
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** Subscribe. The first reader triggers the one read; later ones reuse it. */
export function useCanvasRailPeek(): boolean {
  return useAnswer().enabled;
}

/** What opens the card: "click" until the prefs say "hover". */
export function useCanvasRailPeekTrigger(): CanvasRailPeekTrigger {
  return useAnswer().trigger;
}

/** Test-only: forget the cached answer so a suite starts from "not asked".
 *  Drops the SHARED prefs read too (api.ts): in one bun process another
 *  flag module's in-flight GET would otherwise answer for this one. */
export function resetCanvasRailPeekFlagForTests() {
  dropSharedPrefsRead();
  reading = null;
  generation += 1;
  answer = null;
  for (const listener of listeners) listener(OFF);
}

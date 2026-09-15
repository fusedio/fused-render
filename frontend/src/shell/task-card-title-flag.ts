// What a task CARD is titled by — the task's own title, or the newest message
// in its conversation (design.md §A, Option 1: the id leads the head row and
// the title row shows the last thing said, by the reader or by Claude). The
// `task_card_last_message` pref (fused_render/shell/prefs.py), experimental and
// default OFF.
//
// A CLONE OF `task-peek-flag.ts`, deliberately down to the shape: one shared
// GET, a generation guard so a publish beats a slower in-flight read, and
// `null` meaning "not asked yet". Three flags that gate a whole behaviour
// should not have three different idioms for the same three states.
//
// WHY THE TRI-STATE MATTERS HERE: OFF must be the card the wall has always
// drawn, and that includes the frames before the answer lands. A premature
// `true` would swap every card's title line for a message and then swap it
// back a moment later — a wall of text that changes under the reader between
// two paints, which is worse than the wall being briefly right. So every
// consumer takes `=== true`, and "not asked yet" is honestly "no".
//
// The DECISION the flag gates is here too (`cardTitleLine`), and only that one:
// which single line the title row shows. It is pure, so the rule can be tested
// without mounting a wall of live chats.
import { useEffect, useState } from "react";
import { getPrefs } from "@platform/lib/api";
import type { Task } from "@platform/lib/api";
import { firstLine } from "./tasks-lib";

let enabled: boolean | null = null;
let reading: Promise<void> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean | null) => void>();

function set(next: boolean | null) {
  if (enabled === next) return;
  enabled = next;
  for (const listener of listeners) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefs()
    // One bounded retry, then a real answer either way — a prefs GET that fails
    // is usually a single dropped request (a reload racing the server's start).
    .catch(() => getPrefs())
    .then((p) => {
      if (generation !== departed) return;
      // `=== true` and nothing looser: a server that predates the switch sends
      // no `task_cards` at all, and that reads as off — which is both the
      // pref's own default and the card the wall has always drawn.
      set(p.task_cards?.last_message === true);
    })
    .catch(() => {
      // STILL NO ANSWER — so `false`, which IS the shipping card. `reading` is
      // cleared so a later mount (or a publish) can ask again.
      if (generation === departed) set(false);
      reading = null;
    })
    .then(() => {});
  return reading;
}

/** Hand over a known-fresh answer (the prefs payload a PUT returned), so the
 *  Preferences toggle takes effect without a reload. */
export function publishTaskCardTitleMode(next: boolean) {
  generation += 1;
  reading = Promise.resolve();
  set(next);
}

/** Current answer without subscribing; `null` until the first read lands. */
export function taskCardTitleModeNow(): boolean | null {
  return enabled;
}

/** Test-only: forget the cached answer so a suite starts from "not asked".
 *  Notifies, like every other write. */
export function resetTaskCardTitleFlagForTests() {
  reading = null;
  generation += 1;
  set(null);
}

/** Subscribe, tri-state: `null` until the one prefs read lands. */
export function useTaskCardTitleFlag(): boolean | null {
  const [current, setCurrent] = useState<boolean | null>(taskCardTitleModeNow);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(taskCardTitleModeNow());
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** The same subscription, flattened: is a card titled by its last message RIGHT
 *  NOW. What every consumer takes — "not asked yet" is honestly "no" here. */
export function useTaskCardTitleMode(): boolean {
  return useTaskCardTitleFlag() === true;
}

/** What the card's title row draws, and whether it is a message. */
export interface CardTitle {
  /** The one line to print. "" when the task has neither a message nor a title,
   *  which is the card's own cue to print its "(untitled)" — one place decides
   *  those words, and it is not this one. */
  text: string;
  /** It is the conversation's newest message rather than the task's title.
   *  The caption follows it (the message, not the title, is what the one-line
   *  clamp is hiding); nothing else on the row changes with it. */
  said: boolean;
}

/** THE ONE LINE THE CARD'S TITLE ROW SHOWS.
 *
 *  With the experiment off — or on, but with nothing said in this conversation
 *  yet (a task that has never run, a server that predates the field) — it is
 *  the task's own title, exactly as the card has always drawn it. The fallback
 *  is not a nicety: a wall where some cards carried a message and others were
 *  simply blank would read as a wall of broken cards. */
export function cardTitleLine(task: Task, on: boolean): CardTitle {
  const said = on ? firstLine(task.last_message?.text ?? "") : "";
  return said ? { text: said, said: true } : { text: firstLine(task.title ?? ""), said: false };
}

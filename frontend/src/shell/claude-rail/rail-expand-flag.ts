// Whether the rail wears its expand / collapse button (ClaudeRail `expandOn`;
// platform/lib/railstate holds the expanded state and width) — the
// `rail_expand_enabled` preference (shell/prefs.py), default OFF. The button
// stands above the rail's nav doors.
//
// The shape of `./rail-mode-doors-flag.ts`: the shared prefs GET, a generation
// guard so a publish beats a slower in-flight read, `null` meaning "not asked
// yet" (rendered as off, the flag's own default), and the last answer
// remembered across a reload so the button does not pop in a moment after the
// rail.
import { useEffect, useState } from "react";
import { dropSharedPrefsRead, getPrefsShared, onPrefsWrittenElsewhere, type Prefs } from "@platform/lib/api";
import { onPrefsReread } from "@platform/lib/prefs-reread";

const MEMO_KEY = "fused-render:rail-expand";
function memoRead(): boolean | null {
  try {
    const v = localStorage.getItem(MEMO_KEY);
    return v === "1" ? true : v === "0" ? false : null;
  } catch {
    return null;
  }
}
function memoWrite(v: boolean): void {
  try {
    localStorage.setItem(MEMO_KEY, v ? "1" : "0");
  } catch {
    /* no storage: nothing remembered */
  }
}

let answer: boolean | null = memoRead();
let reading: Promise<void> | null = null;
/** A failed read is retried while a reader is mounted. */
const RETRY_MS = 15_000;
let retry: ReturnType<typeof setTimeout> | null = null;
let generation = 0;
const listeners = new Set<(v: boolean | null) => void>();

/** The prefs payload's sub-object, read the server's way: only `true` is on. */
function fromPrefs(p: Prefs["rail_expand"]): boolean {
  return p?.enabled === true;
}

function set(next: boolean) {
  memoWrite(next);
  if (answer === next) return;
  answer = next;
  for (const listener of [...listeners]) listener(next);
}

function read(): Promise<void> {
  if (reading) return reading;
  const departed = generation;
  reading = getPrefsShared()
    .then((p) => {
      if (generation === departed) set(fromPrefs(p.rail_expand));
    })
    .catch(() => {
      reading = null;
      if (listeners.size > 0 && !retry) {
        retry = setTimeout(() => {
          retry = null;
          if (listeners.size > 0) void read();
        }, RETRY_MS);
      }
    })
    .then(() => {});
  return reading;
}

/** A known-fresh answer (the prefs payload a Settings control's PUT returned). */
export function publishRailExpand(next: Prefs["rail_expand"]) {
  generation += 1;
  reading = Promise.resolve();
  set(fromPrefs(next));
}

// Another tab's PUT: the prefs channel carries every written payload.
onPrefsWrittenElsewhere((p) => {
  if (p.rail_expand !== undefined) publishRailExpand(p.rail_expand);
});

// ANOTHER WINDOW FLIPPED THE SWITCH AND THIS ONE CAME BACK (platform/lib
// prefs-reread; CodeRabbit on #2396): with no BroadcastChannel the write above
// never arrives, and a window would keep the doors it loaded with. The shared
// re-read on focus brings the answer here, under the same generation guard; a
// failed read changes nothing. Followed from the first hook on
// (rail-free-pins-flag's way), not at import.
let following = false;
function onReread(p: Promise<Prefs>): void {
  const departed = generation;
  p.then((prefs) => {
    if (generation === departed) set(fromPrefs(prefs.rail_expand));
  }).catch(() => {});
}

/** The switch as it is KNOWN: on, off, or `null` — not answered yet (no
 *  remembered answer and the read still out). The first reader triggers the
 *  one read; later ones reuse it. ClaudeRail reads `null` as "do not judge a
 *  saved mode pin yet". */
export function useRailExpandAnswer(): boolean | null {
  const [current, setCurrent] = useState<boolean | null>(answer);
  useEffect(() => {
    listeners.add(setCurrent);
    setCurrent(answer);
    if (!following) {
      following = true;
      onPrefsReread(onReread);
    }
    void read();
    return () => {
      listeners.delete(setCurrent);
    };
  }, []);
  return current;
}

/** The switch is on (not answered yet reads as off, the flag's default). */
export function useRailExpand(): boolean {
  return useRailExpandAnswer() === true;
}

/** Test-only: forget the cached answer so a suite starts from "not asked". */
export function resetRailExpandFlagForTests() {
  dropSharedPrefsRead();
  reading = null;
  if (retry) clearTimeout(retry);
  retry = null;
  generation += 1;
  answer = null;
  try {
    localStorage.removeItem(MEMO_KEY);
  } catch {
    /* no storage */
  }
  for (const listener of [...listeners]) listener(null);
}

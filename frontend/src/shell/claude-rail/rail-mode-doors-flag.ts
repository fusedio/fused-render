// Whether a file page's rail shows the file's other modes as doors above Git
// (ClaudeRail `fileModes`, mode-peek-store, ModePeek) — the
// `rail_mode_doors_enabled` preference (shell/prefs.py), default OFF.
//
// The shared prefs GET, a generation
// guard so a publish beats a slower in-flight read, `null` meaning "not asked
// yet" (rendered as off, the flag's own default), and the last answer
// remembered across a reload so the doors do not pop in a moment after the
// rail.
import { useEffect, useState } from "react";
import { dropSharedPrefsRead, getPrefsShared, onPrefsWrittenElsewhere, type Prefs } from "@platform/lib/api";
import { onPrefsReread } from "@platform/lib/prefs-reread";

const MEMO_KEY = "fused-render:rail-mode-doors";
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
function fromPrefs(p: Prefs["rail_mode_doors"]): boolean {
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
      if (generation === departed) set(fromPrefs(p.rail_mode_doors));
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
export function publishRailModeDoors(next: Prefs["rail_mode_doors"]) {
  generation += 1;
  reading = Promise.resolve();
  set(fromPrefs(next));
}

// Another tab's PUT: the prefs channel carries every written payload.
onPrefsWrittenElsewhere((p) => {
  if (p.rail_mode_doors !== undefined) publishRailModeDoors(p.rail_mode_doors);
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
    if (generation === departed) set(fromPrefs(prefs.rail_mode_doors));
  }).catch(() => {});
}

/** The switch as it is KNOWN: on, off, or `null` — not answered yet (no
 *  remembered answer and the read still out). The first reader triggers the
 *  one read; later ones reuse it. ClaudeRail reads `null` as "do not judge a
 *  saved mode pin yet". */
export function useRailModeDoorsAnswer(): boolean | null {
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
export function useRailModeDoors(): boolean {
  return useRailModeDoorsAnswer() === true;
}

/** Test-only: forget the cached answer so a suite starts from "not asked". */
export function resetRailModeDoorsFlagForTests() {
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

// A template mode BORROWED FROM A DIRECTORY by a surface whose subject is not
// that directory.
//
// `git` is what needs it. A working tree belongs to the FOLDER — you stash a
// tree, not a file — so the registry binds `git` to the universal "/" directory
// key alone and its gate refuses anything that is not a directory
// (templates/git/condition.py writes both halves down). A FILE's own
// `stat.templates` therefore never mentions git, and yet "what has changed in
// here" is exactly as useful while reading one file as while browsing its folder.
// Both companion sidebars borrow it: the file preview's from the file's PARENT
// (apps/explorer/Preview.tsx), the listing pane's from the folder it is BROWSING
// (apps/explorer/Listing.tsx). The listing pane borrows `claude` the same way —
// its chat is the folder view's companion, aimed at whichever row is selected.
//
// Borrowing asks the server the same two questions every other mode surface
// asks, about the directory instead of about the subject:
//
//   GET /api/fs/stat        does this directory offer the mode at all, and which
//                           template folder backs it? (the iframe needs the
//                           path, the switcher needs the icon)
//   GET /api/fs/conditions   does its condition.py gate allow this directory?
//
// NOTHING HERE RELAXES EITHER ANSWER, and that is the point of going through the
// server rather than hard-coding "files in a repo get a git tab". A user registry
// that drops `git` from "/" drops it from these sidebars too, and a gate that
// says false hides it exactly as it would on the folder's own mode menu — the
// verdict policy is lib/mode-visibility's ONE policy, so an entry hides only on
// an EXPLICIT denial and a failed probe cannot silently empty a switcher.
import { useEffect, useState } from "react";
import { resolveConditions, statPath, type TemplateEntry } from "@platform/lib/api";
import { isModeVisible } from "@platform/lib/mode-visibility";
import { KNOWN_SENTINEL_MODES } from "@apps/explorer/ModeSwitcher";

// What one directory offers, as the two probes resolve it.
interface DirModes {
  templates: TemplateEntry[];
  // Never null here (unlike the surfaces' own `conditions` state): the load
  // below waits for the verdicts before it settles, because a borrowed mode has
  // nothing to render in the meantime. `{}` is a FAILED call, not a denial.
  conditions: Record<string, boolean>;
}

// Per-directory cache, because the callers ask for one answer REPEATEDLY. The
// listing pane is keyed on the previewed row and remounts on every selection
// change; the file preview remounts on every file opened. The directory under
// both of them stays put, so without a cache arrow-keying down a folder would
// re-stat and re-gate that folder once per keystroke — and the git gate forks a
// `git rev-parse` each time.
//
// TTL'd rather than permanent: the answer is not immutable (a `git init`, a
// registry edit) and a shell tab can live for days, so a stale "no repository
// here" must not outlive the session. Long enough to cover a walk through a
// folder, short enough that the fix for a wrong answer is to wait a moment.
const TTL_MS = 30_000;

const cache = new Map<string, { at: number; modes: Promise<DirModes> }>();

function loadDirModes(dir: string): Promise<DirModes> {
  const hit = cache.get(dir);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.modes;
  const modes = statPath(dir).then(async (st) => {
    // The same defensive sentinel filter every mode surface applies (SPEC
    // PT-12): an entry with path===null that isn't a known sentinel would build
    // a `path=null` render URL.
    const templates = st.templates.filter(
      (e) => e.path !== null || KNOWN_SENTINEL_MODES.has(e.mode)
    );
    if (!templates.some((e) => e.conditional)) return { templates, conditions: {} };
    // A failed gate probe resolves to NO verdicts, which lib/mode-visibility
    // reads as "show the entry" — never as a denial (see its header).
    const conditions = await resolveConditions(dir).then(
      (r) => r.conditions,
      () => ({})
    );
    return { templates, conditions };
  });
  // A REJECTED load is not cached: a transient stat failure would otherwise hide
  // the borrowed mode for the whole TTL. Guarded on identity so a newer entry
  // for the same directory is never evicted by an older failure.
  modes.catch(() => {
    if (cache.get(dir)?.modes === modes) cache.delete(dir);
  });
  cache.set(dir, { at: Date.now(), modes });
  return modes;
}

export interface DirMode {
  // The directory's entry for the mode: a PLACEHOLDER while `pending` (the mode
  // name only — the real template path and icon arrive with the stat), and null
  // when the directory does not offer the mode at all — an unknown mode, an
  // explicit gate denial, or a probe that failed outright.
  entry: TemplateEntry | null;
  // The probe is still in flight.
  //
  // Callers list the placeholder as a PENDING switcher entry rather than waiting
  // to add a real one, which is CT-12's posture for a gated mode and holds here
  // for a second reason as well: the mode's URL param is read at mount, so a
  // `?_side=git` deep link would be resolved against a list that does not yet
  // contain git and quietly rewritten away before the answer landed.
  pending: boolean;
}

// Shared, so a caller that re-renders without changing directory does not get a
// fresh object and a pointless commit.
const ABSENT: DirMode = { entry: null, pending: false };

function placeholderFor(mode: string): DirMode {
  return { entry: { mode, path: null, icon: null }, pending: true };
}

// `dir === null` switches the whole thing off and makes no request — how callers
// skip the probe on the surfaces that cannot show a borrowed mode at all (an
// embedded listing, a panel pane, the app builder).
export function useDirMode(dir: string | null, mode: string): DirMode {
  const [state, setState] = useState<DirMode>(() =>
    dir === null ? ABSENT : placeholderFor(mode)
  );
  useEffect(() => {
    if (dir === null) {
      setState(ABSENT);
      return;
    }
    let alive = true;
    setState(placeholderFor(mode));
    loadDirModes(dir).then(
      (r) => {
        if (!alive) return;
        const entry = r.templates.find((e) => e.mode === mode) ?? null;
        setState(
          entry && isModeVisible(entry, r.conditions) ? { entry, pending: false } : ABSENT
        );
      },
      () => {
        if (alive) setState(ABSENT);
      }
    );
    return () => {
      alive = false;
    };
  }, [dir, mode]);
  return state;
}

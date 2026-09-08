// The Claude Code setup MACHINE — snapshot, the install / update / doctor /
// sign-in / link-path actions, and the polls that follow each — as one hook.
//
// Lifted out of ClaudeHealthStrip so the first-run wizard's "Claude Code" step
// (shell/onboarding) can drive exactly the same endpoints with exactly the
// same recovery rules, instead of forking 250 lines and drifting. The strip
// keeps its render; this keeps the rules. Every comment below that argues a
// rule came over verbatim from the strip, because the rules did.
import { useCallback, useEffect, useRef, useState } from "react";

import {
  cancelClaudeLogin,
  getClaudeHealth,
  getClaudeInstall,
  getClaudeLogin,
  linkClaudePath,
  refreshClaudeHealth,
  runClaudeDoctor,
  startClaudeInstall,
  startClaudeLogin,
  type ClaudeDoctor,
  type ClaudeHealth,
  type ClaudeInstallStatus,
  type ClaudeLoginStatus,
} from "./api";
import type { ClaudeIssue } from "./claude-health";

// The last snapshot seen, so walking between Home and /apps — which both render
// the strip — starts from what we already know instead of flashing an empty
// frame.
//
// A SEED, NOT A SHORT-CIRCUIT. It used to also skip the fetch, which is what
// made the strip unable to notice its own problem being fixed: the only thing
// that ever refreshed it was the button, so a user who signed in and came back
// still faced a card telling them to sign in. The server holds the real cache
// (on disk, and age-bounded), so re-asking on every mount is a small GET —
// there was never anything to save here.
let cached: ClaudeHealth | null = null;

//: How long after a check a window-focus event is taken as "they may have gone
//: and fixed it". Focus/blur flap in bursts (a click through the window, a
//: notification, an OS overlay), and each forced re-check is real subprocess
//: work, so near-simultaneous ones collapse into the first.
export const FOCUS_RECHECK_MS = 3000;

/** How often to ask the server how the install is getting on. The record is an
    in-memory read on the server side, so this is cheap; the interval is set by
    what reads as live rather than by cost. */
const INSTALL_POLL_MS = 1200;

/** How often to ask whether the browser sign-in has finished. Slower than the
    install poll on purpose: nothing changes here until a person has finished
    with a browser window, so a faster tick would only spend requests watching
    someone read a consent screen. */
const LOGIN_POLL_MS = 2000;

export interface ClaudeSetup {
  health: ClaudeHealth | null;
  /** A snapshot we have not fetched yet is NOT the same as one that says
      everything is fine — render nothing until this is true. */
  loaded: boolean;
  /** A forced re-check is in flight. */
  busy: boolean;
  install: ClaudeInstallStatus | null;
  login: ClaudeLoginStatus | null;
  doctor: ClaudeDoctor | null;
  /** A REFUSAL, shown verbatim (the 409 from an update that would no-op names
      the command that would actually work). */
  actionError: string | null;
  acting: boolean;
  /** The PATH line landed — the sentence the user still needs about it. */
  linkedNote: string | null;
  /** Re-probe. `force` bypasses the server's age-bounded cache. */
  load: (force: boolean) => void;
  act: (issue: ClaudeIssue) => void;
  cancelLogin: () => void;
}

/**
 * @param watching Whether a claim is on screen that a window-focus should
 *   re-check. COMING BACK TO THE WINDOW IS THE SIGNAL: every fix this asks for
 *   happens somewhere else — a terminal, an installer — so the moment the user
 *   returns is exactly when "is it still true?" should be re-asked. Only while
 *   something IS showing: a healthy machine must not spawn probes for tabbing
 *   around.
 */
export function useClaudeSetup(watching: boolean): ClaudeSetup {
  const [health, setHealth] = useState<ClaudeHealth | null>(cached);
  const [loaded, setLoaded] = useState(cached !== null);
  const [busy, setBusy] = useState(false);
  const lastCheck = useRef(0);
  // The install/update record, polled only while one is running. Null means
  // nothing has been started from this page.
  const [install, setInstall] = useState<ClaudeInstallStatus | null>(null);
  // `claude doctor`'s report, once it has been asked for. Seeded from the
  // snapshot, because the server already ran one whenever it found something
  // worth explaining.
  const [doctor, setDoctor] = useState<ClaudeDoctor | null>(null);
  // The browser sign-in record, polled only while one is open. Its own state
  // because it is its own endpoint for its own reason: this one waits on a
  // person and can be called off.
  const [login, setLogin] = useState<ClaudeLoginStatus | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);
  // Held HERE rather than refreshing the snapshot away, because the row's last
  // job is a sentence the user still needs: only NEW terminal windows read the
  // rc file. The server's cache is refreshed, so the next natural re-check
  // (focus, navigation) retires the row.
  const [linkedNote, setLinkedNote] = useState<string | null>(null);

  const load = useCallback((force: boolean) => {
    lastCheck.current = Date.now();
    if (force) setBusy(true);
    (force ? refreshClaudeHealth() : getClaudeHealth()).then(
      (h) => {
        cached = h;
        setHealth(h);
        setLoaded(true);
        setBusy(false);
      },
      () => {
        // A FAILED PROBE IS NOT A FINDING. If /api/claude/health itself cannot
        // be reached then the server is what is wrong, and the app has louder
        // ways of saying so. Keep whatever we last knew rather than inventing
        // a worse answer.
        setLoaded(true);
        setBusy(false);
      },
    );
  }, []);

  useEffect(() => {
    load(false);
  }, [load]);

  useEffect(() => {
    if (health?.doctor) setDoctor(health.doctor);
  }, [health]);

  // RECOVER AN INSTALL THAT IS ALREADY RUNNING. The record lives on the server
  // and outlives any component. ONLY A RUNNING RECORD IS ADOPTED: a finished
  // one from earlier belongs to a problem that is already gone.
  useEffect(() => {
    getClaudeInstall().then(
      (rec) => {
        if (rec.state === "running") setInstall(rec);
      },
      () => {
        /* A recovery, not a prerequisite. */
      },
    );
  }, []);

  // The same recovery for a sign-in: the browser window is in front of the
  // user RIGHT NOW, and a remount that showed "Sign in" again would invite a
  // second press that can only earn a 409.
  useEffect(() => {
    getClaudeLogin().then(
      (rec) => {
        if (rec.in_flight) setLogin(rec);
      },
      () => {
        /* A recovery, not a prerequisite. */
      },
    );
  }, []);

  // POLL ONLY WHILE SOMETHING IS RUNNING.
  useEffect(() => {
    if (install?.state !== "running") return;
    let alive = true;
    const timer = window.setInterval(() => {
      getClaudeInstall().then(
        (next) => {
          if (!alive) return;
          setInstall(next);
          // A finished install changed the machine. Re-probe so the UI can
          // retire the claim the user just fixed.
          if (next.state === "done") load(true);
        },
        () => {
          /* A failed poll is not a failed install — keep what we last knew. */
        },
      );
    }, INSTALL_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [install?.state, load]);

  // POLL ONLY WHILE A SIGN-IN IS OPEN. The CLI writes a credential this app
  // never sees, so the only authority on whether it worked is `claude auth
  // status` — re-probe when the record stops being in flight.
  useEffect(() => {
    if (!login?.in_flight) return;
    let alive = true;
    const timer = window.setInterval(() => {
      getClaudeLogin().then(
        (next) => {
          if (!alive) return;
          setLogin(next);
          if (!next.in_flight) load(true);
        },
        () => {
          /* A failed poll is not a failed sign-in — keep what we last knew. */
        },
      );
    }, LOGIN_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [login?.in_flight, load]);

  const act = useCallback((issue: ClaudeIssue) => {
    if (!issue.action) return;
    setActionError(null);
    setActing(true);
    const done = () => setActing(false);
    if (issue.action.kind === "doctor") {
      runClaudeDoctor().then(
        (res) => {
          done();
          if (res.doctor) setDoctor(res.doctor);
          // `ok: false` with no report is itself the finding — two probes have
          // now failed to get a word out of this binary.
          else setActionError(res.error || "The diagnostics did not answer.");
        },
        (e) => {
          done();
          setActionError(String(e?.message || e));
        },
      );
      return;
    }
    if (issue.action.kind === "link-path") {
      linkClaudePath().then(
        (res) => {
          done();
          setLinkedNote(
            `Added to ${res.rc_file ?? "your shell profile"}. Terminals ` +
              "opened from now on will find `claude` — one that is already " +
              "open needs a new tab or window.",
          );
        },
        (e) => {
          done();
          // The server's own refusal, verbatim.
          setActionError(String(e?.message || e));
        },
      );
      return;
    }
    if (issue.action.kind === "login") {
      startClaudeLogin().then(
        (rec) => {
          done();
          setLogin(rec);
        },
        (e) => {
          done();
          // A 409 here means a browser window is already open and waiting.
          setActionError(String(e?.message || e));
        },
      );
      return;
    }
    startClaudeInstall(issue.action.kind).then(
      (rec) => {
        done();
        setInstall(rec);
      },
      (e) => {
        done();
        // A 409 here is the "that update would do nothing, run this instead"
        // refusal.
        setActionError(String(e?.message || e));
      },
    );
  }, []);

  // Calling off a sign-in. The server settles the record before answering, so
  // what comes back is already not in flight and the poll stops on its own.
  const cancelLogin = useCallback(() => {
    setActionError(null);
    cancelClaudeLogin().then(
      (rec) => setLogin(rec),
      () => setLogin(null),
    );
  }, []);

  useEffect(() => {
    if (!watching) return;
    const onFocus = () => {
      if (document.visibilityState === "hidden") return;
      if (Date.now() - lastCheck.current < FOCUS_RECHECK_MS) return;
      load(true);
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, [watching, load]);

  return {
    health,
    loaded,
    busy,
    install,
    login,
    doctor,
    actionError,
    acting,
    linkedNote,
    load,
    act,
    cancelLogin,
  };
}

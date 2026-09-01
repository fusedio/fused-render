// The first-run heads-up: three lines on the front door saying whether Claude
// Code is there, new enough, and signed in — BEFORE a prompt has been spent
// finding out.
//
// **The failure this exists to prevent.** On a fresh install the Home hero
// invites a prompt. The app folder gets created, the session fails, and the
// TroubleCard explains it well — but the user has already typed a brief and
// watched a folder appear before learning that the thing the app is built around
// was never set up. The doctrine: a surface renders only when it works, "so
// it's never a dead link". This is that gate for everything Claude-dependent.
//
// **It is not a wizard and not a gate.** The file explorer is completely useful
// without Claude Code, and nothing here blocks it: the strip is a row above the
// page that says what is wrong and how to fix it, and it disappears the moment
// there is nothing to say. It renders NOTHING in the common case.
//
// The TroubleCard stays exactly as it is. Preflight is an addition, not a
// replacement — a CLI can break between this check and the call.
import { useCallback, useEffect, useRef, useState } from "react";

import {
  cancelClaudeLogin,
  getClaudeHealth,
  getClaudeInstall,
  getClaudeLogin,
  refreshClaudeHealth,
  runClaudeDoctor,
  startClaudeInstall,
  startClaudeLogin,
  type ClaudeDoctor,
  type ClaudeHealth,
  type ClaudeInstallStatus,
  type ClaudeLoginStatus,
} from "@platform/lib/api";
import {
  claudeIssues,
  dismiss as rememberDismissal,
  isDismissed,
  issueHelpUrl,
  type ClaudeIssue,
} from "@platform/lib/claude-health";

// The last snapshot seen, so walking between Home and /apps — which both render
// this — starts from what we already know instead of flashing an empty frame.
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
const FOCUS_RECHECK_MS = 3000;

function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="update-badge-command">
      <code>{command}</code>
      <button
        type="button"
        className="update-badge-copy"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(command);
          } catch {
            // Clipboard denied. The command is on screen either way, and a
            // button stuck on "Copied" would be a lie about what happened.
            return;
          }
          setCopied(true);
          window.setTimeout(() => setCopied(false), 2000);
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

/** How often to ask the server how the install is getting on. The record is an
    in-memory read on the server side, so this is cheap; the interval is set by
    what reads as live rather than by cost. */
const INSTALL_POLL_MS = 1200;

/** How often to ask whether the browser sign-in has finished. Slower than the
    install poll on purpose: nothing changes here until a person has finished
    with a browser window, so a faster tick would only spend requests watching
    someone read a consent screen. */
const LOGIN_POLL_MS = 2000;

/** `claude doctor`'s own words, or the installer's. Rendered verbatim and in a
 *  scroll box rather than summarised: the whole reason to surface either is
 *  that the exact string is what a user can search for and what an issue needs.
 */
function OutputBlock({ label, text }: { label: string; text: string }) {
  if (!text.trim()) return null;
  return (
    <div className="claude-health-output">
      <div className="claude-health-output-label">{label}</div>
      <pre>{text}</pre>
    </div>
  );
}

function DoctorReport({ doctor }: { doctor: ClaudeDoctor }) {
  if (!doctor.warnings.length) {
    return <OutputBlock label="claude doctor" text={doctor.text} />;
  }
  return (
    <div className="claude-health-doctor">
      <div className="claude-health-output-label">
        claude doctor found {doctor.warnings.length}{" "}
        {doctor.warnings.length === 1 ? "problem" : "problems"}
      </div>
      <ul className="claude-health-doctor-list">
        {doctor.warnings.map((w, i) => (
          <li key={i}>
            <span className="claude-health-doctor-problem">{w.problem}</span>
            {/* The CLI's own suggested fix. Shown as a command to copy when it
                reads like one, because "Run claude install to repair the
                installation." is exactly the sentence a user then has to
                retype by hand. */}
            {w.fix && <span className="claude-health-doctor-fix">{w.fix}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function IssueRow({
  issue,
  install,
  login,
  doctor,
  onAct,
  onCancelLogin,
  busy,
  actionError,
}: {
  issue: ClaudeIssue;
  install: ClaudeInstallStatus | null;
  login: ClaudeLoginStatus | null;
  doctor: ClaudeDoctor | null;
  onAct: (issue: ClaudeIssue) => void;
  onCancelLogin: () => void;
  busy: boolean;
  actionError: string | null;
}) {
  // The install record belongs to whichever issue asked for it — a running
  // install is about `missing`, a running update about `outdated` — so a row
  // only shows progress for its OWN action. Without this the update row would
  // narrate an install it did not start. `doctor` never matches, because it is
  // not run through that record at all: it is a single bounded probe that
  // answers inline.
  const mine = Boolean(install && issue.action && install.action === issue.action.kind);
  const running = Boolean(mine && install!.state === "running");
  const failed = Boolean(mine && install!.state === "error");
  const finished = Boolean(mine && install!.state === "done");

  // The sign-in is tracked in its own record, for the reason it has its own
  // endpoints: it waits on a person rather than running to completion, so a
  // "Working…" that cannot be called off would be the app telling the user to
  // wait for something only they can finish.
  const signingIn = Boolean(issue.action?.kind === "login" && login?.in_flight);
  const loginError = issue.action?.kind === "login" ? login?.error ?? null : null;

  return (
    <li className="claude-health-issue">
      <div className="claude-health-issue-title">{issue.title}</div>
      <p className="claude-health-issue-detail">{issue.detail}</p>

      {issue.action && (
        <div className="claude-health-actions">
          <button
            type="button"
            className="claude-health-action"
            onClick={() => onAct(issue)}
            disabled={busy || running || signingIn}
          >
            {running || signingIn
              ? "Working…"
              : finished
                ? "Done"
                : issue.action.label}
          </button>
          {signingIn && (
            <button
              type="button"
              className="claude-health-action claude-health-action-quiet"
              onClick={onCancelLogin}
            >
              Cancel
            </button>
          )}
          {/* What will actually run, before it runs. Piping a remote script
              into a shell on someone's behalf is a thing to disclose, not to
              do quietly behind a friendly label. */}
          {issue.command && (
            <code className="claude-health-action-cmd">{issue.command}</code>
          )}
        </div>
      )}

      {running && (
        <p className="claude-health-progress" role="status">
          {install!.detail || "Working…"}
        </p>
      )}
      {signingIn && (
        <p className="claude-health-progress" role="status">
          Finish signing in with the browser window that just opened.
        </p>
      )}
      {/* The child's own diagnosis. `Login failed: Request failed with status
          code 400` is the loopback exchange rejecting the code, and it is the
          only diagnosis on offer — the server derives this one line and keeps
          the rest of the output in memory. */}
      {loginError && !signingIn && (
        <p className="claude-health-error">{loginError}</p>
      )}
      {failed && (
        <OutputBlock
          label={install!.error || "It didn't work"}
          text={install!.output}
        />
      )}
      {actionError && <p className="claude-health-error">{actionError}</p>}
      {doctor && <DoctorReport doctor={doctor} />}

      {/* Still a command to copy, even where a button exists: a user on a
          locked-down machine, or one who would simply rather run it themselves,
          should not have to press our button to find out what it was. */}
      {issue.command && !issue.action && <CopyCommand command={issue.command} />}
      <a
        className="version-panel-link"
        href={issueHelpUrl(issue)}
        target="_blank"
        rel="noreferrer"
      >
        How to fix this ↗
      </a>
    </li>
  );
}

export function ClaudeHealthStrip() {
  const [health, setHealth] = useState<ClaudeHealth | null>(cached);
  // A snapshot we have not fetched yet is NOT the same as one that says
  // everything is fine, and rendering the strip before it arrives would flash a
  // "can't find Claude Code" on every load of a perfectly healthy machine.
  const [loaded, setLoaded] = useState(cached !== null);
  const [busy, setBusy] = useState(false);
  // Re-render after a dismissal. The dismissal ITSELF lives in lib/claude-health,
  // keyed on which problems were dismissed; a local `closed` flag used to shadow
  // it and was wrong in one direction that matters — dismissing "not signed in"
  // suppressed a LATER, different problem for the rest of the page's life, when
  // the signature check exists precisely so a new problem still gets through.
  const [, redraw] = useState(0);
  const lastCheck = useRef(0);
  // The install/update record, polled only while one is running. Null means
  // nothing has been started from this page.
  const [install, setInstall] = useState<ClaudeInstallStatus | null>(null);
  // `claude doctor`'s report, once it has been asked for. Seeded from the
  // snapshot, because the server already ran one whenever it found something
  // worth explaining — making the user press a button for a report we are
  // already holding would be the same do-our-work-for-us failure the shell
  // adoption exists to avoid.
  const [doctor, setDoctor] = useState<ClaudeDoctor | null>(null);
  // The browser sign-in record, polled only while one is open. Its own state
  // rather than a branch of `install`, because it is its own endpoint for its
  // own reason: this one waits on a person and can be called off.
  const [login, setLogin] = useState<ClaudeLoginStatus | null>(null);
  // A REFUSAL, shown verbatim. The one that matters is the 409 from an update
  // that would no-op: its text names the command that would actually work, and
  // rewording it would throw that away.
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);

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
        // ways of saying so (main.tsx's boot card, the status banner). Claiming
        // Claude Code is missing on the strength of our own failed request would
        // be the app blaming the user's machine for its own fault. Keep whatever
        // we last knew rather than inventing a worse answer.
        setLoaded(true);
        setBusy(false);
      },
    );
  }, []);

  useEffect(() => {
    load(false);
  }, [load]);

  // Seed the doctor report from the snapshot the server already took.
  useEffect(() => {
    if (health?.doctor) setDoctor(health.doctor);
  }, [health]);

  // RECOVER AN INSTALL THAT IS ALREADY RUNNING. The record lives on the server
  // and outlives this component — Home and /apps both render the strip, and the
  // shell tears one down on every navigation. Without this the remounted strip
  // starts from `null`, never polls, shows no progress, and a second press of
  // the button gets a 409 for an install the user cannot see.
  //
  // ONLY A RUNNING RECORD IS ADOPTED. A finished one from earlier in the
  // session belongs to a problem that is already gone, and picking it up would
  // render "Done" on a button whose issue is still on screen.
  useEffect(() => {
    getClaudeInstall().then(
      (rec) => {
        if (rec.state === "running") setInstall(rec);
      },
      () => {
        /* Nothing to recover, or the server did not answer. Either way the
           button still works — this is a recovery, not a prerequisite. */
      },
    );
  }, []);

  // The same recovery for a sign-in, and it matters more: the browser window is
  // in front of the user RIGHT NOW, so a remounted strip that showed "Sign in"
  // again would invite a second press that can only earn a 409 for a window
  // already open. Only an in-flight record is adopted; a finished one belongs to
  // an attempt whose outcome the health snapshot already reflects.
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

  // POLL ONLY WHILE SOMETHING IS RUNNING. An install outlives this component —
  // the user can navigate away and the download manager keeps showing it — so
  // the poll is tied to the record's state, not to the component's lifetime.
  useEffect(() => {
    if (install?.state !== "running") return;
    let alive = true;
    const timer = window.setInterval(() => {
      getClaudeInstall().then(
        (next) => {
          if (!alive) return;
          setInstall(next);
          // A finished install changed the machine. Re-probe so the strip can
          // close itself rather than sitting on a claim the user just fixed.
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

  // POLL ONLY WHILE A SIGN-IN IS OPEN. The end of one is not a message the
  // server can push, and it is not the child's exit code either: the CLI writes
  // a credential this app never sees, so the only authority on whether it worked
  // is `claude auth status`. When the record stops being in flight, re-probe —
  // and let the strip close itself on the answer rather than on a guess.
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

  const act = useCallback(
    (issue: ClaudeIssue) => {
      if (!issue.action) return;
      setActionError(null);
      setActing(true);
      const done = () => setActing(false);
      if (issue.action.kind === "doctor") {
        runClaudeDoctor().then((res) => {
          done();
          if (res.doctor) setDoctor(res.doctor);
          // `ok: false` with no report is itself the finding — two probes have
          // now failed to get a word out of this binary — so its sentence is
          // shown rather than swallowed.
          else setActionError(res.error || "The diagnostics did not answer.");
        }, (e) => {
          done();
          setActionError(String(e?.message || e));
        });
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
            // The server's own words. A 409 here means a browser window is
            // already open and waiting — which is the thing the user needs to
            // know, and a reworded "please wait" would hide it.
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
          // The server's own words. A 409 here is the "that update would do
          // nothing, run this instead" refusal.
          setActionError(String(e?.message || e));
        },
      );
    },
    [],
  );

  // Calling off a sign-in. The server settles the record before answering, so
  // what comes back is already not in flight and the poll stops on its own.
  const cancelLogin = useCallback(() => {
    setActionError(null);
    cancelClaudeLogin().then(
      (rec) => setLogin(rec),
      () => setLogin(null),
    );
  }, []);

  const issues = claudeIssues(health);
  const showing = loaded && issues.length > 0 && !isDismissed(issues);

  // COMING BACK TO THE WINDOW IS THE SIGNAL. Every fix this card asks for
  // happens somewhere else — a terminal, an installer — so the moment the user
  // returns is exactly when "is it still true?" should be re-asked, and the card
  // should be able to answer by disappearing. Making them press a button to
  // dismiss a warning they have already acted on is the app failing to notice
  // its own advice was taken.
  //
  // Only while something IS showing: with nothing on screen there is no claim to
  // re-check, and a healthy machine must not spawn probes for tabbing around.
  // Forced rather than a plain read, because the server's cache is age-bounded
  // and a sign-in usually lands well inside that window — the cheap read is
  // exactly the one that would still say "signed out".
  useEffect(() => {
    if (!showing) return;
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
  }, [showing, load]);

  if (!showing) return null;

  const check = () => load(true);

  const close = () => {
    rememberDismissal(issues);
    redraw((n) => n + 1);
  };

  return (
    <section className="claude-health" role="status" aria-label="Claude Code setup">
      <div className="claude-health-head">
        <h2 className="claude-health-title">
          {/* Says what is still needed, not that something is broken: nothing IS
              broken — the app is running and the explorer works. Same posture as
              the TroubleCard's warning tint (SPEC §42: "Nothing red"). */}
          Finish setting up Claude Code
        </h2>
        <div className="claude-health-head-actions">
          <button
            type="button"
            className="version-panel-link"
            onClick={check}
            disabled={busy}
          >
            {busy ? "Checking…" : "Check again"}
          </button>
          <button
            type="button"
            className="claude-health-close"
            onClick={close}
            aria-label="Dismiss"
            title="Dismiss"
          >
            ✕
          </button>
        </div>
      </div>
      <ul className="claude-health-issues">
        {issues.map((issue) => (
          <IssueRow
            key={issue.id}
            issue={issue}
            install={install}
            login={login}
            // The report belongs to the row that can act on it, and only the
            // broken-install row can.
            doctor={issue.id === "broken" ? doctor : null}
            onAct={act}
            onCancelLogin={cancelLogin}
            busy={acting}
            actionError={issue.action ? actionError : null}
          />
        ))}
      </ul>
    </section>
  );
}

export default ClaudeHealthStrip;

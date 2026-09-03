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
import { useEffect, useState } from "react";

import type {
  ClaudeDoctor,
  ClaudeInstallStatus,
  ClaudeLoginStatus,
} from "@platform/lib/api";
import { useClaudeSetup } from "@platform/lib/claude-setup";
import {
  claudeIssues,
  dismiss as rememberDismissal,
  isDismissed,
  issueHelpUrl,
  type ClaudeIssue,
} from "@platform/lib/claude-health";

export function CopyCommand({ command }: { command: string }) {
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

export function IssueRow({
  issue,
  install,
  login,
  doctor,
  onAct,
  onCancelLogin,
  busy,
  actionError,
  doneNote,
}: {
  issue: ClaudeIssue;
  install: ClaudeInstallStatus | null;
  login: ClaudeLoginStatus | null;
  doctor: ClaudeDoctor | null;
  onAct: (issue: ClaudeIssue) => void;
  onCancelLogin: () => void;
  busy: boolean;
  actionError: string | null;
  /** A sentence about a fix that already worked — the PATH line landed, and
      the part the user still has to know is that only NEW terminals see it. */
  doneNote?: string | null;
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
  // `doneNote` is the inline action's way of saying it worked — link-path runs
  // in a single request and never goes through the install record.
  const finished = Boolean(mine && install!.state === "done") || Boolean(doneNote);

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
      {doneNote && (
        <p className="claude-health-progress" role="status">
          {doneNote}
        </p>
      )}
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
  // Re-render after a dismissal. The dismissal ITSELF lives in lib/claude-health,
  // keyed on which problems were dismissed; a local `closed` flag used to shadow
  // it and was wrong in one direction that matters — dismissing "not signed in"
  // suppressed a LATER, different problem for the rest of the page's life, when
  // the signature check exists precisely so a new problem still gets through.
  const [, redraw] = useState(0);
  // The machine — snapshot, actions, polls, focus re-check — is lib/claude-setup,
  // shared with the first-run wizard's Claude step. `watching` tells it a claim
  // is on screen worth re-checking when the window comes back.
  const [watching, setWatching] = useState(false);
  const {
    health, loaded, busy, install, login, doctor, actionError, acting,
    linkedNote, load, act, cancelLogin,
  } = useClaudeSetup(watching);

  const issues = claudeIssues(health);
  const showing = loaded && issues.length > 0 && !isDismissed(issues);
  useEffect(() => setWatching(showing), [showing]);

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
            doneNote={issue.id === "not-on-path" ? linkedNote : null}
          />
        ))}
      </ul>
    </section>
  );
}

export default ClaudeHealthStrip;

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
//
// NOT MOUNTED ANY MORE (Home, Apps). Claude Code became an OPTIONAL setup
// step (shell/onboarding/progress OPTIONAL_STAGES): the app is usable without
// it, so a card on the front door nagging about it is wrong, and the same
// facts live in the wizard's Claude step as a checklist. `IssueRow` is the
// part that step still uses; the strip component stays for the day a surface
// that truly needs the CLI wants a preflight again.
import { useState } from "react";

import type {
  ClaudeDoctor,
  ClaudeInstallStatus,
  ClaudeLoginStatus,
} from "@platform/lib/api";
import { issueHelpUrl, type ClaudeIssue } from "@platform/lib/claude-health";
import { CommandLine } from "./CommandLine";
import { copyToClipboard } from "@platform/lib/clipboard";

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

/** The device code, in the command plate's skin with its Copy button — and no
 *  Run, because a code is something to type, not to execute. */
function DeviceCode({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="update-badge-command">
      <code>{code}</code>
      <button
        type="button"
        className="update-badge-copy"
        onClick={async () => {
          if (!(await copyToClipboard(code))) return;
          setCopied(true);
          window.setTimeout(() => setCopied(false), 2000);
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
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
            // An OPTIONAL issue's button is quiet, for the same reason Cancel
            // is: the accent is the app's "this is the next thing to do", and
            // a row that changes nothing about whether the app works is never
            // that. On the wizard's Claude step it would also be the one
            // yellow button on the screen, out-shouting Next.
            className={
              issue.optional
                ? "claude-health-action claude-health-action-quiet"
                : "claude-health-action"
            }
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
        </div>
      )}

      {/* What will actually run, before it runs — always shown, even where a
          button already offers to do it: a user on a locked-down machine, or
          one who would simply rather run it themselves, should not have to
          press our button to find out what it was. Piping a remote script
          into a shell on someone's behalf is a thing to disclose, not to do
          quietly behind a friendly label. */}
      {issue.command && (
        <CommandLine command={issue.command} disabled={busy || running || signingIn} />
      )}

      {running && (
        <p className="claude-health-progress" role="status">
          {install!.detail || "Working…"}
        </p>
      )}
      {/* A DEVICE-CODE sign-in (Copilot, D10) has a code to type, and it is
          drawn only while that flow is OPEN: the record keeps the last one's
          code after it ends, and a stale code is a code that no longer works. */}
      {signingIn && login?.detail?.code && login.detail.url ? (
        <>
          <p className="claude-health-progress" role="status">
            Open{" "}
            <a href={login.detail.url} target="_blank" rel="noreferrer">
              {login.detail.url}
            </a>{" "}
            and enter this code.
          </p>
          <DeviceCode code={login.detail.code} />
        </>
      ) : signingIn ? (
        <p className="claude-health-progress" role="status">
          Finish signing in with the browser window that just opened.
        </p>
      ) : null}
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

// The strip component itself (`ClaudeHealthStrip`) is gone: nothing mounted it
// since the onboarding Tools step took over the preflight (progress.ts), and
// its dismissal lived in localStorage — a fact the port walk reset on every
// launch (D1377). `IssueRow` is what the two surfaces that still render these
// problems (Preferences, the onboarding Tools step) use.

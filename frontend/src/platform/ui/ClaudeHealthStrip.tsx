// The first-run heads-up: three lines on the front door saying whether Claude
// Code is there, new enough, and signed in — BEFORE a prompt has been spent
// finding out.
//
// **The failure this exists to prevent.** On a fresh install the Home hero
// invites a prompt. The app folder gets created, the session fails, and the
// TroubleCard explains it well — but the user has already typed a brief and
// watched a folder appear before learning that the thing the app is built around
// was never set up. `/api/config` had the right doctrine all along: it publishes
// `learn_mount_ready` so the sidebar's Learn entry renders only when it works,
// "so it's never a dead link". This is that gate for everything Claude-dependent.
//
// **It is not a wizard and not a gate.** The file explorer is completely useful
// without Claude Code, and nothing here blocks it: the strip is a row above the
// page that says what is wrong and how to fix it, and it disappears the moment
// there is nothing to say. It renders NOTHING in the common case.
//
// The TroubleCard stays exactly as it is. Preflight is an addition, not a
// replacement — a CLI can break between this check and the call.
import { useCallback, useEffect, useRef, useState } from "react";

import { getClaudeHealth, refreshClaudeHealth, type ClaudeHealth } from "@platform/lib/api";
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

function IssueRow({ issue }: { issue: ClaudeIssue }) {
  return (
    <li className="claude-health-issue">
      <div className="claude-health-issue-title">{issue.title}</div>
      <p className="claude-health-issue-detail">{issue.detail}</p>
      {issue.command && <CopyCommand command={issue.command} />}
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
          <IssueRow key={issue.id} issue={issue} />
        ))}
      </ul>
    </section>
  );
}

export default ClaudeHealthStrip;

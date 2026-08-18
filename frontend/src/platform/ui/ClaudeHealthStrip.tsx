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
import { useCallback, useEffect, useState } from "react";

import { getClaudeHealth, refreshClaudeHealth, type ClaudeHealth } from "@platform/lib/api";
import {
  claudeIssues,
  dismiss as rememberDismissal,
  isDismissed,
  issueHelpUrl,
  type ClaudeIssue,
} from "@platform/lib/claude-health";

// Module-scoped, so walking between Home and /apps — which both render this —
// does not re-probe, and does not flash a strip that a moment ago was closed.
// Deliberately NOT persisted: the snapshot is already cached server-side (on
// disk, keyed on the binary's mtime), so a reload costs one small GET.
let cached: ClaudeHealth | null = null;

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
  const [closed, setClosed] = useState(false);

  useEffect(() => {
    if (cached !== null) return;
    let alive = true;
    getClaudeHealth().then(
      (h) => {
        cached = h;
        if (!alive) return;
        setHealth(h);
        setLoaded(true);
      },
      () => {
        // A FAILED PROBE IS NOT A FINDING. If /api/claude/health itself cannot
        // be reached then the server is what is wrong, and the app has louder
        // ways of saying so (main.tsx's boot card, the status banner). Claiming
        // Claude Code is missing on the strength of our own failed request would
        // be the app blaming the user's machine for its own fault.
        if (alive) setLoaded(true);
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  const check = useCallback(() => {
    setBusy(true);
    refreshClaudeHealth().then(
      (h) => {
        cached = h;
        setHealth(h);
        setBusy(false);
      },
      () => setBusy(false),
    );
  }, []);

  const issues = claudeIssues(health);
  if (!loaded || !issues.length || closed || isDismissed(issues)) return null;

  const close = () => {
    rememberDismissal(issues);
    setClosed(true);
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

// The card shown when something AROUND the app is broken — Claude Code
// missing, a session that would not start, config that would not load, a
// template registry that will not parse (SPEC §42, lib/trouble).
//
// One component for all of them, because the user's next three questions are
// always the same: what happened, what do I do, and how do I hand this to
// somebody who can help. A bare error string answers only the first, and in our
// vocabulary rather than theirs.
//
// The COPY BUTTON is the part that earns its place. Everything else here is a
// nudge toward a page; the copied block is the thing a user can paste into
// their own AI or into an issue and get an actual answer from, because it
// carries the installation it happened in — which is the fact every diagnosis
// needs and the one nobody can look up for themselves.
import { useState } from "react";

import {
  CLAUDE_INSTALL_COMMAND,
  isClaudeTrouble,
  troubleHelpUrl,
  troubleInstructions,
  troubleKind,
  troubleReport,
  type TroubleFacts,
} from "@platform/lib/trouble";

// Plain-language headings and explanations, following the download page's
// troubleshooting tabs so the app and the page tell one story. Deliberately
// says what is true rather than what is polite: "the app can't find Claude
// Code" is a fact the user can act on, "an error occurred" is not.
const SAID: Record<string, { title: string; explain: string }> = {
  notfound: {
    title: "The app can't find Claude Code",
    explain:
      "Fused Render uses Claude Code on this computer to build and fix things, " +
      "and it could not find it. Either it was never installed, or it is " +
      "somewhere the app cannot see.",
  },
  login: {
    title: "Claude Code isn't signed in",
    explain:
      "Claude Code is installed but not signed in to your account yet, so " +
      "nothing can run. Signing in happens in a terminal, once.",
  },
  limit: {
    title: "Your Claude usage limit was reached",
    explain:
      "Nothing is broken — your plan includes a set amount of use and it is " +
      "spent for now. The message below says when it resets.",
  },
  // No explanation for `raw`, deliberately: we do not know what this is, and a
  // paragraph saying so in three clauses was reassurance rather than
  // information. The error itself and the two copy buttons are the answer.
  raw: { title: "Something went wrong", explain: "" },
};

function CopyLine({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="update-badge-copy"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
        } catch {
          // No clipboard permission. Saying nothing would leave the user
          // pressing it again forever; the text is on screen either way.
          return;
        }
        setCopied(true);
        window.setTimeout(() => setCopied(false), 2000);
      }}
    >
      {copied ? "Copied" : label}
    </button>
  );
}

export function TroubleCard({
  what,
  error,
  facts,
  onRetry,
  compact,
  title,
  explain,
  children,
}: {
  /** What the app was doing, in the user's terms — goes in the report. */
  what: string;
  /** The failure, verbatim. */
  error: string;
  /** Whatever the caller could learn about this installation; all optional,
      because the boot failure is exactly the case that knows none of it. */
  facts?: TroubleFacts;
  onRetry?: () => void;
  /** The notification-stack variant: 340px wide, stacked under a failed row.
      Says WHICH failure and where to go, and leaves the explaining to the
      Preferences tab — a full card in a corner popup is a wall, and the
      surface it sits in is a notification rather than a page. */
  compact?: boolean;
  /** Words for a caller that knows more than the classifier can. The template
      registry is the case: it is not one of the download page's four, so it
      classifies as `raw` and gets "Something went wrong" — true, useless, and
      wrong about a fault we can name exactly. The LINK still follows the
      classification, because `raw` is genuinely the right tab for it. */
  title?: string;
  explain?: string;
  /** Extra actions belonging to the calling surface (e.g. "Fix this"). */
  children?: React.ReactNode;
}) {
  const kind = troubleKind(error);
  const fallback = SAID[kind] ?? SAID.raw;
  const said = { title: title ?? fallback.title, explain: explain ?? fallback.explain };
  const ctx = { what, error, ...(facts ?? {}) };
  const report = troubleReport(ctx);
  const instructions = troubleInstructions(ctx);

  if (compact) {
    return (
      <div className="trouble-compact" role="alert">
        <div className="trouble-title">{said.title}</div>
        <div className="trouble-compact-error">{String(error || "").trim()}</div>
        <div className="trouble-actions">
          <CopyLine text={report} label="Copy the details" />
          <CopyLine text={instructions} label="Copy Claude Code instructions" />
          <a
            className="version-panel-link"
            href={troubleHelpUrl(kind)}
            target="_blank"
            rel="noreferrer"
          >
            How to fix this ↗
          </a>
          {children}
        </div>
      </div>
    );
  }

  return (
    <div className="trouble-card" role="alert">
      <div className="trouble-title">{said.title}</div>
      {said.explain && <p className="trouble-explain">{said.explain}</p>}

      {/* Verbatim, in a box, scrollable. Rewording it would make it
          unsearchable, and searching it is the first thing anyone does. */}
      <pre className="trouble-error">{String(error || "(no message)").trim()}</pre>

      {kind === "notfound" && (
        <div className="trouble-install">
          <div className="trouble-label">Install Claude Code</div>
          <div className="update-badge-command">
            <code>{CLAUDE_INSTALL_COMMAND}</code>
            <CopyLine text={CLAUDE_INSTALL_COMMAND} label="Copy" />
          </div>
          <p className="deploy-muted">
            Run it in a terminal, then quit Fused Render and open it again.
          </p>
        </div>
      )}

      <div className="trouble-actions">
        {/* FIRST, and labelled for what it is FOR rather than what it does:
            this is the one action that helps whether the user fixes it
            themselves or asks someone else. */}
        <CopyLine text={report} label="Copy the details" />
        {/* The other reader. `report` describes the problem to a PERSON; this
            is a brief for an agent that can act on it — the goal, the checks
            worth running first, and what "fixed" looks like. Pasting an error
            alone gets a guess back. */}
        <CopyLine text={instructions} label="Copy Claude Code instructions" />
        <a
          className="version-panel-link"
          href={troubleHelpUrl(kind)}
          target="_blank"
          rel="noreferrer"
        >
          {isClaudeTrouble(kind) ? "How to fix this" : "Troubleshooting"} ↗
        </a>
        {onRetry && (
          <button type="button" className="version-panel-link" onClick={onRetry}>
            Try again
          </button>
        )}
        {children}
      </div>
    </div>
  );
}

export default TroubleCard;

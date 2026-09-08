// When the failure is CLAUDE ITSELF rather than a message the run produced
// (SPEC §42, T:13556-13784). This is the surface a user reaches for when they
// want to use Claude, so "claude CLI not found" landing here as a red line is
// the exact complaint the trouble card exists to answer: it names a CLI in our
// vocabulary and leaves the reader with nowhere to go.
//
// The template keeps its own copy of the classifier and the strings because it
// is served standalone and shares no module with the shell (a parity test pins
// the two together). Native has no such excuse, so this is a thin wrapper over
// `platform/ui/TroubleCard` — one card, one set of words, one copy block.
import { useState } from "react";

import { CLAUDE_INSTALL_COMMAND } from "@platform/lib/trouble";
import { TroubleCard } from "@platform/ui/TroubleCard";

import type { Trouble, TroubleKind } from "../protocol/controller-api";

/** The kinds the chat's controller reports that the message classifier cannot
 *  infer on its own — a run id the server has forgotten says nothing about
 *  Claude, and an install that has not finished is not a Claude fault at all.
 *  Anything absent here is left to `troubleKind(error)`, which is right for the
 *  three cases the download page already documents. */
const SAID: Partial<Record<TroubleKind, { title: string; explain: string }>> = {
  "cli-missing": {
    title: "The app can't find Claude Code",
    explain:
      "Fused Render uses Claude Code on this computer to build and fix things, " +
      "and it could not find it. Either it was never installed, or it is " +
      "somewhere the app cannot see.",
  },
  "cli-broken": {
    title: "Claude Code would not start",
    explain:
      "Claude Code is installed, but running it failed. The message below is " +
      "what it printed on the way out.",
  },
  "needs-install": {
    title: "This app hasn't finished installing",
    explain:
      "The chat needs a piece of the app that is still being set up. Nothing " +
      "is broken — try again once the install finishes.",
  },
  engine: {
    title: "The app's engine didn't answer",
    explain:
      "The chat asked this computer's copy of Fused Render to do something and " +
      "got no answer back. Quitting and reopening the app clears this.",
  },
  "unknown-run": {
    title: "That turn is no longer running",
    explain:
      "The link or tab you came back to points at a turn this computer no " +
      "longer has. The conversation itself is intact — send a new message to " +
      "carry on.",
  },
  network: {
    title: "The connection dropped",
    explain: "The request to Claude did not get through. Sending it again usually works.",
  },
};

export interface TroubleViewProps {
  trouble: Trouble;
  /** What the app was doing, in the user's terms — goes in the report.
   *  T:13757 spells it "using the chat on <FILE|this folder>". */
  what?: string;
  onRetry?: () => void;
}

export function TroubleView({ trouble, what, onRetry }: TroubleViewProps) {
  const said = SAID[trouble.kind];
  return (
    <div className="turn trouble">
      <TroubleCard
        what={what ?? "using the chat"}
        error={trouble.message}
        {...(said ? { title: said.title, explain: said.explain } : {})}
        {...(onRetry ? { onRetry } : {})}
      >
        {/* The verbatim traceback is the thing a user pastes somewhere and gets
            an actual answer from, and it is not part of the one-line message
            the card prints — so it gets its own button rather than being
            reworded into the report. */}
        {trouble.detail ? <CopyDetail text={trouble.detail} /> : null}
      </TroubleCard>
      {trouble.detail ? <pre className="trouble-error">{trouble.detail}</pre> : null}
      {trouble.kind === "cli-missing" ? (
        <div className="trouble-cmd">
          <code>{CLAUDE_INSTALL_COMMAND}</code>
          <CopyDetail text={CLAUDE_INSTALL_COMMAND} label="Copy" />
        </div>
      ) : null}
    </div>
  );
}

/** T:13636-13642 — clipboard write, "Copied" for 2 s, and nothing said when
 *  the clipboard refuses: the text is on screen either way. */
function CopyDetail({ text, label = "Copy the full output" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="trouble-btn"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
        } catch {
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

export default TroubleView;

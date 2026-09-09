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
import { splitTroubleMessage, troubleExplain } from "../protocol/trouble";

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
  const lines = splitTroubleMessage(trouble.message);
  // Our own words when we have them, else the action sentence out of the
  // message — which is a better description than the classifier's generic one
  // for exactly the messages that carry one. Neither present: the card's own
  // fallback copy stands, so nothing is passed at all.
  const explain = said?.explain ?? troubleExplain(lines);
  return (
    <div className="turn trouble">
      <TroubleCard
        what={what ?? "using the chat"}
        /* THE CLI'S OWN WORDS ONLY. The card already carries our sentence as its
           title and its explanation, and the "How to fix this ↗" link as a
           button — so handing the whole rewritten message to the verbatim block
           printed all three of them a second time, in a monospace box, as if the
           CLI had said them. `raw` is the part that is genuinely verbatim, and
           the part `troubleKind` classifies on either way. */
        error={lines.raw ?? trouble.message}
        {...(said ? { title: said.title } : {})}
        {...(explain ? { explain } : {})}
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

/** ONE LINE PER ANSWER. `_account_error`'s rewrite is three statements in one
 *  sentence — what happened, what to do, where to read more — and the middle one
 *  is the only one the reader has to act on, so it gets a line of its own rather
 *  than a clause in the middle of a URL-bearing paragraph. Anything the split
 *  does not recognise renders as it always did: one line, still linkified.
 *
 *  Used by the red error ROW as well as here (`ui/Turn`), because the row is the
 *  surface the complaint was actually about — the card below it was already
 *  saying most of this in the right shape. */
export function TroubleMessage({ text }: { text: string }) {
  const lines = splitTroubleMessage(text);
  if (!lines.action && !lines.help && !lines.raw) {
    return <>{linkifyTrouble(lines.lead)}</>;
  }
  return (
    <>
      <div className="err-line">{linkifyTrouble(lines.lead)}</div>
      {lines.action ? (
        <div className="err-line err-action">{linkifyTrouble(lines.action)}</div>
      ) : null}
      {lines.help ? (
        <div className="err-line err-help">
          Help: <TroubleLink href={lines.help} />
        </div>
      ) : null}
      {/* The CLI's exact bytes, kept and demoted: it is what a bug report is
          matched on and what a search engine answers, and it is also the part
          that made the row read as a stack trace addressed to nobody. `title`
          so a truncating surface still hands it over on hover. */}
      {lines.raw ? (
        <div className="err-line err-raw" title={lines.raw}>
          {lines.raw}
        </div>
      ) : null}
    </>
  );
}

/** Trailing punctuation a URL is followed BY rather than made of: a help link
 *  at the end of a sentence must not swallow the period. */
const TRAILING = /[.,;:!?)\]]+$/;

/** The pieces of a trouble line that are not plain text: a backticked span, an
 *  http(s) URL, or a slash command (`/login`) — which is the token the whole
 *  message exists to name and read as prose in the middle of a sentence.
 *
 *  NOT MARKDOWN, and deliberately nothing like it. These bytes are agent.py's
 *  or the CLI's; the funnel that makes markup out of model prose
 *  (MarkdownView → marked → DOMPurify) is the app's only innerHTML site and
 *  this must not become a second one. Every branch below returns an ELEMENT or
 *  a string — never a string of HTML. */
const PIECE = /`([^`]+)`|(https?:\/\/[^\s<>()]+)|(^|\s)(\/[a-z][a-z0-9_-]*)\b/g;

/** `text` as nodes: links clickable, code spans and slash commands set apart,
 *  everything else a text node. */
export function linkifyTrouble(text: string): React.ReactNode[] {
  const src = String(text || "");
  const out: React.ReactNode[] = [];
  let at = 0;
  PIECE.lastIndex = 0;
  for (let m = PIECE.exec(src); m; m = PIECE.exec(src)) {
    if (m.index > at) out.push(src.slice(at, m.index));
    at = m.index + m[0].length;
    if (m[1] !== undefined) {
      out.push(<code key={out.length}>{m[1]}</code>);
    } else if (m[2] !== undefined) {
      const url = m[2].replace(TRAILING, "");
      out.push(<TroubleLink key={out.length} href={url} />);
      out.push(m[2].slice(url.length));
    } else {
      // The whitespace the command was matched WITH belongs to the prose.
      if (m[3]) out.push(m[3]);
      out.push(<code key={out.length}>{m[4]}</code>);
    }
  }
  if (at < src.length) out.push(src.slice(at));
  return out;
}

/** A help URL as the link it is. `noopener` alongside `noreferrer` because the
 *  hole this closes is the opened page reaching back through `window.opener`,
 *  and that is the attribute that names it. */
function TroubleLink({ href }: { href: string }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {href}
    </a>
  );
}

export default TroubleView;

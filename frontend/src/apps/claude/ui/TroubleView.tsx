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

import { TroubleCard } from "@platform/ui/TroubleCard";

import { limitExplain } from "../protocol/quota";

import type { Trouble, TroubleKind } from "../protocol/controller-api";
import { platformKindOf, splitTroubleMessage, troubleExplain } from "../protocol/trouble";

/** The chat had no target at all — a different fact and a different thing to
 *  do, so it is not folded into `boot`'s sentence (P3R1-8). */
export const NO_TARGET_SAID = {
  title: "There's nothing to open a chat on.",
  explain: "Open a file or a folder first, then start the chat.",
};

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
  /**
   * THE CHAT DID NOT BOOT (P3R1-8, owner 2026-09-10). Two sentences: what
   * happened, and the one thing to do about it.
   *
   * What was here instead was "Something went wrong" over a monospace box
   * reading "There is no claude template for this folder." — three faults in one
   * card. The title said nothing. The sentence named an internal thing (the
   * folder's *template*) that a reader has no way to have an opinion about, and
   * it was also a LIE for the commonest case: the 8 s backstop lands on this
   * same branch when `/api/fs/stat` simply never answers, and then the folder's
   * template is fine and the request is not. And the words were printed as if a
   * program had said them, in the box reserved for a program's own output.
   *
   * So: our sentence in the title, the action in the explanation, and NO
   * verbatim block — there are no machine words behind this failure to quote
   * (`ClaudeChat` passes `message: ""`, and `TroubleCard` draws no box for it).
   * "Reload" rather than "retry" because a stalled stat is usually a server that
   * has gone away, and a button that re-runs the same request would answer the
   * reader with the same wait — and the card DRAWS that button (R1-4):
   * `ClaudeChat` passes `onRetry` plus the label this sentence names, so the
   * one action the copy asks for is one press away rather than a thing the
   * reader is told to go and do.
   */
  boot: {
    title: "This chat couldn't load.",
    explain: "Reload the page, or check that Fused Render is still running.",
  },
};

export interface TroubleViewProps {
  trouble: Trouble;
  /** What the app was doing, in the user's terms — goes in the report.
   *  T:13757 spells it "using the chat on <FILE|this folder>". */
  what?: string;
  onRetry?: () => void;
  /** Passed through to the card: what the retry button says when "Try again" is
   *  the wrong promise (the boot failure asks for a reload). */
  retryLabel?: string;
  /** Words for a caller that knows more than the kind does — the boot failure's
   *  two shapes share one kind and differ only in these (P3R1-8). */
  said?: { title: string; explain: string };
  /** For a `limit`: whether the comeback row is STILL on the schedule. The
   *  trouble's own `scheduled` flag says the POST landed; this says the banner
   *  is still up — false once the user cancels it, so the card stops promising
   *  a follow-up that will not come. Absent = trust the flag. */
  comebackPending?: boolean;
}

export function TroubleView({
  trouble,
  what,
  onRetry,
  retryLabel,
  said: saidProp,
  comebackPending,
}: TroubleViewProps) {
  const said = saidProp ?? SAID[trouble.kind];
  const lines = splitTroubleMessage(trouble.message);
  // Our own words when we have them, else the action sentence out of the
  // message — which is a better description than the classifier's generic one
  // for exactly the messages that carry one. Neither present: the card's own
  // fallback copy stands, so nothing is passed at all.
  //
  // A LIMIT WITH ITS WINDOW says when: the CLI reported the reset as an epoch
  // beside the failure (`Trouble.quota`), so the card prints the clock and the
  // distance itself — and, once the server holds the comeback row, that a
  // follow-up is scheduled — instead of telling the reader to find the time
  // in the CLI's sentence below.
  const explain =
    trouble.kind === "limit" && trouble.quota
      ? limitExplain(trouble.quota, Date.now(), !!trouble.scheduled && comebackPending !== false)
      : (said?.explain ?? troubleExplain(lines));
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
        /* THE CLASSIFICATION WE ALREADY MADE. `protocol/trouble.ts` decided
           this when the failure arrived and `platformKindOf` translates our
           vocabulary back to the card's — so the card no longer re-derives it
           from the sliced `raw` above, which need not still match the regex the
           whole message did. That is what makes the card the ONE drawer of the
           install box below. */
        kind={platformKindOf(trouble.kind)}
        {...(said ? { title: said.title } : {})}
        {...(explain ? { explain } : {})}
        {...(onRetry ? { onRetry } : {})}
        {...(retryLabel ? { retryLabel } : {})}
      >
        {/* The verbatim traceback is the thing a user pastes somewhere and gets
            an actual answer from, and it is not part of the one-line message
            the card prints — so it gets its own button rather than being
            reworded into the report. */}
        {trouble.detail ? <CopyDetail text={trouble.detail} /> : null}
      </TroubleCard>
      {/* ONE VERBATIM BLOCK PER CARD, which is all T:13676-13679 draws. The card
          above already prints a `.trouble-error` (the CLI's own words), so a
          second one here was a duplicate whenever `detail` and `message` carry
          the same text — the usual case for a failure whose whole message IS
          the traceback. Shown only when it genuinely adds something the card is
          not already showing; the Copy button inside the card carries it
          either way. */}
      {trouble.detail && trouble.detail.trim() !== (lines.raw ?? trouble.message).trim() ? (
        <pre className="trouble-error">{trouble.detail}</pre>
      ) : null}
      {/* NO INSTALL BOX HERE. T:13681-13691 draws exactly one, inside the card,
          and `platform/ui/TroubleCard.tsx` is that one — complete with the "Run
          it in a terminal, then quit Fused Render and open it again" hint this
          copy never had. Drawing our own as well put the same `curl … | bash`
          box with its own Copy button on screen TWICE in a single card. */}
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

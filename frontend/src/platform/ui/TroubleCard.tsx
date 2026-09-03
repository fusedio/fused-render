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
//
// WARNING-toned (orange bucket), not error-toned: nothing here is a crash. The
// app is working; a thing beside it is not.
import { ExternalLinkIcon } from "lucide-react";

import {
  CLAUDE_INSTALL_COMMAND,
  isClaudeTrouble,
  troubleHelpUrl,
  troubleInstructions,
  troubleKind,
  troubleReport,
  type TroubleFacts,
} from "@platform/lib/trouble";
import { cn } from "@platform/lib/utils";
import { Alert, AlertDescription, AlertTitle } from "@platform/shadcn/ui/alert";
import { Button, buttonVariants } from "@platform/shadcn/ui/button";
import { CommandPlate, CopyButton } from "@platform/ui/CopyButton";
import { StatusDot } from "@platform/ui/flow/StatusIcon";
import { bucketText } from "@platform/ui/status-colors";

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

function HelpLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      className={buttonVariants({ variant: "outline", size: "sm" })}
      href={href}
      target="_blank"
      rel="noreferrer"
    >
      {children}
      <ExternalLinkIcon />
    </a>
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
  /** The notification-stack variant: no plate of its own, stacked under a
      failed row. Says WHICH failure and where to go, and leaves the explaining
      to the Preferences tab — a full card in a corner popup is a wall. */
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
      <div className="flex flex-col gap-1.5 px-2.5 pt-1.5 pb-2.5 text-xs" role="alert">
        <div className={cn("font-semibold", bucketText.orange)}>{said.title}</div>
        {/* Clamped rather than scrolled: this sits in a narrow column where a
            long traceback would own the whole card, and the copy button
            carries the full text anyway. */}
        <div className="line-clamp-3 break-words text-muted-foreground">{String(error || "").trim()}</div>
        <div className="flex flex-wrap items-center gap-2">
          <CopyButton text={report} label="Copy the details" />
          <CopyButton text={instructions} label="Copy Claude Code instructions" />
          <HelpLink href={troubleHelpUrl(kind)}>How to fix this</HelpLink>
          {children}
        </div>
      </div>
    );
  }

  return (
    <Alert className="my-3 max-w-2xl gap-2 px-4 py-3.5">
      <AlertTitle className={cn("flex items-center gap-2 text-sm font-semibold", bucketText.orange)}>
        <StatusDot bucket="orange" />
        {said.title}
      </AlertTitle>
      {said.explain && <AlertDescription>{said.explain}</AlertDescription>}

      {/* Verbatim, in a box, scrollable. Rewording it would make it
          unsearchable, and searching it is the first thing anyone does. Wraps
          rather than ellipsising — a clipped traceback is the half that does not
          say what happened — and caps its height so a long one cannot push the
          actions below the fold. */}
      <pre className="m-0 max-h-56 overflow-auto rounded-md border border-border bg-background px-2.5 py-2 font-mono text-xs whitespace-pre-wrap break-words">
        {String(error || "(no message)").trim()}
      </pre>

      {kind === "notfound" && (
        <div className="flex flex-col gap-1.5">
          <div className="text-xs font-semibold">Install Claude Code</div>
          <CommandPlate command={CLAUDE_INSTALL_COMMAND} />
          <p className="m-0 text-sm text-muted-foreground">
            Run it in a terminal, then quit Fused Render and open it again.
          </p>
        </div>
      )}

      {/* ONE look for the whole row, links and buttons alike: they are peers —
          copy this, read about this, retry this. */}
      <div className="flex flex-wrap items-center gap-2">
        {/* FIRST, and labelled for what it is FOR rather than what it does:
            this is the one action that helps whether the user fixes it
            themselves or asks someone else. */}
        <CopyButton text={report} label="Copy the details" />
        {/* The other reader. `report` describes the problem to a PERSON; this
            is a brief for an agent that can act on it — the goal, the checks
            worth running first, and what "fixed" looks like. Pasting an error
            alone gets a guess back. */}
        <CopyButton text={instructions} label="Copy Claude Code instructions" />
        <HelpLink href={troubleHelpUrl(kind)}>
          {isClaudeTrouble(kind) ? "How to fix this" : "Troubleshooting"}
        </HelpLink>
        {onRetry && (
          <Button type="button" variant="outline" size="sm" onClick={onRetry}>
            Try again
          </Button>
        )}
        {children}
      </div>
    </Alert>
  );
}

export default TroubleCard;

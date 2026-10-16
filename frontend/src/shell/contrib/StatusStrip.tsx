// The one visual unit every contribution-loop notice on the app page is drawn
// with: a glyph plate whose colour says what kind of moment this is, ONE plain
// sentence, an optional quiet line under it, at most one primary button on the
// right, and the quiet secondary actions as a row of text buttons underneath.
// "One plain sentence and one button when a decision is needed" (handoff
// principle 5) is the shape this component enforces — there is no slot for a
// second primary.
//
// Tones: `neutral` (a fact: your changes, waiting), `working` (Claude or the
// server is doing something now), `attention` (a decision only the user can
// make), `success` (it worked — calm, never a confetti moment). `slim` is the
// one-line dismissible notice ("Updated to alice's latest…").
import type { ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "@platform/lib/utils";

export type StripTone = "neutral" | "working" | "attention" | "success";

export function StatusStrip({
  tone,
  icon,
  sentence,
  detail,
  primary,
  secondary,
  onDismiss,
  dismissLabel = "Dismiss",
  slim,
  children,
  busy,
}: {
  tone: StripTone;
  icon: ReactNode;
  sentence: ReactNode;
  detail?: ReactNode;
  primary?: ReactNode;
  secondary?: ReactNode;
  onDismiss?: () => void;
  dismissLabel?: string;
  slim?: boolean;
  children?: ReactNode;
  /** Marks the region busy for assistive tech while an action runs. */
  busy?: boolean;
}) {
  return (
    <section
      className={cn("contrib-strip", `is-${tone}`, slim && "is-slim")}
      // A polite live region: a state change after a background check (the
      // author merged, an update landed) is announced without stealing focus.
      aria-live="polite"
      aria-busy={busy || undefined}
    >
      <div className="contrib-strip-plate" aria-hidden="true">
        {icon}
      </div>
      <div className="contrib-strip-main">
        <div className="contrib-strip-row">
          <div className="contrib-strip-text">
            <p className="contrib-strip-sentence">{sentence}</p>
            {detail && <p className="contrib-strip-detail">{detail}</p>}
          </div>
          {primary && <div className="contrib-strip-primary">{primary}</div>}
          {onDismiss && (
            <button
              type="button"
              className="contrib-strip-dismiss"
              onClick={onDismiss}
              aria-label={dismissLabel}
              title={dismissLabel}
            >
              <X aria-hidden="true" />
            </button>
          )}
        </div>
        {children}
        {secondary && <div className="contrib-strip-secondary">{secondary}</div>}
      </div>
    </section>
  );
}

/** A quiet text action for the strip's secondary row (and the inbox cards).
 *  A raw button with a full reset in contrib.css: the shell ships Tailwind
 *  without preflight, so a shadcn ghost/link variant would inherit the UA's
 *  grey button face. */
export function QuietAction({
  children,
  onClick,
  disabled,
  tone,
  href,
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  tone?: "danger";
  href?: string;
}) {
  const className = cn("contrib-quiet", tone === "danger" && "is-danger");
  if (href) {
    return (
      <a className={className} href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  }
  return (
    <button type="button" className={className} onClick={onClick} disabled={disabled}>
      {children}
    </button>
  );
}

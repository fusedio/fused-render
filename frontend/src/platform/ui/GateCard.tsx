// ONE approval card for every surface that asks a person to say yes or no: the
// Claude sidebar's permission card, the Bots app's approval card, the card a
// Super Bot relays from another bot, and the phone view's card.
//
// PRESENTATIONAL ONLY. It owns the layout and the look: the header, the model's
// risk read (pill + one sentence), a sub line, the verbatim payload in a clipped
// block, a collapsed "Details" disclosure for the rest of the input, the button
// row and the status line. It owns NO decision logic. Each caller decides which
// actions exist, what they send and when the card counts as resolved, and each
// caller keeps its own click plumbing (the Bots thread reads `attrs` through a
// delegated handler; the Claude card passes `onClick`).
import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from "react";

import { severityLabel } from "@platform/lib/risk";
import { cn } from "@platform/lib/utils";

import { CollapsiblePre } from "./CollapsiblePre";
import "./gate-card.css";

export interface GateAction {
  key: string;
  text: ReactNode;
  /** primary = the filled yes; default = an outlined button; link = a quiet underlined choice. */
  kind: "primary" | "default" | "link";
  title?: string;
  disabled?: boolean;
  onClick?: () => void;
  /** Spread onto the button: the data-* hooks a delegated click handler reads. */
  attrs?: Record<string, string>;
}

export interface GateStatus {
  tone: "allow" | "deny" | "expired" | "queued" | "";
  text: string;
}

export interface GateCardProps {
  head: ReactNode;
  /** low | medium | high. Anything else (absent, or from a newer server) draws no pill. */
  severity?: string;
  riskSummary?: string;
  /** The assessment is still being written: a muted placeholder, until the card is resolved. */
  riskPending?: boolean;
  sub?: string;
  /** The verbatim payload. Never cut off silently (see CollapsiblePre). */
  body?: string;
  /** The noun in "Show full {bodyWhat}". */
  bodyWhat: string;
  /** Extra input, shown collapsed under the body. */
  details?: string;
  actions: GateAction[];
  /** True hides the actions: the card is a receipt. */
  resolved: boolean;
  status?: GateStatus;
  /** Phone: buttons at least 44px tall and sharing the row. */
  touch?: boolean;
  className?: string;
  rootProps?: HTMLAttributes<HTMLDivElement> & Record<`data-${string}`, string | number | undefined>;
}

export function GateCard(p: GateCardProps) {
  const label = severityLabel(p.severity);
  const why = (p.riskSummary || "").trim();
  const pending = !!p.riskPending && !p.resolved && !label && !why;
  return (
    <div
      {...p.rootProps}
      className={cn("gate-card", p.resolved && "is-resolved", p.touch && "is-touch", p.className)}
    >
      <div className="gate-head">{p.head}</div>
      {label || why ? (
        <div className="gate-risk">
          {label ? <span className={cn("gate-pill", p.severity)}>{label}</span> : null}
          {why ? <span className="gate-sum">{why}</span> : null}
        </div>
      ) : pending ? (
        <div className="gate-risk is-pending">Assessing risk…</div>
      ) : null}
      {p.sub ? <div className="gate-sub">{p.sub}</div> : null}
      {p.body ? <CollapsiblePre text={p.body} what={p.bodyWhat} /> : null}
      {p.details ? (
        <details className="gate-details">
          <summary>Details</summary>
          <pre>{p.details}</pre>
        </details>
      ) : null}
      {p.resolved ? null : (
        <div className="gate-actions">
          {p.actions.map((a) => (
            <button
              key={a.key}
              type="button"
              className={cn("gate-btn", a.kind === "primary" && "is-primary", a.kind === "link" && "is-link")}
              disabled={a.disabled}
              {...(a.title ? { title: a.title } : {})}
              {...(a.attrs as ButtonHTMLAttributes<HTMLButtonElement> | undefined)}
              onClick={a.onClick}
            >
              {a.text}
            </button>
          ))}
        </div>
      )}
      {p.status?.text ? <div className={cn("gate-status", p.status.tone)}>{p.status.text}</div> : null}
    </div>
  );
}

export default GateCard;

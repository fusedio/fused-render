// The model-written risk read on an approval card (agent_engine._risk_fields). Both fields are absent when the
// assessment failed; the card then draws no pill and no summary.
export type Severity = "low" | "medium" | "high";

const LABELS: Record<Severity, string> = { low: "Low risk", medium: "Medium risk", high: "High risk" };

/** "Low risk" / "Medium risk" / "High risk", or null for anything else (absent, or a value from a newer server). */
export function severityLabel(sev: unknown): string | null {
  return typeof sev === "string" && Object.prototype.hasOwnProperty.call(LABELS, sev) ? LABELS[sev as Severity] : null;
}

// agent_engine YES / NO, the words the gate takes as a verdict.
const YES = /^\s*(y|yes|yep|yeah|ok|okay|sure|approve|approved|go(?!\s+(to|back|on|and)\b)|go ahead|do it|proceed|confirm|allow)\b/i;
const NO = /^\s*(n|no|nope|deny|denied|stop|don'?t|cancel|skip)\b/i;

/** A settled approval card's caption, from the user line that answered it. */
export function approvalVerdict(answer: string | null | undefined): "Approved" | "Denied" | null {
  if (answer == null) return null;
  if (YES.test(answer)) return "Approved";
  if (NO.test(answer)) return "Denied";
  return null;
}

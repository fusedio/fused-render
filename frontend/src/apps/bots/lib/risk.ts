// The model-written risk read on an approval card (agent_engine._risk_fields). Both fields are absent when the
// assessment failed; the card then draws no pill and no summary.
export type Severity = "low" | "medium" | "high";

const LABELS: Record<Severity, string> = { low: "Low risk", medium: "Medium risk", high: "High risk" };

/** "Low risk" / "Medium risk" / "High risk", or null for anything else (absent, or a value from a newer server). */
export function severityLabel(sev: unknown): string | null {
  return typeof sev === "string" && Object.prototype.hasOwnProperty.call(LABELS, sev) ? LABELS[sev as Severity] : null;
}

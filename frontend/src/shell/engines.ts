// What the Inference engines tab SAYS, separated from what it draws (D301).
//
// The rendering is four lines of JSX; the sentences are where this feature can
// actually be wrong, and they are wrong in ways a screenshot does not reveal —
// a preference reported as in force when it is not, a control greyed out with
// no reason beside it, a capability with no engine that reads as a bug rather
// than as an unsupported machine. So the wording lives here, as plain
// functions over the server's payload, and `engines.test.ts` drives them.
//
// Nothing here invents copy about a MACHINE. Availability reasons ("needs Apple
// Silicon — MLX runs on Metal only (this is windows/amd64)") come from the
// registry and are passed through untouched: the page cannot know them, and a
// second copy would drift from the one the AI Models page shows.
import type { CapabilityEngine, EngineChoice } from "@platform/lib/api";

// The capability vocabulary is the Hub's own tags, which are exact and not
// especially readable. Keyed off the server's list rather than replacing it —
// an unknown capability renders as itself, so a capability added server-side
// appears here (ugly but present) instead of vanishing from the page.
const CAPABILITY_LABELS: Record<string, string> = {
  "text-generation": "Text generation",
  "text-to-image": "Image generation",
  "automatic-speech-recognition": "Speech to text",
};

export function capabilityLabel(capability: string): string {
  return CAPABILITY_LABELS[capability] ?? capability;
}

/** The line under the control: what is serving this capability right now.
 *
 *  Reports the EFFECTIVE runner, never the selected one — the same discipline
 *  the Call log section follows with `effective_enabled`. The control shows the
 *  choice you made; this line reports reality, and they are allowed to differ.
 */
export function servingLine(row: CapabilityEngine): string {
  if (!row.effective) return "Nothing on this machine can do this yet.";
  return `Currently using ${row.effectiveLabel ?? row.effective}.`;
}

/** Why the stored choice is not in force, or null when it is.
 *
 *  Null for "auto", which is honoured by definition — a page that warned on
 *  every fresh machine would teach the user to ignore the warning.
 */
export function ignoredWarning(row: CapabilityEngine): string | null {
  if (!row.ignoredReason) return null;
  const chosen = row.choices.find((c) => c.code === row.selected);
  const name = chosen?.label ?? row.selected;
  // The choice is KEPT, and saying so matters: this is the machine that cannot
  // honour it, not the end of it. A user who syncs prefs.json between a Mac and
  // a Windows box should not have to set it again on the way back.
  return `${name} is not being used here — ${row.ignoredReason}. The choice is kept for a machine that can run it.`;
}

/** The tooltip/label suffix for one option. Null when there is nothing to add.
 *
 *  A disabled control with no explanation is the thing the greying-out rule
 *  exists to prevent, so an unavailable choice ALWAYS has a reason — the
 *  fallback exists because a null reason from the server would otherwise
 *  produce a silently dead radio.
 */
export function choiceReason(choice: EngineChoice): string | null {
  if (!choice.available) return choice.reason ?? "not available on this machine";
  return choice.note;
}

/** Whether picking `code` for `row` would change which backend actually runs.
 *
 *  The Preferences page uses it to decide whether to WARN before writing: a
 *  switch that changes the effective engine unloads that capability's resident
 *  model and changes the suggested models on the AI Models page, and neither
 *  should be a surprise. Choosing an unusable runner changes what is stored and
 *  nothing else, so it earns no warning.
 */
export function wouldChangeEngine(row: CapabilityEngine, code: string, auto: string): boolean {
  if (code === row.selected) return false;
  if (code === auto) {
    // Back to automatic: it changes things only if the stored preference was
    // the thing in force. An override that was ALREADY being ignored resolves
    // to the automatic answer today, so clearing it moves nothing.
    return row.ignoredReason === null && row.selected !== auto;
  }
  const choice = row.choices.find((c) => c.code === code);
  if (!choice?.available) return false;
  return code !== row.effective;
}

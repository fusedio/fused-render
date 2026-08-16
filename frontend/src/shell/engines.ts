// What the Inference engines tab SAYS, separated from what it draws (D302).
//
// The rendering is four lines of JSX; the sentences are where this feature can
// actually be wrong, and they are wrong in ways a screenshot does not reveal —
// a preference reported as in force when it is not, an option greyed out with
// no reason in it, a capability with no engine that reads as a bug rather
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
  if (!row.effective) return "Not available on this machine.";
  return `Using ${row.effectiveLabel ?? row.effective}.`;
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
  // One sentence, and it survives the trim because it is the ONLY signal that
  // a stored preference was dropped: the select still shows the user's choice,
  // so without this the page states something untrue. The reason is the
  // registry's own and is passed through; what went is the second sentence
  // about the choice being kept for another machine, which is reassurance
  // rather than information — the choice being still selected says it.
  return `${name} is not used here — ${row.ignoredReason}.`;
}

/** Why one option cannot be picked — null when it can.
 *
 *  A disabled control with no explanation is the thing the greying-out rule
 *  exists to prevent, so an unavailable choice ALWAYS has a reason: the
 *  fallback covers a null from the server, which would otherwise produce a
 *  silently dead menu item.
 *
 *  Returning null for an available choice is what lets the caller append this
 *  unconditionally: the engine picker folds the reason into the option's own
 *  label, because a disabled `<option>` has nowhere else to say anything.
 *
 *  Deliberately says nothing about an AVAILABLE engine. It used to return the
 *  runner's `note` — what using that backend is like — and the page rendered it
 *  after every label; that is editorial copy on a settings page, and the note
 *  still has a home on the AI Models page, where somebody is choosing what to
 *  download and the sentence can change a decision.
 */
export function choiceReason(choice: EngineChoice): string | null {
  if (choice.available) return null;
  return choice.reason ?? "not available on this machine";
}

/** Whether picking `code` for `row` would change which backend actually runs.
 *
 *  The Preferences page uses it to decide whether to WARN before writing: a
 *  switch that changes the effective engine unloads that capability's resident
 *  model and changes the suggested models on the AI Models page, and neither
 *  should be a surprise. Choosing an unusable runner changes what is stored and
 *  nothing else, so it earns no warning.
 *
 *  **The question is always "does the EFFECTIVE runner move", never "does the
 *  stored value move".** Those come apart in both directions, and getting it
 *  wrong is not harmless in either: a missing warning hides an unload, and a
 *  spurious one claims a model was evicted and a list rewritten when the server
 *  did nothing at all — which teaches the user that the message means nothing.
 */
export function wouldChangeEngine(row: CapabilityEngine, code: string, auto: string): boolean {
  if (code === row.selected) return false;
  if (code === auto) {
    // Back to automatic. `effective` is what auto would resolve to whenever the
    // stored override is NOT in force, so an ignored override moves nothing —
    // and an HONOURED one moves nothing either when it happens to name the
    // runner auto would have picked anyway, which is the common case on the
    // machine the preference was set on (a Mac that chose mlx-whisper, the same
    // runner the registry order puts first). Comparing against the ordering's
    // own answer is the only way to tell those apart, so `auto` is the one
    // option whose consequence cannot be decided from `selected` alone.
    return row.ignoredReason === null && row.selected !== auto
      && !isFirstAvailable(row, row.selected);
  }
  const choice = row.choices.find((c) => c.code === code);
  if (!choice?.available) return false;
  return code !== row.effective;
}

/** Is `code` what the registry's ordering would pick on its own?
 *
 *  The server's rows arrive in registry order and carry each runner's
 *  availability, so "what auto resolves to" is derivable here rather than
 *  needing a second field — first-match-wins over the same list the server
 *  filters, which is the rule `registry._first_available` implements.
 */
function isFirstAvailable(row: CapabilityEngine, code: string): boolean {
  return row.choices.find((c) => c.available)?.code === code;
}

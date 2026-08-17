// What the Inference engines tab SAYS, separated from what it draws (D302).
//
// The tab is the Engines tab of /ai-models (shell/AiModelsEngines.tsx); it was
// a Preferences tab when this module was written, and the move is exactly what
// this split made cheap — the page that draws these sentences changed and not
// one of them did. What the move added is `switchOutcome` at the bottom: a
// switch used to be two navigations from anything it affected, and is now a tab
// click from all of it, so the page has to know when a switch changed
// something. Still a plain function over the payload, still driven by
// `engines.test.ts`.
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
import type { CapabilityEngine, EngineChoice, Prefs } from "@platform/lib/api";

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
  // The SHORT name. This line sits directly under the picker, whose options
  // carry "(Apple Silicon)" and "(PyTorch)" because that is where the reader
  // is choosing between backends; saying it again one line below is the
  // repetition the qualifier-free name exists to remove. `ignoredWarning`
  // below is the opposite case and stays long — it quotes an option back.
  return `Using ${row.effectiveShortLabel ?? row.effectiveLabel ?? row.effective}.`;
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
 *  runner's `note` — what using that backend is like — appended to every
 *  option's label, which is a paragraph inside a dropdown. The note is now a
 *  line under the row (`engineNote`), where it can be read without opening the
 *  menu and only for the backend actually in force.
 */
export function choiceReason(choice: EngineChoice): string | null {
  if (choice.available) return null;
  return choice.reason ?? "not available on this machine";
}

/** What running on the backend that is SERVING this capability is like, or null.
 *
 *  Three of the six runners have something to say — MLX FLUX reserves much more
 *  memory than Diffusers and is untested below 32GB, MLX Whisper transcribes on
 *  the GPU, PyTorch wants an NVIDIA card — and the FLUX one is a caution, not a
 *  fact: it is the sentence that tells somebody on a 16GB Mac to move back to
 *  Diffusers, which is precisely the switch the control above it makes.
 *
 *  **It lived over the Discover tab's capability sections and came back here.**
 *  There it was a fact about a backend printed above a grid of models, present
 *  under three headings and absent under the others, so the page read as
 *  blotchy and the notes as noise — and the warning about memory was two tabs
 *  from the only control that answers it.
 *
 *  EFFECTIVE, never selected, for `servingLine`'s reason: the row reports what
 *  is running, and a note about a backend this machine refused to use would be
 *  a sentence about somewhere else.
 */
export function engineNote(row: CapabilityEngine): string | null {
  if (!row.effective) return null;
  return row.choices.find((c) => c.code === row.effective)?.note ?? null;
}

/** Whether picking `code` for `row` would change which backend actually runs.
 *
 *  The Engines tab uses it to decide whether to WARN before writing: a switch
 *  that changes the effective engine unloads that capability's resident model
 *  and rewrites the suggestions on the Discover tab beside it, and neither
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

/** What a completed engine PUT actually did — null when it did nothing anyone
 *  can see.
 *
 *  Two questions with two different answerers, and the page needs both. Whether
 *  a model was EVICTED is the server's answer (`unloaded`), because residency is
 *  in no other field of this payload and the usual case is that nothing was
 *  resident at all. Whether the effective engine MOVED is `wouldChangeEngine`
 *  over the row the PUT replaced, and it is the question that decides whether
 *  the switch is worth mentioning at all.
 *
 *  It returns one value because the caller has one decision to make twice over:
 *  which sentence to show, and whether the /ai-models listing the page is
 *  holding has gone stale. Both follow from the same outcome — every card's
 *  `engine` field is the registry's answer under the preference this PUT just
 *  replaced, so a switch that moved the effective engine rewrote every engine
 *  tag and every Load refusal on the Local tab, and an eviction falsified a
 *  Loaded badge. A null outcome changed neither, and must not cost a disk walk.
 *
 *  `row` is the PRE-write row: the comparison only means anything against the
 *  state the PUT replaced, and afterwards the server's answer is the new
 *  reality with nothing left to compare it against.
 */
export function switchOutcome(
  row: CapabilityEngine,
  code: string,
  auto: string,
  next: Prefs,
): "unloaded" | "switched" | null {
  if ((next.engines.unloaded?.length ?? 0) > 0) return "unloaded";
  return wouldChangeEngine(row, code, auto) ? "switched" : null;
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

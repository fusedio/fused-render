// Decision 9: the "Press Enter to search" placeholder shown while a
// path/pattern query sits uncommitted (decision 4's gate). Driven off
// decision 5's `typedAddress` — the same `TypedAddress` the Enter handler
// branches on, and checks FIRST — not off the gate alone: a resolved, real
// folder in the field means Enter navigates, not searches, and the prompt
// has to say so rather than promising a search next to a dropdown naming
// the exact folder Enter would open. Being a pure projection of
// `typedAddress` is what keeps the prompt and the handler from disagreeing.
import type { TypedAddress } from "@apps/explorer/listing/useTypedPathAddress";

export function enterPrompt(typedAddress: TypedAddress): string {
  // Resolved to a real path: name it. Whether it's a file or a folder, Enter
  // opens it, not a search.
  if (typedAddress.status === "exists") {
    const trimmed = typedAddress.path.replace(/\/+$/, "");
    const name = trimmed.split("/").pop() || trimmed;
    return `Press Enter to open ${name}`;
  }
  // "checking" is still resolving — do not flip the wording into a third
  // state that appears and vanishes mid-keystroke. Enter falls through to
  // the search commit while unresolved, so the search wording is what is
  // actually true right now, same as "idle" and "missing".
  return "Press Enter to search";
}

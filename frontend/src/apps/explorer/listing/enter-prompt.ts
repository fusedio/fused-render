// Decision 9: the "Press Enter to search" placeholder shown while a
// path/pattern query sits uncommitted (decision 4's gate). It used to be a
// constant, derived only from that gate — never from decision 5's
// `typedAddress`, which is what Enter actually checks FIRST. That let the
// prompt promise a search when Enter was really about to navigate: a
// resolved, real folder in the field said "Press Enter to search" right next
// to a dropdown naming the exact folder it would open.
//
// Driven off the same `TypedAddress` the Enter handler branches on, so the
// two cannot disagree — this is a pure projection of that value, not a
// second opinion about it.
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

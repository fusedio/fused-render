// The contribution loop's product sentences (docs/CONTRIB_LOOP.md §13), in
// ONE place so the card, the dialogs and the inbox can never drift apart in
// how they name the same thing. Two rules every string here keeps:
//
//   No git words. "your changes", "alice's latest", "proposal", "merged" —
//   never branch, fork, commit, push, conflict, main.
//   No pronouns for the author. The GitHub login stands in ("alice's latest
//   version", not "her latest version") — §13's own "merged" row says "her",
//   and this file deliberately does not.
import type { CopyStatus } from "@platform/lib/contrib";

export function plural(n: number, one: string, many = one + "s"): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The author's display name: the login, else the owner half of the repo. */
export function authorOf(copy: Pick<CopyStatus, "author" | "repo" | "remix_of">): string {
  return copy.author || copy.repo?.split("/")[0] || copy.remix_of?.split("/")[0] || "the author";
}

export function possessive(name: string): string {
  return name.endsWith("s") ? `${name}'` : `${name}'s`;
}

/** Edits the user would count: committed edits plus one for unsaved work on
 *  top, so a copy with only an unsaved tweak still reads "1 edit". */
export function editCount(copy: Pick<CopyStatus, "changes" | "dirty">): number {
  return copy.changes + (copy.dirty ? 1 : 0);
}

export function githubUrl(repo: string): string {
  return `https://github.com/${repo}`;
}

/** "1:05" — minutes and seconds, for a running merge's elapsed clock. */
export function elapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** The progress steps of sending a proposal, keyed by the job's `step`. */
export const SEND_STEPS: { key: string; label: string }[] = [
  { key: "copy", label: "Making your copy on GitHub…" },
  { key: "send", label: "Sending your changes…" },
  { key: "open", label: "Opening the proposal…" },
];

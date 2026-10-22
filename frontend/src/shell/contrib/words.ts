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

const NO_NAME = "the author";

/** The author's display name: the login, else the owner half of the repo,
 *  else "the author" (lower case — it is mostly read mid-sentence; see
 *  `atStart` for the sentence-initial spelling). */
export function authorOf(copy: Pick<CopyStatus, "author" | "repo" | "remix_of">): string {
  return copy.author || copy.repo?.split("/")[0] || copy.remix_of?.split("/")[0] || NO_NAME;
}

/** A name opening a sentence. Only the fallback is capitalised: a GitHub
 *  login is spelled the way its owner spells it ("alice" stays "alice"). */
export function atStart(name: string): string {
  return name === NO_NAME ? "The author" : name;
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

/** One step of a long operation: its label, and for the steps whose length
 *  is GitHub's or Claude's to decide, a hint saying so (§16.13: latency is
 *  fine as long as the person is told why). */
export interface Step {
  key: string;
  label: string;
  hint?: string;
}

/** What GitHub's own delay usually is, in one clause the hints share. */
const GITHUB_LAG = "GitHub shows a change a few seconds after it arrives.";

/** The progress steps of sending a proposal, keyed by the job's `step`. */
export const SEND_STEPS: Step[] = [
  {
    key: "copy",
    label: "Making your copy on GitHub…",
    hint: "Usually a few seconds; GitHub sometimes takes up to a minute to set a copy up.",
  },
  { key: "send", label: "Sending your changes…" },
  { key: "open", label: "Opening the proposal…" },
];

/** Update proposal: the push, then a short wait for GitHub to show it. */
export const UPDATE_STEPS: Step[] = [
  { key: "send", label: "Sending your newer edits…" },
  { key: "confirm", label: "Waiting for GitHub to confirm…", hint: GITHUB_LAG },
];

export const WITHDRAW_STEPS: Step[] = [
  { key: "close", label: "Closing the proposal…" },
  { key: "tidy", label: "Removing your copy on GitHub…" },
];

/** Accept, on the author's side: GitHub merges, then her copy is synced. */
export const ACCEPT_STEPS: Step[] = [
  { key: "merge", label: "Merging on GitHub…", hint: "GitHub usually answers in a few seconds." },
  { key: "sync", label: "Bringing your copy up to date…" },
];

/** The steps of each long action the Source panel runs, by action id; an
 *  action not listed shows one unnamed step. */
export const STEPS_FOR: Record<string, Step[]> = {
  "update-proposal": UPDATE_STEPS,
  withdraw: WITHDRAW_STEPS,
  "push-main": [{ key: "run", label: "Sending your changes to the app…" }],
  "bring-in": [{ key: "run", label: "Bringing in the latest…" }],
  "publish-now": [{ key: "run", label: "Publishing…" }],
  "put-on-top": [{ key: "run", label: "Publishing…" }],
  "publish-over": [{ key: "run", label: "Publishing…" }],
  "move-aside": [{ key: "run", label: "Moving your file aside and updating…" }],
  discard: [{ key: "run", label: "Discarding…" }],
  remix: [{ key: "run", label: "Publishing your remix…" }],
};

/** Why the panel's "Checked …" line reads as it does: when checks happen,
 *  so nobody waits on one that is not coming. */
export function checksWhen(state: string, who: string): string {
  const what = state === "proposed" || state === "merged" ? `${possessive(who)} reply` : `${possessive(who)} latest`;
  return `Checks for ${what} when you open the app, every minute while it's open, and when you press Check.`;
}

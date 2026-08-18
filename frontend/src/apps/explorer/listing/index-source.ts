// Which source answers the in-folder search, decided from what the server
// said — never from a rule kept here.
//
// The box used to RACE two sources: it asked the index for the folder's whole
// corpus and, if that had not produced within 150 ms, started a live streamed
// walk alongside it; first to produce took the answer (the deleted
// listing/source-race). That was the right shape when both sources could
// answer any folder. They cannot: the index either covers a folder or does
// not, and the reason it does not is a fact only the server holds — the mount
// policy is `MountGuard`'s, the ignore list is the scan config's, and a
// package is a shape of the store. A second copy of any of those in TypeScript
// would drift from the original silently, and the drift would show up as two
// searches disagreeing about the same folder.
//
// So `GET /api/index/rank` answers with a `reason`, and this file is the whole
// of the client's policy: three outcomes, and the only one that carries
// judgement is what to do when a folder stays uncovered after we asked for it
// to be scanned.
//
//   answer  the index answered; render it.
//   scan    nothing here yet, but a scan would fix that — ask for one.
//   poll    an answer is coming (a scan is running); ask again shortly and
//           keep rendering whatever came back meanwhile.
//   walk    no scan will ever cover this folder; the live streamed walk is
//           the only source there is.
//
// Note what the client does NOT do: it never decides that a folder is
// mount-backed, ignored or a package. It walks when the server has said it
// cannot answer AND cannot be made to — one rule, in one direction.

import type { RankReason } from "@platform/lib/api";

export type SearchStep = "answer" | "scan" | "poll" | "walk";

// Ranked answers that may still read `uncovered` after a scan was asked for,
// before the folder is written off. `runner.start` returns as soon as the
// worker is spawned, so the run is not yet listed as live and the next answer
// can legitimately still be a miss; giving up on that one would abandon every
// on-demand scan the instant it was requested.
export const UNCOVERED_GRACE = 3;

// How many times a scan in flight is polled before the box settles for what it
// has. A first whole-home scan is ~10 s and a rescan of a big root can be
// minutes; the rows already returned are real, and re-asking for them at a
// fixed cadence for the length of a scan is not what the poll is for. At
// SCAN_POLL_MS this is a couple of minutes.
export const MAX_SCANNING_POLLS = 80;

export interface SourceInput {
  reason: RankReason;
  /** A scan has already been asked for, for this folder and generation. */
  asked: boolean;
  /** Ranked answers received since that ask. */
  sinceAsk: number;
  /** Polls ISSUED for the current scan — ticks, not answers (see below). */
  polls: number;
  /** Whether the last answer actually covered the folder. */
  covered: boolean;
}

/** What the box should do with the answer it just got. */
export function nextStep(input: SourceInput): SearchStep {
  const { reason, asked, sinceAsk, polls, covered } = input;
  // Permanently uncoverable, each for its own reason, all one condition here.
  if (reason === "mount" || reason === "package" || reason === "ignored") {
    return "walk";
  }
  if (reason === "scanning") {
    if (polls < MAX_SCANNING_POLLS) return "poll";
    // Out of patience. What that means depends on whether the scan ever
    // produced anything: a COVERED folder has real rows to settle for, while
    // an uncovered one would settle for `hits: []` — an empty list for a
    // folder the walk searches fine, which is the same "blame the user's files
    // for the app's state" the uncovered branch below refuses. A scan of an
    // uncovered root reports `scanning` too, so this is not a rare corner.
    return covered ? "answer" : "walk";
  }
  if (reason === "uncovered") {
    if (!asked) return "scan";
    // Scanned, and still not covered: another filesystem, or a scan that
    // failed. Asking again is the retry loop this design refuses, and "no
    // matches" would blame the user's files for the app's state — so the walk,
    // which is the same last resort the mount case gets.
    return sinceAsk < UNCOVERED_GRACE ? "poll" : "walk";
  }
  // "" — and anything a newer server grows that this build has not heard of:
  // it ANSWERED, and its hits are on screen.
  return "answer";
}

/**
 * Whether an answer is worth putting in the session memo.
 *
 * Only a settled one for a covered folder. An answer taken while a scan is
 * running is a snapshot of a folder still being indexed, and serving it back
 * on a backspace would freeze the very trickle the poll exists to show — which
 * includes the answer that settles only because the poll ceiling ran out, the
 * case a `step === "answer"` test alone would get wrong.
 *
 * Takes the step and the reason rather than reading the caller's "am I
 * polling?" state, because that state is one React commit behind at exactly
 * the moment this is asked: the answer that ENDS a scan is delivered by a
 * callback whose closure still says a scan is running.
 */
export function remembersAnswer(step: SearchStep, reason: RankReason): boolean {
  return step === "answer" && reason === "";
}

export interface ProgressInput {
  searching: boolean;
  /** The live walk is answering this folder. */
  walkMode: boolean;
  /** A ranked request is out. */
  pending: boolean;
  /** A scan covering this folder is running and being polled. */
  polling: boolean;
  /** The walk's browser-side scoring pass has not published yet. */
  scanning: boolean;
}

export interface Progress {
  /** An answer is still on its way: the "Searching…" row and the spinner. */
  answerComing: boolean;
  /** ...and it is a MOMENTARY wait, which is what the heavy dim is for. */
  inFlight: boolean;
}

/**
 * The two different questions the box asks about its own progress.
 *
 * They came apart when the index gained an on-demand scan. "Is an answer
 * coming?" now has two sources — a round trip, and a scan landing rows — and
 * answering it with the round trip alone is what made an empty first answer
 * during a scan render as a confident "No matches" for the whole time the scan
 * was working.
 *
 * They must not be merged either. The heavy dim is calibrated for something
 * that clears in a moment; a scan runs for seconds to minutes, and dimming the
 * rows for its duration would say "this is about to change" for far longer
 * than a reader can hold that thought. That state already has its own, quieter
 * treatment: the "indexing…" caveat.
 */
export function searchProgress(input: ProgressInput): Progress {
  const { searching, walkMode, pending, polling, scanning } = input;
  if (!searching) return { answerComing: false, inFlight: false };
  if (walkMode) return { answerComing: scanning, inFlight: scanning };
  return { answerComing: pending || polling, inFlight: pending };
}

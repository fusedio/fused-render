// What the in-folder search does with a ranked answer — decided from what the
// server said, never from a rule kept here.
//
// The index either covers a folder or it does not, and the reason it does not
// is a fact only the server holds: the mount policy is `MountGuard`'s, the
// ignore list is the scan config's, and a package is a shape of the store. A
// second copy of any of those in TypeScript would drift from the original
// silently, and the drift would show up as the search disagreeing with itself
// about the same folder.
//
// So `GET /api/index/rank` answers with a `reason`, and this file is the
// whole of the client's policy: three outcomes.
//
//   answer  render what came back — a real ranked answer, or a reason the
//           folder cannot be covered right now, which the search box turns
//           into the index gap (lib/home-search's `indexGap`).
//   scan    nothing here yet, but a scan would fix that — ask for one.
//   poll    an answer is coming (a scan is running); ask again shortly and
//           keep rendering whatever came back meanwhile.
//
// Note what the client does NOT do: it never decides that a folder is
// mount-backed, ignored, a package, or that indexing has been turned off in
// Preferences — it reports what the server said and stops there.

import type { RankReason } from "@platform/lib/api";

export type SearchStep = "answer" | "scan" | "poll";

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
//
// Per polling EPISODE, and per folder+generation within one: a query typed
// midway through a scan inherits the patience already spent on that scan
// rather than restarting it, because the thing being waited for is the scan
// and not the query — while the next scan of the same folder starts over.
//
// Counted in POLLS ISSUED, not answers received, and that is load-bearing. A
// tick used to abort the request in flight before issuing its own, so if a
// rank round trip consistently outlasted the interval — likeliest exactly
// here, while a compaction is running — no answer ever landed and a ceiling
// counted in answers was never approached. A tick leaves a live request alone
// now (the in-flight guard in useListingSearch), but the ceiling stays
// counted in ticks: it is the one measure the loop cannot starve, whatever the
// server does with the requests it is sent.
export const MAX_SCANNING_POLLS = 80;

export interface SourceInput {
  reason: RankReason;
  /** A scan has already been asked for, for this folder and generation. */
  asked: boolean;
  /** Ranked answers received since that ask. */
  sinceAsk: number;
  /** Polls ISSUED for the current scan — ticks, not answers (see below). */
  polls: number;
}

/** What the box should do with the answer it just got. */
export function nextStep(input: SourceInput): SearchStep {
  const { reason, asked, sinceAsk, polls } = input;
  // Permanently uncoverable, each for its own reason, all one condition here.
  // `disabled` belongs in this set even though it is not permanent the way
  // the other three are — the user can flip the preference back on — because
  // there is no server signal to poll for that, so a scan is exactly as
  // unaskable-for as it is for a mount, a package, or an ignored folder.
  // `fda` likewise: the grant lands on the NEXT launch, not this one.
  if (
    reason === "mount" ||
    reason === "package" ||
    reason === "ignored" ||
    reason === "disabled" ||
    reason === "fda"
  ) {
    return "answer";
  }
  if (reason === "scanning") {
    if (polls < MAX_SCANNING_POLLS) return "poll";
    // Out of patience. Whatever the scan produced (real rows, or none for a
    // folder that stays uncovered) is what there is to settle for — the index
    // gap the caller renders for an empty, still-uncovered answer says so.
    return "answer";
  }
  if (reason === "uncovered") {
    if (!asked) return "scan";
    // Scanned, and still not covered: another filesystem, or a scan that
    // failed. Asking again is the retry loop this design refuses, so this
    // settles for the index gap rather than looping forever.
    return sinceAsk < UNCOVERED_GRACE ? "poll" : "answer";
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
  /** A ranked request is out. */
  pending: boolean;
  /** A scan covering this folder is running and being polled. */
  polling: boolean;
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
  const { searching, pending, polling } = input;
  if (!searching) return { answerComing: false, inFlight: false };
  return { answerComing: pending || polling, inFlight: pending };
}

// Which of the two corpus sources — the file index or the live streamed walk —
// is allowed to publish, when both are running at once.
//
// The index is normally the fast one, but it is not reliably fast: the first
// search after a scan completes pays a whole-corpus gitignore sweep on the
// server (server/index_gitignore.py), and while that runs the box showed
// nothing at all for seconds and then everything at once — strictly worse than
// the streamed walk it replaced, which paints from its first NDJSON batch.
// So the walk is started if the index has not produced within a short budget
// (INDEX_RACE_MS) and the two race.
//
// The rule that makes that safe is FIRST TO PRODUCE TAKES THE WHOLE ANSWER.
// Interleaving the two entry lists would double-count every path they share —
// duplicate rows, and a "N matches" count that is simply wrong — so exactly
// one source ever writes into the entries array, and the loser is cancelled
// so the server-side walk generator is closed rather than left streaming into
// nothing.
//
// Pure and separate from the hook because "who may write" is the one thing
// here that must not be wrong, and a closure buried in an effect is not
// something a test can pin.

export type Source = "index" | "walk";

export interface SourceRace {
  /**
   * Whether `who` may publish. The first source to ask wins and keeps winning;
   * every later ask by the loser answers false. Claiming cancels the other
   * source exactly once.
   */
  claim(who: Source): boolean;
  /** Whether anyone has produced yet. */
  claimed(): boolean;
  /** Who won, or null while both are still running. */
  winner(): Source | null;
}

export function startRace(cancel: (loser: Source) => void): SourceRace {
  let won: Source | null = null;
  return {
    claim(who) {
      if (won === null) {
        won = who;
        cancel(who === "index" ? "walk" : "index");
      }
      return won === who;
    },
    claimed: () => won !== null,
    winner: () => won,
  };
}

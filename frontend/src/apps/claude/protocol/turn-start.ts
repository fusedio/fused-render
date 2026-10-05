// WHEN A REPLY SLICE BEGAN — the stamp the `show more` hover measures
// "Worked for" from (ui/run-when.ts), read off a poll payload.
//
// The window a poll returns is sliced into one reply per seam
// (`turn_breaks`); the CLI opened each of those replies with an echoed user
// row, and `turn_starts` carries every such row's stamp in order. So slice `j`
// starts at `turn_starts[j + dropped]`, where `dropped` is how many leading
// slices the page's base already accounts for (`landedWindow`: seams at or
// before the base are filtered out, and their echoes with them).
//
// `turn_ts` — the LAST echo — is the fallback for the last slice on a server
// that predates `turn_starts`. It is deliberately not used for an earlier
// slice: a follow-up folded into the first reply before its bubble existed
// (two echoes, one slice) would start that reply at the follow-up's echo, a
// few seconds to minutes late (Bugbot, PR #1430).
import type { PollResponse } from "./types";

export function sliceStartedAt(
  poll: Pick<PollResponse, "turn_ts" | "turn_starts">,
  j: number,
  last: number,
  dropped = 0,
): number | undefined {
  const starts = Array.isArray(poll.turn_starts) ? poll.turn_starts : [];
  const at = starts[j + dropped];
  if (typeof at === "number" && Number.isFinite(at) && at > 0) return at;
  if (starts.length) return undefined;
  const lastEcho = poll.turn_ts;
  return j === last && typeof lastEcho === "number" && Number.isFinite(lastEcho) && lastEcho > 0
    ? lastEcho
    : undefined;
}

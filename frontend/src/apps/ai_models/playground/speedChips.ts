// ---- the image rail's three step counts ---------------------------------------
// The Playground offers a model's curated step count, a slower one, and the
// server's generic ceiling. The middle number is the only one that has to be
// worked out, and it has to stay strictly between the other two: three chips
// where two carry the same number is a rail that looks broken and offers a
// choice that isn't one.

/** The server's own generic step default, and the top of the rail. */
export const SERVER_STEPS = 28;

/** The middle chip's step count, given the model's own curated one.
 *
 *  Three times the model's count is the shape this ladder wants — a distilled
 *  model curated at 4 offers 12, which is a real step up and still well under
 *  the ceiling. That multiple only works while the curated count is small: a
 *  model curated at 16 would ask for 48, which clamps to `SERVER_STEPS` and
 *  makes the middle chip a duplicate of the top one. Where 3x overshoots, the
 *  midpoint between the model's count and the ceiling is used instead, which is
 *  strictly between the two by construction wherever there is room for it.
 *
 *  Returns null when there is no room — a model curated at or above the ceiling,
 *  or one step below it, has no distinct middle number to offer and the caller
 *  should show two chips rather than invent one. */
export function middleSteps(modelSteps: number): number | null {
  if (modelSteps >= SERVER_STEPS - 1) return null;
  const tripled = modelSteps * 3;
  const middle = tripled <= SERVER_STEPS - 1
    ? tripled
    : Math.round((modelSteps + SERVER_STEPS) / 2);
  // The round can land back on either neighbour for a count adjacent to the
  // ceiling; the guard above rules that out, but the clamp states the invariant
  // the callers actually rely on rather than leaving it implied by arithmetic.
  return Math.max(modelSteps + 1, Math.min(SERVER_STEPS - 1, middle));
}

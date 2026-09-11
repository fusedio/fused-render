// ONE MERGE FOR EVERY SEND, because a spread is not a merge.
//
// Both send roads used to read `{ ...opts, ...takeAttachments() }`, which is a
// REPLACEMENT: the tray's `blocks` / `readDirs` / `attachments` overwrote the
// caller's, and the caller's overwrote the tray's for anything the tray left
// out. Latent through PR2 — nothing else supplies blocks yet — and a silent
// dropped `<annotations>` block the moment PR3 lands, which is the worst shape
// this bug could have: the message goes out, the run succeeds, and the notes the
// user typed are simply not in it.
//
// So the three ADDITIVE fields of `SendOptions` are concatenated here, once, and
// the blocks go through `composeBlocks` so their order on the wire is §D's
// (state, pane-shot, annotations, then the typed text) rather than whichever
// owner happened to be spread last.
import { composeBlocks } from "../protocol/wire";
import type { SendOptions } from "../protocol/controller-api";

/**
 * `base` is the caller's (the composer's pills, PR3's annotations, PR4's app
 * state); `extra` is what the tray took out of itself for THIS message. Every
 * scalar is the caller's unless only the tray named it; the three lists are the
 * union.
 *
 * THE CALLER WINS THE SCALARS, and the code has to say so rather than rely on
 * `take()` never returning one: `{ ...base, ...extra }` reads as a merge but
 * gives EXTRA precedence, so the day the tray learns to carry a `model` (a
 * per-attachment vision model, say) it would silently overrule the pill the user
 * set. `model`/`effort`/`permission` are the composer's answer about this turn
 * and there is no second claimant. A key the caller merely left `undefined` is
 * NOT an answer, so it does not blank what the tray put there — which is the
 * "unless only the tray named it" half of the rule above.
 */
/**
 * THE SEND PATH'S OWN BLOCK, ADDED TO THE CALLER'S — never over it.
 *
 * `mergeSendOptions` unions `base.blocks` with `extra.blocks`, and the send path
 * built its `base` as `{ ...opts, blocks: [mine] }`: a REPLACEMENT, one line
 * above the call to the very helper that exists to prevent one. Any block the
 * caller brought (PR4's `<live-app-state>`, a walkthrough's own) was dropped
 * silently, the message going out and the run succeeding without it (whole-stack
 * review, PR #1074).
 *
 * So the addition is a named function with a test of its own rather than a
 * spread at each call site, and it goes through `composeBlocks` — the union is
 * ORDERED (state, pane-shot, annotations), whichever owner emitted which.
 */
export function sendBlocks(
  caller: readonly (string | null | undefined)[] | null | undefined,
  ...mine: (string | null | undefined)[]
): string[] {
  return composeBlocks(caller, mine);
}

export function mergeSendOptions(base: SendOptions, extra: SendOptions): SendOptions {
  const out: SendOptions = { ...extra };
  for (const [k, v] of Object.entries(base)) {
    if (v !== undefined) (out as Record<string, unknown>)[k] = v;
  }
  const blocks = composeBlocks(base.blocks, extra.blocks);
  if (blocks.length) out.blocks = blocks;
  else delete out.blocks;
  const readDirs = [...(base.readDirs ?? []), ...(extra.readDirs ?? [])];
  // Deduped: a Read rule granted twice is a rule that grows on every turn
  // (T:11698 keeps one copy of each for the same reason).
  const dirs = [...new Set(readDirs)];
  if (dirs.length) out.readDirs = dirs;
  else delete out.readDirs;
  const attachments = [...(base.attachments ?? []), ...(extra.attachments ?? [])];
  if (attachments.length) out.attachments = attachments;
  else delete out.attachments;
  return out;
}

// WHICH USER LINES ARE QUEUED: typed while a turn runs and not yet echoed by
// the CLI. They are drawn in a strip above the composer, outside the
// transcript, and become transcript bubbles only once nothing names them as
// queued any more.
//
// Three sources, one list, in the order typed:
//   - the page outbox's lines (`UserTurn.pending` "queued" / "held"), and a
//     follow-up still on its way to the run ("sending"),
//   - the controller's lines the inbox took (`queuedKeys`),
//   - the server's inbox rows nothing on this page is drawing (a reload, another
//     tab) — `protocol/inbox.inboxBubbles`.
// PURE: no React, no fetches.
import type { InboxMessage } from "./types";
import type { Turn } from "./controller-api";
import { inboxBubbles } from "./inbox";

export interface QueuedLine {
  /** The turn key for a line this page minted; the inbox entry id otherwise. */
  id: string;
  text: string;
  /** Set for an outbox line (the page still holds it; it can be pulled back into
   *  the box). Absent for a line the inbox holds. `"sending"` is a follow-up on
   *  its way to a live run (`UserTurn.pending`): listed from the keypress, and
   *  not the reader's to pull back or send now — the send is already under way. */
  pending?: "queued" | "held" | "sending";
  /** Typed in THIS view (the line is one of this page's own turns), as against a
   *  line only the run's inbox knows — another tab's, or one a reload restored.
   *  Only a local line can "just arrive" (`ui/QueuedStrip`). */
  local?: true;
}

export function queuedLines(input: {
  turns: readonly Turn[];
  queuedKeys: readonly string[];
  inbox: readonly InboxMessage[] | null | undefined;
}): { lines: QueuedLine[]; hiddenKeys: Set<string> } {
  const sent = new Set(input.queuedKeys);
  const lines: QueuedLine[] = [];
  const hiddenKeys = new Set<string>();
  for (const t of input.turns) {
    if (t.role !== "user") continue;
    const pending =
      t.pending === "queued" || t.pending === "held" || t.pending === "sending" ? t.pending : undefined;
    if (!pending && !sent.has(t.key)) continue;
    hiddenKeys.add(t.key);
    lines.push({ id: t.key, text: t.text, local: true, ...(pending ? { pending } : {}) });
  }
  // An entry with a uuid is judged by the server alone (it lists a line until
  // the CLI echoes it), so an earlier transcript line with the same words never
  // retires it; only a line this page is already drawing in the strip does. An
  // entry with no uuid can only be matched by its words, against the transcript
  // too.
  //
  // …EXCEPT THE VERY MESSAGE THE TRANSCRIPT IS ALREADY DRAWING. The message that
  // OPENS a turn is a transcript bubble from the keypress, and the run lists it
  // in its inbox until the CLI echoes it — a second or two in which the reader's
  // first message stood in the transcript AND in the queue under it, as if it
  // were waiting behind something. Matched by id (`UserTurn.clientId` is the
  // entry's own `uuid`), never by words, so the rule above still holds: another
  // message that merely says the same thing keeps its row.
  const drawn = new Set<string>();
  for (const t of input.turns) {
    if (t.role === "user" && t.clientId && !hiddenKeys.has(t.key)) drawn.add(t.clientId);
  }
  // A "sending" line has no inbox entry yet (the controller has not posted it),
  // so its words retire nothing: an entry that says the same thing is another
  // message, still waiting, and keeps its row (CodeRabbit, PR #2347).
  const optimistic = lines.filter((l) => l.pending !== "sending").map((l) => l.text);
  const rows = Array.isArray(input.inbox) ? input.inbox : [];
  const named = inboxBubbles(
    rows.filter((r) => r && r.uuid && !drawn.has(r.uuid)),
    optimistic,
    [],
  );
  const unnamed = inboxBubbles(
    rows.filter((r) => r && !r.uuid),
    optimistic,
    input.turns.filter((t) => t.role === "user").map((t) => t.text),
  );
  for (const r of [...named, ...unnamed]) lines.push({ id: r.id, text: r.text });
  return { lines, hiddenKeys };
}

// How a permission mode is SAID, and what it means — the one copy both the
// chat composer's Approvals pill and the New task card's Permissions pill read
// (Akshil, 2026-11-02: "change the chat composer to match what we have in
// modal so both are consistent"). The keys are agent.py's `PERMISSION_MODES`
// and stay exactly as they are on the wire; only the reading lives here.
//
// In `platform/` because the two readers sit in different layers (shell and
// apps/claude) and neither may import the other.

/** The label a pill and its menu row print. */
export const PERMISSION_LABELS: Record<string, string> = {
  auto: "Auto",
  acceptEdits: "Accept edits",
  plan: "Plan only",
  prompt: "Ask every time",
};

/** The one-line meaning of each mode, shown on HOVER of its menu row
 *  (`data-hint`, platform/lib/hints) rather than printed under the menu: a
 *  sentence that only described the picked mode sat at the bottom of every
 *  menu, and the reader weighing the other three got nothing for them. */
export const PERMISSION_HINTS: Record<string, string> = {
  auto: "Approves safe actions on its own and refuses risky ones. You can allow a refused action from the chat.",
  acceptEdits: "File edits go through on their own. Other actions wait for you, bar the commands the agent already trusts.",
  plan: "Reads and plans only. Changes nothing until you say so.",
  prompt: "Holds every action for your approval.",
};

/** A mode this map has never heard of shows its key, which is still better
 *  than hiding it. An OWN-property check: a stored key like "toString" must
 *  read as itself, not as the inherited function. */
export const permissionLabel = (key: string): string =>
  Object.prototype.hasOwnProperty.call(PERMISSION_LABELS, key) ? PERMISSION_LABELS[key] : key;
